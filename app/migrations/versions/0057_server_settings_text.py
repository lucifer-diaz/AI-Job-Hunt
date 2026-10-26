"""server_settings.value holds text

Settings → AI agents & automation lets an admin choose the agent graph modes, the application worker's mode and the
browser-use options (app.services.server_settings.CONFIG): words and switches, not only whole numbers. The column
becomes text; the worker limits already saved keep their numbers as text.

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-27 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0057'
down_revision: str | None = '0056'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The settings this revision may store as words (app.services.server_settings.CONFIG); a downgrade removes them.
WORDED = ("agent_graphs_enabled", "qualification_graph_mode", "preparation_graph_mode", "job_insights_graph_mode",
          "application_execution_mode", "browser_use_webui_enabled", "browser_use_headless",
          "application_auth_sessions_enabled", "application_login_domains", "agent_langsmith_export_enabled",
          "agent_langsmith_project", "agent_langsmith_endpoint")


def upgrade() -> None:
    with op.batch_alter_table('server_settings') as batch:
        batch.alter_column('value', type_=sa.String(500), existing_type=sa.Integer(), existing_nullable=False,
                           postgresql_using='value::text')


def downgrade() -> None:
    names = ", ".join(f"'{n}'" for n in WORDED)
    op.execute(sa.text(f"DELETE FROM server_settings WHERE name IN ({names})"))
    with op.batch_alter_table('server_settings') as batch:
        batch.alter_column('value', type_=sa.Integer(), existing_type=sa.String(500), existing_nullable=False,
                           postgresql_using='value::integer')
