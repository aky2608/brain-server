"""Widen pending_approvals status constraint; add lock columns for builder worker"""
from alembic import op
import sqlalchemy as sa

revision = "022_approval_statuses"
down_revision = "021_seed_build_shortcut"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_pending_approvals_status", "pending_approvals")
    op.create_check_constraint(
        "ck_pending_approvals_status",
        "pending_approvals",
        "status IN ('pending','approved','killed','expired','running','awaiting_review','failed')",
    )
    op.add_column("pending_approvals", sa.Column("locked_by", sa.Text(), nullable=True))
    op.add_column("pending_approvals", sa.Column("locked_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pending_approvals", "locked_at")
    op.drop_column("pending_approvals", "locked_by")
    op.drop_constraint("ck_pending_approvals_status", "pending_approvals")
    op.create_check_constraint(
        "ck_pending_approvals_status",
        "pending_approvals",
        "status IN ('pending','approved','killed','expired')",
    )
