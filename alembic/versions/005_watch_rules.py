"""watch rules: expand agent_watch_rules + /watch shortcut seed

Revision ID: 005_watch_rules
Revises: 004_scheduling
Create Date: 2026-08-22
"""

revision = "005_watch_rules"
down_revision = "004_scheduling"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    # Expand agent_watch_rules from the Phase 2.1 stub.
    # server_defaults here are transient: they satisfy NOT NULL for any pre-existing rows,
    # then we strip them so future inserts must supply values explicitly.
    op.add_column("agent_watch_rules", sa.Column(
        "rule_type", sa.Text(), nullable=False, server_default="unknown"
    ))
    op.add_column("agent_watch_rules", sa.Column(
        "condition", postgresql.JSONB(), nullable=False, server_default="{}"
    ))
    op.add_column("agent_watch_rules", sa.Column(
        "interrupt_tier", sa.Text(), nullable=False, server_default="log_only"
    ))
    op.add_column("agent_watch_rules", sa.Column(
        "cooldown_hours", sa.Integer(), nullable=False, server_default="24"
    ))
    op.add_column("agent_watch_rules", sa.Column(
        "enabled", sa.Boolean(), nullable=False, server_default="true"
    ))
    op.add_column("agent_watch_rules", sa.Column(
        "missed_count", sa.Integer(), nullable=False, server_default="0"
    ))
    op.add_column("agent_watch_rules", sa.Column(
        "last_cleared_at", sa.TIMESTAMP(timezone=True), nullable=True
    ))

    # Drop transient server_defaults — inserts must be explicit from now on
    op.alter_column("agent_watch_rules", "rule_type", server_default=None)
    op.alter_column("agent_watch_rules", "condition", server_default=None)
    op.alter_column("agent_watch_rules", "interrupt_tier", server_default=None)

    # Seed the four canonical watch rules.
    # gate_missed: two thresholds — warn at 3 (morning_brief), escalate at 5 (always).
    # The WatchAgent reads threshold_warn and threshold_escalate from condition.
    op.execute("""
        INSERT INTO agent_watch_rules
            (rule_type, condition, interrupt_tier, cooldown_hours, enabled)
        VALUES
            (
                'gate_missed',
                '{"threshold_warn": 3, "threshold_escalate": 5, "window_days": 7}',
                'morning_brief',
                24,
                true
            ),
            (
                'reminder_snoozed',
                '{"threshold": 3}',
                'morning_brief',
                24,
                true
            ),
            (
                'interview_prep',
                '{"days_warning": 5}',
                'always',
                24,
                true
            ),
            (
                'scheduling_conflict',
                '{"overdue_threshold": 1}',
                'log_only',
                6,
                true
            )
    """)

    # /watch shortcut → routes to watch_agent via DISPATCH_MAP
    op.execute("""
        INSERT INTO capture_shortcuts (alias, category, subcategory, agent)
        VALUES ('watch', NULL, NULL, 'watch_agent')
        ON CONFLICT (alias) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM capture_shortcuts WHERE alias = 'watch'")
    op.execute("DELETE FROM agent_watch_rules WHERE rule_type IN "
               "('gate_missed','reminder_snoozed','interview_prep','scheduling_conflict')")
    op.drop_column("agent_watch_rules", "last_cleared_at")
    op.drop_column("agent_watch_rules", "missed_count")
    op.drop_column("agent_watch_rules", "enabled")
    op.drop_column("agent_watch_rules", "cooldown_hours")
    op.drop_column("agent_watch_rules", "interrupt_tier")
    op.drop_column("agent_watch_rules", "condition")
    op.drop_column("agent_watch_rules", "rule_type")
