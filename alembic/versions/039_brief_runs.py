"""brief_runs table and items.completed_at

brief_runs stores the ordered item_ids from each morning brief so that
/done and the ✅ inline buttons can resolve indices without re-running the
brief query.  items.completed_at records when a task was marked done — used
to bound the daily done counts without changing the plan_bucket.

Revision ID: 039_brief_runs
Revises: 038_agent_runs
Create Date: 2026-10-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "039_brief_runs"
down_revision: Union[str, Sequence[str], None] = "038_agent_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE brief_runs (
            id       BIGSERIAL PRIMARY KEY,
            sent_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            item_ids JSONB NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_br_sent_at ON brief_runs (sent_at DESC)")
    op.execute("ALTER TABLE items ADD COLUMN completed_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE items DROP COLUMN IF EXISTS completed_at")
    op.execute("DROP TABLE IF EXISTS brief_runs")
