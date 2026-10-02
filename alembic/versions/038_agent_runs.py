"""agent_runs and heartbeat_alerts tables

agent_runs records one row per component invocation so that "returned without
error" is never the only success signal.  heartbeat_alerts deduplicates alerts
so a dead component fires once per day, not once per heartbeat check.

Revision ID: 038_agent_runs
Revises: 037_section_lapses
Create Date: 2026-10-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
# IMPORTANT: revision string must be ≤32 characters — it is stored in alembic_version VARCHAR(32).
revision: str = "038_agent_runs"
down_revision: Union[str, Sequence[str], None] = "037_section_lapses"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agent_runs (
            id          BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            component   TEXT NOT NULL,
            started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ,
            outcome     TEXT CHECK (outcome IN ('acted', 'no_action_needed', 'skipped', 'failed')),
            detail      TEXT,
            error       TEXT
        )
    """)
    op.execute("""
        CREATE INDEX ix_ar_component_started
            ON agent_runs (component, started_at DESC)
    """)

    op.execute("""
        CREATE TABLE heartbeat_alerts (
            id         BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            component  TEXT NOT NULL,
            reason     TEXT NOT NULL,
            alerted_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX ix_ha_component_alerted
            ON heartbeat_alerts (component, alerted_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS heartbeat_alerts")
    op.execute("DROP TABLE IF EXISTS agent_runs")
