"""
SchedulingAgent — deterministic plan enforcement, zero LLM.

Four invariants applied in order on every run:
  1. rollover_yesterday   — unfinished today-items from prior dates get plan_date=today
                            and a visible rollover_note; they never silently vanish.
  2. enforce_50pct_rule   — tasks <50% done with <6h to deadline are pushed to tomorrow
                            (too late to finish; reschedule rather than fail).
  3. energy_aware_sort    — if latest energy_score ≤ 2, heavy tasks (build/wayclear/accrediq)
                            are moved out of Today; degrades silently with no reading.
  4. enforce_max5         — at most 5 *pending* items stay in Today; excess queues to
                            this_week, never force-displacing in_progress work.

Decisions are returned in SchedulingOutput.decisions for personal_agent to
write to agent_decisions (sole-writer invariant).

Triggered via /plan slash command — works for cron, reactive (task_status change),
and manual invocation without any special-casing in the router.
"""
import logging
import os
from datetime import date, timedelta
from typing import ClassVar, Optional

import psycopg

from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel

logger = logging.getLogger("brain")


class SchedulingInput(NarrowModel):
    trigger: str  # "cron" | "reactive" | "manual"
    item_id: Optional[str] = None  # changed item for reactive runs; None for cron/manual


class SchedulingOutput(NarrowModel):
    today_count: int
    rescheduled: list[str]
    rolled_over: list[str]
    plan_written: bool
    decisions: list[dict]  # {action_taken, reason, item_id} relayed to personal_agent for logging


class SchedulingAgent(BaseAgent):
    interrupt_tier: ClassVar[InterruptTier] = InterruptTier.log_only
    cost_tier: ClassVar[CostTier] = CostTier.free
    requires_context: ClassVar[list[str]] = []

    InputSchema = SchedulingInput
    OutputSchema = SchedulingOutput

    def handle(self, input: SchedulingInput) -> SchedulingOutput:
        url = os.environ.get("BRAIN_DB_URL", "")
        if not url:
            logger.error("scheduling_agent: BRAIN_DB_URL not set")
            return SchedulingOutput(
                today_count=0, rescheduled=[], rolled_over=[],
                plan_written=False, decisions=[],
            )

        decisions: list[dict] = []
        rolled: list[str] = []
        rescheduled: list[str] = []

        try:
            with psycopg.connect(url) as conn:
                rolled = _rollover_yesterday(conn, decisions)
                rescheduled = _enforce_50pct_rule(conn, decisions)
                _energy_aware_sort(conn, decisions)
                today_count = _enforce_max5(conn, decisions)
                conn.commit()
        except Exception:
            logger.exception("scheduling_agent: DB error during plan enforcement")
            return SchedulingOutput(
                today_count=0, rescheduled=rescheduled, rolled_over=rolled,
                plan_written=False, decisions=decisions,
            )

        logger.info(
            "scheduling_agent complete",
            extra={"ctx": {
                "trigger": input.trigger,
                "rolled_over": len(rolled),
                "rescheduled": len(rescheduled),
                "today_count": today_count,
            }},
        )
        return SchedulingOutput(
            today_count=today_count,
            rescheduled=rescheduled,
            rolled_over=rolled,
            plan_written=True,
            decisions=decisions,
        )


# ---------------------------------------------------------------------------
# Invariant helpers — each mutates DB and appends to decisions list
# ---------------------------------------------------------------------------

def _rollover_yesterday(conn: psycopg.Connection, decisions: list[dict]) -> list[str]:
    """
    Unfinished today-items from prior dates roll forward with a visible note.
    Runs first so rolled items are counted by the max-5 cap.
    """
    today = date.today()
    rows = conn.execute(
        """SELECT id, plan_date FROM items
            WHERE plan_bucket = 'today'
              AND task_status IS DISTINCT FROM 'done'
              AND status = 'active'
              AND (plan_date IS NULL OR plan_date < %s)""",
        (today,),
    ).fetchall()

    if not rows:
        return []

    ids = [str(r[0]) for r in rows]
    for item_id, old_date in rows:
        note = f"rolled from {old_date}" if old_date else "rolled over (no date set)"
        conn.execute(
            "UPDATE items SET plan_date = %s, rollover_note = %s WHERE id = %s",
            (today, note, item_id),
        )
        decisions.append({
            "action_taken": "rollover",
            "reason": note,
            "item_id": str(item_id),
        })

    return ids


