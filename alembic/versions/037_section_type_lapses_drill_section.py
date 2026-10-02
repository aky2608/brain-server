"""sections.type, revision_questions.lapses, drill_sessions.section_id

sections.type distinguishes note-taking sections from drill-only sections.
revision_questions.lapses counts failures after an item had climbed at least
one rung — the primary weakness signal (SuperMemo leech criterion).
drill_sessions.section_id allows section-scoped drill sessions.

Revision ID: 037_section_type_lapses_drill_section
Revises: 036_section_summaries
Create Date: 2026-10-02
"""

revision      = "037_section_lapses"
down_revision = "036_section_summaries"
branch_labels = None
depends_on    = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.add_column(
        "sections",
        sa.Column(
            "type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'notes'"),
        ),
    )
    op.create_check_constraint(
        "ck_sections_type",
        "sections",
        "type IN ('notes', 'drill')",
    )

    op.add_column(
        "revision_questions",
        sa.Column(
            "lapses",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    op.add_column(
        "drill_sessions",
        sa.Column("section_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_drill_sessions_section_id",
        "drill_sessions", "sections",
        ["section_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_drill_sessions_section_id", "drill_sessions", ["section_id"])


def downgrade() -> None:
    op.drop_index("ix_drill_sessions_section_id", table_name="drill_sessions")
    op.drop_constraint("fk_drill_sessions_section_id", "drill_sessions", type_="foreignkey")
    op.drop_column("drill_sessions", "section_id")
    op.drop_column("revision_questions", "lapses")
    op.drop_constraint("ck_sections_type", "sections", type_="check")
    op.drop_column("sections", "type")
