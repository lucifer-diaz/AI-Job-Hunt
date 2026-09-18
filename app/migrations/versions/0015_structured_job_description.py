"""Structured job descriptions: normalized text, structured data, requirement evidence, parse tasks and history

Existing jobs keep their raw description untouched. They start at status RAW; the scheduler parses them in
batches (app.services.job_description), so nothing blocks access to a job while it waits.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-18 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.models at this revision.
JD_STATUSES = ('RAW', 'NORMALIZED', 'PARSED', 'NEEDS_REVIEW', 'MANUALLY_CORRECTED')
JD_PARSE_STATUSES = ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')
ACTIVE_JD_PARSE_STATUSES = ('PENDING', 'PROCESSING')
REQUIREMENT_TYPES = ('SKILL', 'EXPERIENCE', 'RESPONSIBILITY', 'LEADERSHIP', 'EDUCATION', 'LOCATION',
                     'WORK_AUTHORIZATION', 'VISA', 'COMPENSATION', 'DOMAIN', 'SENIORITY', 'OTHER')
REQUIREMENT_LEVELS = ('REQUIRED', 'PREFERRED', 'INFORMATIONAL', 'UNKNOWN')
JOB_CHANGE_SOURCES = ('PARSER', 'USER', 'SYSTEM')
ERROR_CATEGORIES = ('TRANSIENT', 'RATE_LIMITED', 'EXTERNAL_SERVICE', 'TIMEOUT', 'LEASE_EXPIRED', 'VALIDATION',
                    'CONFIGURATION', 'BLOCKED', 'UNKNOWN')


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'),
                     nullable=False)


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('normalized_description', sa.Text(), nullable=True))
        batch.add_column(sa.Column('structured_description', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('structured_description_version', sa.String(length=20), nullable=True))
        batch.add_column(sa.Column('structured_description_status', sa.String(length=25), nullable=False,
                                   server_default='RAW'))
        batch.add_column(sa.Column('structured_description_parsed_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('structured_description_hash', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('structured_description_candidate', sa.JSON(), nullable=True))
        batch.create_check_constraint('ck_jobs_structured_description_status',
                                      f"structured_description_status IN ({_values(JD_STATUSES)})")

    op.create_table(
        'job_requirement_evidence',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('job_requirement_evidence'),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_job_requirement_evidence_job_id'),
                  nullable=False),
        sa.Column('item_id', sa.String(length=60), nullable=False),
        sa.Column('requirement_type', sa.String(length=25), nullable=False),
        sa.Column('normalized_value', sa.String(length=300), nullable=False),
        sa.Column('requirement_level', sa.String(length=15), nullable=False),
        sa.Column('source_text', sa.Text(), nullable=False),
        sa.Column('source_section', sa.String(length=120), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('is_explicit', sa.Boolean(), nullable=False),
        sa.Column('is_derived', sa.Boolean(), nullable=False),
        sa.Column('origin', sa.String(length=10), nullable=False, server_default='PARSER'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"requirement_type IN ({_values(REQUIREMENT_TYPES)})",
                           name='ck_job_requirement_evidence_type'),
        sa.CheckConstraint(f"requirement_level IN ({_values(REQUIREMENT_LEVELS)})",
                           name='ck_job_requirement_evidence_level'),
        sa.CheckConstraint('confidence >= 0 AND confidence <= 1', name='ck_job_requirement_evidence_confidence'),
        sa.CheckConstraint("origin IN ('PARSER', 'USER')", name='ck_job_requirement_evidence_origin'),
    )
    op.create_index('ix_job_requirement_evidence_user_id', 'job_requirement_evidence', ['user_id'])
    op.create_index('ix_job_requirement_evidence_job_id', 'job_requirement_evidence', ['job_id'])
    op.create_index('ix_job_requirement_evidence_job_type', 'job_requirement_evidence', ['job_id', 'requirement_type'])

    op.create_table(
        'job_structured_data_changes',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('job_structured_data_changes'),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_job_structured_data_changes_job_id'),
                  nullable=False),
        sa.Column('field_path', sa.String(length=300), nullable=False),
        sa.Column('old_value', sa.JSON(), nullable=True),
        sa.Column('new_value', sa.JSON(), nullable=True),
        sa.Column('change_source', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"change_source IN ({_values(JOB_CHANGE_SOURCES)})",
                           name='ck_job_structured_data_changes_source'),
    )
    op.create_index('ix_job_structured_data_changes_user_id', 'job_structured_data_changes', ['user_id'])
    op.create_index('ix_job_structured_data_changes_job_created', 'job_structured_data_changes',
                    ['job_id', 'created_at'])

    op.create_table(
        'job_description_parse_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('job_description_parse_tasks'),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_jd_parse_tasks_job_id'), nullable=False),
        sa.Column('description_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.String(length=40), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('error_category', sa.String(length=32), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(length=200), nullable=True),
        sa.Column('lock_token', sa.String(length=36), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({_values(JD_PARSE_STATUSES)})", name='ck_jd_parse_tasks_status'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_jd_parse_tasks_attempt_count'),
        sa.CheckConstraint(f"error_category IN ({_values(ERROR_CATEGORIES)})", name='ck_jd_parse_tasks_error_category'),
    )
    op.create_index('ix_job_description_parse_tasks_user_id', 'job_description_parse_tasks', ['user_id'])
    op.create_index('ix_job_description_parse_tasks_job_id', 'job_description_parse_tasks', ['job_id'])
    op.create_index('ix_jd_parse_tasks_claim', 'job_description_parse_tasks', ['status', 'next_attempt_at'])
    op.create_index('uq_jd_parse_tasks_active_job', 'job_description_parse_tasks', ['job_id'], unique=True,
                    postgresql_where=sa.text(f"status IN ({_values(ACTIVE_JD_PARSE_STATUSES)})"),
                    sqlite_where=sa.text(f"status IN ({_values(ACTIVE_JD_PARSE_STATUSES)})"))

    with op.batch_alter_table('llm_request_logs') as batch:
        batch.add_column(sa.Column('raw_job_tokens', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('selected_job_context_tokens', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('job_context_reduction_percent', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_column('job_context_reduction_percent')
        batch.drop_column('selected_job_context_tokens')
        batch.drop_column('raw_job_tokens')
    op.drop_table('job_description_parse_tasks')
    op.drop_table('job_structured_data_changes')
    op.drop_table('job_requirement_evidence')
    with op.batch_alter_table('jobs') as batch:
        batch.drop_constraint('ck_jobs_structured_description_status', type_='check')
        for column in ('structured_description_candidate', 'structured_description_hash',
                       'structured_description_parsed_at', 'structured_description_status',
                       'structured_description_version', 'structured_description', 'normalized_description'):
            batch.drop_column(column)
