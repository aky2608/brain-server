"""scheduling: rollover_note column + /plan shortcut seed

Revision ID: 004_scheduling
Revises: 003_seed_shortcuts
Create Date: 2026-08-21
"""

revision = "004_scheduling"
down_revision = "003_seed_shortcuts"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    # Visible rollover field — surfaced directly to app, not buried in metadata
    op.add_column("items", sa.Column("rollover_note", sa.Text(), nullable=True))

    # /plan shortcut → routes to scheduling_agent via DISPATCH_MAP
    op.execute("""
        INSERT INTO capture_shortcuts (alias, category, subcategory, agent)
        VALUES ('plan', NULL, NULL, 'scheduling_agent')
        ON CONFLICT (alias) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM capture_shortcuts WHERE alias = 'plan'")
    op.drop_column("items", "rollover_note")
