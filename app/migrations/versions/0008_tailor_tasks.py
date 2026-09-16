"""background tailoring tasks: single and bulk tailoring run server-side and survive page reloads

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-16 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'tailor_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_tailor_tasks_user_id'),
                  nullable=False),
        sa.Column('mode', sa.String(length=10), nullable=False),
        sa.Column('regenerate', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('stage', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('locked_by', sa.String(length=200), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint("mode IN ('tailor', 'skip')", name='ck_tailor_tasks_mode'),
        sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
                           name='ck_tailor_tasks_status'),
    )
    op.create_index('ix_tailor_tasks_job_id', 'tailor_tasks', ['job_id'])
    op.create_index('ix_tailor_tasks_user_id', 'tailor_tasks', ['user_id'])
    op.create_index('ix_tailor_tasks_status', 'tailor_tasks', ['status', 'id'])


def downgrade() -> None:
    op.drop_table('tailor_tasks')
