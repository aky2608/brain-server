import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import psycopg
from dateutil import parser as _du_parser
from dotenv import load_dotenv
from langgraph.graph import END

from agents.base import GraphState
from agents.specialists.capture import (
    _IST,
    _check_calendar_conflict,
    _extract_date_phrase,
    _resolve_date_phrase,
)
from shortcuts import lookup_shortcut, parse_slash

_CONFLICT_RE     = re.compile(r"^(yes|no|skip)(?:\s+([0-9a-f]{6}))?(?=\s|$)", re.IGNORECASE)
_CAL_CONFLICT_RE = re.compile(r"^(keep|move|cancel)\b(.*)", re.IGNORECASE | re.DOTALL)

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
    "project_agent": "project_agent",        # /build → Coding Agent spec packaging
}


def _db_url() -> str:
    return os.environ.get("BRAIN_DB_URL", "")


def _get_pending_calendar_conflicts() -> list[dict]:
    url = _db_url()
    if not url:
        return []
    try:
        with psycopg.connect(url) as conn:
            rows = conn.execute(
                """SELECT id, new_item_id, code FROM calendar_conflicts
                   WHERE status = 'pending'
                   ORDER BY created_at LIMIT 5"""
            ).fetchall()
        return [{"id": str(r[0]), "new_item_id": str(r[1]), "code": r[2] or ""} for r in rows]
    except Exception:
        logger.exception("calendar_conflicts poll failed")
        return []


def _resolve_calendar_conflict(
    conflict: dict,
    answer: str,
    time_rest: str,
    source: str,
    item_id: Optional[str],
) -> str:
    """Apply KEEP/MOVE/CANCEL.  Returns 'ok', 'unresolved_time', or 'error'."""
    url = _db_url()
    if not url:
        return "error"
    conflict_id = conflict["id"]
    new_item_id = conflict["new_item_id"]
    try:
        with psycopg.connect(url) as conn:
            chat_row  = conn.execute(
                "SELECT metadata->>'chat_id' FROM items WHERE id = %s", (item_id,)
            ).fetchone() if item_id else None
            recipient = str(chat_row[0]) if chat_row and chat_row[0] else source

            if answer == "keep":
                conn.execute(
                    "UPDATE calendar_conflicts SET status='kept', resolved_at=now() WHERE id=%s",
                    (conflict_id,),
                )
                reply = "Kept both \u2014 calendar unchanged."
                conn.execute(
                    "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
                    (recipient, reply),
                )
                conn.commit()
                return "ok"

            elif answer == "move":
                if time_rest:
                    # Fetch current starts_at for bare-time (day-only) resolution
                    existing_row = conn.execute(
                        "SELECT starts_at FROM items WHERE id = %s", (new_item_id,)
                    ).fetchone()
                    existing_sa = existing_row[0] if existing_row and existing_row[0] else None

                    now_ist = datetime.now(_IST)
                    phrase  = _extract_date_phrase(time_rest)
                    new_dt: datetime | None = None
                    new_inferred = False

                    if phrase:
                        new_dt, new_inferred = _resolve_date_phrase(phrase, now_ist)

                    if new_dt is None and existing_sa:
                        # Bare time like "4pm" — keep existing date, replace time
                        try:
                            base = existing_sa.astimezone(_IST)
                            t = _du_parser.parse(
                                time_rest,
                                default=datetime(base.year, base.month, base.day, 0, 0, 0),
                            )
                            new_dt = t.replace(tzinfo=_IST) if t.tzinfo is None else t.astimezone(_IST)
                            new_inferred = False
                        except Exception:
                            pass

                    if new_dt is None:
                        reply = (
                            f"Couldn\u2019t parse \u201c{time_rest}\u201d as a time. "
                            "Try: MOVE 5pm, MOVE tomorrow at 10am, MOVE 26 Sep 3pm."
                        )
                        conn.execute(
                            "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
                            (recipient, reply),
                        )
                        conn.commit()
                        return "unresolved_time"

                    conn.execute(
                        "UPDATE calendar_conflicts SET status='moved', resolved_at=now() WHERE id=%s",
                        (conflict_id,),
                    )
                    conn.execute(
                        "UPDATE items SET starts_at=%s, time_inferred=%s WHERE id=%s",
                        (new_dt, new_inferred, new_item_id),
                    )
                    new_time_s = new_dt.astimezone(_IST).strftime("%-H:%M on %-d %b")
                    reply = f"Rescheduled to {new_time_s}."
                    conn.execute(
                        "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
                        (recipient, reply),
                    )
                    conn.commit()

                    # Re-check for conflict at the new slot (only when time is explicit)
                    if not new_inferred:
                        _check_calendar_conflict(new_item_id, new_dt, url)
                    return "ok"

                else:
                    # No time — clear the slot entirely
                    conn.execute(
                        "UPDATE calendar_conflicts SET status='moved', resolved_at=now() WHERE id=%s",
                        (conflict_id,),
                    )
                    conn.execute(
                        "UPDATE items SET starts_at=NULL, is_event=false, time_inferred=false WHERE id=%s",
                        (new_item_id,),
                    )
                    reply = "Unplanned \u2014 starts_at cleared. Reschedule when ready."
                    conn.execute(
                        "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
                        (recipient, reply),
                    )
                    conn.commit()
                    return "ok"

            else:  # cancel
                conn.execute(
                    "UPDATE calendar_conflicts SET status='cancelled', resolved_at=now() WHERE id=%s",
                    (conflict_id,),
                )
                conn.execute("UPDATE items SET status='inactive' WHERE id=%s", (new_item_id,))
                reply = "Cancelled \u2014 item removed."
                conn.execute(
                    "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
                    (recipient, reply),
                )
                conn.commit()
                return "ok"

    except Exception:
        logger.exception("calendar_conflict resolution failed")
        return "error"


