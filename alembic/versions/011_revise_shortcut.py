"""seed capture_shortcuts with /revise alias → revision_agent

Revision ID: 011_revise_shortcut
Revises: 010_job_queue_outbox
Create Date: 2026-09-05
"""

revision = "011_revise_shortcut"
down_revision = "010_job_queue_outbox"
branch_labels = None
depends_on = None

from alembic import op


def upgrade() -> None:
    op.execute("""
        INSERT INTO capture_shortcuts (alias, agent)
        VALUES ('revise', 'revision_agent')
        ON CONFLICT (alias) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM capture_shortcuts WHERE alias = 'revise'")
