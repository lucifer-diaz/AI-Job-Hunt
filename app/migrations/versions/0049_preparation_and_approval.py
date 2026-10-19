"""Resume preparation and Job Insights graphs, and the approved packet (Phase 4)

* job_preparation_tasks: one run of the resume preparation graph (app.agents.preparation) for one job: its lease,
  attempts, the step it waits at, the revision it is on, the review decision, what a person decided and how it ended.
  At most one active task per job and mode.
* approved_packets: the exact packet a person approved (app.services.approval), an immutable snapshot and its
  digest; a later edit or approval revokes it, never rewrites it.
* job_insight_tasks.agent_thread_id / staged: the graph thread of a research run when JOB_INSIGHTS_GRAPH_MODE=on,
  and its validated synthesis between two graph steps (cleared when the run settles).

Additive; nothing existing changes. Downgrade drops the new table and columns (graph runs and approval snapshots go
with them; jobs, applications, tailored versions and insights are untouched).

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-19 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0049'
down_revision: str | None = '0048'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED")
ACTIVE = "status IN ('PENDING', 'PROCESSING')"


def upgrade() -> None:
    op.create_table(
        'job_preparation_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_job_preparation_tasks_user_id'),
                  nullable=False),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_job_preparation_tasks_job_id'),
                  nullable=False),
        sa.Column('agent_thread_id', sa.Integer(),
                  sa.ForeignKey('agent_threads.id', ondelete='SET NULL',
                                name='fk_job_preparation_tasks_agent_thread_id'), nullable=True),
        sa.Column('mode', sa.String(10), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('reason', sa.String(40), nullable=False),
        sa.Column('step', sa.String(60), nullable=True),
        sa.Column('waiting_for', sa.String(40), nullable=True),
        sa.Column('outcome', sa.String(30), nullable=True),
        sa.Column('result_code', sa.String(60), nullable=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('decision', sa.String(20), nullable=True),
        sa.Column('person_decision', sa.String(20), nullable=True),
        sa.Column('variant_id', sa.Integer(),
                  sa.ForeignKey('resume_variants.id', ondelete='SET NULL',
                                name='fk_job_preparation_tasks_variant_id'), nullable=True),
        sa.Column('parity', sa.JSON(), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
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
                           name='ck_job_preparation_tasks_status'),
        sa.CheckConstraint("mode IN ('ON', 'SHADOW')", name='ck_job_preparation_tasks_mode'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_job_preparation_tasks_attempt_count'),
        sa.CheckConstraint('revision >= 0', name='ck_job_preparation_tasks_revision'),
    )
    op.create_index('ix_job_preparation_tasks_user_id', 'job_preparation_tasks', ['user_id'])
    op.create_index('ix_job_preparation_tasks_job_id', 'job_preparation_tasks', ['job_id'])
    op.create_index('ix_job_preparation_tasks_agent_thread_id', 'job_preparation_tasks', ['agent_thread_id'])
    op.create_index('ix_job_preparation_tasks_claim', 'job_preparation_tasks', ['status', 'next_attempt_at'])
    op.create_index('uq_job_preparation_tasks_active_job', 'job_preparation_tasks', ['job_id', 'mode'],
                    unique=True, postgresql_where=sa.text(ACTIVE), sqlite_where=sa.text(ACTIVE))

    op.create_table(
        'approved_packets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_approved_packets_user_id'), nullable=False),
        sa.Column('application_id', sa.Integer(),
                  sa.ForeignKey('applications.id', ondelete='CASCADE', name='fk_approved_packets_application_id'),
                  nullable=False),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_approved_packets_job_id'), nullable=False),
        sa.Column('variant_id', sa.Integer(),
                  sa.ForeignKey('resume_variants.id', ondelete='SET NULL', name='fk_approved_packets_variant_id'),
                  nullable=True),
        sa.Column('variant_version', sa.Integer(), nullable=False),
        sa.Column('serializer_version', sa.Integer(), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('content', sa.JSON(), nullable=False),
        sa.Column('reviewer_score', sa.Float(), nullable=True),
        sa.Column('override', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_reason', sa.String(200), nullable=True),
    )
    op.create_index('ix_approved_packets_user_id', 'approved_packets', ['user_id'])
    op.create_index('ix_approved_packets_application_id', 'approved_packets', ['application_id'])
    op.create_index('ix_approved_packets_job_id', 'approved_packets', ['job_id'])

    with op.batch_alter_table('job_insight_tasks') as batch:
        batch.add_column(sa.Column('agent_thread_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('staged', sa.JSON(), nullable=True))
        batch.create_foreign_key('fk_job_insight_tasks_agent_thread_id', 'agent_threads', ['agent_thread_id'], ['id'],
                                 ondelete='SET NULL')
        batch.create_index('ix_job_insight_tasks_agent_thread_id', ['agent_thread_id'])


def downgrade() -> None:
    with op.batch_alter_table('job_insight_tasks') as batch:
        batch.drop_index('ix_job_insight_tasks_agent_thread_id')
        batch.drop_constraint('fk_job_insight_tasks_agent_thread_id', type_='foreignkey')
        batch.drop_column('staged')
        batch.drop_column('agent_thread_id')
    op.drop_index('ix_approved_packets_job_id', table_name='approved_packets')
    op.drop_index('ix_approved_packets_application_id', table_name='approved_packets')
    op.drop_index('ix_approved_packets_user_id', table_name='approved_packets')
    op.drop_table('approved_packets')
    op.drop_index('uq_job_preparation_tasks_active_job', table_name='job_preparation_tasks')
    op.drop_index('ix_job_preparation_tasks_claim', table_name='job_preparation_tasks')
    op.drop_index('ix_job_preparation_tasks_agent_thread_id', table_name='job_preparation_tasks')
    op.drop_index('ix_job_preparation_tasks_job_id', table_name='job_preparation_tasks')
    op.drop_index('ix_job_preparation_tasks_user_id', table_name='job_preparation_tasks')
    op.drop_table('job_preparation_tasks')
