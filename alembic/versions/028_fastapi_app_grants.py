"""Grant SELECT, INSERT, UPDATE on core tables + sequences to fastapi_app

Tables deliberately excluded:
  alembic_version       — migrations run as supabase_admin
  checkpoint_migrations — LangGraph-owned
  policies, builds, contacts — v2 leftovers, no live references in application code
"""
from alembic import op

revision = "028_fastapi_app_grants"
down_revision = "027_interrupt_dismissal"
branch_labels = None
depends_on = None

_TABLES = [
    "agent_decisions",   # had ar only; needs UPDATE for dismiss
    "items",             # core capture table; needs INSERT
    "agent_watch_rules", # watch agent updates last_notified_at, missed_count
    "capture_shortcuts", # had ar only; needs INSERT/UPDATE
]


def upgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON {table} TO fastapi_app"
        )
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fastapi_app"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM fastapi_app"
    )
    for table in reversed(_TABLES):
        op.execute(
            f"REVOKE SELECT, INSERT, UPDATE ON {table} FROM fastapi_app"
        )
