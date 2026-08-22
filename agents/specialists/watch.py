"""
WatchAgent — zero-LLM condition evaluator for agent_watch_rules.

Evaluates each enabled rule against recent items/decisions, fires interrupts
respecting the per-rule cooldown (last_notified_at + cooldown_hours).

ADHD invariants enforced:
  - Escalate friction, never lock: tiers route to morning_brief or log_only by
    default; only interview_prep fires 'always'.
  - A completed drill clears its missed-counter — a stale gate_missed rule MUST
    NOT fire after the drill was done. Clear check runs before threshold check.
  - Interrupts are cooldown-gated so one unresolved condition cannot spam.

Decisions are returned for PersonalAgent to write to agent_decisions
(sole-writer invariant — this agent never writes agent_decisions directly).
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import ClassVar, Optional

import psycopg

from agents.base import BaseAgent, CostTier, GraphState, InterruptTier, NarrowModel

logger = logging.getLogger("brain")


class WatchInput(NarrowModel):
    trigger: str  # "cron" | "manual"


class WatchOutput(NarrowModel):
    rules_evaluated: int
    interrupts_fired: list[str]        # rule_types that triggered
    decisions: list[dict]              # {agent_name, action_taken, reason, interrupt_tier}


class WatchAgent(BaseAgent):
    interrupt_tier: ClassVar[InterruptTier] = InterruptTier.log_only
    cost_tier: ClassVar[CostTier] = CostTier.free
    requires_context: ClassVar[list[str]] = []

    InputSchema = WatchInput
    OutputSchema = WatchOutput

    def handle(self, input: WatchInput) -> WatchOutput:
        url = os.environ.get("BRAIN_DB_URL", "")
        if not url:
            logger.error("watch_agent: BRAIN_DB_URL not set")
            return WatchOutput(rules_evaluated=0, interrupts_fired=[], decisions=[])

        decisions: list[dict] = []
        fired: list[str] = []

        try:
            with psycopg.connect(url) as conn:
                rules = conn.execute(
                    "SELECT id, rule_type, condition, interrupt_tier, cooldown_hours, "
                    "       enabled, missed_count, last_notified_at, last_cleared_at "
                    "FROM agent_watch_rules WHERE enabled = true"
                ).fetchall()

                cols = [
                    "id", "rule_type", "condition", "interrupt_tier", "cooldown_hours",
                    "enabled", "missed_count", "last_notified_at", "last_cleared_at",
                ]
                rule_dicts = [dict(zip(cols, r)) for r in rules]

                evaluators = {
                    "gate_missed": _eval_gate_missed,
                    "reminder_snoozed": _eval_reminder_snoozed,
                    "interview_prep": _eval_interview_prep,
                    "scheduling_conflict": _eval_scheduling_conflict,
                }

                for rule in rule_dicts:
                    ev = evaluators.get(rule["rule_type"])
                    if ev is None:
                        logger.warning(
                            "watch_agent: unknown rule_type %r — skipping",
                            rule["rule_type"],
                        )
                        continue

                    decision = ev(conn, rule)
                    if decision:
                        fired.append(rule["rule_type"])
                        decisions.append(decision)
                        conn.execute(
                            "UPDATE agent_watch_rules SET last_notified_at = now() WHERE id = %s",
                            (rule["id"],),
                        )

                conn.commit()

        except Exception:
            logger.exception("watch_agent: DB error during rule evaluation")
            return WatchOutput(
                rules_evaluated=0, interrupts_fired=fired, decisions=decisions
            )

        logger.info(
            "watch_agent complete",
            extra={"ctx": {
                "trigger": input.trigger,
                "rules_evaluated": len(rule_dicts) if "rule_dicts" in dir() else 0,
                "interrupts_fired": fired,
            }},
        )
        return WatchOutput(
            rules_evaluated=len(rule_dicts) if "rule_dicts" in dir() else 0,
            interrupts_fired=fired,
            decisions=decisions,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cooldown_elapsed(rule: dict) -> bool:
    last = rule.get("last_notified_at")
    if last is None:
        return True
    cooldown_hours = rule.get("cooldown_hours") or 24
    if isinstance(last, str):
        last = datetime.fromisoformat(last.replace("Z", "+00:00"))
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last > timedelta(hours=cooldown_hours)


def _decision(action_taken: str, reason: str, interrupt_tier: str) -> dict:
    return {
        "agent_name": "watch_agent",
        "action_taken": action_taken,
        "reason": reason,
        "interrupt_tier": interrupt_tier,
    }


# ---------------------------------------------------------------------------
# Rule evaluators — each returns a decision dict or None
# ---------------------------------------------------------------------------

def _eval_gate_missed(conn: psycopg.Connection, rule: dict) -> Optional[dict]:
    """
    Count undone GATE drill tasks in the rolling window.

    Clear-check runs FIRST: if any GATE drill was completed after last_cleared_at,
    reset missed_count and return None — the nag must respect completed work.
    Threshold check only runs if no recent completion found.
    """
    cond = rule.get("condition") or {}
    threshold_warn = int(cond.get("threshold_warn", 3))
    threshold_escalate = int(cond.get("threshold_escalate", 5))
    window_days = int(cond.get("window_days", 7))

    # Check for completed GATE drills since last clear (or all-time if never cleared)
    # status filter deliberately omitted: a drill completed then archived/deleted still
    # counts as done — "a completed drill always clears its missed-counter", full stop.
    last_cleared = rule.get("last_cleared_at")
    if last_cleared:
        completed = conn.execute(
            """SELECT COUNT(*) FROM items
                WHERE action_class = 'task'
                  AND task_status = 'done'
                  AND (subcategory = 'gate' OR ai_tags @> '["gate"]'::jsonb)
                  AND updated_at > %s""",
            (last_cleared,),
        ).fetchone()[0]
    else:
        completed = conn.execute(
            """SELECT COUNT(*) FROM items
                WHERE action_class = 'task'
                  AND task_status = 'done'
                  AND (subcategory = 'gate' OR ai_tags @> '["gate"]'::jsonb)""",
        ).fetchone()[0]

    if completed > 0:
        conn.execute(
            "UPDATE agent_watch_rules SET missed_count = 0, last_cleared_at = now() WHERE id = %s",
            (rule["id"],),
        )
        return None

    missed = conn.execute(
        f"""SELECT COUNT(*) FROM items
            WHERE status = 'active'
              AND action_class = 'task'
              AND (task_status IS NULL OR task_status != 'done')
              AND (subcategory = 'gate' OR ai_tags @> '["gate"]'::jsonb)
              AND created_at > now() - interval '{window_days} days'""",
    ).fetchone()[0]

    conn.execute(
        "UPDATE agent_watch_rules SET missed_count = %s WHERE id = %s",
        (missed, rule["id"]),
    )

    if missed < threshold_warn:
        return None

    if not _cooldown_elapsed(rule):
        return None

    tier = "always" if missed >= threshold_escalate else rule["interrupt_tier"]
    return _decision(
        action_taken="watch_gate_missed",
        reason=f"GATE drills: {missed} undone in rolling {window_days}d (warn≥{threshold_warn}, escalate≥{threshold_escalate})",
        interrupt_tier=tier,
    )


def _eval_reminder_snoozed(conn: psycopg.Connection, rule: dict) -> Optional[dict]:
    """
    Items with snooze_count >= threshold in their metadata without task_status='done'.
    Gracefully returns None when no snooze tracking exists in the data yet.
    """
    cond = rule.get("condition") or {}
    threshold = int(cond.get("threshold", 3))

    try:
        rows = conn.execute(
            """SELECT id FROM items
                WHERE status = 'active'
                  AND (task_status IS NULL OR task_status != 'done')
                  AND (metadata->>'snooze_count') IS NOT NULL
                  AND (metadata->>'snooze_count')::int >= %s""",
            (threshold,),
        ).fetchall()
    except Exception:
        return None

    if not rows:
        return None

    if not _cooldown_elapsed(rule):
        return None

    return _decision(
        action_taken="watch_reminder_snoozed",
        reason=f"{len(rows)} item(s) snoozed ≥{threshold}× without progress",
        interrupt_tier=rule["interrupt_tier"],
    )


def _eval_interview_prep(conn: psycopg.Connection, rule: dict) -> Optional[dict]:
    """
    Interview ≤N days away with zero prep entries this week → always-interrupt.
    If prep entries exist since last_cleared_at, clear the counter and stand down.
    """
    cond = rule.get("condition") or {}
    days_warning = int(cond.get("days_warning", 5))

    interviews = conn.execute(
        """SELECT id FROM items
            WHERE status = 'active'
              AND action_class = 'task'
              AND task_deadline IS NOT NULL
              AND task_deadline > now()
              AND task_deadline <= now() + interval '1 day' * %s
              AND (task_status IS NULL OR task_status != 'done')
              AND (subcategory = 'interview'
                   OR ai_tags @> '["interview"]'::jsonb
                   OR raw_content ILIKE '%%interview%%')""",
        (days_warning,),
    ).fetchall()

    if not interviews:
        return None

    # Check for prep entries this week — if found, clear and stand down
    prep = conn.execute(
        """SELECT COUNT(*) FROM items
            WHERE status = 'active'
              AND created_at >= date_trunc('week', now())
              AND (ai_tags @> '["prep"]'::jsonb
                   OR ai_tags @> '["interview-prep"]'::jsonb
                   OR subcategory = 'interview-prep')""",
    ).fetchone()[0]

    if prep > 0:
        conn.execute(
            "UPDATE agent_watch_rules SET missed_count = 0, last_cleared_at = now() WHERE id = %s",
            (rule["id"],),
        )
        return None

    if not _cooldown_elapsed(rule):
        return None

    return _decision(
        action_taken="watch_interview_prep",
        reason=f"Interview in ≤{days_warning}d with zero prep entries this week",
        interrupt_tier=rule["interrupt_tier"],
    )


def _eval_scheduling_conflict(conn: psycopg.Connection, rule: dict) -> Optional[dict]:
    """
    Overdue items still sitting in Today's plan — deadline passed but not rescheduled.
    The 50%-rule in SchedulingAgent should prevent this; this is a safety-net check.
    """
    cond = rule.get("condition") or {}
    threshold = int(cond.get("overdue_threshold", 1))

    overdue = conn.execute(
        """SELECT COUNT(*) FROM items
            WHERE status = 'active'
              AND plan_bucket = 'today'
              AND task_status IS DISTINCT FROM 'done'
              AND task_deadline IS NOT NULL
              AND task_deadline < now()""",
    ).fetchone()[0]

    if overdue < threshold:
        return None

    if not _cooldown_elapsed(rule):
        return None

    return _decision(
        action_taken="watch_scheduling_conflict",
        reason=f"{overdue} item(s) in Today are overdue — deadline passed without rescheduling",
        interrupt_tier=rule["interrupt_tier"],
    )


# ---------------------------------------------------------------------------
# Graph node
# ---------------------------------------------------------------------------

_agent = WatchAgent()


def watch_agent_node(state: GraphState) -> dict:
    source = state.get("source", "")
    trigger = "cron" if source == "system" else "manual"
    result = _agent.handle(WatchInput(trigger=trigger))
    return {"specialist_result": result.model_dump()}
