"""Add dismissed_at to agent_decisions; partial index for open always-tier interrupts"""
from alembic import op
import sqlalchemy as sa

revision = "027_interrupt_dismissal"
down_revision = "026_build_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_decisions",
        sa.Column("dismissed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.execute(
        """
        CREATE INDEX ix_agent_decisions_open_always
        ON agent_decisions (interrupt_tier, created_at)
        WHERE dismissed_at IS NULL AND interrupt_tier = 'always'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_decisions_open_always")
    op.drop_column("agent_decisions", "dismissed_at")
