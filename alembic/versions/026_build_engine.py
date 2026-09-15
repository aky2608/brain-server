"""Add engine column to pending_approvals (claude|aider, default claude)"""
from alembic import op
import sqlalchemy as sa

revision = "026_build_engine"
down_revision = "025_build_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pending_approvals",
        sa.Column("engine", sa.Text(), nullable=False, server_default=sa.text("'claude'")),
    )
    op.create_check_constraint(
        "ck_pending_approvals_engine",
        "pending_approvals",
        "engine IN ('claude', 'aider')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_pending_approvals_engine", "pending_approvals", type_="check")
    op.drop_column("pending_approvals", "engine")
