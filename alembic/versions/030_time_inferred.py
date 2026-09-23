"""Add time_inferred to items — true when starts_at was synthesised from a date-only parse"""
from alembic import op
import sqlalchemy as sa

revision      = "030_time_inferred"
down_revision = "029_calendar_fields"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("time_inferred", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("items", "time_inferred")
