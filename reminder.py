#!/usr/bin/env python3
"""Fire reminders for upcoming dated items. Run by brain-reminder.timer every 15 min.

Due-reminder logic:
  time_inferred=false — fire when now >= starts_at - COALESCE(reminder_offset_minutes, 60) min
                        and now < starts_at (explicit time; offset counts from it)
  time_inferred=true  — the 09:00 default wasn't the user's choice, so no offset math.
                        Fire once at 08:00 IST on the day (starts_at - 1 hour).

reminded_at and the outbox row are written in a single transaction — a crash cannot
double-send.  Items whose starts_at has already passed are never matched (now < starts_at).
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg

_IST = timezone(timedelta(hours=5, minutes=30))


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


def _item_name(title: str | None, raw_content: str) -> str:
    if title and title.strip():
        return title.strip()
    return raw_content.strip()[:80]


def _format_eta(starts_at: datetime, now: datetime) -> str:
    delta = starts_at - now
    total_mins = int(delta.total_seconds() / 60)
    if total_mins < 60:
        return f"in {total_mins} min"
    hours, mins = divmod(total_mins, 60)
    return f"in {hours} h {mins} min" if mins else f"in {hours} h"


def _build_message(name: str, starts_at: datetime, time_inferred: bool, now: datetime) -> str:
    if time_inferred:
        local = starts_at.astimezone(_IST)
        return f"\u23f0 {name} \u2014 today at {local.strftime('%H:%M')} (time approximate)"
    return f"\u23f0 {name} \u2014 {_format_eta(starts_at, now)}"


def main() -> None:
    url = _db_url()
    chat_id = _tg_chat_id()

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            """SELECT id, title, raw_content, starts_at, time_inferred
                 FROM items
                WHERE status = 'active'
                  AND starts_at IS NOT NULL
                  AND reminded_at IS NULL
                  AND task_status IS DISTINCT FROM 'done'
                  AND (
                    -- explicit time: offset window before starts_at
                    (time_inferred = false
                     AND now() >= starts_at
                              - make_interval(mins => COALESCE(reminder_offset_minutes, 60))
                     AND now() < starts_at)
                    OR
                    -- inferred time (09:00 default): fire at 08:00 on the day
                    (time_inferred = true
                     AND now() >= starts_at - interval '1 hour'
                     AND now() < starts_at)
                  )""",
        ).fetchall()

        if not rows:
            print("reminder: nothing due")
            return

        now = datetime.now(_IST)
        fired = 0

        for item_id, title, raw_content, starts_at, time_inferred in rows:
            name = _item_name(title, raw_content or "")
            message = _build_message(name, starts_at, time_inferred, now)

            # Atomic: outbox write + reminded_at stamp — crash-safe, no double-send
            conn.execute(
                """INSERT INTO outbox (channel, recipient, message)
                   VALUES ('telegram', %s, %s)""",
                (chat_id, message),
            )
            conn.execute(
                "UPDATE items SET reminded_at = now() WHERE id = %s",
                (item_id,),
            )
            conn.execute(
                """INSERT INTO agent_decisions
                       (agent_name, item_id, action_taken, reason, interrupt_tier)
                   VALUES ('scheduling_agent', %s, 'reminder_sent', %s, 'log_only')""",
                (
                    str(item_id),
                    f"reminder fired for '{name}' — starts_at {starts_at.isoformat()} "
                    f"time_inferred={time_inferred}",
                ),
            )
            fired += 1

        conn.commit()

    print(f"reminder: {fired} reminder(s) sent")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"reminder error: {e}", file=sys.stderr)
        sys.exit(1)
