"""Add reminder columns to items

reminder_offset_minutes — per-item override; null means use the default (60 min).
reminded_at             — set when the reminder fires; null means not yet sent.
Partial index for the 15-minute poller: only rows that are active, have a
starts_at, and haven't been reminded yet.
"""
from alembic import op
import sqlalchemy as sa

revision      = "031_reminders"
down_revision = "030_time_inferred"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column("items", sa.Column("reminder_offset_minutes", sa.Integer(), nullable=True))
    op.add_column("items", sa.Column("reminded_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.execute(
        """CREATE INDEX items_reminder_due_idx ON items (starts_at)
           WHERE status = 'active'
             AND starts_at IS NOT NULL
             AND reminded_at IS NULL"""
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS items_reminder_due_idx")
    op.drop_column("items", "reminded_at")
    op.drop_column("items", "reminder_offset_minutes")
