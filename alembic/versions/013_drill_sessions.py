"""drill_sessions + drill_session_answers: explicit timed drill flow

drill_sessions        — one row per drill attempt; timing, verified flag, reason.
drill_session_answers — one row per question slot; raw answer + per-question score.
elapsed_seconds is a generated column (ended_at - started_at), always consistent.

Also:
  - Seeds /drill capture_shortcut → revision_agent.
  - Adds min_required key to gate_missed watch rule condition (default 3).

Revision ID: 013_drill_sessions
Revises: 012_items_rls_fastapi_app
Create Date: 2026-09-05
"""

revision = "013_drill_sessions"
down_revision = "012_items_rls_fastapi_app"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table(
        "drill_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("notebook_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("questions_total", sa.Integer(), nullable=False),
        sa.Column(
            "questions_answered",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "elapsed_seconds",
            sa.Integer(),
            sa.Computed(
                "EXTRACT(EPOCH FROM ended_at - started_at)::INT",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "flag_fast",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("score_avg", sa.Numeric(4, 2), nullable=True),
        sa.Column(
            "verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["notebook_id"], ["notebooks.id"]),
    )

    # Single-user: at most one open session at a time.
    op.create_index(
        "uix_drill_sessions_open",
        "drill_sessions",
        ["id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_index("ix_drill_sessions_notebook", "drill_sessions", ["notebook_id"])

    op.create_table(
        "drill_session_answers",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("question_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("user_answer", sa.Text(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("answered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["drill_sessions.id"]),
        sa.ForeignKeyConstraint(["question_id"], ["revision_questions.id"]),
        sa.UniqueConstraint("session_id", "position", name="uq_dsa_session_position"),
        sa.CheckConstraint("score BETWEEN 0 AND 10", name="ck_dsa_score"),
    )

    op.create_index("ix_dsa_session_id", "drill_session_answers", ["session_id"])

    # /drill shortcut — routes to revision_agent, which handles the full
    # drill flow inside handle() alongside /revise commands.
    op.execute("""
        INSERT INTO capture_shortcuts (alias, agent)
        VALUES ('drill', 'revision_agent')
        ON CONFLICT (alias) DO NOTHING
    """)

    # Add min_required to the gate_missed watch rule condition.
    # Default 3: below this, a drill submission is rejected without grading.
    op.execute("""
        UPDATE agent_watch_rules
           SET condition = condition || '{"min_required": 3}'::jsonb
         WHERE rule_type = 'gate_missed'
           AND NOT (condition ? 'min_required')
    """)


def downgrade() -> None:
    op.execute("DELETE FROM capture_shortcuts WHERE alias = 'drill'")
    op.execute("""
        UPDATE agent_watch_rules
           SET condition = condition - 'min_required'
         WHERE rule_type = 'gate_missed'
    """)
    op.drop_index("ix_dsa_session_id", table_name="drill_session_answers")
    op.drop_table("drill_session_answers")
    op.drop_index("ix_drill_sessions_notebook", table_name="drill_sessions")
    op.drop_index("uix_drill_sessions_open", table_name="drill_sessions")
    op.drop_table("drill_sessions")
