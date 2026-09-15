"""scheduler_jobs leases and run history; posting compensation on jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-15 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _utc_now():
    if op.get_bind().dialect.name == 'postgresql':
        return sa.text("(now() at time zone 'utc')")
    return sa.text('CURRENT_TIMESTAMP')


def upgrade() -> None:
    op.create_table('scheduler_jobs',
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('locked_by', sa.String(length=200), nullable=True),
        sa.Column('locked_at', sa.DateTime(), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(), nullable=True),
        sa.Column('last_started_at', sa.DateTime(), nullable=True),
        sa.Column('last_finished_at', sa.DateTime(), nullable=True),
        sa.Column('last_status', sa.String(length=20), nullable=True),
        sa.Column('last_trigger', sa.String(length=20), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_result', sa.JSON(), nullable=True),
        sa.Column('last_duration_ms', sa.Integer(), nullable=True),
        sa.Column('run_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failure_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=_utc_now()),
        sa.PrimaryKeyConstraint('name'),
    )
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('compensation', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('compensation_checked_at', sa.DateTime(), nullable=True))
    op.create_index('ix_jobs_compensation_checked_at', 'jobs', ['compensation_checked_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_jobs_compensation_checked_at', table_name='jobs')
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('compensation_checked_at')
        batch.drop_column('compensation')
    op.drop_table('scheduler_jobs')
