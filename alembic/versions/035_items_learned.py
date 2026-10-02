"""items.learned: boolean flag for #learned / TIL: captures

Set at capture time by regex — no LLM call. Enables pinned TIL lists
per section and feeds the weekly section summary prompt.

Revision ID: 035_items_learned
Revises: 034_sections
Create Date: 2026-10-01
"""

revision      = "035_items_learned"
down_revision = "034_sections"
branch_labels = None
depends_on    = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column(
            "learned",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_items_learned",
        "items",
        ["learned"],
        postgresql_where=sa.text("learned = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_items_learned", table_name="items")
    op.drop_column("items", "learned")
