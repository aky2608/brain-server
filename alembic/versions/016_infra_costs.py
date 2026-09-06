"""infra_costs: static monthly infrastructure cost config

Amounts stored in INR. USD source figures and conversion rate (94.49 on
2026-09-06) recorded in each row's note column for auditability.
Updates go via direct DB UPDATE or a future PATCH endpoint — no redeploy
needed. UNIQUE on item supports upsert-style edits.

Revision ID: 016_infra_costs
Revises: 015_seed_missed_charge_rule
Create Date: 2026-09-06
"""

revision = "016_infra_costs"
down_revision = "015_seed_missed_charge_rule"
branch_labels = None
depends_on = None

from alembic import op


def upgrade() -> None:
    op.execute("""
        CREATE TABLE infra_costs (
            id          BIGSERIAL PRIMARY KEY,
            item        TEXT NOT NULL UNIQUE,
            amount_inr  NUMERIC(8,2) NOT NULL DEFAULT 0,
            note        TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        INSERT INTO infra_costs (item, amount_inr, note) VALUES
            ('Brain VPS',                 1150.00, 'Separate from business VPS to avoid resource contention. ~$12/mo at 94.49 INR/USD (2026-09-06)'),
            ('Old VPS (Veridh/Dikam)',    1150.00, 'Already paying. ~$12/mo at 94.49 INR/USD (2026-09-06)'),
            ('Claude Pro (dev)',          1900.00, 'Dev sessions only. ~$20/mo at 94.49 INR/USD (2026-09-06)'),
            ('1min.ai',                      0.00, 'Lifetime deal; gpt-4o-mini confirmed working for UNIFY_CHAT_WITH_AI'),
            ('Claude API (feature tier)',    95.00, 'Midpoint of $0-2/mo at 94.49 INR/USD (2026-09-06)'),
            ('News Agent APIs',             400.00, 'Midpoint of $3-5/1k queries (Brave/Tavily); on-demand only. At 94.49 INR/USD (2026-09-06)')
    """)

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE infra_costs TO fastapi_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE infra_costs_id_seq TO fastapi_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS infra_costs")
