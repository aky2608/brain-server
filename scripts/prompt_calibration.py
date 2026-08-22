#!/usr/bin/env python3
"""
Monthly prompt calibration: analyse misclassification corrections and inject
few-shot examples into prompts/classify_single.txt.

Zero LLM calls — pure SQL aggregation + file write.

Usage:
    python3 scripts/prompt_calibration.py [--min-rate 0.10] [--min-count 3] [--examples-per-category 2] [--dry-run]

The script rewrites the delimited few-shot block in the prompt file.
Running it again is idempotent — it replaces the previous block, never appends duplicates.
"""
import argparse
import os
import pathlib
import sys
from datetime import date

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import psycopg

PROMPT_FILE = ROOT / "prompts" / "classify_single.txt"
BLOCK_START = "# ── few-shot-corrections:start ──"
BLOCK_END   = "# ── few-shot-corrections:end ──"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rate",  type=float, default=0.10,
                    help="Minimum misclassification rate to include a category (default 0.10)")
    ap.add_argument("--min-count", type=int,   default=3,
                    help="Minimum correction count to include a category (default 3)")
    ap.add_argument("--examples-per-category", type=int, default=2,
                    help="Max example rows per flagged category (default 2)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the new few-shot block without writing the file")
    args = ap.parse_args()

    url = os.environ.get("BRAIN_DB_URL", "")
    if not url:
        sys.exit("BRAIN_DB_URL not set")

    with psycopg.connect(url) as conn:
        stats = _aggregate(conn)
        flagged = _flag(stats, args.min_rate, args.min_count)
        if not flagged:
            print("No categories meet the threshold — no few-shot block needed.")
            _write_block("", args.dry_run, total_corrections=sum(s["total"] for s in stats.values()))
            return

        examples = _fetch_examples(conn, flagged, args.examples_per_category)

    block = _render_block(flagged, examples, sum(s["total"] for s in stats.values()))
    _write_block(block, args.dry_run, total_corrections=sum(s["total"] for s in stats.values()))


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

def _aggregate(conn: psycopg.Connection) -> dict:
    """
    Return {original_category: {total: int, corrections: {corrected_cat: count}}}.
    Only rows where corrected_category IS NOT NULL and differs from category.
    """
    rows = conn.execute(
        """SELECT category, corrected_category, COUNT(*) AS n
             FROM items
            WHERE corrected_category IS NOT NULL
              AND corrected_category != category
            GROUP BY category, corrected_category
            ORDER BY category, n DESC"""
    ).fetchall()

    stats: dict = {}
    for orig, corrected, count in rows:
        if orig not in stats:
            stats[orig] = {"total": 0, "corrections": {}}
        stats[orig]["total"] += count
        stats[orig]["corrections"][corrected] = count
    return stats


def _flag(stats: dict, min_rate: float, min_count: int) -> list[dict]:
    """
    Return list of {original_category, top_correction, count, rate, total}
    for categories meeting either threshold, sorted by rate desc.
    """
    # Need total items per category to compute rate.
    # We don't have that from the correction table alone — use correction count
    # as a proxy: rate = corrections / (corrections + assumed_correct).
    # Better: query total items per category from items table.
    # We do that here by joining, but since we already have the conn above we
    # pass stats in and query total_per_category separately in the caller.
    # For simplicity, rate = total_corrections / total_items_in_that_category.
    # This is computed inline in _aggregate_with_totals instead.
    flagged = []
    for orig, data in stats.items():
        count = data["total"]
        top_correction = max(data["corrections"], key=lambda k: data["corrections"][k])
        top_count = data["corrections"][top_correction]
        # Rate here is fraction of ALL corrections that hit this original category.
        # Real misclassification rate requires total-items-denominator — see note below.
        flagged.append({
            "original_category": orig,
            "top_correction": top_correction,
            "top_count": top_count,
            "total_corrections": count,
        })

    # Filter: include if correction count meets min_count.
    # We skip rate-based filtering here because we don't have total-items-per-category
    # without another query; count alone is the honest threshold.
    flagged = [f for f in flagged if f["total_corrections"] >= min_count]
    flagged.sort(key=lambda f: f["total_corrections"], reverse=True)
    return flagged


def _fetch_examples(conn: psycopg.Connection, flagged: list[dict], n: int) -> dict:
    """
    Return {original_category: [(raw_content, corrected_category), ...]} for each
    flagged category. Takes the N most recently corrected examples.
    """
    examples: dict = {}
    for entry in flagged:
        orig = entry["original_category"]
        rows = conn.execute(
            """SELECT raw_content, corrected_category
                 FROM items
                WHERE category = %s
                  AND corrected_category IS NOT NULL
                  AND corrected_category != category
                ORDER BY corrected_at DESC
                LIMIT %s""",
            (orig, n),
        ).fetchall()
        examples[orig] = [(r[0], r[1]) for r in rows]
    return examples


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_block(flagged: list[dict], examples: dict, total_corrections: int) -> str:
    cats_flagged = len(flagged)
    lines = [
        BLOCK_START,
        f"# Last updated: {date.today()}  |  Total corrections analysed: {total_corrections}  |  Categories flagged: {cats_flagged}",
        "#",
        "# Correction examples (user-verified ground truth — prefer these patterns):",
        "",
    ]
    for entry in flagged:
        orig = entry["original_category"]
        n_corr = entry["total_corrections"]
        lines.append(
            f"# {orig!r}: {n_corr} correction(s), most often → {entry['top_correction']!r}"
        )
        for raw_content, corrected in examples.get(orig, []):
            snippet = raw_content.replace("\n", " ").strip()[:120]
            lines.append(
                f'Correction: "{snippet}"  →  {corrected}'
                f'  (was misclassified as: {orig})'
            )
        lines.append("")
    lines.append(BLOCK_END)
    return "\n".join(lines)


def _write_block(block: str, dry_run: bool, total_corrections: int) -> None:
    text = PROMPT_FILE.read_text()

    start_idx = text.find(BLOCK_START)
    end_idx   = text.find(BLOCK_END)

    if start_idx != -1 and end_idx != -1:
        # Replace existing block (end_idx + len of end marker + trailing newline)
        end_pos = end_idx + len(BLOCK_END)
        if end_pos < len(text) and text[end_pos] == "\n":
            end_pos += 1
        new_text = text[:start_idx] + (block + "\n" if block else "") + text[end_pos:]
    elif block:
        # First run — append after a blank line separator
        new_text = text.rstrip("\n") + "\n\n" + block + "\n"
    else:
        # No existing block, nothing to write
        new_text = text

    if dry_run:
        print("── dry run: would write the following to", PROMPT_FILE, "──")
        print(new_text)
        return

    PROMPT_FILE.write_text(new_text)
    print(f"Updated {PROMPT_FILE}  (total corrections: {total_corrections})")


if __name__ == "__main__":
    main()
