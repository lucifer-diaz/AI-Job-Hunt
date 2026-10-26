"""Guarded submission and ATS logins (Phase 6)

* application_execution_tasks: `mode` may be GUARDED_AUTO (the column grows to 20 characters), and `backend` records
  which browser backend a run uses (ats_api, playwright, browser_use_webui).
* submission_intents: one authorized final click, bound to the posting's pre-submit claim (one per claim). CLICKING is
  committed immediately before the click, so a crash afterwards is reconciled, never retried.
* ats_routes: an account's ATS tenants (board, company, apply and login domains, what runs there learned).
* ats_credentials: a saved ATS login per domain, the password encrypted; never returned by any API.
* ats_sessions: a browser session a person logged in to, per domain, encrypted.
* llm_request_logs.stage may be BROWSER_NAVIGATION (the BROWSER_NAVIGATOR worker).

Additive except the mode check. Downgrade drops the four tables and the column, and refuses while any task is in
GUARDED_AUTO mode (those rows have no Phase 5 meaning).

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-26 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0055'
down_revision: str | None = '0054'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODES = ("OBSERVE", "ASSIST", "GUARDED_AUTO")
PHASE5_MODES = ("OBSERVE", "ASSIST")
INTENT_STATUSES = ("ARMED", "CLICKING", "CONFIRMED", "UNCERTAIN", "ABORTED", "RESOLVED_SUBMITTED",
                   "RESOLVED_NOT_SUBMITTED")
CREDENTIAL_STATUSES = ("PENDING", "ACTIVE")
# Frozen copies of app.models.LLM_STAGES before and after this revision.
OLD_STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'JD_PARSER', 'JD_RECOVERY', 'APPLICATION', 'FOLLOW_UP',
              'INBOX', 'COMPANY_INSIGHTS', 'INTERVIEW_INSIGHTS', 'ADVISOR', 'OTHER')
NEW_STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'JD_PARSER', 'JD_RECOVERY', 'APPLICATION', 'FOLLOW_UP',
              'INBOX', 'COMPANY_INSIGHTS', 'INTERVIEW_INSIGHTS', 'ADVISOR', 'BROWSER_NAVIGATION', 'OTHER')


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'),
                     nullable=False)


def _stages(stages: tuple[str, ...]) -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_constraint('ck_llm_request_logs_stage', type_='check')
        batch.create_check_constraint('ck_llm_request_logs_stage', f"stage IN ({_in(stages)})")


def upgrade() -> None:
    _stages(NEW_STAGES)
    with op.batch_alter_table('application_execution_tasks') as batch:
        batch.add_column(sa.Column('backend', sa.String(30), nullable=True))
        batch.alter_column('mode', type_=sa.String(20), existing_type=sa.String(10), existing_nullable=False)
        batch.drop_constraint('ck_application_execution_tasks_mode', type_='check')
        batch.create_check_constraint('ck_application_execution_tasks_mode', f"mode IN ({_in(MODES)})")

    op.create_table(
        'submission_intents',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('submission_intents'),
        sa.Column('claim_id', sa.Integer(),
                  sa.ForeignKey('application_claims.id', ondelete='CASCADE', name='fk_submission_intents_claim_id'),
                  nullable=False),
        sa.Column('execution_task_id', sa.Integer(),
                  sa.ForeignKey('application_execution_tasks.id', ondelete='SET NULL',
                                name='fk_submission_intents_execution_task_id'), nullable=True),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='CASCADE', name='fk_submission_intents_job_id'), nullable=False),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('packet_digest', sa.String(64), nullable=True),
        sa.Column('form_fingerprint', sa.String(64), nullable=True),
        sa.Column('backend', sa.String(30), nullable=False),
        sa.Column('route', sa.String(240), nullable=False),
        sa.Column('domain', sa.String(255), nullable=False),
        sa.Column('file_digests', sa.JSON(), nullable=True),
        sa.Column('evidence', sa.JSON(), nullable=True),
        sa.Column('reason', sa.String(300), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('clicking_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({_in(INTENT_STATUSES)})", name='ck_submission_intents_status'),
        sa.UniqueConstraint('claim_id', name='uq_submission_intents_claim'),
    )
    op.create_index('ix_submission_intents_user_id', 'submission_intents', ['user_id'])
    op.create_index('ix_submission_intents_job_id', 'submission_intents', ['job_id'])
    op.create_index('ix_submission_intents_task', 'submission_intents', ['execution_task_id', 'status'])

    op.create_table(
        'ats_routes',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('ats_routes'),
        sa.Column('ats', sa.String(40), nullable=False),
        sa.Column('tenant', sa.String(200), nullable=False),
        sa.Column('apply_domain', sa.String(255), nullable=False),
        sa.Column('login_domain', sa.String(255), nullable=True),
        sa.Column('auth_required', sa.String(20), nullable=False),
        sa.Column('backend', sa.String(30), nullable=True),
        sa.Column('source_board', sa.String(50), nullable=True),
        sa.Column('company', sa.String(255), nullable=True),
        sa.Column('form_fingerprint', sa.String(64), nullable=True),
        sa.Column('last_job_id', sa.Integer(), nullable=True),
        sa.Column('last_outcome', sa.String(60), nullable=True),
        sa.Column('needs_login', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'ats', 'tenant', name='uq_ats_routes_user_ats_tenant'),
    )
    op.create_index('ix_ats_routes_user_id', 'ats_routes', ['user_id'])

    op.create_table(
        'ats_credentials',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('ats_credentials'),
        sa.Column('domain', sa.String(255), nullable=False),
        sa.Column('username', sa.String(320), nullable=False),
        sa.Column('password_encrypted', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({_in(CREDENTIAL_STATUSES)})", name='ck_ats_credentials_status'),
        sa.UniqueConstraint('user_id', 'domain', name='uq_ats_credentials_user_domain'),
    )
    op.create_index('ix_ats_credentials_user_id', 'ats_credentials', ['user_id'])

    op.create_table(
        'ats_sessions',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('ats_sessions'),
        sa.Column('domain', sa.String(255), nullable=False),
        sa.Column('ats', sa.String(40), nullable=True),
        sa.Column('storage_state_encrypted', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('user_id', 'domain', name='uq_ats_sessions_user_domain'),
    )
    op.create_index('ix_ats_sessions_user_id', 'ats_sessions', ['user_id'])


def downgrade() -> None:
    guarded = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM application_execution_tasks WHERE mode = 'GUARDED_AUTO'")).scalar()
    if guarded:
        raise RuntimeError(f"{guarded} application executions ran in GUARDED_AUTO mode; Phase 5 has no such mode. "
                           "Keep 0055, or delete those rows deliberately first.")
    op.drop_table('ats_sessions')
    op.drop_table('ats_credentials')
    op.drop_table('ats_routes')
    op.drop_table('submission_intents')
    with op.batch_alter_table('application_execution_tasks') as batch:
        batch.drop_constraint('ck_application_execution_tasks_mode', type_='check')
        batch.create_check_constraint('ck_application_execution_tasks_mode', f"mode IN ({_in(PHASE5_MODES)})")
        batch.alter_column('mode', type_=sa.String(10), existing_type=sa.String(20), existing_nullable=False)
        batch.drop_column('backend')
    op.execute(sa.text("UPDATE llm_request_logs SET stage = 'OTHER' WHERE stage = 'BROWSER_NAVIGATION'"))
    _stages(OLD_STAGES)
