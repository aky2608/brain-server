"""
Reclassify items that were silently mislabelled during the 1min.ai outage on 2026-10-26.

Target: active items where category='thoughts' AND (ai_summary IS NULL OR ai_summary = '')
AND the capture date (IST) was 2026-10-26. These got classification_status='done' with
invented defaults instead of 'failed' because _classify used to return a default dict.

For each item:
  - Re-runs _classify with the original raw_content, source, capture_type
  - Writes the real classification via _write_classification
  - Re-enqueues people extraction if the new category warrants it
  - Runs FinanceAgent inline if subcategory='finance'

Run once manually:
    cd /opt/brain && python3 scripts/reclassify_failed.py

Dry-run (no writes):
    cd /opt/brain && python3 scripts/reclassify_failed.py --dry-run
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.specialists.capture import (
    _classify,
    _enqueue_people_extraction,
    _should_extract_people,
    _write_classification,
)
from agents.specialists.finance import FinanceAgent, FinanceInput

_IST = timezone(timedelta(hours=5, minutes=30))

_TARGET_DATE_IST = "2026-10-26"


def main(dry_run: bool) -> None:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        print("ERROR: BRAIN_DB_URL not set")
        sys.exit(1)

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            """SELECT id, raw_content, source, capture_type, created_at
                 FROM items
                WHERE status = 'active'
                  AND category = 'thoughts'
                  AND (ai_summary IS NULL OR ai_summary = '')
                  AND (created_at AT TIME ZONE 'Asia/Kolkata')::date >= %s
             ORDER BY created_at""",
            (_TARGET_DATE_IST,),
        ).fetchall()

    if not rows:
        print("No items matched — nothing to reclassify.")
        return

    print(f"Found {len(rows)} item(s) to reclassify.{' (DRY RUN)' if dry_run else ''}\n")

    finance_agent = FinanceAgent()

    ok = 0
    failed = 0
    skipped = 0

    for item_id, raw_content, source, capture_type, created_at in rows:
        item_id = str(item_id)
        if not raw_content or not raw_content.strip():
            print(f"  SKIP    {item_id}  (empty content)")
            skipped += 1
            continue

        cls = _classify(raw_content, source or "unknown", capture_type or "text")
        if cls is None:
            print(f"  FAIL    {item_id}  (_classify returned None — API still down?)")
            failed += 1
            continue

        cat = cls.get("category", "thoughts")
        sub = cls.get("subcategory")
        preview = (raw_content or "").strip()[:60]

        if dry_run:
            print(f"  DRY     {item_id}  {cat}/{sub}  {preview!r}")
            ok += 1
            continue

        try:
            with psycopg.connect(url) as conn:
                # Use the item's original created_at so date_phrase resolves against
                # the correct day, not today.
                captured_at_ist = created_at.astimezone(_IST) if created_at else datetime.now(_IST)
                _write_classification(item_id, cls, conn, captured_at_ist)
                conn.commit()
        except Exception as exc:
            print(f"  FAIL    {item_id}  write_classification failed: {exc}")
            failed += 1
            continue

        # People extraction
        people_enqueued = False
        if _should_extract_people(cat, sub):
            try:
                _enqueue_people_extraction(item_id, raw_content, source or "unknown")
                people_enqueued = True
            except Exception as exc:
                print(f"  WARN    {item_id}  people enqueue failed: {exc}")

        # Finance chain — run inline rather than through the graph
        finance_result = None
        if sub == "finance":
            try:
                finance_out = finance_agent.handle(
                    FinanceInput(item_id=item_id, raw=raw_content, source=source or "unknown")
                )
                finance_result = f"tx_id={finance_out.transaction_id}" if finance_out.transaction_id else "non-transaction"
            except Exception as exc:
                print(f"  WARN    {item_id}  finance_agent failed: {exc}")

        parts = [f"  OK      {item_id}  {cat}/{sub}"]
        if people_enqueued:
            parts[0] += "  people:enqueued"
        if finance_result:
            parts[0] += f"  finance:{finance_result}"
        parts[0] += f"  {preview!r}"
        print(parts[0])
        ok += 1

    print(f"\nDone: {ok} reclassified, {failed} failed, {skipped} skipped.")
    if failed:
        print("Items that failed are still category='thoughts' with empty ai_summary — re-run when the API is back.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reclassify mislabelled captures from 2026-10-26 outage")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without writing")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
