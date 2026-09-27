"""Job description recovery: description provenance on jobs, and the recovery task queue

Existing jobs keep their description untouched and get NULL provenance (the text the job board sent). The recovery
sweep assesses their description_quality in batches, so nothing is rewritten by this migration.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-27 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0025'
down_revision: str | None = '0024'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.models at this revision.
JD_FETCH_STATUSES = ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'BLOCKED', 'SKIPPED')
ACTIVE_JD_FETCH_STATUSES = ('PENDING', 'PROCESSING')
JD_FETCH_METHODS = ('HTTP', 'PLAYWRIGHT', 'LLM_ASSISTED')
ERROR_CATEGORIES = ('TRANSIENT', 'RATE_LIMITED', 'EXTERNAL_SERVICE', 'TIMEOUT', 'LEASE_EXPIRED', 'VALIDATION',
                    'CONFIGURATION', 'BLOCKED', 'UNKNOWN')
JOB_COLUMNS = (
    ('description_source', sa.String(length=20)),
    ('description_quality', sa.String(length=20)),
    ('description_content_hash', sa.String(length=64)),
    ('original_description', sa.Text()),
    ('recovered_description', sa.Text()),
    ('description_fetch_method', sa.String(length=20)),
    ('description_source_url', sa.String(length=1200)),
    ('description_final_url', sa.String(length=1200)),
    ('description_fetched_at', sa.DateTime()),
    ('description_recovery_task_id', sa.Integer()),
)


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        for name, kind in JOB_COLUMNS:
            batch.add_column(sa.Column(name, kind, nullable=True))

    op.create_table(
        'job_description_fetch_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_job_description_fetch_tasks_user_id'),
                  nullable=False),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_jd_fetch_tasks_job_id'), nullable=False),
        sa.Column('source_url', sa.String(length=1200), nullable=False),
        sa.Column('final_url', sa.String(length=1200), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.String(length=40), nullable=False),
        sa.Column('result_code', sa.String(length=40), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('fetch_method', sa.String(length=20), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('error_category', sa.String(length=32), nullable=True),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('redirect_count', sa.Integer(), nullable=True),
        sa.Column('recovered_chars', sa.Integer(), nullable=True),
        sa.Column('playwright_used', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('llm_used', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('llm_model', sa.String(length=200), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('diagnostics', sa.JSON(), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(length=200), nullable=True),
        sa.Column('lock_token', sa.String(length=36), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({_values(JD_FETCH_STATUSES)})", name='ck_jd_fetch_tasks_status'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_jd_fetch_tasks_attempt_count'),
        sa.CheckConstraint(f"fetch_method IS NULL OR fetch_method IN ({_values(JD_FETCH_METHODS)})",
                           name='ck_jd_fetch_tasks_fetch_method'),
        sa.CheckConstraint(f"error_category IN ({_values(ERROR_CATEGORIES)})", name='ck_jd_fetch_tasks_error_category'),
    )
    op.create_index('ix_job_description_fetch_tasks_user_id', 'job_description_fetch_tasks', ['user_id'])
    op.create_index('ix_job_description_fetch_tasks_job_id', 'job_description_fetch_tasks', ['job_id'])
    op.create_index('ix_jd_fetch_tasks_claim', 'job_description_fetch_tasks', ['status', 'next_attempt_at'])
    op.create_index('ix_jd_fetch_tasks_user_created', 'job_description_fetch_tasks', ['user_id', 'created_at'])
    op.create_index('uq_jd_fetch_tasks_active_job', 'job_description_fetch_tasks', ['job_id'], unique=True,
                    postgresql_where=sa.text(f"status IN ({_values(ACTIVE_JD_FETCH_STATUSES)})"),
                    sqlite_where=sa.text(f"status IN ({_values(ACTIVE_JD_FETCH_STATUSES)})"))


def downgrade() -> None:
    op.drop_table('job_description_fetch_tasks')
    with op.batch_alter_table('jobs') as batch:
        for name, _kind in reversed(JOB_COLUMNS):
            batch.drop_column(name)
