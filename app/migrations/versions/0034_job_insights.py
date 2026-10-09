"""Job Insights: the research queue, one insight package per job, and the observations it rests on

New tables only; nothing existing is rewritten. Approved jobs from before this revision are queued in small batches by
the job_insights scheduler job, not here.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-07 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0034'
down_revision: str | None = '0033'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.models at this revision.
TASK_STATUSES = ('PENDING', 'PROCESSING', 'COMPLETED', 'PARTIAL', 'FAILED', 'SKIPPED')
ACTIVE_TASK_STATUSES = ('PENDING', 'PROCESSING')
TASK_REASONS = ('AUTO_APPROVED', 'USER_REQUEST', 'REFRESH', 'BACKLOG')
INSIGHT_STATUSES = ('NONE', 'COMPLETED', 'PARTIAL', 'FAILED')
CATEGORIES = ('COMPENSATION', 'COMPANY', 'VALUES', 'CULTURE', 'SENTIMENT', 'INTERVIEW_PROCESS', 'INTERVIEW_QUESTION')
SOURCE_TYPES = ('EMPLOYER', 'JOB_DESCRIPTION', 'RECRUITER', 'OFFICIAL_COMPANY', 'GLASSDOOR', 'PAYSCALE', 'INDEED',
                'LEVELS_FYI', 'PUBLIC_WEB')
MATCH_STATUSES = ('ACCEPTED', 'NEEDS_REVIEW', 'REJECTED')
ERROR_CATEGORIES = ('TRANSIENT', 'RATE_LIMITED', 'EXTERNAL_SERVICE', 'TIMEOUT', 'LEASE_EXPIRED', 'VALIDATION',
                    'CONFIGURATION', 'BLOCKED', 'UNKNOWN')


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'),
                     nullable=False)


def upgrade() -> None:
    op.create_table(
        'job_insight_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('job_insight_tasks'),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_job_insight_tasks_job_id'),
                  nullable=False),
        sa.Column('application_id', sa.Integer(),
                  sa.ForeignKey('applications.id', ondelete='SET NULL', name='fk_job_insight_tasks_application_id'),
                  nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.String(length=20), nullable=False),
        sa.Column('force_refresh', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('error_category', sa.String(length=32), nullable=True),
        sa.Column('source_status', sa.JSON(), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(length=200), nullable=True),
        sa.Column('lock_token', sa.String(length=36), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('run_now_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({_values(TASK_STATUSES)})", name='ck_job_insight_tasks_status'),
        sa.CheckConstraint(f"reason IN ({_values(TASK_REASONS)})", name='ck_job_insight_tasks_reason'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_job_insight_tasks_attempt_count'),
        sa.CheckConstraint(f"error_category IN ({_values(ERROR_CATEGORIES)})",
                           name='ck_job_insight_tasks_error_category'),
    )
    op.create_index('ix_job_insight_tasks_user_id', 'job_insight_tasks', ['user_id'])
    op.create_index('ix_job_insight_tasks_job_id', 'job_insight_tasks', ['job_id'])
    op.create_index('ix_job_insight_tasks_claim', 'job_insight_tasks', ['status', 'next_attempt_at'])
    op.create_index('ix_job_insight_tasks_user_created', 'job_insight_tasks', ['user_id', 'created_at'])
    op.create_index('uq_job_insight_tasks_active_job', 'job_insight_tasks', ['job_id'], unique=True,
                    postgresql_where=sa.text(f"status IN ({_values(ACTIVE_TASK_STATUSES)})"),
                    sqlite_where=sa.text(f"status IN ({_values(ACTIVE_TASK_STATUSES)})"))

    op.create_table(
        'job_insights',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('job_insights'),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_job_insights_job_id'),
                  nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('company_name', sa.String(length=300), nullable=False),
        sa.Column('canonical_company', sa.String(length=300), nullable=False),
        sa.Column('company_domain', sa.String(length=300), nullable=True),
        sa.Column('company_website', sa.String(length=1200), nullable=True),
        sa.Column('company_website_source', sa.String(length=20), nullable=True),
        sa.Column('original_title', sa.String(length=300), nullable=False),
        sa.Column('normalized_title', sa.String(length=300), nullable=False),
        sa.Column('seniority', sa.String(length=40), nullable=True),
        sa.Column('discipline', sa.String(length=60), nullable=True),
        sa.Column('city', sa.String(length=120), nullable=True),
        sa.Column('country', sa.String(length=120), nullable=True),
        sa.Column('currency', sa.String(length=10), nullable=True),
        sa.Column('compensation', sa.JSON(), nullable=True),
        sa.Column('company', sa.JSON(), nullable=True),
        sa.Column('interview_process', sa.JSON(), nullable=True),
        sa.Column('reported_questions', sa.JSON(), nullable=True),
        sa.Column('generated_questions', sa.JSON(), nullable=True),
        sa.Column('questions_to_ask', sa.JSON(), nullable=True),
        sa.Column('focus_areas', sa.JSON(), nullable=True),
        sa.Column('value_prep', sa.JSON(), nullable=True),
        sa.Column('compensation_prep', sa.JSON(), nullable=True),
        sa.Column('risks', sa.JSON(), nullable=True),
        sa.Column('unknowns', sa.JSON(), nullable=True),
        sa.Column('recruiter_input', sa.JSON(), nullable=True),
        sa.Column('prompt_versions', sa.JSON(), nullable=True),
        sa.Column('model', sa.String(length=200), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('generated_at', sa.DateTime(), nullable=True),
        sa.Column('refreshed_at', sa.DateTime(), nullable=True),
        sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"status IN ({_values(INSIGHT_STATUSES)})", name='ck_job_insights_status'),
        sa.UniqueConstraint('job_id', name='uq_job_insights_job_id'),
    )
    op.create_index('ix_job_insights_user_id', 'job_insights', ['user_id'])

    op.create_table(
        'insight_observations',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('insight_observations'),
        sa.Column('job_insight_id', sa.Integer(),
                  sa.ForeignKey('job_insights.id', ondelete='CASCADE', name='fk_insight_observations_job_insight_id'),
                  nullable=False),
        sa.Column('category', sa.String(length=30), nullable=False),
        sa.Column('source_type', sa.String(length=30), nullable=False),
        sa.Column('source_name', sa.String(length=40), nullable=False),
        sa.Column('provider', sa.String(length=40), nullable=True),
        sa.Column('actor_id', sa.String(length=300), nullable=True),
        sa.Column('source_url', sa.String(length=1200), nullable=True),
        sa.Column('source_reference', sa.String(length=300), nullable=True),
        sa.Column('title', sa.String(length=300), nullable=True),
        sa.Column('match_scope', sa.String(length=30), nullable=True),
        sa.Column('match_status', sa.String(length=20), nullable=False),
        sa.Column('match_reason', sa.String(length=300), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('recency', sa.String(length=10), nullable=True),
        sa.Column('observed_at', sa.DateTime(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.Column('cache_key', sa.String(length=400), nullable=True),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"category IN ({_values(CATEGORIES)})", name='ck_insight_observations_category'),
        sa.CheckConstraint(f"source_type IN ({_values(SOURCE_TYPES)})", name='ck_insight_observations_source_type'),
        sa.CheckConstraint(f"match_status IN ({_values(MATCH_STATUSES)})", name='ck_insight_observations_match_status'),
        sa.CheckConstraint('confidence >= 0 AND confidence <= 1', name='ck_insight_observations_confidence'),
    )
    op.create_index('ix_insight_observations_user_id', 'insight_observations', ['user_id'])
    op.create_index('ix_insight_observations_job_insight_id', 'insight_observations', ['job_insight_id'])
    op.create_index('ix_insight_observations_insight_category', 'insight_observations', ['job_insight_id', 'category'])
    op.create_index('ix_insight_observations_cache', 'insight_observations', ['user_id', 'cache_key', 'fetched_at'])


def downgrade() -> None:
    op.drop_table('insight_observations')
    op.drop_table('job_insights')
    op.drop_table('job_insight_tasks')
