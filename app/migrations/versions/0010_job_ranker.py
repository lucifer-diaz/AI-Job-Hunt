"""job ranker: RANK_PENDING and RANKING states, resume snapshots, ranking tasks and versioned ranking results

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-16 21:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.services.lifecycle at this revision. Changing the enums needs a new migration.
WORKFLOW_STATES = (
    'DISCOVERED', 'NORMALIZED', 'FILTERED', 'RANK_PENDING', 'RANKING', 'RANKED', 'QUALIFIED', 'TAILORING',
    'TAILORED', 'REVIEWED', 'READY_TO_APPLY', 'APPLYING', 'APPLIED', 'FOLLOWUP_DUE', 'FOLLOWED_UP', 'INTERVIEW',
    'REJECTED', 'OFFER', 'ARCHIVED', 'SKIPPED', 'FAILED',
)
PREVIOUS_WORKFLOW_STATES = (
    'DISCOVERED', 'NORMALIZED', 'FILTERED', 'RANKED', 'QUALIFIED', 'TAILORING', 'TAILORED', 'REVIEWED',
    'READY_TO_APPLY', 'APPLYING', 'APPLIED', 'FOLLOWUP_DUE', 'FOLLOWED_UP', 'INTERVIEW', 'REJECTED', 'OFFER',
    'ARCHIVED', 'SKIPPED', 'FAILED',
)
ERROR_CATEGORIES = (
    'TRANSIENT', 'RATE_LIMITED', 'EXTERNAL_SERVICE', 'TIMEOUT', 'LEASE_EXPIRED', 'VALIDATION', 'CONFIGURATION',
    'BLOCKED', 'UNKNOWN',
)
TASK_STATUSES = ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'SKIPPED')
ACTIVE_TASK_STATUSES = ('PENDING', 'PROCESSING')
DECISIONS = ('QUALIFIED', 'ARCHIVED')


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def _state_checks(states: Sequence[str]) -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_constraint('ck_jobs_state', type_='check')
        batch.drop_constraint('ck_jobs_previous_state', type_='check')
        batch.create_check_constraint('ck_jobs_state', f"state IN ({_values(states)})")
        batch.create_check_constraint('ck_jobs_previous_state', f"previous_state IN ({_values(states)})")
    with op.batch_alter_table('workflow_transitions') as batch:
        batch.drop_constraint('ck_workflow_transitions_from_state', type_='check')
        batch.drop_constraint('ck_workflow_transitions_to_state', type_='check')
        batch.create_check_constraint('ck_workflow_transitions_from_state', f"from_state IN ({_values(states)})")
        batch.create_check_constraint('ck_workflow_transitions_to_state', f"to_state IN ({_values(states)})")


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'),
                     nullable=False)


def upgrade() -> None:
    _state_checks(WORKFLOW_STATES)

    op.create_table(
        'resume_versions',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('resume_versions'),
        sa.Column('candidate_profile_id', sa.Integer(),
                  sa.ForeignKey('candidate_profiles.id', ondelete='CASCADE',
                                name='fk_resume_versions_candidate_profile_id'), nullable=False),
        sa.Column('resume_document_id', sa.Integer(),
                  sa.ForeignKey('resume_documents.id', ondelete='SET NULL', name='fk_resume_versions_resume_document_id'),
                  nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('candidate_profile_id', 'content_hash', name='uq_resume_versions_profile_hash'),
        sa.UniqueConstraint('candidate_profile_id', 'version', name='uq_resume_versions_profile_version'),
    )
    op.create_index('ix_resume_versions_user_id', 'resume_versions', ['user_id'])
    op.create_index('ix_resume_versions_candidate_profile_id', 'resume_versions', ['candidate_profile_id'])

    op.create_table(
        'job_rankings',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('job_rankings'),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_job_rankings_job_id'),
                  nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('forced', sa.Boolean(), nullable=False),
        sa.Column('trigger', sa.String(length=40), nullable=False),
        sa.Column('candidate_profile_id', sa.Integer(),
                  sa.ForeignKey('candidate_profiles.id', ondelete='CASCADE', name='fk_job_rankings_candidate_profile_id'),
                  nullable=False),
        sa.Column('resume_id', sa.Integer(),
                  sa.ForeignKey('resume_versions.id', ondelete='CASCADE', name='fk_job_rankings_resume_id'),
                  nullable=False),
        sa.Column('resume_version', sa.Integer(), nullable=False),
        sa.Column('resume_content_hash', sa.String(length=64), nullable=False),
        sa.Column('profile_hash', sa.String(length=64), nullable=False),
        sa.Column('evidence_hash', sa.String(length=64), nullable=False),
        sa.Column('job_hash', sa.String(length=64), nullable=False),
        *[sa.Column(name, sa.Float(), nullable=False) for name in (
            'overall_fit_score', 'confidence', 'candidate_fit_score', 'resume_fit_score', 'seniority_match_score',
            'skills_match_score', 'leadership_match_score', 'domain_match_score', 'location_match_score')],
        *[sa.Column(name, sa.JSON(), nullable=False) for name in (
            'strong_matches', 'partial_matches', 'missing_requirements', 'fatal_gaps', 'resume_gaps',
            'relevant_evidence_ids', 'discarded_evidence_ids', 'recommended_resume_focus')],
        sa.Column('qualification_recommendation', sa.String(length=20), nullable=False),
        sa.Column('reasoning_summary', sa.Text(), nullable=False),
        sa.Column('decision', sa.String(length=20), nullable=False),
        sa.Column('decision_reasons', sa.JSON(), nullable=False),
        sa.Column('policy', sa.JSON(), nullable=False),
        sa.Column('provider', sa.String(length=30), nullable=False),
        sa.Column('model', sa.String(length=300), nullable=False),
        sa.Column('prompt_version', sa.String(length=20), nullable=False),
        sa.Column('raw_output', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('job_id', 'version', name='uq_job_rankings_job_version'),
        sa.CheckConstraint(f"decision IN ({_values(DECISIONS)})", name='ck_job_rankings_decision'),
    )
    op.create_index('ix_job_rankings_user_id', 'job_rankings', ['user_id'])
    op.create_index('ix_job_rankings_job_id', 'job_rankings', ['job_id'])
    op.create_index('ix_job_rankings_candidate_profile_id', 'job_rankings', ['candidate_profile_id'])
    op.create_index('ix_job_rankings_resume_id', 'job_rankings', ['resume_id'])
    op.create_index('uq_job_rankings_inputs', 'job_rankings',
                    ['job_id', 'candidate_profile_id', 'resume_content_hash', 'prompt_version', 'profile_hash',
                     'evidence_hash', 'job_hash'], unique=True,
                    postgresql_where=sa.text('forced IS FALSE'), sqlite_where=sa.text('forced = 0'))

    op.create_table(
        'job_ranking_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('job_ranking_tasks'),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_job_ranking_tasks_job_id'),
                  nullable=False),
        sa.Column('candidate_profile_id', sa.Integer(),
                  sa.ForeignKey('candidate_profiles.id', ondelete='SET NULL',
                                name='fk_job_ranking_tasks_candidate_profile_id'), nullable=True),
        sa.Column('resume_id', sa.Integer(),
                  sa.ForeignKey('resume_versions.id', ondelete='SET NULL', name='fk_job_ranking_tasks_resume_id'),
                  nullable=True),
        sa.Column('ranking_id', sa.Integer(),
                  sa.ForeignKey('job_rankings.id', ondelete='SET NULL', name='fk_job_ranking_tasks_ranking_id'),
                  nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.String(length=40), nullable=False),
        sa.Column('force', sa.Boolean(), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('error_category', sa.String(length=32), nullable=True),
        sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('locked_at', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(length=200), nullable=True),
        sa.Column('lock_token', sa.String(length=36), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({_values(TASK_STATUSES)})", name='ck_job_ranking_tasks_status'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_job_ranking_tasks_attempt_count'),
        sa.CheckConstraint(f"error_category IN ({_values(ERROR_CATEGORIES)})",
                           name='ck_job_ranking_tasks_error_category'),
    )
    op.create_index('ix_job_ranking_tasks_user_id', 'job_ranking_tasks', ['user_id'])
    op.create_index('ix_job_ranking_tasks_job_id', 'job_ranking_tasks', ['job_id'])
    op.create_index('ix_job_ranking_tasks_claim', 'job_ranking_tasks', ['status', 'next_attempt_at'])
    op.create_index('ix_job_ranking_tasks_lock_expires_at', 'job_ranking_tasks', ['lock_expires_at'])
    op.create_index('uq_job_ranking_tasks_active_job', 'job_ranking_tasks', ['job_id'], unique=True,
                    postgresql_where=sa.text(f"status IN ({_values(ACTIVE_TASK_STATUSES)})"),
                    sqlite_where=sa.text(f"status IN ({_values(ACTIVE_TASK_STATUSES)})"))


def downgrade() -> None:
    op.drop_table('job_ranking_tasks')
    op.drop_table('job_rankings')
    op.drop_table('resume_versions')
    # Jobs in the new states go back to where the previous lifecycle would have them.
    op.execute("UPDATE jobs SET state = 'FILTERED' WHERE state IN ('RANK_PENDING', 'RANKING')")
    op.execute("UPDATE jobs SET previous_state = 'FILTERED' WHERE previous_state IN ('RANK_PENDING', 'RANKING')")
    op.execute("DELETE FROM workflow_transitions WHERE from_state IN ('RANK_PENDING', 'RANKING')"
               " OR to_state IN ('RANK_PENDING', 'RANKING')")
    _state_checks(PREVIOUS_WORKFLOW_STATES)
