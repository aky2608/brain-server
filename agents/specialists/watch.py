"""
WatchAgent — zero-LLM condition evaluator for agent_watch_rules.

Evaluates each enabled rule against recent items/decisions, fires interrupts
respecting the per-rule cooldown (last_notified_at + cooldown_hours).

ADHD invariants enforced:
  - Escalate friction, never lock: tiers route to morning_brief or log_only by
    default; only interview_prep fires 'always'.
  - A completed drill resets the tasks missed-counter, but absence-of-practice
    still fires independently — one session does not satisfy min_required.
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


def _tg_chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")


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
        rules_evaluated = 0

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
                rules_evaluated = len(rule_dicts)

                evaluators = {
                    "gate_missed": _eval_gate_missed,
                    "reminder_snoozed": _eval_reminder_snoozed,
                    "interview_prep": _eval_interview_prep,
                    "scheduling_conflict": _eval_scheduling_conflict,
                    "missed_charge": _eval_missed_charge,
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
                        # Atomic with outbox write below — last_notified_at is never
                        # stamped without the message being queued.
                        conn.execute(
                            "UPDATE agent_watch_rules SET last_notified_at = now() WHERE id = %s",
                            (rule["id"],),
                        )
                        tier = decision["interrupt_tier"]
                        if tier == "always":
                            chat_id = _tg_chat_id()
                            if chat_id:
                                conn.execute(
                                    "INSERT INTO outbox (channel, recipient, message)"
                                    " VALUES ('telegram', %s, %s)",
                                    (chat_id, f"\u26a0\ufe0f {rule['rule_type']}: {decision['reason']}"),
                                )
                            else:
                                logger.warning("watch_agent: TELEGRAM_CHAT_ID not set; skipping outbox for %s", rule["rule_type"])
                        # morning_brief: no 6:30am delivery mechanism exists yet —
                        # intentionally unbuilt, not an oversight. reminder_snoozed,
                        # missed_charge, and non-escalated gate_missed all land here.
                        # log_only: no message by design.

                conn.commit()

        except Exception:
            logger.exception("watch_agent: DB error during rule evaluation")
            return WatchOutput(
                rules_evaluated=rules_evaluated, interrupts_fired=fired, decisions=decisions
            )

        logger.info(
            "watch_agent complete",
            extra={"ctx": {
                "trigger": input.trigger,
                "rules_evaluated": rules_evaluated,
                "interrupts_fired": fired,
            }},
        )
        return WatchOutput(
            rules_evaluated=rules_evaluated,
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
    Fires if undone GATE tasks OR absent practice in the rolling window.

    Clear-check resets missed_count (tasks counter) when any verified session or
    revision review falls within the relevant window, but does NOT suppress the
    absence condition. A single completed session resets the tasks counter yet can
    still leave sessions_in_window below min_required — that nag is intentional.
    The old "clear check → return None" invariant applied when this rule only
    tracked undone tasks; it does not hold for the absence condition.

    Conditions (fire on either):
      undone_tasks  — active gate-tagged items not done, count >= threshold_warn
                      escalates to 'always' at threshold_escalate
      absence       — verified drill_sessions in window < min_required (default 3)
                      escalates to 'always' when gap since last session > 2 * window_days

    One rule row covers both conditions; they share a single cooldown.
    """
    cond = rule.get("condition") or {}
    threshold_warn = int(cond.get("threshold_warn", 3))
    threshold_escalate = int(cond.get("threshold_escalate", 5))
    window_days = int(cond.get("window_days", 7))
    min_required = int(cond.get("min_required", 3))

    last_cleared = rule.get("last_cleared_at")
    if last_cleared:
        cleared = conn.execute(
            """SELECT EXISTS (
                   SELECT 1 FROM drill_sessions
                   WHERE verified = true AND created_at > %s

                   UNION ALL

                   SELECT 1 FROM revision_reviews rr
                   JOIN revision_questions rq ON rq.id = rr.question_id
                   JOIN notebooks nb ON nb.id = rq.notebook_id
                   WHERE rr.reviewed_at > %s AND nb.notebook_type = 'gate_subject'
                   LIMIT 1
               )""",
            (last_cleared, last_cleared),
        ).fetchone()[0]
    else:
        # Bound to window start — without this, any historical session sets
        # cleared=True and the rule can never fire on a fresh install.
        cleared = conn.execute(
            f"""SELECT EXISTS (
                   SELECT 1 FROM drill_sessions
                   WHERE verified = true
                     AND created_at > now() - interval '{window_days} days'

                   UNION ALL

                   SELECT 1 FROM revision_reviews rr
                   JOIN revision_questions rq ON rq.id = rr.question_id
                   JOIN notebooks nb ON nb.id = rq.notebook_id
                   WHERE rr.reviewed_at > now() - interval '{window_days} days'
                     AND nb.notebook_type = 'gate_subject'
                   LIMIT 1
               )""",
        ).fetchone()[0]

    if cleared:
        conn.execute(
            "UPDATE agent_watch_rules SET missed_count = 0, last_cleared_at = now() WHERE id = %s",
            (rule["id"],),
        )

    sessions_in_window = conn.execute(
        f"""SELECT COUNT(*) FROM drill_sessions
            WHERE verified = true
              AND created_at > now() - interval '{window_days} days'""",
    ).fetchone()[0]

    missed = conn.execute(
        f"""SELECT COUNT(*) FROM items
            WHERE status = 'active'
              AND action_class = 'task'
              AND (task_status IS NULL OR task_status != 'done')
              AND (subcategory = 'gate' OR ai_tags @> '["gate"]'::jsonb)
              AND created_at > now() - interval '{window_days} days'""",
    ).fetchone()[0]

    if not cleared:
        conn.execute(
            "UPDATE agent_watch_rules SET missed_count = %s WHERE id = %s",
            (missed, rule["id"]),
        )

    tasks_fire = missed >= threshold_warn
    absence_fire = sessions_in_window < min_required

    if not (tasks_fire or absence_fire):
        return None

    if not _cooldown_elapsed(rule):
        return None

    parts: list[str] = []
    tiers: list[str] = []

    if tasks_fire:
        tiers.append("always" if missed >= threshold_escalate else rule["interrupt_tier"])
        parts.append(f"{missed} undone tasks (warn≥{threshold_warn})")

    if absence_fire:
        last_row = conn.execute(
            "SELECT MAX(created_at) FROM drill_sessions WHERE verified = true",
        ).fetchone()
        last_session_at = last_row[0] if last_row else None
        if last_session_at is None:
            gap_days = 2 * window_days + 1
        else:
            if last_session_at.tzinfo is None:
                last_session_at = last_session_at.replace(tzinfo=timezone.utc)
            gap_days = (datetime.now(timezone.utc) - last_session_at).days
        tiers.append("always" if gap_days > 2 * window_days else "morning_brief")
        parts.append(f"{sessions_in_window}/{min_required} sessions in {window_days}d (last {gap_days}d ago)")

    tier = "always" if "always" in tiers else tiers[0]
    return _decision(
        action_taken="watch_gate_missed",
        reason=f"GATE: {'; '.join(parts)}",
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


def _eval_missed_charge(conn: psycopg.Connection, rule: dict) -> Optional[dict]:
    """
    Recurring charges where next_expected_date has passed the grace period
    and no transaction has landed since last_seen_date.

    Auto-resolves without status mutation: when the late charge finally arrives,
    _update_recurrence advances next_expected_date, clearing the NOT EXISTS check
    on the next WatchAgent run. status='missed' is only set by explicit user action.
    """
    cond = rule.get("condition") or {}
    grace = int(cond.get("grace_period_days", 3))

    overdue = conn.execute(
        """SELECT rg.merchant, rg.expected_amount, rg.next_expected_date
           FROM recurrence_groups rg
           WHERE rg.status = 'active'
             AND rg.next_expected_date < CURRENT_DATE - %s
             AND NOT EXISTS (
                 SELECT 1 FROM transactions t
                 WHERE lower(t.merchant) = lower(rg.merchant)
                   AND t.transaction_date > rg.last_seen_date
             )""",
        (grace,),
    ).fetchall()

    if not overdue:
        return None

    if not _cooldown_elapsed(rule):
        return None

    merchants = ", ".join(r[0] for r in overdue)
    return _decision(
        action_taken="watch_missed_charge",
        reason=f"{len(overdue)} recurring charge(s) overdue >{grace}d: {merchants}",
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
