"""The qualification graph's task table (Phase 3)

* job_qualification_tasks: one run of the qualification graph (app.agents.qualification) for one job: its lease,
  attempts, the step it waits at, how it ended, and for shadow runs the old-vs-new comparison (ids and enums only).
  At most one active task per job and mode.

Additive; nothing existing changes. Downgrade drops the table (graph runs and shadow comparisons go with it; the jobs,
their tasks and their transitions are untouched).

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-17 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0047'
down_revision: str | None = '0046'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED")
ACTIVE = "status IN ('PENDING', 'PROCESSING')"


def upgrade() -> None:
    op.create_table(
        'job_qualification_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_job_qualification_tasks_user_id'),
                  nullable=False),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_job_qualification_tasks_job_id'),
                  nullable=False),
        sa.Column('agent_thread_id', sa.Integer(),
                  sa.ForeignKey('agent_threads.id', ondelete='SET NULL',
                                name='fk_job_qualification_tasks_agent_thread_id'), nullable=True),
        sa.Column('mode', sa.String(10), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('reason', sa.String(40), nullable=False),
        sa.Column('step', sa.String(60), nullable=True),
        sa.Column('waiting_for', sa.String(40), nullable=True),
        sa.Column('outcome', sa.String(30), nullable=True),
        sa.Column('result_code', sa.String(60), nullable=True),
        sa.Column('parity', sa.JSON(), nullable=True),
        sa.Column('paid_calls', sa.Integer(), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('error_category', sa.String(30), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('locked_at', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(200), nullable=True),
        sa.Column('lock_token', sa.String(36), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('run_now_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({', '.join(repr(s) for s in STATUSES)})",
                           name='ck_job_qualification_tasks_status'),
        sa.CheckConstraint("mode IN ('ON', 'SHADOW')", name='ck_job_qualification_tasks_mode'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_job_qualification_tasks_attempt_count'),
    )
    op.create_index('ix_job_qualification_tasks_user_id', 'job_qualification_tasks', ['user_id'])
    op.create_index('ix_job_qualification_tasks_job_id', 'job_qualification_tasks', ['job_id'])
    op.create_index('ix_job_qualification_tasks_agent_thread_id', 'job_qualification_tasks', ['agent_thread_id'])
    op.create_index('ix_job_qualification_tasks_claim', 'job_qualification_tasks', ['status', 'next_attempt_at'])
    op.create_index('uq_job_qualification_tasks_active_job', 'job_qualification_tasks', ['job_id', 'mode'],
                    unique=True, postgresql_where=sa.text(ACTIVE), sqlite_where=sa.text(ACTIVE))


def downgrade() -> None:
    op.drop_index('uq_job_qualification_tasks_active_job', table_name='job_qualification_tasks')
    op.drop_index('ix_job_qualification_tasks_claim', table_name='job_qualification_tasks')
    op.drop_index('ix_job_qualification_tasks_agent_thread_id', table_name='job_qualification_tasks')
    op.drop_index('ix_job_qualification_tasks_job_id', table_name='job_qualification_tasks')
    op.drop_index('ix_job_qualification_tasks_user_id', table_name='job_qualification_tasks')
    op.drop_table('job_qualification_tasks')
