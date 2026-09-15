"""Partial unique index on pending_approvals to prevent duplicate in-flight builds"""
from alembic import op

revision = "025_build_idempotency"
down_revision = "024_build_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE UNIQUE INDEX uq_pending_approvals_active_task
        ON pending_approvals (project_id, md5(spec->>'task'))
        WHERE status IN ('pending', 'running', 'awaiting_review')
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_pending_approvals_active_task")
