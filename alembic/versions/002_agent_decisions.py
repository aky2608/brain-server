"""agent_decisions: dispatch audit log (sole writer: Personal Agent)

Revision ID: 002_agent_decisions
Revises: 001_schema_gaps
Create Date: 2026-08-21
"""

revision = "002_agent_decisions"
down_revision = "001_schema_gaps"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    op.create_table(
        "agent_decisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("item_id", postgresql.UUID(), sa.ForeignKey("items.id"), nullable=True),
        sa.Column("action_taken", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("interrupt_tier", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("ix_agent_decisions_created_at", "agent_decisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_decisions_created_at", table_name="agent_decisions")
    op.drop_table("agent_decisions")