def _enforce_50pct_rule(conn: psycopg.Connection, decisions: list[dict]) -> list[str]:
    """
    Tasks with <50% progress and <6h to deadline are pushed to tomorrow.
    Better to reschedule cleanly than to fail visibly.
    """
    tomorrow = date.today() + timedelta(days=1)
    rows = conn.execute(
        """SELECT id, task_deadline, task_progress FROM items
            WHERE task_status IS DISTINCT FROM 'done'
              AND status = 'active'
              AND plan_bucket IN ('today', 'this_week')
              AND task_deadline IS NOT NULL
              AND task_deadline < now() + interval '6 hours'
              AND COALESCE(task_progress, 0) < 0.5""",
    ).fetchall()

    if not rows:
        return []

    ids = [str(r[0]) for r in rows]
    for item_id, deadline, progress in rows:
        pct = int((progress or 0) * 100)
        conn.execute(
            """UPDATE items
                  SET plan_bucket = 'this_week',
                      plan_date   = %s,
                      plan_order  = NULL
                WHERE id = %s""",
            (tomorrow, item_id),
        )
        decisions.append({
            "action_taken": "reschedule_50pct",
            "reason": f"{pct}% done with deadline {deadline.isoformat()} in <6h — pushed to {tomorrow}",
            "item_id": str(item_id),
        })

    return ids


def _energy_aware_sort(conn: psycopg.Connection, decisions: list[dict]) -> None:
    """
    If latest energy reading is low (≤ 2/5), heavy tasks leave Today.
    Degrades silently when no energy reading exists — no crash, no change.
    """
    row = conn.execute(
        """SELECT energy_score FROM items
            WHERE energy_score IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1""",
    ).fetchone()

    if not row or row[0] is None or row[0] > 2:
        return

    energy = row[0]
    heavy = conn.execute(
        """SELECT id FROM items
            WHERE plan_bucket = 'today'
              AND task_status IS DISTINCT FROM 'done'
              AND status = 'active'
              AND (action_class = 'build'
                   OR subcategory IN ('wayclear', 'accrediq'))""",
    ).fetchall()

    if not heavy:
        return

    tomorrow = date.today() + timedelta(days=1)
    for (item_id,) in heavy:
        conn.execute(
            """UPDATE items
                  SET plan_bucket = 'this_week',
                      plan_date   = %s,
                      plan_order  = NULL
                WHERE id = %s""",
            (tomorrow, item_id),
        )
        decisions.append({
            "action_taken": "energy_defer",
            "reason": f"energy_score={energy}/5 — heavy task moved to this_week",
            "item_id": str(item_id),
        })


def _enforce_max5(conn: psycopg.Connection, decisions: list[dict]) -> int:
    """
    At most 5 pending items stay in Today. in_progress items are never demoted.
    Excess pending items (lowest plan_order priority / null order) queue to this_week.
    Returns final count of active today items (pending + in_progress).
    """
    # in_progress items are sacred — never touch them
    pending = conn.execute(
        """SELECT id FROM items
            WHERE plan_bucket = 'today'
              AND task_status IS DISTINCT FROM 'done'
              AND COALESCE(task_status, 'pending') = 'pending'
              AND status = 'active'
            ORDER BY
                plan_order ASC NULLS LAST,
                created_at ASC
            OFFSET 5""",
    ).fetchall()

    tomorrow = date.today() + timedelta(days=1)
    for (item_id,) in pending:
        conn.execute(
            """UPDATE items
                  SET plan_bucket = 'this_week',
                      plan_date   = %s,
                      plan_order  = NULL
                WHERE id = %s""",
            (tomorrow, item_id),
        )
        decisions.append({
            "action_taken": "max5_queue",
            "reason": "Today cap reached (5 pending) — queued to this_week",
            "item_id": str(item_id),
        })

    row = conn.execute(
        """SELECT COUNT(*) FROM items
            WHERE plan_bucket = 'today'
              AND task_status IS DISTINCT FROM 'done'
              AND status = 'active'""",
    ).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Graph node
# ---------------------------------------------------------------------------

_agent = SchedulingAgent()


def scheduling_agent_node(state: GraphState) -> dict:
    capture_uuid = state.get("capture_uuid")
    # capture_uuid present + looks like a UUID → reactive (specific task changed)
    # "cron" sentinel or None → cron/manual run
    is_reactive = capture_uuid and capture_uuid not in ("cron", "manual")
    result = _agent.handle(SchedulingInput(
        trigger="reactive" if is_reactive else "cron",
        item_id=capture_uuid if is_reactive else None,
    ))
    return {"specialist_result": result.model_dump()}
