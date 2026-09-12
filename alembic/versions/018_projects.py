"""projects and project_activity tables"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "018_projects"
down_revision = "017_people"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("notebook_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_projects_status"),
        sa.UniqueConstraint("alias", name="uq_projects_alias"),
        sa.ForeignKeyConstraint(["notebook_id"], ["notebooks.id"],
                                name="fk_projects_notebook_id"),
    )

    op.create_table(
        "project_activity",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        # 'decision' omitted — tags unreliable in capture write-block; add via ALTER TABLE when a real write site exists.
        sa.CheckConstraint(
            "event_type IN ('capture', 'task_done', 'project_created', 'project_archived')",
            name="ck_project_activity_event_type",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"],
                                name="fk_project_activity_project_id"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"],
                                name="fk_project_activity_item_id"),
    )
    op.create_index("ix_project_activity_project_created",
                    "project_activity", ["project_id", "created_at"],
                    postgresql_ops={"created_at": "DESC"})

    # notebook_id left NULL — auto-create-notebook is a CREATE endpoint concern, not a seed concern.
    op.execute("""
        INSERT INTO projects (name, alias) VALUES
            ('WayClear', 'wayclear'),
            ('AccredIQ', 'accrediq')
        ON CONFLICT ON CONSTRAINT uq_projects_alias DO NOTHING
    """)


def downgrade() -> None:
    op.drop_index("ix_project_activity_project_created")
    op.drop_table("project_activity")
    op.drop_table("projects")
