import logging
import os
import re
from typing import Optional

import psycopg
from dotenv import load_dotenv
from langgraph.graph import END

from agents.base import GraphState
from shortcuts import lookup_shortcut, parse_slash

_CONFLICT_RE = re.compile(r"^(yes|no|skip)(?:\s+([0-9a-f]{6}))?(?=\s|$)", re.IGNORECASE)

load_dotenv()

logger = logging.getLogger("brain")

# Static graph routing table: routing_key → LangGraph node name.
# capture_shortcuts.agent column must match keys defined here.
# Add new specialists here when they're wired into graph.py.
DISPATCH_MAP: dict[str, str] = {
    "capture_agent": "capture_agent",        # default classify path (Weekend 7+)
    "echo": "echo_agent",                    # Weekend 6 proof — keep forever
    "why": "why_agent",
    "scheduling_agent": "scheduling_agent",  # cron 6:30am + reactive on task_status change
    "watch_agent": "watch_agent",            # zero-LLM watch rule evaluator (Phase 2.7)
    "notebook_agent": "notebook_agent",      # /gate → GATE subject notebook routing
    "revision_agent": "revision_agent",      # /revise → spaced repetition
    "finance_agent": "finance_agent",        # chained after capture_agent for life/finance items
    "people_agent": "people_agent",          # /people pending + conflict resolution
}


def _db_url() -> str:
    return os.environ.get("BRAIN_DB_URL", "")


def _get_pending_conflicts() -> list[dict]:
    url = _db_url()
    if not url:
        return []
    try:
        with psycopg.connect(url) as conn:
            rows = conn.execute(
                """SELECT id FROM people_conflicts
                   WHERE status = 'pending'
                   ORDER BY created_at LIMIT 5"""
            ).fetchall()
        return [{"id": str(r[0])} for r in rows]
    except Exception:
        logger.exception("people_conflicts poll failed")
        return []


def _write_outbox_direct(recipient: str, message: str) -> None:
    url = _db_url()
    if not url:
        return
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
                (recipient, message),
            )
            conn.commit()
    except Exception:
        logger.exception("outbox write failed in personal_agent")


def _log_decision(
    agent_name: str,
    action_taken: str,
    reason: str,
    interrupt_tier: str = "log_only",
    item_id: Optional[str] = None,
) -> None:
    """Write one row to agent_decisions. Personal Agent is sole writer — specialists must not call this."""
    url = _db_url()
    if not url:
        return
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                """INSERT INTO agent_decisions
                       (agent_name, item_id, action_taken, reason, interrupt_tier)
                   VALUES (%s, %s, %s, %s, %s)""",
                (agent_name, item_id, action_taken, reason, interrupt_tier),
            )
            conn.commit()
    except Exception:
        logger.exception("agent_decisions write failed")


