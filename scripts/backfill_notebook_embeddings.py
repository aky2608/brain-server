"""
Backfill embeddings for notebook items that were filed via /gate before
notebook_agent gained embedding support.

Targets: items WHERE notebook_id IS NOT NULL AND embedding IS NULL AND status != 'deleted'

raw_content already holds clean text (notebook_agent strips the /gate <alias> prefix
before writing to the DB), so no re-stripping is needed.

Run once manually:
    cd /opt/brain && python3 scripts/backfill_notebook_embeddings.py
"""
import os
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.specialists.capture import _embed, _link_embeddings, _store_embedding


def main() -> None:
    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        print("ERROR: BRAIN_DB_URL not set")
        sys.exit(1)

    with psycopg.connect(url) as conn:
        rows = conn.execute(
            """SELECT id, raw_content
                 FROM items
                WHERE notebook_id IS NOT NULL
                  AND embedding IS NULL
                  AND status != 'deleted'
             ORDER BY created_at""",
        ).fetchall()

    if not rows:
        print("Nothing to backfill.")
        return

    print(f"Found {len(rows)} item(s) to embed.")

    ok = 0
    skipped = 0
    for item_id, raw_content in rows:
        if not raw_content or not raw_content.strip():
            print(f"  SKIP  {item_id}  (empty content)")
            skipped += 1
            continue

        vector = _embed(raw_content)
        if not vector:
            print(f"  FAIL  {item_id}  (_embed returned None)")
            skipped += 1
            continue

        try:
            with psycopg.connect(url) as conn:
                _store_embedding(item_id, vector, conn)
                link_count = _link_embeddings(item_id, vector, conn)
                conn.commit()
            print(f"  OK    {item_id}  links={link_count}  preview={raw_content[:60]!r}")
            ok += 1
        except Exception as exc:
            print(f"  FAIL  {item_id}  DB write failed: {exc}")
            skipped += 1

    print(f"\nDone: {ok} embedded, {skipped} skipped/failed.")


if __name__ == "__main__":
    main()
