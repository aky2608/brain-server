#!/usr/bin/env python3
"""Write a failure notification to the outbox for Telegram delivery.

Usage: notify_failure.py <message>
Called by OnFailure= systemd units.
"""
import os
import sys

import psycopg


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: notify_failure.py <message>", file=sys.stderr)
        sys.exit(1)

    message = sys.argv[1]
    db_url = os.environ.get("BRAIN_DB_URL", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not db_url:
        print("BRAIN_DB_URL not set", file=sys.stderr)
        sys.exit(1)
    if not chat_id:
        print("TELEGRAM_CHAT_ID not set", file=sys.stderr)
        sys.exit(1)

    with psycopg.connect(db_url) as conn:
        conn.execute(
            "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
            (chat_id, message),
        )
        conn.commit()
    print(f"notify_failure: queued message to outbox")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"notify_failure error: {e}", file=sys.stderr)
        sys.exit(1)
