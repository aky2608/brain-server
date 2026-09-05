"""grant fastapi_app read/write access to items via RLS policy

RLS is enabled on items but no policy exists for fastapi_app, so all
SELECT/UPDATE/DELETE from agents (psycopg with BRAIN_DB_URL) return 0 rows.
This silently breaks notebook_agent UPDATEs and revision_agent item reads.
main.py uses the Supabase service_role client (bypasses RLS) for its own
item writes, but agents use psycopg directly and need an explicit policy.

Single-user system: all items belong to Ashish; USING (true) is correct.

Revision ID: 012_items_rls_fastapi_app
Revises: 011_revise_shortcut
Create Date: 2026-09-05
"""

revision = "012_items_rls_fastapi_app"
down_revision = "011_revise_shortcut"
branch_labels = None
depends_on = None

from alembic import op


def upgrade() -> None:
    op.execute("""
        CREATE POLICY fastapi_app_all ON items
            FOR ALL
            TO fastapi_app
            USING (true)
            WITH CHECK (true)
    """)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS fastapi_app_all ON items")
