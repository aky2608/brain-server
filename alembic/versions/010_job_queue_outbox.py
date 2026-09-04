"""job_queue + outbox: durable background work and delivery

job_queue — replaces in-memory FastAPI BackgroundTasks and the two lifespan
    loops (batch_classification_loop, watch_eval_loop). One row = one unit of
    work. Worker claims with UPDATE...RETURNING + immediate commit, then
    executes outside that transaction so the lock is never held open for the
    job's duration. locked_by/locked_at support stale-lock recovery if the
    worker dies mid-job.

outbox — separate from job_queue deliberately. job_queue retries mean "redo
    the work"; outbox retries mean "resend the message". Conflating the two
    risks duplicate side-effects (e.g. duplicate revision_reviews rows) when a
    Telegram delivery failure triggers a full job re-run. The job writes to its
    target table AND outbox in one transaction; a small delivery loop retries
    outbox rows independently, never touching the underlying work.

Revision ID: 010_job_queue_outbox
Revises: 009_revision
Create Date: 2026-09-04
"""

revision = "010_job_queue_outbox"
down_revision = "009_revision"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table(
        "job_queue",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("5"), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.SmallInteger(), server_default=sa.text("3"), nullable=False),
        sa.Column(
            "scheduled_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed','dead')",
            name="ck_jq_status",
        ),
    )

    # Worker poll: cheapest "give me next job" — filtered to queued only
    op.create_index(
        "ix_jq_poll",
        "job_queue",
        ["priority", "scheduled_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )

    # Stale-lock recovery: find rows where worker died mid-job
    op.create_index(
        "ix_jq_stale",
        "job_queue",
        ["locked_at"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "outbox",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed')",
            name="ck_ob_status",
        ),
    )

    op.create_index(
        "ix_ob_pending",
        "outbox",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_ob_pending", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_jq_stale", table_name="job_queue")
    op.drop_index("ix_jq_poll", table_name="job_queue")
    op.drop_table("job_queue")
