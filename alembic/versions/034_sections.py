"""sections: notebook sub-groupings for chapters, topics, project areas

items.section_id and revision_questions.section_id are nullable FKs with
ON DELETE SET NULL — if a section is hard-deleted, items become unsectioned
rather than breaking the FK.  Archiving is the intended path; hard delete
is handled safely regardless.

Revision ID: 034_sections
Revises: 033_conflict_code
Create Date: 2026-09-27
"""

revision      = "034_sections"
down_revision = "033_conflict_code"
branch_labels = None
depends_on    = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table(
        "sections",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("notebook_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["notebook_id"], ["notebooks.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("notebook_id", "name", name="uq_sections_notebook_name"),
    )
    op.create_index("ix_sections_notebook_id", "sections", ["notebook_id"])

    op.add_column("items", sa.Column("section_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_items_section_id", "items", "sections",
        ["section_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_items_section_id", "items", ["section_id"])

    op.add_column("revision_questions", sa.Column("section_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_rq_section_id", "revision_questions", "sections",
        ["section_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_rq_section_id", "revision_questions", ["section_id"])


def downgrade() -> None:
    op.drop_index("ix_rq_section_id", table_name="revision_questions")
    op.drop_constraint("fk_rq_section_id", "revision_questions", type_="foreignkey")
    op.drop_column("revision_questions", "section_id")

    op.drop_index("ix_items_section_id", table_name="items")
    op.drop_constraint("fk_items_section_id", "items", type_="foreignkey")
    op.drop_column("items", "section_id")

    op.drop_index("ix_sections_notebook_id", table_name="sections")
    op.drop_table("sections")
