"""seed /build shortcut and brainapp project row"""

revision = "021_seed_build_shortcut"
down_revision = "020_coding_agent"
branch_labels = None
depends_on = None

from alembic import op


def upgrade() -> None:
    op.execute("""
        INSERT INTO capture_shortcuts (alias, agent)
        VALUES ('build', 'project_agent')
        ON CONFLICT (alias) DO NOTHING
    """)

    # build_enabled intentionally omitted — defaults to false.
    # Enable deliberately after each rebuild:
    #   UPDATE projects SET build_enabled = true WHERE alias = 'brainapp';
    op.execute("""
        INSERT INTO projects (name, alias, repo_url, local_path)
        VALUES (
            'Brain App',
            'brainapp',
            'git@github.com:aky2608/brain-app.git',
            '/opt/agent-workspaces/brain-app'
        )
        ON CONFLICT (alias) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM capture_shortcuts WHERE alias = 'build'")
    op.execute("DELETE FROM projects WHERE alias = 'brainapp'")
