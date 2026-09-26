#!/usr/bin/env python3
"""Morning brief: compose one Telegram message and write it to the outbox.
Run by brain-brief.timer at 06:35 IST daily.

Runs after brain-scheduler.timer (06:30). The scheduler enqueues a pure-SQL
job (SchedulingAgent, cost_tier=free, zero LLM); the worker claims it within
2 s and it completes in well under a second. The 5-minute gap is generous
slack, not a meaningful bound.

No LLM call. All sections are plain SQL queries. Sections with no rows are
omitted. If every section is empty a short fallback line is sent — silence
is indistinguishable from failure.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg

_IST = timezone(timedelta(hours=5, minutes=30))

_GATE_WINDOW_DEFAULT = 7
_GATE_TARGET_DEFAULT = 3


def _db_url() -> str:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        raise RuntimeError("BRAIN_DB_URL not set")
    return url


def _tg_chat_id() -> str:
    cid = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not cid:
        raise RuntimeError("TELEGRAM_CHAT_ID not set")
    return cid


def _item_name(title: str | None, raw_content: str | None) -> str:
    if title and title.strip():
        return title.strip()
    return (raw_content or "").strip()[:80]


def _fmt_time(dt: datetime, inferred: bool) -> str:
    local = dt.astimezone(_IST)
    prefix = "~" if inferred else ""
    return f"{prefix}{local.strftime('%H:%M')}"


# ---------------------------------------------------------------------------
# Section builders — each returns a list of lines or []
# ---------------------------------------------------------------------------

def _section_today(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT title, raw_content, starts_at, time_inferred
             FROM items
            WHERE status = 'active'
              AND plan_bucket = 'today'
              AND action_class = 'task'
              AND task_status IS DISTINCT FROM 'done'
            ORDER BY starts_at NULLS LAST, plan_order NULLS LAST""",
    ).fetchall()
    if not rows:
        return []
    lines = ["\U0001f4c5 Today"]
    for title, raw_content, starts_at, time_inferred in rows:
        name = _item_name(title, raw_content)
        if starts_at:
            lines.append(f"  {_fmt_time(starts_at, time_inferred)}  {name}")
        else:
            lines.append(f"  \u2022 {name}")
    return lines


def _section_rollovers(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT title, raw_content, rollover_note
             FROM items
            WHERE status = 'active'
              AND plan_bucket = 'today'
              AND rollover_note IS NOT NULL
            ORDER BY plan_order NULLS LAST""",
    ).fetchall()
    if not rows:
        return []
    # Note: no rolled_over_at timestamp exists — this surfaces all items in
    # today's plan carrying a rollover note, not only this morning's moves.
    lines = ["\u21a9 Rolled over"]
    for title, raw_content, rollover_note in rows:
        name = _item_name(title, raw_content)
        lines.append(f"  \u2022 {name} \u2014 {rollover_note}")
    return lines


def _section_due(conn: psycopg.Connection) -> list[str]:
    lines = []

    revision_due = conn.execute(
        """SELECT COUNT(*) FROM revision_questions
            WHERE next_review_date <= CURRENT_DATE
              AND archived_at IS NULL""",
    ).fetchone()[0]

    gate_rule = conn.execute(
        """SELECT condition FROM agent_watch_rules
            WHERE rule_type = 'gate_missed' AND enabled = true
            LIMIT 1""",
    ).fetchone()
    if gate_rule:
        cond = gate_rule[0] or {}
        window_days = int(cond.get("window_days", _GATE_WINDOW_DEFAULT))
        target = int(cond.get("min_required", _GATE_TARGET_DEFAULT))
    else:
        window_days = _GATE_WINDOW_DEFAULT
        target = _GATE_TARGET_DEFAULT

    sessions = conn.execute(
        f"""SELECT COUNT(*) FROM drill_sessions
             WHERE verified = true
               AND created_at > now() - interval '{window_days} days'""",
    ).fetchone()[0]

    if revision_due == 0 and sessions >= target:
        return []

    lines.append("\U0001f4da Due")
    if revision_due > 0:
        lines.append(f"  Revision: {revision_due} due")
    lines.append(f"  GATE: {sessions}/{target} sessions this {window_days}d window")
    return lines


def _section_watch(conn: psycopg.Connection) -> tuple[list[str], list[int]]:
    """Returns (lines, decision_ids_to_mark)."""
    rows = conn.execute(
        """SELECT id, reason
             FROM agent_decisions
            WHERE interrupt_tier = 'morning_brief'
              AND dismissed_at IS NULL
            ORDER BY created_at""",
    ).fetchall()
    if not rows:
        return [], []
    lines = ["\U0001f441 Watch"]
    ids = []
    for row_id, reason in rows:
        lines.append(f"  \u2022 {reason}")
        ids.append(row_id)
    return lines, ids


def _section_overdue(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT title, raw_content, task_deadline
             FROM items
            WHERE status = 'active'
              AND task_deadline IS NOT NULL
              AND task_deadline < now()
              AND task_status IS DISTINCT FROM 'done'
            ORDER BY task_deadline""",
    ).fetchall()
    if not rows:
        return []
    lines = ["\u26a0\ufe0f Overdue"]
    for title, raw_content, deadline in rows:
        name = _item_name(title, raw_content)
        local = deadline.astimezone(_IST)
        lines.append(f"  \u2022 {name} (due {local.strftime('%H:%M %d %b')})")
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    url = _db_url()
    chat_id = _tg_chat_id()

    with psycopg.connect(url) as conn:
        today_lines = _section_today(conn)
        rollover_lines = _section_rollovers(conn)
        due_lines = _section_due(conn)
        watch_lines, watch_ids = _section_watch(conn)
        overdue_lines = _section_overdue(conn)

        all_sections = [today_lines, rollover_lines, due_lines, watch_lines, overdue_lines]
        body_parts = ["\n".join(s) for s in all_sections if s]

        if body_parts:
            message = "\n\n".join(body_parts)
        else:
            message = "Good morning \u2014 nothing scheduled today."

        conn.execute(
            "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
            (chat_id, message),
        )

        if watch_ids:
            # Marks morning_brief decisions as consumed so they don't repeat tomorrow.
            # dismissed_at here means "delivered by brief", not "dismissed in dashboard".
            # The two meanings are merged: morning_brief decisions never appear in the
            # interrupts tile (that query filters interrupt_tier='always'), so there is
            # no current UI side-effect. A separate briefed_at column would be cleaner
            # if the dashboard ever surfaces delivery status independently.
            conn.execute(
                "UPDATE agent_decisions SET dismissed_at = now() WHERE id = ANY(%s)",
                (watch_ids,),
            )

        conn.commit()

    sections_sent = sum(1 for s in all_sections if s)
    print(f"brief: sent ({sections_sent} section(s), {len(watch_ids)} watch notice(s) marked)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"brief error: {e}", file=sys.stderr)
        sys.exit(1)
