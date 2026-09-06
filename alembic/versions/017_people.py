"""people entity resolution: people, people_mentions, people_conflicts

Three tables + pg_trgm for fuzzy name matching.

PRE-FLIGHT (run once as postgres before alembic upgrade):
    docker exec supabase-db psql -U postgres -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
Alembic runs as fastapi_app which cannot CREATE EXTENSION. The migration
will fail on the GIN index if pg_trgm is absent.

Revision ID: 017_people
Revises: 016_infra_costs
Create Date: 2026-09-06
"""

revision = "017_people"
down_revision = "016_infra_costs"
branch_labels = None
depends_on = None

from alembic import op


def upgrade() -> None:
    # ── people ────────────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE people (
            id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            name              TEXT        NOT NULL,
            name_normalized   TEXT        NOT NULL,
            relationship_hint TEXT,
            status            TEXT        NOT NULL DEFAULT 'provisional',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_people_status CHECK (status IN ('provisional', 'confirmed'))
        )
    """)

    # Requires pg_trgm — run the pre-flight CREATE EXTENSION first.
    op.execute("""
        CREATE INDEX ix_people_trgm
            ON people USING GIN (name_normalized gin_trgm_ops)
    """)

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE people TO fastapi_app")

    # ── people_mentions ───────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE people_mentions (
            id           BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            item_id      UUID        NOT NULL REFERENCES items(id)   ON DELETE CASCADE,
            person_id    UUID                 REFERENCES people(id)  ON DELETE SET NULL,
            matched_text TEXT        NOT NULL,
            match_type   TEXT        NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_pm_match_type CHECK (
                match_type IN ('exact', 'fuzzy', 'pending_conflict', 'manual')
            )
        )
    """)

    op.execute("CREATE INDEX ix_pm_item   ON people_mentions (item_id)")
    op.execute("CREATE INDEX ix_pm_person ON people_mentions (person_id) WHERE person_id IS NOT NULL")

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE people_mentions TO fastapi_app")

    # ── people_conflicts ──────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE people_conflicts (
            id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            candidate_person_id  UUID        NOT NULL REFERENCES people(id)  ON DELETE CASCADE,
            mention_text         TEXT        NOT NULL,
            item_id              UUID        NOT NULL REFERENCES items(id)   ON DELETE CASCADE,
            similarity_score     REAL        NOT NULL,
            status               TEXT        NOT NULL DEFAULT 'pending',
            asked_at             TIMESTAMPTZ,
            resolved_at          TIMESTAMPTZ,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_pc_status CHECK (status IN ('pending', 'merged', 'rejected', 'snoozed'))
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX uq_pc_open
            ON people_conflicts (candidate_person_id, lower(mention_text))
            WHERE status = 'pending'
    """)

    op.execute("""
        CREATE INDEX ix_pc_pending
            ON people_conflicts (created_at)
            WHERE status = 'pending'
    """)

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE people_conflicts TO fastapi_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS people_conflicts")
    op.execute("DROP TABLE IF EXISTS people_mentions")
    op.execute("DROP TABLE IF EXISTS people")
    # pg_trgm extension intentionally NOT dropped — may be used elsewhere
