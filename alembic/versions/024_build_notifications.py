"""Add reply_markup JSONB column to outbox for inline keyboard support"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "024_build_notifications"
down_revision = "023_step_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("reply_markup", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox", "reply_markup")
