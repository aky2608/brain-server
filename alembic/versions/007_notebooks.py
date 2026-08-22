"""notebooks table, capture_shortcuts FK activation, GATE subject seed

Revision ID: 007_notebooks
Revises: 006_thought_links
Create Date: 2026-08-22
"""

revision = "007_notebooks"
down_revision = "006_thought_links"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.create_table(
        "notebooks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("notebook_type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "notebook_type IN ('gate_subject', 'general', 'project')",
            name="ck_notebooks_type",
        ),
        sa.UniqueConstraint("name", "notebook_type", name="uq_notebooks_name_type"),
    )

    # Activates the inert bigint column added in 001_schema_gaps (Weekend 6).
    # Safe: all existing capture_shortcuts rows have notebook_id IS NULL,
    # confirmed by pre-migration audit (2026-08-22).
    op.create_foreign_key(
        "fk_capture_shortcuts_notebook_id",
        "capture_shortcuts",
        "notebooks",
        ["notebook_id"],
        ["id"],
    )

    # GATE subject notebooks — four subjects in scope as of 2026-08-22.
    # ON CONFLICT DO NOTHING: safe to re-run, idempotent.
    op.execute("""
        INSERT INTO notebooks (name, notebook_type) VALUES
            ('Operating Systems', 'gate_subject'),
            ('DBMS',              'gate_subject'),
            ('Computer Networks', 'gate_subject'),
            ('Algorithms',        'gate_subject')
        ON CONFLICT ON CONSTRAINT uq_notebooks_name_type DO NOTHING
    """)

    # /gate shortcut — notebook_agent not yet wired into DISPATCH_MAP.
    # Until it is, /gate captures fall through to echo and log
    # route_slash_unknown:gate in agent_decisions. Captures are not lost.
    # Wire notebook_agent in graph.py + personal.py in the same PR that
    # builds the agent.
    op.execute("""
        INSERT INTO capture_shortcuts (alias, category, subcategory, agent)
        VALUES ('gate', 'learning', 'gate', 'notebook_agent')
        ON CONFLICT (alias) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM capture_shortcuts WHERE alias = 'gate'")
    op.drop_constraint(
        "fk_capture_shortcuts_notebook_id", "capture_shortcuts", type_="foreignkey"
    )
    op.drop_table("notebooks")
