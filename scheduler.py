#!/usr/bin/env python3
"""Enqueue a daily /plan job. Run by brain-scheduler.timer at 6:30am IST.

Exits 0 on success, 1 on failure. Does not poll — enqueue and exit.
"""
import json
import os
import sys

import psycopg


def _db_url() -> str:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        raise RuntimeError("BRAIN_DB_URL not set")
    return url


def main() -> None:
    payload = json.dumps({"item_id": "scheduler-cron", "content": "/plan", "source": "system"})
    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            "INSERT INTO job_queue (job_type, payload) VALUES ('graph_invoke', %s)",
            (payload,),
        )
        conn.commit()
    print("scheduler: enqueued /plan job")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"scheduler error: {e}", file=sys.stderr)
        sys.exit(1)
