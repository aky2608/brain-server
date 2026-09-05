import logging
import os
from typing import Optional

import psycopg
from dotenv import load_dotenv
from langgraph.graph import END

from agents.base import GraphState
from shortcuts import lookup_shortcut, parse_slash

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
}


def _db_url() -> str:
    return os.environ.get("BRAIN_DB_URL", "")


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
        # Relay any decisions a specialist surfaced — sole-writer invariant: only
        # personal_agent writes to agent_decisions; specialists return decisions for us to log.
        for d in (state["specialist_result"] or {}).get("decisions", []):
            _log_decision(
                agent_name=d.get("agent_name", "scheduling_agent"),
                action_taken=d.get("action_taken", ""),
                reason=d.get("reason", ""),
                interrupt_tier=d.get("interrupt_tier", "log_only"),
                item_id=d.get("item_id"),
            )
        return {"routed_to": None}

    raw = (state.get("raw_input") or "").strip()
    lower = raw.lower()

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
