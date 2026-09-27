"""Discovery quality: domain filter, removed-posting tombstones, feedback events and a resume per job

jobs gains domain_match / domain_reason / domain_checked_at (NULL = not assessed; the ranking sweep assesses the
backlog), listing_checked_at, and resume_id (NULL = the account's active resume, which is what every existing job
keeps). removed_postings stops a deleted dead posting from being imported again; job_feedback_events is what the
domain filter learns from.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-28 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0026'
down_revision: str | None = '0025'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEEDBACK_ACTIONS = ('DISMISSED', 'REJECTED', 'DELETED', 'SHORTLISTED', 'TAILORED', 'APPROVED', 'APPLIED', 'KEPT')


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'),
                     nullable=False)


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('domain_match', sa.String(length=20), nullable=True))
        batch.add_column(sa.Column('domain_reason', sa.String(length=300), nullable=True))
        batch.add_column(sa.Column('domain_checked_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('listing_checked_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('resume_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_jobs_resume_id', 'resumes', ['resume_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_jobs_domain_match', 'jobs', ['domain_match'])
    op.create_index('ix_jobs_resume_id', 'jobs', ['resume_id'])

    op.create_table(
        'removed_postings',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('removed_postings'),
        sa.Column('source', sa.String(length=50), nullable=False),
        sa.Column('source_ref', sa.String(length=500), nullable=False),
        sa.Column('canonical_url', sa.String(length=1200), nullable=True),
        sa.Column('dedupe_key', sa.String(length=700), nullable=True),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('company', sa.String(length=300), nullable=False),
        sa.Column('reason', sa.String(length=40), nullable=False),
        sa.Column('removed_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_removed_postings_user_id', 'removed_postings', ['user_id'])
    op.create_index('ix_removed_postings_removed_at', 'removed_postings', ['removed_at'])
    op.create_index('ix_removed_postings_user_ref', 'removed_postings', ['user_id', 'source', 'source_ref'])
    op.create_index('ix_removed_postings_user_url', 'removed_postings', ['user_id', 'canonical_url'])
    op.create_index('ix_removed_postings_user_dedupe', 'removed_postings', ['user_id', 'dedupe_key'])

    op.create_table(
        'job_feedback_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('job_feedback_events'),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('company', sa.String(length=300), nullable=False),
        sa.Column('terms', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"action IN ({_values(FEEDBACK_ACTIONS)})", name='ck_job_feedback_events_action'),
    )
    op.create_index('ix_job_feedback_events_user_id', 'job_feedback_events', ['user_id'])
    op.create_index('ix_job_feedback_events_user_created', 'job_feedback_events', ['user_id', 'created_at'])


def downgrade() -> None:
    op.drop_table('job_feedback_events')
    op.drop_table('removed_postings')
    op.drop_index('ix_jobs_resume_id', table_name='jobs')
    op.drop_index('ix_jobs_domain_match', table_name='jobs')
    with op.batch_alter_table('jobs') as batch:
        batch.drop_constraint('fk_jobs_resume_id', type_='foreignkey')
        for column in ('resume_id', 'listing_checked_at', 'domain_checked_at', 'domain_reason', 'domain_match'):
            batch.drop_column(column)