def _get_pending_conflicts() -> list[dict]:
    url = _db_url()
    if not url:
        return []
    try:
        with psycopg.connect(url) as conn:
            rows = conn.execute(
                """SELECT id FROM people_conflicts
                   WHERE status IN ('pending', 'snoozed')
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


def _real_uuid(val: Optional[str]) -> Optional[str]:
    """Return val only if it parses as a UUID, else None.
    Guards against synthetic IDs like 'watch-cron' violating the items FK."""
    if not val:
        return None
    try:
        UUID(val)
        return val
    except ValueError:
        return None


def _log_route(
    destination: str,
    action_taken: str,
    reason: str,
    item_id: Optional[str] = None,
) -> None:
    """Write two rows atomically: personal_agent routing row + destination agent row.
    Use for every routing decision so personal_agent is visible in agent_decisions."""
    url = _db_url()
    if not url:
        return
    try:
        with psycopg.connect(url) as conn:
            conn.execute(
                """INSERT INTO agent_decisions
                       (agent_name, item_id, action_taken, reason, interrupt_tier)
                   VALUES (%s, %s, %s, %s, 'log_only')""",
                ("personal_agent", item_id, f"route_to:{destination}", reason),
            )
            conn.execute(
                """INSERT INTO agent_decisions
                       (agent_name, item_id, action_taken, reason, interrupt_tier)
                   VALUES (%s, %s, %s, %s, 'log_only')""",
                (destination, item_id, action_taken, reason),
            )
            conn.commit()
    except Exception:
        logger.exception("agent_decisions write failed")


def _capture_fragments(fragments: list[str]) -> int:
    """
    Insert one items row + one job_queue row per fragment.
    source='slash_plan' is identifiable in trails.
    Returns count of successfully inserted items.
    """
    url = _db_url()
    if not url:
        return 0
    inserted = 0
    try:
        with psycopg.connect(url) as conn:
            for fragment in fragments:
                row = conn.execute(
                    """INSERT INTO items (raw_content, source, capture_type, classification_status)
                       VALUES (%s, 'slash_plan', 'text', 'instant')
                       RETURNING id""",
                    (fragment,),
                ).fetchone()
                if not row:
                    continue
                item_id = str(row[0])
                conn.execute(
                    "INSERT INTO job_queue (job_type, payload) VALUES ('graph_invoke', %s)",
                    (json.dumps({"item_id": item_id, "content": fragment, "source": "slash_plan"}),),
                )
                inserted += 1
            conn.commit()
    except Exception:
        logger.exception("_capture_fragments: DB error inserting %d fragments", len(fragments))
    return inserted


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
    item_id = _real_uuid(state.get("capture_uuid"))

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
                item_id=d.get("item_id") or item_id,
            )
        # Chain to finance_agent when capture_agent classifies a life/finance item.
        # finance_agent's output has no subcategory key, so this fires exactly once.
        if result.get("subcategory") == "finance" and result.get("item_id"):
            _log_route("finance_agent", "route_chain:finance",
                       "capture_agent classified life/finance; chaining", item_id=item_id)
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
                    _log_route("people_agent", f"resolve_conflict:{answer}:{code}",
                               "conflict reply with explicit code", item_id=item_id)
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
                _log_route("people_agent", f"resolve_conflict:{answer}:implicit",
                           "single open conflict, no code needed", item_id=item_id)
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

    # 0b. Calendar conflict reply — KEEP/MOVE/CANCEL [code] [time]
    m_cal = _CAL_CONFLICT_RE.match(raw)
    if m_cal:
        pending_cal = _get_pending_calendar_conflicts()
        if pending_cal:
            verb      = m_cal.group(1).lower()
            rest      = m_cal.group(2).strip()
            src       = state.get("source", "")
            codes_map = {c["code"].lower(): c for c in pending_cal if c.get("code")}

            # Identify which conflict and split off any time remainder.
            tokens    = rest.split(None, 1)
            first_tok = tokens[0].lower() if tokens else ""
            if first_tok in codes_map:
                conflict  = codes_map[first_tok]
                time_rest = tokens[1] if len(tokens) > 1 else ""
            elif not rest:
                # No extra tokens — implicit only when exactly one open conflict.
                conflict  = pending_cal[0] if len(pending_cal) == 1 else None
                time_rest = ""
            else:
                # Has trailing text but first token isn't a known code — treat as
                # implicit conflict (single open) with full rest as time expression.
                conflict  = pending_cal[0] if len(pending_cal) == 1 else None
                time_rest = rest

            if conflict is None:
                known = ", ".join(c["code"] for c in pending_cal if c.get("code"))
                _write_outbox_direct(
                    src,
                    f"Multiple open calendar conflicts \u2014 include the code, "
                    f"e.g. KEEP {pending_cal[0].get('code', '???')}\nOpen: {known}",
                )
                return {"routed_to": None, "specialist_result": {"handled": "cal_conflict_ambiguous"}}

            outcome = _resolve_calendar_conflict(conflict, verb, time_rest, src, item_id)
            if outcome == "unresolved_time":
                # Message already sent inside _resolve_calendar_conflict; leave pending.
                return {"routed_to": None, "specialist_result": {"handled": "cal_conflict_unresolved_time"}}
            if outcome == "ok":
                _log_decision(
                    "capture_agent",
                    f"calendar_conflict:{verb}:{conflict.get('code', 'implicit')}",
                    f"user answered {verb!r} for conflict {conflict['id']}",
                    "log_only", item_id=conflict["new_item_id"],
                )
            return {"routed_to": None, "specialist_result": {"handled": f"calendar_conflict:{verb}"}}
        # _CAL_CONFLICT_RE matched but no pending calendar conflicts — fall through.

    # 1. why/explain hard fork — no LLM; reads agent_decisions
    if lower.startswith("why") or lower.startswith("explain"):
        _log_route("why_agent", "route_why", "why/explain prefix — reading stored agent_decisions",
                   item_id=item_id)
        return {"routed_to": "why"}

    # 2. Slash command: /alias [rest of text]
    alias = parse_slash(raw)

    # /people subcommands — handled before capture_shortcuts lookup.
    if alias == "people":
        subcommand = raw.strip()[len("/people"):].strip().lower()
        if subcommand == "pending":
            _log_route("people_agent", "route_slash:people:pending", "/people pending",
                       item_id=item_id)
            return {
                "routed_to": "people_agent", "specialist_result": None,
                "people_action": "list_pending",
                "conflict_id": None, "conflict_answer": None,
            }
        _log_route("echo_agent", f"route_slash_unknown:people:{subcommand}",
                   f"/people {subcommand!r} not a known subcommand", item_id=item_id)
        return {"routed_to": "echo"}

    if alias is not None:
        # /plan <text> → split on commas, capture each fragment, then schedule as normal.
        # /plan alone (no remainder) falls through unchanged.
        if alias == "plan":
            remainder = raw.strip()[len("/plan"):].strip()
            if remainder:
                fragments = [f.strip() for f in remainder.split(",") if f.strip()][:10]
                if fragments:
                    n = _capture_fragments(fragments)
                    names = ", ".join(f'"{f}"' for f in fragments)
                    _log_decision(
                        "personal_agent",
                        "slash_plan_capture",
                        f"captured {n}/{len(fragments)} fragment(s) from /plan: {names}",
                        item_id=item_id,
                    )

        shortcut = lookup_shortcut(alias)
        if shortcut:
            routing_key = shortcut["agent"] or "echo"
            if routing_key in DISPATCH_MAP:
                _log_route(
                    DISPATCH_MAP[routing_key],
                    f"route_slash:{alias}",
                    f"/{alias} → capture_shortcuts → routing_key={routing_key!r}",
                    item_id=item_id,
                )
                return {"routed_to": routing_key}
        # alias not in capture_shortcuts or agent not in DISPATCH_MAP → fall through to echo
        _log_route(
            "echo_agent",
            f"route_slash_unknown:{alias}",
            f"/{alias} not in capture_shortcuts or no wired agent, defaulting to echo",
            item_id=item_id,
        )
        return {"routed_to": "echo"}

    # 3. Pre-classified category set by /capture endpoint
    category = state.get("routed_to")
    if category:
        if category in DISPATCH_MAP:
            _log_route(
                DISPATCH_MAP[category],
                f"route_category:{category}",
                f"classified category {category!r} in DISPATCH_MAP",
                item_id=item_id,
            )
            return {"routed_to": category}
        _log_route(
            "capture_agent",
            f"route_category_unhandled:{category}",
            f"category {category!r} has no specialist wired yet, classify via capture_agent",
            item_id=item_id,
        )
        return {"routed_to": "capture_agent"}

    # 4. Fallback — capture_agent is the default classify path for all non-slash non-why input
    _log_route(
        "capture_agent",
        "route_fallback",
        "no slash/why/category match; routing to capture_agent for classify+embed",
        item_id=item_id,
    )
    return {"routed_to": "capture_agent"}


def route_from_personal(state: GraphState) -> str:
    if state.get("specialist_result") is not None:
        return END
    routed_to = state.get("routed_to", "")
    return DISPATCH_MAP.get(routed_to, END)