def personal_agent_node(state: GraphState) -> dict:
    """
    Sole entry point. Two modes:

    Merge mode (specialist_result is set):
        Clear routed_to and stop — pipeline complete.

    Routing mode (specialist_result is None):
        Priority order:
        1. why/explain prefix → why_agent (reads stored reasoning, zero LLM cost)
        2. Slash command /alias → capture_shortcuts lookup (already short-circuited at
           /capture for stored items; this handles graph-direct invocations)
        3. Pre-classified category (routed_to set by /capture upstream) → DISPATCH_MAP
        4. Fallback → echo (Weekend 6 proof sentinel)

        Every routing decision is written to agent_decisions before returning.
    """
    if state.get("specialist_result") is not None:
        result = state["specialist_result"] or {}
        # Relay any decisions a specialist surfaced — sole-writer invariant: only
        # personal_agent writes to agent_decisions; specialists return decisions for us to log.
        for d in result.get("decisions", []):
            _log_decision(
                agent_name=d.get("agent_name", "scheduling_agent"),
                action_taken=d.get("action_taken", ""),
                reason=d.get("reason", ""),
                interrupt_tier=d.get("interrupt_tier", "log_only"),
                item_id=d.get("item_id"),
            )
        # Chain to finance_agent when capture_agent classifies a life/finance item.
        # finance_agent's output has no subcategory key, so this fires exactly once.
        if result.get("subcategory") == "finance" and result.get("item_id"):
            return {"routed_to": "finance_agent", "specialist_result": None}
        return {"routed_to": None}

    raw = (state.get("raw_input") or "").strip()
    lower = raw.lower()

    # 0. Conflict reply — YES/NO/SKIP [6-char code], checked before any other routing.
    m = _CONFLICT_RE.match(raw)
    if m:
        pending = _get_pending_conflicts()
        if pending:
            answer = m.group(1).lower()
            code   = m.group(2).lower() if m.group(2) else None
            if code:
                conflict = next(
                    (c for c in pending if str(c["id"]).replace("-", "")[-6:] == code), None
                )
                if conflict:
                    _log_decision("people_agent", f"resolve_conflict:{answer}:{code}",
                                  "conflict reply with explicit code")
                    return {
                        "routed_to": "people_agent", "specialist_result": None,
                        "people_action": "resolve_conflict",
                        "conflict_id": conflict["id"], "conflict_answer": answer,
                    }
                known = ", ".join(str(c["id"]).replace("-", "")[-6:] for c in pending)
                _write_outbox_direct(state.get("source", ""),
                                     f"Code {code} not found. Open: {known}")
                return {"routed_to": None, "specialist_result": {"handled": "conflict_code_unknown"}}
            elif len(pending) == 1:
                _log_decision("people_agent", f"resolve_conflict:{answer}:implicit",
                              "single open conflict, no code needed")
                return {
                    "routed_to": "people_agent", "specialist_result": None,
                    "people_action": "resolve_conflict",
                    "conflict_id": pending[0]["id"], "conflict_answer": answer,
                }
            else:
                known = ", ".join(str(c["id"]).replace("-", "")[-6:] for c in pending)
                first_code = str(pending[0]["id"]).replace("-", "")[-6:]
                _write_outbox_direct(
                    state.get("source", ""),
                    f"Multiple open conflicts \u2014 include the code, e.g. YES {first_code}\nOpen: {known}",
                )
                return {"routed_to": None, "specialist_result": {"handled": "conflict_ambiguous"}}
        # CONFLICT_RE matched but no pending conflicts — fall through to normal routing.

    # 1. why/explain hard fork — no LLM; reads agent_decisions
    if lower.startswith("why") or lower.startswith("explain"):
        _log_decision(
            agent_name="why_agent",
            action_taken="route_why",
            reason="why/explain prefix — reading stored agent_decisions",
        )
        return {"routed_to": "why"}

    # 2. Slash command: /alias [rest of text]
    alias = parse_slash(raw)

    # /people subcommands — handled before capture_shortcuts lookup.
    if alias == "people":
        subcommand = raw.strip()[len("/people"):].strip().lower()
        if subcommand == "pending":
            _log_decision("people_agent", "route_slash:people:pending", "/people pending")
            return {
                "routed_to": "people_agent", "specialist_result": None,
                "people_action": "list_pending",
                "conflict_id": None, "conflict_answer": None,
            }
        _log_decision("echo_agent", f"route_slash_unknown:people:{subcommand}",
                      f"/people {subcommand!r} not a known subcommand")
        return {"routed_to": "echo"}

    if alias is not None:
        shortcut = lookup_shortcut(alias)
        if shortcut:
            routing_key = shortcut["agent"] or "echo"
            if routing_key in DISPATCH_MAP:
                _log_decision(
                    agent_name=DISPATCH_MAP[routing_key],
                    action_taken=f"route_slash:{alias}",
                    reason=f"/{alias} → capture_shortcuts → routing_key={routing_key!r}",
                )
                return {"routed_to": routing_key}
        # alias not in capture_shortcuts or agent not in DISPATCH_MAP → fall through to echo
        _log_decision(
            agent_name="echo_agent",
            action_taken=f"route_slash_unknown:{alias}",
            reason=f"/{alias} not in capture_shortcuts or no wired agent, defaulting to echo",
        )
        return {"routed_to": "echo"}

    # 3. Pre-classified category set by /capture endpoint
    category = state.get("routed_to")
    if category:
        if category in DISPATCH_MAP:
            _log_decision(
                agent_name=DISPATCH_MAP[category],
                action_taken=f"route_category:{category}",
                reason=f"classified category {category!r} in DISPATCH_MAP",
            )
            return {"routed_to": category}
        _log_decision(
            agent_name="capture_agent",
            action_taken=f"route_category_unhandled:{category}",
            reason=f"category {category!r} has no specialist wired yet, classify via capture_agent",
        )
        return {"routed_to": "capture_agent"}

    # 4. Fallback — capture_agent is the default classify path for all non-slash non-why input
    _log_decision(
        agent_name="capture_agent",
        action_taken="route_fallback",
        reason="no slash/why/category match; routing to capture_agent for classify+embed",
    )
    return {"routed_to": "capture_agent"}


def route_from_personal(state: GraphState) -> str:
    if state.get("specialist_result") is not None:
        return END
    routed_to = state.get("routed_to", "")
    return DISPATCH_MAP.get(routed_to, END)
