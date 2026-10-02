#!/usr/bin/env python3
"""Weekly section summaries: one LLM paragraph per active section.
Run by brain-weekly.timer at 20:00 IST every Sunday.

Activity window: Monday 00:00 through Sunday 23:59 IST (the week just ending).
An active section is one with at least one item created in that window.

Drill stats are scoped to the notebook — drill_sessions.notebook_id has no
section_id. Section-level drill stats will start flowing automatically once
revision_questions.section_id is populated by the notebook agent.

# TODO: when Bible §10 misclassification-rate reporting and agent cost digest
# are built, merge all three into this script rather than adding more weekly jobs.
"""
import os
import pathlib
import sys
from datetime import date, timedelta

import httpx
import psycopg

_IST_OFFSET  = timedelta(hours=5, minutes=30)
_PROMPT_PATH = pathlib.Path(__file__).parent / "prompts" / "review_weekly.txt"
_PROMPT_TMPL = _PROMPT_PATH.read_text()


def _db_url() -> str:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        raise RuntimeError("BRAIN_DB_URL not set")
    return url


def _week_bounds() -> tuple[date, date]:
    """Return (monday, sunday) for the week that just ended."""
    today = date.today()
    # Sunday run: today is Sunday; week ran Mon–Sun.
    # weekday(): Mon=0 … Sun=6
    days_since_monday = today.weekday() if today.weekday() != 6 else 6
    monday = today - timedelta(days=days_since_monday)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _call_llm(prompt: str) -> str:
    url     = os.environ.get("ONEMIN_API_URL", "").rstrip("/")
    api_key = os.environ.get("ONEMIN_API_KEY", "")
    if not url or not api_key:
        raise RuntimeError("ONEMIN_API_URL or ONEMIN_API_KEY not set")
    r = httpx.post(
        f"{url}/api/chat-with-ai",
        headers={"API-KEY": api_key, "Content-Type": "application/json"},
        json={
            "type": "UNIFY_CHAT_WITH_AI",
            "model": "gpt-4o-mini",
            "promptObject": {"prompt": prompt},
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["aiRecord"]["aiRecordDetail"]["resultObject"][0]


def _active_sections(conn: psycopg.Connection, week_start: date, week_end: date) -> list[dict]:
    """Sections with at least one item created in [week_start, week_end]."""
    rows = conn.execute(
        """SELECT DISTINCT s.id, s.name, n.id AS notebook_id, n.name AS notebook_name
             FROM sections s
             JOIN notebooks n ON n.id = s.notebook_id
             JOIN items i ON i.section_id = s.id
            WHERE i.created_at >= %s
              AND i.created_at < %s
              AND i.status != 'deleted'
              AND s.archived_at IS NULL""",
        (week_start, week_end + timedelta(days=1)),
    ).fetchall()
    return [
        {"section_id": r[0], "section_name": r[1],
         "notebook_id": r[2], "notebook_name": r[3]}
        for r in rows
    ]


def _section_items(
    conn: psycopg.Connection, section_id: int, week_start: date, week_end: date
) -> list[str]:
    rows = conn.execute(
        """SELECT COALESCE(title, LEFT(raw_content, 120))
             FROM items
            WHERE section_id = %s
              AND created_at >= %s
              AND created_at < %s
              AND status != 'deleted'
            ORDER BY learned DESC, created_at DESC
            LIMIT 20""",
        (section_id, week_start, week_end + timedelta(days=1)),
    ).fetchall()
    return [r[0] or "(untitled)" for r in rows]


def _notebook_drill_stats(
    conn: psycopg.Connection, notebook_id: int, week_start: date, week_end: date
) -> tuple[int, float | None]:
    """Returns (session_count, avg_score_or_None) for verified sessions this week."""
    row = conn.execute(
        """SELECT COUNT(*), AVG(score_avg)
             FROM drill_sessions
            WHERE notebook_id = %s
              AND verified = true
              AND created_at >= %s
              AND created_at < %s""",
        (notebook_id, week_start, week_end + timedelta(days=1)),
    ).fetchone()
    count     = int(row[0]) if row and row[0] else 0
    avg_score = float(row[1]) if row and row[1] is not None else None
    return count, avg_score


def _build_prompt(
    section_name: str,
    notebook_name: str,
    week_start: date,
    week_end: date,
    items: list[str],
    drill_count: int,
    avg_score: float | None,
) -> str:
    item_list = "\n".join(f"- {t}" for t in items) if items else "(none)"
    drill_avg = f", avg score {avg_score:.1f}/10" if avg_score is not None else ""
    return _PROMPT_TMPL.format(
        section_name=section_name,
        notebook_name=notebook_name,
        week_start=week_start.strftime("%d %b %Y"),
        week_end=week_end.strftime("%d %b %Y"),
        item_count=len(items),
        item_list=item_list,
        drill_count=drill_count,
        drill_avg=drill_avg,
    )


def _upsert_summary(
    conn: psycopg.Connection, section_id: int, week_start: date, summary: str
) -> None:
    conn.execute(
        """INSERT INTO section_summaries (section_id, week_start, summary, generated_at)
           VALUES (%s, %s, %s, now())
           ON CONFLICT (section_id, week_start)
           DO UPDATE SET summary = EXCLUDED.summary, generated_at = now()""",
        (section_id, week_start, summary),
    )


def main() -> None:
    week_start, week_end = _week_bounds()
    print(f"weekly_review: week {week_start} – {week_end}")

    with psycopg.connect(_db_url()) as conn:
        sections = _active_sections(conn, week_start, week_end)

        if not sections:
            print("weekly_review: no active sections this week, nothing to do")
            return

        print(f"weekly_review: {len(sections)} active section(s)")
        written = 0

        for sec in sections:
            sid  = sec["section_id"]
            snm  = sec["section_name"]
            nid  = sec["notebook_id"]
            nnm  = sec["notebook_name"]

            items                 = _section_items(conn, sid, week_start, week_end)
            drill_count, avg_score = _notebook_drill_stats(conn, nid, week_start, week_end)
            prompt                = _build_prompt(snm, nnm, week_start, week_end,
                                                  items, drill_count, avg_score)
            try:
                summary = _call_llm(prompt).strip()
            except Exception as e:
                print(f"  [{snm}] LLM call failed: {e} — skipping", file=sys.stderr)
                continue

            _upsert_summary(conn, sid, week_start, summary)
            print(f"  [{snm}] written ({len(items)} items, {drill_count} drills)")
            written += 1

        conn.commit()

    print(f"weekly_review: done — {written}/{len(sections)} section(s) written")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"weekly_review error: {e}", file=sys.stderr)
        sys.exit(1)
