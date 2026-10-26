"""
FinanceAgent — classify + extract financial transactions from finance-tagged captures.

One LLM call (Haiku via 1min.ai) per capture:
  - Classifies: genuine transaction vs. finance-adjacent content (news, reminders, notes)
  - If genuine: extracts {amount, direction, merchant, category, date}
  - Non-transactions return immediately without writing to transactions table

Zero LLM for recurrence detection — pure date-interval math against existing rows.
"""
import json
import logging
import os
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, Optional

import httpx
import psycopg

from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel

logger = logging.getLogger("brain")

# Claude models are not supported for UNIFY_CHAT_WITH_AI on this 1min.ai account/plan —
# confirmed via direct API testing. Both models below are verified working.
# ⚠ gpt-4.1-nano deprecationDate: 2026-10-21 — replace fallback before that date.
_ONEMIN_MODEL = "gpt-4o-mini"
_FALLBACK_MODEL = "gpt-4.1-nano"

# Recurrence detection thresholds
_RECURRENCE_MIN_OCCURRENCES = 3   # need at least this many same-merchant rows
_INTERVAL_TOLERANCE_DAYS = 5      # ±5 days still counts as consistent
_KNOWN_INTERVALS = [7, 14, 30, 31, 365]

_PROMPT_TMPL: Optional[str] = None


def _load_prompt() -> str:
    global _PROMPT_TMPL
    if _PROMPT_TMPL is None:
        p = Path(__file__).parent.parent.parent / "prompts" / "finance_extract.txt"
        _PROMPT_TMPL = p.read_text()
    return _PROMPT_TMPL


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class FinanceInput(NarrowModel):
    item_id: str    # UUID — already written to items by CaptureAgent
    raw: str        # raw_content, from GraphState.raw_input
    source: str


class FinanceOutput(NarrowModel):
    item_id: str
    transaction_id: Optional[int] = None
    extraction_success: bool
    decisions: list[dict] = []


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

_agent: Optional["FinanceAgent"] = None


class FinanceAgent(BaseAgent):
    interrupt_tier: ClassVar[InterruptTier] = InterruptTier.log_only
    cost_tier: ClassVar[CostTier] = CostTier.flash
    requires_context: ClassVar[list[str]] = []

    InputSchema = FinanceInput
    OutputSchema = FinanceOutput

    def handle(self, input: FinanceInput) -> FinanceOutput:
        extracted = _extract(input.raw)
        decisions: list[dict] = []

        if not extracted or not extracted.get("is_transaction"):
            logger.info(
                "finance_agent: non-transaction capture, skipping ledger write",
                extra={"ctx": {"item_id": input.item_id}},
            )
            decisions.append({
                "agent_name": "finance_agent",
                "action_taken": "rejected:non-transaction",
                "reason": "LLM classified as finance-adjacent, not a real transaction",
                "interrupt_tier": "log_only",
            })
            return FinanceOutput(item_id=input.item_id, extraction_success=False, decisions=decisions)

        amount_raw = extracted.get("amount")
        if amount_raw is None:
            logger.warning(
                "finance_agent: is_transaction=true but amount is null",
                extra={"ctx": {"item_id": input.item_id}},
            )
            decisions.append({
                "agent_name": "finance_agent",
                "action_taken": "rejected:missing-amount",
                "reason": "is_transaction=true but amount field is null in LLM response",
                "interrupt_tier": "log_only",
            })
            return FinanceOutput(item_id=input.item_id, extraction_success=False, decisions=decisions)

        try:
            amount = Decimal(str(amount_raw))
        except Exception:
            logger.error(
                "finance_agent: unparseable amount %r", amount_raw,
                extra={"ctx": {"item_id": input.item_id}},
            )
            decisions.append({
                "agent_name": "finance_agent",
                "action_taken": "rejected:unparseable-amount",
                "reason": f"could not parse amount {amount_raw!r} as Decimal",
                "interrupt_tier": "log_only",
            })
            return FinanceOutput(item_id=input.item_id, extraction_success=False, decisions=decisions)

        direction = extracted.get("direction") or "debit"
        merchant_raw = extracted.get("merchant")
        merchant = merchant_raw.strip() if merchant_raw else None
        category = extracted.get("category")
        date_str = extracted.get("date")

        authoritative_date, created_date = _item_tx_date(input.item_id)

        # Use the item's authoritative date (starts_at → created_at) rather than the
        # model's date_str.  The model is only consulted as a last resort when the item
        # has no starts_at, and even then only for month/day — the year is clamped to
        # created_at's year to prevent the LLM defaulting bare dates to a past year.
        tx_date = authoritative_date
        if date_str and authoritative_date == created_date:
            # starts_at was absent; try to use the model's day/month on the correct year
            try:
                parsed = date.fromisoformat(date_str)
                if parsed.year < created_date.year - 1:
                    logger.warning(
                        "finance_agent: model date %s is >1 year before capture %s — clamping year to %s",
                        parsed, created_date, created_date.year,
                    )
                    parsed = parsed.replace(year=created_date.year)
                tx_date = parsed
            except ValueError:
                pass  # leave tx_date = created_date

        tx_id = _write_transaction(
            item_id=input.item_id,
            amount=amount,
            direction=direction,
            merchant=merchant,
            category=category,
            tx_date=tx_date,
        )

        if tx_id:
            decisions.append({
                "agent_name": "finance_agent",
                "action_taken": f"transaction:written:{direction}",
                "reason": f"{direction} {amount} merchant={merchant or 'unknown'} date={tx_date}",
                "interrupt_tier": "log_only",
            })
        else:
            decisions.append({
                "agent_name": "finance_agent",
                "action_taken": "transaction:failed",
                "reason": "DB insert failed; see logs",
                "interrupt_tier": "log_only",
            })

        recurrence_decision = None
        if tx_id and merchant:
            recurrence_decision = _update_recurrence(merchant, amount, tx_date)
        if recurrence_decision:
            decisions.append(recurrence_decision)

        return FinanceOutput(
            item_id=input.item_id,
            transaction_id=tx_id,
            extraction_success=tx_id is not None,
            decisions=decisions,
        )


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

