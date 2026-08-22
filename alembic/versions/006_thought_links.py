"""thought_links: title column, link join table, drop linked_items

Revision ID: 006_thought_links
Revises: 005_watch_rules
Create Date: 2026-08-22
"""

revision = "006_thought_links"
down_revision = "005_watch_rules"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    # User-controlled title for wikilink resolution.
    # NULL for most captures (SMS, voice, etc.) — only rows with a title
    # are candidates for [[wikilink]] matching.
    op.add_column("items", sa.Column("title", sa.Text(), nullable=True))

    # Resolved-links-only join table. Both FKs are NOT NULL by design:
    # unresolved [[wikilinks]] stay implicit in raw_content and are scanned
    # on demand — we don't persist pending rows here.
    # wikilink_text carries the raw [[bracket text]] that produced the link,
    # so the app can display "linked via [[X]]" without re-parsing raw_content.
    op.create_table(
        "thought_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source_item_id", postgresql.UUID(), nullable=False),
        sa.Column("target_item_id", postgresql.UUID(), nullable=False),
        sa.Column("link_type", sa.Text(), nullable=False),
        sa.Column("wikilink_text", sa.Text(), nullable=True),
        sa.Column("similarity_score", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_item_id"], ["items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_item_id"], ["items.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "source_item_id", "target_item_id", "link_type",
            name="uq_thought_links_source_target_type",
        ),
    )

    op.create_index("ix_thought_links_source", "thought_links", ["source_item_id"])
    op.create_index("ix_thought_links_target", "thought_links", ["target_item_id"])

    # Drop the unused JSONB column — zero data, never referenced in code
    op.drop_column("items", "linked_items")


def downgrade() -> None:
    op.add_column(
        "items",
        sa.Column(
            "linked_items",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=True,
        ),
    )
    op.drop_index("ix_thought_links_target", table_name="thought_links")
    op.drop_index("ix_thought_links_source", table_name="thought_links")
    op.drop_table("thought_links")
    op.drop_column("items", "title")
