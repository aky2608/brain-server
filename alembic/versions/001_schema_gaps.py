"""schema gaps: corrected_category, capture_uuid, embedding_model, capture_shortcuts, agent_watch_rules

Revision ID: 001_schema_gaps
Revises:
Create Date: 2026-08-21
"""

revision = "001_schema_gaps"
down_revision = None
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    # items — correction feedback loop (5.4)
    op.add_column("items", sa.Column("corrected_category", sa.Text(), nullable=True))
    op.add_column("items", sa.Column("corrected_at", sa.TIMESTAMP(timezone=True), nullable=True))

    # items — client-side dedup / idempotent upsert (5.4)
    op.add_column("items", sa.Column("capture_uuid", postgresql.UUID(), nullable=True))
    op.create_unique_constraint("uq_items_capture_uuid", "items", ["capture_uuid"])

    # items — embedding version tracking (5.3)
    op.add_column("items", sa.Column("embedding_model", sa.Text(), nullable=True))

    # capture_shortcuts — slash-command routing table (Section 6)
    op.create_table(
        "capture_shortcuts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("subcategory", sa.Text(), nullable=True),
        sa.Column("agent", sa.Text(), nullable=True),
        sa.Column("notebook_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("alias", name="uq_capture_shortcuts_alias"),
    )

    # agent_watch_rules — minimal; full columns (rule_type, interrupt_tier, cooldown) in Phase 2.7
    op.create_table(
        "agent_watch_rules",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("last_notified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("agent_watch_rules")
    op.drop_table("capture_shortcuts")
    op.drop_constraint("uq_items_capture_uuid", "items")
    op.drop_column("items", "embedding_model")
    op.drop_column("items", "capture_uuid")
    op.drop_column("items", "corrected_at")
    op.drop_column("items", "corrected_category")
