"""workflow state, retry and lease columns on jobs; workflow_transitions history

Existing jobs are backfilled from the legacy dashboard `status` (and application stage / review
score), and each gets one history row recording that backfill.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-14 18:10:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.services.lifecycle at this revision. Changing the enums needs a new migration.
WORKFLOW_STATES = (
    'DISCOVERED', 'NORMALIZED', 'FILTERED', 'RANKED', 'QUALIFIED', 'TAILORING', 'TAILORED', 'REVIEWED',
    'READY_TO_APPLY', 'APPLYING', 'APPLIED', 'FOLLOWUP_DUE', 'FOLLOWED_UP', 'INTERVIEW', 'REJECTED', 'OFFER',
    'ARCHIVED', 'SKIPPED', 'FAILED',
)
ERROR_CATEGORIES = (
    'TRANSIENT', 'RATE_LIMITED', 'EXTERNAL_SERVICE', 'TIMEOUT', 'LEASE_EXPIRED', 'VALIDATION', 'CONFIGURATION',
    'BLOCKED', 'UNKNOWN',
)
LEASE_CHECK = ("(locked_at IS NULL AND locked_by IS NULL AND lock_token IS NULL AND lock_expires_at IS NULL)"
               " OR (locked_at IS NOT NULL AND locked_by IS NOT NULL AND lock_token IS NOT NULL"
               " AND lock_expires_at IS NOT NULL)")

# Legacy dashboard status -> canonical state. Post-submission detail comes from the tracker stage.
LEGACY_STATE = """CASE
    WHEN status = 'applied' AND EXISTS (SELECT 1 FROM applications a WHERE a.job_id = jobs.id
         AND a.stage IN ('interviewing', 'takehome', 'negotiating')) THEN 'INTERVIEW'
    WHEN status = 'applied' AND EXISTS (SELECT 1 FROM applications a WHERE a.job_id = jobs.id
         AND a.stage = 'offer') THEN 'OFFER'
    WHEN status = 'applied' AND EXISTS (SELECT 1 FROM applications a WHERE a.job_id = jobs.id
         AND a.stage = 'rejected') THEN 'REJECTED'
    WHEN status = 'applied' AND EXISTS (SELECT 1 FROM applications a WHERE a.job_id = jobs.id
         AND a.stage = 'closed') THEN 'ARCHIVED'
    WHEN status = 'applied' THEN 'APPLIED'
    WHEN status = 'approved' THEN 'READY_TO_APPLY'
    WHEN status = 'pending_approval' THEN 'REVIEWED'
    WHEN status = 'tailored' AND EXISTS (SELECT 1 FROM resume_variants r WHERE r.job_id = jobs.id
         AND r.reviewer_score IS NOT NULL) THEN 'REVIEWED'
    WHEN status = 'tailored' THEN 'TAILORED'
    WHEN status = 'shortlisted' THEN 'QUALIFIED'
    WHEN status = 'archived' THEN 'ARCHIVED'
    WHEN status = 'dismissed' THEN 'SKIPPED'
    WHEN status = 'rejected' THEN 'REJECTED'
    WHEN fit_score IS NOT NULL THEN 'RANKED'
    ELSE 'DISCOVERED'
END"""


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def _utc_now():
    if op.get_bind().dialect.name == 'postgresql':
        return sa.text("(now() at time zone 'utc')")
    return sa.text('CURRENT_TIMESTAMP')


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('state', sa.String(length=32), nullable=False, server_default='DISCOVERED'))
        batch.add_column(sa.Column('previous_state', sa.String(length=32), nullable=True))
        batch.add_column(sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('last_error', sa.Text(), nullable=True))
        batch.add_column(sa.Column('error_category', sa.String(length=32), nullable=True))
        batch.add_column(sa.Column('retryable', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('last_attempt_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('next_attempt_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('locked_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('locked_by', sa.String(length=200), nullable=True))
        batch.add_column(sa.Column('lock_token', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('lock_expires_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('created_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))

    op.execute(f"UPDATE jobs SET state = {LEGACY_STATE}, created_at = discovered_at, updated_at = discovered_at")

    with op.batch_alter_table('jobs') as batch:
        batch.alter_column('created_at', existing_type=sa.DateTime(), nullable=False, server_default=_utc_now())
        batch.alter_column('updated_at', existing_type=sa.DateTime(), nullable=False, server_default=_utc_now())
        batch.create_check_constraint('ck_jobs_state', f"state IN ({_values(WORKFLOW_STATES)})")
        batch.create_check_constraint('ck_jobs_previous_state', f"previous_state IN ({_values(WORKFLOW_STATES)})")
        batch.create_check_constraint('ck_jobs_error_category', f"error_category IN ({_values(ERROR_CATEGORIES)})")
        batch.create_check_constraint('ck_jobs_attempt_count', 'attempt_count >= 0')
        batch.create_check_constraint('ck_jobs_lease', LEASE_CHECK)
    op.create_index('ix_jobs_workflow_claim', 'jobs', ['state', 'next_attempt_at'], unique=False)
    op.create_index('ix_jobs_lock_expires_at', 'jobs', ['lock_expires_at'], unique=False)

    op.create_table('workflow_transitions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('from_state', sa.String(length=32), nullable=True),
        sa.Column('to_state', sa.String(length=32), nullable=False),
        sa.Column('actor', sa.String(length=200), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('error_category', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"from_state IN ({_values(WORKFLOW_STATES)})", name='ck_workflow_transitions_from_state'),
        sa.CheckConstraint(f"to_state IN ({_values(WORKFLOW_STATES)})", name='ck_workflow_transitions_to_state'),
        sa.CheckConstraint(f"error_category IN ({_values(ERROR_CATEGORIES)})",
                           name='ck_workflow_transitions_error_category'),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_workflow_transitions_job_id'), 'workflow_transitions', ['job_id'], unique=False)
    op.execute("INSERT INTO workflow_transitions"
               " (job_id, from_state, to_state, actor, reason, attempt_count, created_at)"
               " SELECT id, NULL, state, 'migration:0002', 'Backfilled from legacy status ' || status, 0, updated_at "
               "FROM jobs ORDER BY id")


def downgrade() -> None:
    op.drop_index(op.f('ix_workflow_transitions_job_id'), table_name='workflow_transitions')
    op.drop_table('workflow_transitions')
    op.drop_index('ix_jobs_lock_expires_at', table_name='jobs')
    op.drop_index('ix_jobs_workflow_claim', table_name='jobs')
    with op.batch_alter_table('jobs') as batch:
        for name in ('ck_jobs_lease', 'ck_jobs_attempt_count', 'ck_jobs_error_category', 'ck_jobs_previous_state',
                     'ck_jobs_state'):
            batch.drop_constraint(name, type_='check')
        for column in ('updated_at', 'created_at', 'lock_expires_at', 'lock_token', 'locked_by', 'locked_at',
                       'next_attempt_at', 'last_attempt_at', 'retryable', 'error_category', 'last_error',
                       'attempt_count', 'previous_state', 'state'):
            batch.drop_column(column)
