"""Application execution foundation (Phase 5): pre-submit claims and the execution worker's queue

* application_claims: one reservation of a posting for one submission (app.services.apply_queue.begin_submission),
  bound to the job, its approval snapshot and digest, with a token and a lease. CLAIMED → RECORDED / RELEASED, or
  UNCERTAIN when the lease ran out (only a person resolves it).
* application_claim_keys: every alias of a claimed posting; unique per account while `held`, so a duplicate job row or
  another URL of the same posting cannot be claimed twice.
* application_execution_tasks: one run of the application execution worker (app.agents.application) for one job:
  its lease, attempts, the execution stage, the analysed form and each field's answer source (never a value). At most
  one active task per job.
* application_execution_attempts: how far each attempt got, metadata only.

Additive; nothing existing changes. Downgrade drops the four tables (claims and executions go with them; jobs,
applications and recorded submissions are untouched).

Revision ID: 0054
Revises: 0053
Create Date: 2026-10-20 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0054'
down_revision: str | None = '0053'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLAIM_STATUSES = ("CLAIMED", "UNCERTAIN", "RECORDED", "RELEASED")
CLAIM_CLIENTS = ("mcp", "apply_kit", "worker")
TASK_STATUSES = ("PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED")
ACTIVE = "status IN ('PENDING', 'PROCESSING')"
MODES = ("OBSERVE", "ASSIST")
STAGES = ("QUEUED", "DISCOVERING", "ROUTED", "FORM_ANALYZING", "ANSWERING", "FILLING", "PRE_SUBMIT_VALIDATION",
          "READY_TO_SUBMIT", "SUBMITTING", "CONFIRMING", "CONFIRMED", "NEEDS_INPUT", "NEEDS_HUMAN", "RETRYING",
          "BLOCKED", "SUBMISSION_UNCERTAIN", "FAILED", "CANCELLED")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'),
                     nullable=False)


def upgrade() -> None:
    op.create_table(
        'application_claims',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('application_claims'),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_application_claims_job_id'), nullable=False),
        sa.Column('application_id', sa.Integer(),
                  sa.ForeignKey('applications.id', ondelete='SET NULL', name='fk_application_claims_application_id'),
                  nullable=True),
        sa.Column('approved_packet_id', sa.Integer(),
                  sa.ForeignKey('approved_packets.id', ondelete='SET NULL',
                                name='fk_application_claims_approved_packet_id'), nullable=True),
        sa.Column('packet_digest', sa.String(64), nullable=True),
        sa.Column('identity', sa.String(800), nullable=False),
        sa.Column('token', sa.String(64), nullable=False),
        sa.Column('client', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('claimed_at', sa.DateTime(), nullable=False),
        sa.Column('lease_expires_at', sa.DateTime(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('resolution', sa.String(200), nullable=True),
        sa.Column('execution_id', sa.Integer(), nullable=True),
        sa.CheckConstraint(f"status IN ({_in(CLAIM_STATUSES)})", name='ck_application_claims_status'),
        sa.CheckConstraint(f"client IN ({_in(CLAIM_CLIENTS)})", name='ck_application_claims_client'),
        sa.UniqueConstraint('token', name='uq_application_claims_token'),
    )
    op.create_index('ix_application_claims_user_id', 'application_claims', ['user_id'])
    op.create_index('ix_application_claims_job_id', 'application_claims', ['job_id'])
    op.create_index('ix_application_claims_lease', 'application_claims', ['status', 'lease_expires_at'])

    op.create_table(
        'application_claim_keys',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('application_claim_keys'),
        sa.Column('claim_id', sa.Integer(),
                  sa.ForeignKey('application_claims.id', ondelete='CASCADE',
                                name='fk_application_claim_keys_claim_id'), nullable=False),
        sa.Column('key', sa.String(800), nullable=False),
        sa.Column('held', sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.create_index('ix_application_claim_keys_user_id', 'application_claim_keys', ['user_id'])
    op.create_index('ix_application_claim_keys_claim_id', 'application_claim_keys', ['claim_id'])
    op.create_index('uq_application_claim_keys_held', 'application_claim_keys', ['user_id', 'key'], unique=True,
                    postgresql_where=sa.text('held'), sqlite_where=sa.text('held'))

    op.create_table(
        'application_execution_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('application_execution_tasks'),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_application_execution_tasks_job_id'),
                  nullable=False),
        sa.Column('application_id', sa.Integer(),
                  sa.ForeignKey('applications.id', ondelete='SET NULL',
                                name='fk_application_execution_tasks_application_id'), nullable=True),
        sa.Column('approved_packet_id', sa.Integer(),
                  sa.ForeignKey('approved_packets.id', ondelete='SET NULL',
                                name='fk_application_execution_tasks_approved_packet_id'), nullable=True),
        sa.Column('packet_digest', sa.String(64), nullable=True),
        sa.Column('agent_thread_id', sa.Integer(),
                  sa.ForeignKey('agent_threads.id', ondelete='SET NULL',
                                name='fk_application_execution_tasks_agent_thread_id'), nullable=True),
        sa.Column('mode', sa.String(10), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('stage', sa.String(30), nullable=False),
        sa.Column('reason', sa.String(40), nullable=False),
        sa.Column('route', sa.String(40), nullable=True),
        sa.Column('form_fingerprint', sa.String(64), nullable=True),
        sa.Column('form', sa.JSON(), nullable=True),
        sa.Column('plan', sa.JSON(), nullable=True),
        sa.Column('interrupt', sa.JSON(), nullable=True),
        sa.Column('confirmed_question_ids', sa.JSON(), nullable=True),
        sa.Column('outcome', sa.String(30), nullable=True),
        sa.Column('result_code', sa.String(60), nullable=True),
        sa.Column('error_category', sa.String(40), nullable=True),
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
        sa.CheckConstraint(f"status IN ({_in(TASK_STATUSES)})", name='ck_application_execution_tasks_status'),
        sa.CheckConstraint(f"mode IN ({_in(MODES)})", name='ck_application_execution_tasks_mode'),
        sa.CheckConstraint(f"stage IN ({_in(STAGES)})", name='ck_application_execution_tasks_stage'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_application_execution_tasks_attempt_count'),
    )
    op.create_index('ix_application_execution_tasks_user_id', 'application_execution_tasks', ['user_id'])
    op.create_index('ix_application_execution_tasks_job_id', 'application_execution_tasks', ['job_id'])
    op.create_index('ix_application_execution_tasks_agent_thread_id', 'application_execution_tasks',
                    ['agent_thread_id'])
    op.create_index('ix_application_execution_tasks_claim', 'application_execution_tasks',
                    ['status', 'next_attempt_at'])
    op.create_index('uq_application_execution_tasks_active_job', 'application_execution_tasks', ['job_id'],
                    unique=True, postgresql_where=sa.text(ACTIVE), sqlite_where=sa.text(ACTIVE))

    op.create_table(
        'application_execution_attempts',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('application_execution_attempts'),
        sa.Column('task_id', sa.Integer(),
                  sa.ForeignKey('application_execution_tasks.id', ondelete='CASCADE',
                                name='fk_application_execution_attempts_task_id'), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('stage_reached', sa.String(30), nullable=True),
        sa.Column('outcome', sa.String(30), nullable=True),
        sa.Column('error_category', sa.String(40), nullable=True),
        sa.Column('form_fingerprint', sa.String(64), nullable=True),
        sa.Column('file_digests', sa.JSON(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('ended_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('task_id', 'attempt', name='uq_application_execution_attempts_task_attempt'),
    )
    op.create_index('ix_application_execution_attempts_user_id', 'application_execution_attempts', ['user_id'])
    op.create_index('ix_application_execution_attempts_task_id', 'application_execution_attempts', ['task_id'])


def downgrade() -> None:
    op.drop_table('application_execution_attempts')
    op.drop_index('uq_application_execution_tasks_active_job', table_name='application_execution_tasks')
    op.drop_table('application_execution_tasks')
    op.drop_index('uq_application_claim_keys_held', table_name='application_claim_keys')
    op.drop_table('application_claim_keys')
    op.drop_table('application_claims')
