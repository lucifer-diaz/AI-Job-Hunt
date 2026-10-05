"""Running workers: a task can be asked to run now

Adds run_now_at to the four worker task tables. A person sets it from Running workers; the next claim takes that task
first, ignores its retry wait and the account's ranking cadence, and makes its calls without waiting for a provider
cooldown. The claim clears it again. NULL everywhere means nothing changes for existing rows.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-04 16:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0032'
down_revision: str | None = '0031'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ('job_description_parse_tasks', 'job_description_fetch_tasks', 'job_ranking_tasks', 'tailor_tasks')


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('run_now_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column('run_now_at')
