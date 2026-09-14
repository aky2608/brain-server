"""Add attempt column to step_executions; rebuild unique constraint to include attempt"""
from alembic import op
import sqlalchemy as sa

revision = "023_step_attempts"
down_revision = "022_approval_statuses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("step_executions",
                  sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")))
    op.drop_constraint("uq_step_executions_approval_step", "step_executions")
    op.create_unique_constraint(
        "uq_step_executions_approval_attempt_step",
        "step_executions",
        ["approval_id", "attempt", "step_no"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_step_executions_approval_attempt_step", "step_executions")
    op.create_unique_constraint(
        "uq_step_executions_approval_step",
        "step_executions",
        ["approval_id", "step_no"],
    )
    op.drop_column("step_executions", "attempt")