def _call_1minai(prompt: str, model: str) -> str:
    r = httpx.post(
        f"{os.environ['ONEMIN_API_URL']}/api/chat-with-ai",
        headers={"API-KEY": os.environ["ONEMIN_API_KEY"], "Content-Type": "application/json"},
        json={"type": "UNIFY_CHAT_WITH_AI", "model": model, "promptObject": {"prompt": prompt}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["aiRecord"]["aiRecordDetail"]["resultObject"][0]


def _extract(raw: str) -> Optional[dict]:
    prompt = _load_prompt().format(raw_content=raw[:1000])
    try:
        resp = _call_1minai(prompt, _ONEMIN_MODEL)
    except Exception:
        logger.warning("finance_agent: 1min.ai call failed, trying fallback", exc_info=True)
        try:
            resp = _call_1minai(prompt, _FALLBACK_MODEL)
        except Exception:
            logger.error("finance_agent: extraction fallback also failed", exc_info=True)
            return None

    clean = re.sub(r"```(?:json)?\n?", "", resp).strip().strip("`")
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        logger.error("finance_agent: JSON parse failed: %r", clean[:200])
        return None


# ---------------------------------------------------------------------------
# DB helpers — each opens its own connection; commits immediately
# ---------------------------------------------------------------------------

def _db_url() -> str:
    return os.environ.get("BRAIN_DB_URL", "")


def _item_tx_date(item_id: str) -> tuple[date, date]:
    """Return (authoritative_date, created_at_date) for the item.

    authoritative_date: starts_at if set (capture already resolved it deterministically),
    otherwise created_at. This is the preferred transaction date — no LLM date parsing needed.
    created_at_date is returned separately so callers can clamp a model-supplied year.
    """
    url = _db_url()
    today = date.today()
    if not url:
        return today, today
    try:
        with psycopg.connect(url) as conn:
            row = conn.execute(
                "SELECT starts_at, created_at FROM items WHERE id = %s", (item_id,)
            ).fetchone()
            if row:
                starts_at, created_at = row
                created_date = created_at.date() if created_at else today
                if starts_at:
                    return starts_at.date(), created_date
                return created_date, created_date
    except Exception:
        logger.warning("finance_agent: could not fetch item dates", exc_info=True)
    return today, today


def _write_transaction(
    item_id: str,
    amount: Decimal,
    direction: str,
    merchant: Optional[str],
    category: Optional[str],
    tx_date: date,
) -> Optional[int]:
    url = _db_url()
    if not url:
        return None
    try:
        with psycopg.connect(url) as conn:
            row = conn.execute(
                """INSERT INTO transactions
                       (item_id, amount, direction, merchant, category, transaction_date)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (item_id, amount, direction, merchant, category, tx_date),
            ).fetchone()
            conn.commit()
            tx_id = row[0] if row else None
            logger.info(
                "finance_agent: transaction written",
                extra={"ctx": {"item_id": item_id, "transaction_id": tx_id,
                               "amount": str(amount), "direction": direction}},
            )
            return tx_id
    except Exception:
        logger.error("finance_agent: transaction insert failed", exc_info=True)
        return None


def _update_recurrence(merchant: str, amount: Decimal, tx_date: date) -> Optional[dict]:
    url = _db_url()
    if not url:
        return
    try:
        with psycopg.connect(url) as conn:
            rows = conn.execute(
                """SELECT transaction_date FROM transactions
                   WHERE lower(merchant) = lower(%s)
                   ORDER BY transaction_date""",
                (merchant,),
            ).fetchall()

            if len(rows) < _RECURRENCE_MIN_OCCURRENCES:
                return None

            dates = [r[0] for r in rows]
            intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
            threshold = max(2, len(intervals) // 2 + 1)
            matched_interval = next(
                (iv for iv in _KNOWN_INTERVALS
                 if sum(1 for gap in intervals
                        if abs(gap - iv) <= _INTERVAL_TOLERANCE_DAYS) >= threshold),
                None,
            )
            if matched_interval is None:
                return None

            last_date = dates[-1]
            next_date = last_date + timedelta(days=matched_interval)

            # Upsert recurrence_group by merchant (case-insensitive)
            existing = conn.execute(
                "SELECT id FROM recurrence_groups WHERE lower(merchant) = lower(%s)",
                (merchant,),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE recurrence_groups SET
                           expected_amount         = %s,
                           expected_interval_days  = %s,
                           last_seen_date          = %s,
                           next_expected_date      = %s,
                           status                  = 'active'
                       WHERE id = %s""",
                    (amount, matched_interval, last_date, next_date, existing[0]),
                )
                group_id = existing[0]
            else:
                row = conn.execute(
                    """INSERT INTO recurrence_groups
                           (merchant, expected_amount, expected_interval_days,
                            last_seen_date, next_expected_date)
                       VALUES (%s, %s, %s, %s, %s)
                       RETURNING id""",
                    (merchant, amount, matched_interval, last_date, next_date),
                ).fetchone()
                group_id = row[0]

            # Back-fill all matching transactions that lack a group
            conn.execute(
                """UPDATE transactions SET recurrence_group_id = %s
                   WHERE lower(merchant) = lower(%s)
                     AND recurrence_group_id IS NULL""",
                (group_id, merchant),
            )
            conn.commit()
            logger.info(
                "finance_agent: recurrence group updated",
                extra={"ctx": {"merchant": merchant, "group_id": group_id,
                               "interval_days": matched_interval}},
            )
            return {
                "agent_name": "finance_agent",
                "action_taken": f"recurrence:updated:{matched_interval}d",
                "reason": f"merchant={merchant} next_expected={next_date} group_id={group_id}",
                "interrupt_tier": "log_only",
            }
    except Exception:
        logger.error("finance_agent: recurrence update failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Graph node
# ---------------------------------------------------------------------------

_agent = FinanceAgent()


def finance_agent_node(state: GraphState) -> dict:
    capture_uuid = state.get("capture_uuid")
    if not capture_uuid:
        logger.error("finance_agent_node: no capture_uuid in state — skipping")
        return {"specialist_result": {"item_id": None, "extraction_success": False}}

    result = _agent.handle(
        FinanceInput(
            item_id=capture_uuid,
            raw=state["raw_input"],
            source=state.get("source", ""),
        )
    )
    return {"specialist_result": result.model_dump()}
