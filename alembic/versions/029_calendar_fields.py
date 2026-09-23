"""Add calendar event fields to items

is_event defaults false. starts_at/ends_at nullable.
task_deadline is unchanged — due-by time, not an occurrence.
"""
from alembic import op
import sqlalchemy as sa

revision = "029_calendar_fields"
down_revision = "028_fastapi_app_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("starts_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("items", sa.Column("ends_at",   sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("items", sa.Column("is_event",  sa.Boolean(), server_default="false", nullable=False))
    op.execute(
        "CREATE INDEX items_starts_at_idx ON items (starts_at) WHERE starts_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS items_starts_at_idx")
    op.drop_column("items", "is_event")
    op.drop_column("items", "ends_at")
    op.drop_column("items", "starts_at")
