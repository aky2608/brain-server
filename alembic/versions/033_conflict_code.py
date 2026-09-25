"""Add short code column to calendar_conflicts

Stores a stable 3-char code (non-confusable alphabet, no 0/O/1/l/I)
derived at insert time, so the reply parser can match it without
deriving from the UUID.  Unique among open conflicts.
"""
from alembic import op
import sqlalchemy as sa

revision      = "033_conflict_code"
down_revision = "032_calendar_conflicts"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column(
        "calendar_conflicts",
        sa.Column("code", sa.Text(), nullable=True),
    )
    op.execute("""
        CREATE UNIQUE INDEX uq_cc_code
            ON calendar_conflicts (code)
            WHERE status = 'pending' AND code IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_cc_code")
    op.drop_column("calendar_conflicts", "code")
