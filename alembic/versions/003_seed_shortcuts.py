"""seed capture_shortcuts with base aliases (/echo, /task, /note)

Revision ID: 003_seed_shortcuts
Revises: 002_agent_decisions
Create Date: 2026-08-21
"""

revision = "003_seed_shortcuts"
down_revision = "002_agent_decisions"
branch_labels = None
depends_on = None

from alembic import op


def upgrade() -> None:
    # ON CONFLICT DO NOTHING — safe to re-run, won't clobber user edits
    op.execute("""
        INSERT INTO capture_shortcuts (alias, category, agent) VALUES
            ('echo',  NULL,   'echo'),
            ('task',  'task', 'echo'),
            ('note',  'note', 'echo')
        ON CONFLICT (alias) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM capture_shortcuts WHERE alias IN ('echo', 'task', 'note')")
