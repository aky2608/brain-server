"""pending_approvals and step_executions for Coding Agent"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "020_coding_agent"
down_revision = "019_project_repos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("diff_text", sa.Text(), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'killed', 'expired')",
            name="ck_pending_approvals_status",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"],
                                name="fk_pending_approvals_project_id"),
    )
    op.create_index("ix_pending_approvals_status_created",
                    "pending_approvals", ["status", "created_at"])

    op.create_table(
        "step_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stdout_ref", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("approval_id", "step_no",
                            name="uq_step_executions_approval_step"),
        sa.ForeignKeyConstraint(["approval_id"], ["pending_approvals.id"],
                                name="fk_step_executions_approval_id"),
    )
    op.create_index("ix_step_executions_approval_id",
                    "step_executions", ["approval_id"])

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON pending_approvals, step_executions TO fastapi_app;"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON pending_approvals, step_executions FROM fastapi_app;"
    )
    op.drop_index("ix_step_executions_approval_id")
    op.drop_table("step_executions")
    op.drop_index("ix_pending_approvals_status_created")
    op.drop_table("pending_approvals")
