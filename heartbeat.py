#!/usr/bin/env python3
"""Heartbeat checker. Run every 30 minutes by brain-heartbeat.timer.

Reads agent_runs to detect stale or failed components. Alerts via the outbox
when a new issue is found. Deduplicates via heartbeat_alerts so a dead
component produces one alert per 24 hours, not one per check.

The checker itself records a run as component='heartbeat'. If heartbeat.py
stops running, its own row goes stale — which nothing here will catch.
The honest fix is an external dead-man's switch: set HEALTHCHECK_PING_URL
(from healthchecks.io or equivalent) in the EnvironmentFile; this script
pings it on every successful completion so the external service alerts you
if pings stop arriving.
"""
import os
import sys
from datetime import timedelta

import psycopg

from db.agent_runs import record_run

# Alert threshold = roughly 2× each component's run interval.
_EXPECTED: dict[str, timedelta] = {
    "watch":            timedelta(hours=2),
    "reminder":         timedelta(minutes=30),
    "scheduler":        timedelta(hours=48),
    "brief":            timedelta(hours=48),
    "weekly_review":    timedelta(days=14),
    "backup":           timedelta(hours=48),
    "batch_classification": timedelta(minutes=10),
    "outbox":           timedelta(minutes=15),
    "job_queue":        timedelta(minutes=15),
}


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


def _alert_if_new(conn: psycopg.Connection, component: str, reason: str) -> bool:
    """Queue a Telegram alert and record it unless one was sent in the last 24 hours."""
    existing = conn.execute(
        """SELECT 1 FROM heartbeat_alerts
           WHERE component = %s AND alerted_at > now() - interval '24 hours'
           LIMIT 1""",
        (component,),
    ).fetchone()
    if existing:
        return False
    conn.execute(
        "INSERT INTO outbox (channel, recipient, message) VALUES ('telegram', %s, %s)",
        (_tg_chat_id(), f"Heartbeat: {component} — {reason}"),
    )
    conn.execute(
        "INSERT INTO heartbeat_alerts (component, reason) VALUES (%s, %s)",
        (component, reason),
    )
    return True


def _check_components(conn: psycopg.Connection) -> list[str]:
    issues = []
    for component, threshold in _EXPECTED.items():
        row = conn.execute(
            """SELECT outcome, started_at
               FROM agent_runs
               WHERE component = %s
               ORDER BY started_at DESC
               LIMIT 1""",
            (component,),
        ).fetchone()

        if row is None:
            if _alert_if_new(conn, component, "no run ever recorded"):
                issues.append(f"{component}: no run recorded")
            continue

        outcome, started_at = row
        is_stale = conn.execute(
            "SELECT now() - %s::timestamptz > %s::interval",
            (started_at, threshold),
        ).fetchone()[0]

        if is_stale:
            reason = f"last run at {started_at.strftime('%Y-%m-%d %H:%M')} UTC — older than {threshold}"
            if _alert_if_new(conn, component, reason):
                issues.append(f"{component}: stale")
        elif outcome == "failed":
            reason = f"last run failed at {started_at.strftime('%Y-%m-%d %H:%M')} UTC"
            if _alert_if_new(conn, component, reason):
                issues.append(f"{component}: last run failed")

    return issues


def _check_outbox_stuck(conn: psycopg.Connection) -> list[str]:
    count = conn.execute(
        """SELECT count(*) FROM outbox
           WHERE status = 'failed' AND created_at < now() - interval '1 hour'""",
    ).fetchone()[0]
    if count == 0:
        return []
    reason = f"{count} message(s) in failed state for >1 hour"
    if _alert_if_new(conn, "outbox_stuck", reason):
        return [f"outbox_stuck: {reason}"]
    return []


def _check_job_queue_stuck(conn: psycopg.Connection) -> list[str]:
    count = conn.execute(
        """SELECT count(*) FROM job_queue
           WHERE status NOT IN ('done', 'dead')
             AND created_at < now() - interval '1 hour'""",
    ).fetchone()[0]
    if count == 0:
        return []
    reason = f"{count} job(s) not done after >1 hour"
    if _alert_if_new(conn, "job_queue_stuck", reason):
        return [f"job_queue_stuck: {reason}"]
    return []


def main() -> None:
    with record_run("heartbeat") as run:
        with psycopg.connect(_db_url()) as conn:
            issues: list[str] = []
            issues += _check_components(conn)
            issues += _check_outbox_stuck(conn)
            issues += _check_job_queue_stuck(conn)
            conn.commit()

        if issues:
            run.outcome = "acted"
            run.detail = f"{len(issues)} alert(s): {'; '.join(issues)}"
        else:
            run.detail = "all clear"

        ping_url = os.environ.get("HEALTHCHECK_PING_URL", "")
        if ping_url:
            import urllib.request
            try:
                urllib.request.urlopen(ping_url, timeout=5)
            except Exception as e:
                print(f"heartbeat: healthcheck ping failed: {e}", file=sys.stderr)

    print(f"heartbeat: {run.detail}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"heartbeat error: {e}", file=sys.stderr)
        sys.exit(1)
