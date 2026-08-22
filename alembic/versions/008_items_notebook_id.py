"""items.notebook_id: real FK column + index for notebook-scoped queries

Revision ID: 008_items_notebook_id
Revises: 007_notebooks
Create Date: 2026-08-22
"""

revision = "008_items_notebook_id"
down_revision = "007_notebooks"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    # Real FK column — not JSONB metadata — so the Revision engine can do
    # indexed "all items in notebook X" queries without full-table scans.
    op.add_column("items", sa.Column("notebook_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_items_notebook_id",
        "items",
        "notebooks",
        ["notebook_id"],
        ["id"],
    )
    op.create_index("ix_items_notebook_id", "items", ["notebook_id"])


def downgrade() -> None:
    op.drop_index("ix_items_notebook_id", table_name="items")
    op.drop_constraint("fk_items_notebook_id", "items", type_="foreignkey")
    op.drop_column("items", "notebook_id")
