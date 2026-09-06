"""seed agent_watch_rules with missed_charge rule

Fires at morning_brief when a recurrence_group's next_expected_date has
passed the grace period with no matching transaction since last_seen_date.
Auto-resolves when the late charge lands — no status mutation on fire.

Revision ID: 015_seed_missed_charge_rule
Revises: 014_finance_transactions
Create Date: 2026-09-06
"""

revision = "015_seed_missed_charge_rule"
down_revision = "014_finance_transactions"
branch_labels = None
depends_on = None

from alembic import op


def upgrade() -> None:
    op.execute("""
        INSERT INTO agent_watch_rules
            (rule_type, condition, interrupt_tier, cooldown_hours, enabled)
        VALUES
            (
                'missed_charge',
                '{"grace_period_days": 3}',
                'morning_brief',
                24,
                true
            )
    """)


def downgrade() -> None:
    op.execute("DELETE FROM agent_watch_rules WHERE rule_type = 'missed_charge'")
