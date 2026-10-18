"""Repair: add job_qualification_tasks.error_category where an early copy of 0047 left it out

An install that ran 0047 before error_category was added already stands at revision '0047', so it never got the
column and every qualification pass failed reading the task table. The step checks first, so on a database that has
the column this revision changes nothing.

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-18 16:30:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0048'
down_revision: str | None = '0047'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('job_qualification_tasks')}
    if 'error_category' not in columns:
        with op.batch_alter_table('job_qualification_tasks') as batch:
            batch.add_column(sa.Column('error_category', sa.String(30), nullable=True))


def downgrade() -> None:
    """A repair: going back leaves the repaired schema as 0047 defines it."""
