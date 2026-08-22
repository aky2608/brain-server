"""revision_questions + revision_reviews: spaced repetition schema

revision_questions — one row per generated question; holds current SR state
    (next_review_date, interval_days) for fast due-today queries.
revision_reviews — one row per review event; full history preserved for
    retention analytics and per-topic improvement tracking.

review_count is intentionally NOT denormalized onto revision_questions —
derive it via COUNT(*) on revision_reviews.

Revision ID: 009_revision
Revises: 008_items_notebook_id
Create Date: 2026-08-22
"""

revision = "009_revision"
down_revision = "008_items_notebook_id"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    op.create_table(
        "revision_questions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("notebook_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_item_ids",
            postgresql.ARRAY(postgresql.UUID()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expected_answer", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("next_review_date", sa.Date(), nullable=True),
        sa.Column(
            "interval_days",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["notebook_id"], ["notebooks.id"]),
        sa.CheckConstraint("interval_days IN (1, 3, 7, 21)", name="ck_rq_interval"),
    )

    op.create_index("ix_rq_notebook", "revision_questions", ["notebook_id"])
    op.create_index(
        "ix_rq_due",
        "revision_questions",
        ["next_review_date"],
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_index(
        "ix_rq_notebook_due",
        "revision_questions",
        ["notebook_id", "next_review_date"],
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    op.create_table(
        "revision_reviews",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("question_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("interval_days_after", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["revision_questions.id"]),
        sa.CheckConstraint("score BETWEEN 0 AND 10", name="ck_rr_score"),
        sa.CheckConstraint(
            "interval_days_after IN (1, 3, 7, 21)",
            name="ck_rr_interval_after",
        ),
    )

    op.create_index("ix_rr_question_id", "revision_reviews", ["question_id"])
    op.create_index("ix_rr_reviewed_at", "revision_reviews", ["reviewed_at"])


def downgrade() -> None:
    op.drop_index("ix_rr_reviewed_at", table_name="revision_reviews")
    op.drop_index("ix_rr_question_id", table_name="revision_reviews")
    op.drop_table("revision_reviews")
    op.drop_index("ix_rq_notebook_due", table_name="revision_questions")
    op.drop_index("ix_rq_due", table_name="revision_questions")
    op.drop_index("ix_rq_notebook", table_name="revision_questions")
    op.drop_table("revision_questions")
