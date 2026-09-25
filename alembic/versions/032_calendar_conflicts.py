"""calendar_conflicts: pending overlap questions for dated events

One row per unresolved conflict between a newly-captured event and an
existing one.  Resolution answers are KEEP / MOVE / CANCEL, handled by
personal_agent.

uq_cc_open ensures we ask at most once per new item while the question
is still pending — a second capture that also overlaps doesn't generate
a second message.
"""
from alembic import op

revision      = "032_calendar_conflicts"
down_revision = "031_reminders"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE calendar_conflicts (
            id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            new_item_id       UUID        NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            existing_item_id  UUID        NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            status            TEXT        NOT NULL DEFAULT 'pending',
            asked_at          TIMESTAMPTZ,
            resolved_at       TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_cc_status
                CHECK (status IN ('pending', 'kept', 'moved', 'cancelled'))
        )
    """)

    # Exactly one open question per new item
    op.execute("""
        CREATE UNIQUE INDEX uq_cc_open
            ON calendar_conflicts (new_item_id)
            WHERE status = 'pending'
    """)

    op.execute("""
        CREATE INDEX ix_cc_pending
            ON calendar_conflicts (created_at)
            WHERE status = 'pending'
    """)

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE calendar_conflicts TO fastapi_app"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS calendar_conflicts")
