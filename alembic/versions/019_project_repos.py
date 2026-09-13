"""add repo columns to projects"""
from alembic import op
import sqlalchemy as sa

revision = "019_project_repos"
down_revision = "018_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("repo_url", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("default_branch", sa.Text(), nullable=False,
                                        server_default=sa.text("'main'")))
    op.add_column("projects", sa.Column("local_path", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("build_enabled", sa.Boolean(), nullable=False,
                                        server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("projects", "build_enabled")
    op.drop_column("projects", "local_path")
    op.drop_column("projects", "default_branch")
    op.drop_column("projects", "repo_url")
