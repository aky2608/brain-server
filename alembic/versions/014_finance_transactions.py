"""transactions + recurrence_groups: finance ledger schema

Two tables. recurrence_groups created first because transactions FKs into it.
Both FKs use ON DELETE SET NULL so the ledger survives item or group cleanup.
Index on (merchant, transaction_date) supports the recurrence interval query.

Grants mirror migration 012 — fastapi_app needs direct psycopg access since
agent DB calls bypass the Supabase service_role client.

Revision ID: 014_finance_transactions
Revises: 013_drill_sessions
Create Date: 2026-09-06
"""

revision = "014_finance_transactions"
down_revision = "013_drill_sessions"
branch_labels = None
depends_on = None

from alembic import op


def upgrade() -> None:
    op.execute("""
        CREATE TABLE recurrence_groups (
            id                      BIGSERIAL PRIMARY KEY,
            merchant                TEXT NOT NULL,
            expected_amount         NUMERIC(10,2),
            expected_interval_days  INT,
            last_seen_date          DATE,
            next_expected_date      DATE,
            status                  TEXT NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active','missed','cancelled')),
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE transactions (
            id                      BIGSERIAL PRIMARY KEY,
            item_id                 UUID REFERENCES items(id) ON DELETE SET NULL,
            amount                  NUMERIC(10,2) NOT NULL,
            direction               TEXT NOT NULL CHECK (direction IN ('debit','credit')),
            merchant                TEXT,
            category                TEXT,
            transaction_date        DATE NOT NULL,
            recurrence_group_id     BIGINT REFERENCES recurrence_groups(id) ON DELETE SET NULL,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE INDEX ix_transactions_merchant_date
            ON transactions (lower(merchant), transaction_date)
    """)

    op.execute("""
        CREATE INDEX ix_transactions_recurrence_group_id
            ON transactions (recurrence_group_id)
        WHERE recurrence_group_id IS NOT NULL
    """)

    # fastapi_app needs direct INSERT/UPDATE via psycopg (agents bypass service_role)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE recurrence_groups TO fastapi_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE transactions TO fastapi_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE recurrence_groups_id_seq TO fastapi_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE transactions_id_seq TO fastapi_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS transactions")
    op.execute("DROP TABLE IF EXISTS recurrence_groups")
