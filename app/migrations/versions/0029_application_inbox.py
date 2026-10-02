"""Application Inbox: Gmail connections, seen messages, email events on the tracker timeline, daily digests

gmail_connections holds one account's read-only Gmail connection (the OAuth refresh token only, encrypted).
application_email_messages is the deduplication record and analysis queue for every message the worker has seen;
application_communication_events is what a matched email means for an application; application_inbox_digests is the
daily digest. Nothing existing is changed.

Each table is created only when it is missing: installs that ran this revision while it was numbered 0028 (before the
automatic tailoring guards took that number) already have them.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-28 20:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0029'
down_revision: str | None = '0028'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.models at this revision.
GMAIL_CONNECTION_STATUSES = ('CONNECTED', 'CONNECTION_REQUIRED', 'REVOKED', 'ERROR')
EMAIL_MATCH_STATUSES = ('MATCHED', 'NEEDS_REVIEW', 'UNMATCHED', 'IGNORED')
EMAIL_MATCH_METHODS = ('DETERMINISTIC', 'LLM_ASSISTED', 'USER', 'NONE')
EMAIL_ANALYSIS_STATUSES = ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'SKIPPED')
EMAIL_EVENT_TYPES = (
    'APPLICATION_CONFIRMATION', 'RECRUITER_RESPONSE', 'INTERVIEW_INVITATION', 'INTERVIEW_RESCHEDULE',
    'ASSESSMENT_INVITATION', 'ASSESSMENT_REMINDER', 'ADDITIONAL_INFORMATION_REQUEST', 'DOCUMENT_REQUEST',
    'FOLLOWUP_RESPONSE', 'APPLICATION_ON_HOLD', 'APPLICATION_REJECTION', 'OFFER', 'OFFER_UPDATE', 'BACKGROUND_CHECK',
    'ONBOARDING', 'GENERAL_RECRUITING_UPDATE', 'AUTOMATED_NOTIFICATION', 'UNKNOWN')
EMAIL_STAGE_DECISIONS = ('APPLIED', 'SUGGESTED', 'NONE', 'INVALID')
EMAIL_REVIEW_STATUSES = ('OPEN', 'ACCEPTED', 'DISMISSED')


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'),
                     nullable=False)


def _missing(table: str) -> bool:
    return table not in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _missing('gmail_connections'):
        _create_gmail_connections()
    if _missing('application_email_messages'):
        _create_application_email_messages()
    if _missing('application_communication_events'):
        _create_application_communication_events()
    if _missing('application_inbox_digests'):
        _create_application_inbox_digests()


def _create_gmail_connections() -> None:
    op.create_table(
        'gmail_connections',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('gmail_connections'),
        sa.Column('email_address', sa.String(length=320), nullable=False),
        sa.Column('refresh_token_enc', sa.Text(), nullable=True),
        sa.Column('scopes', sa.String(length=500), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('connected_at', sa.DateTime(), nullable=True),
        sa.Column('last_sync_at', sa.DateTime(), nullable=True),
        sa.Column('last_successful_sync_at', sa.DateTime(), nullable=True),
        sa.Column('history_id', sa.String(length=40), nullable=True),
        sa.Column('sync_locked_by', sa.String(length=200), nullable=True),
        sa.Column('sync_lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"status IN ({_values(GMAIL_CONNECTION_STATUSES)})", name='ck_gmail_connections_status'),
    )
    op.create_index('ix_gmail_connections_user_id', 'gmail_connections', ['user_id'], unique=True)


def _create_application_email_messages() -> None:
    op.create_table(
        'application_email_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('application_email_messages'),
        sa.Column('gmail_message_id', sa.String(length=64), nullable=False),
        sa.Column('gmail_thread_id', sa.String(length=64), nullable=False),
        sa.Column('sender', sa.String(length=500), nullable=False),
        sa.Column('sender_domain', sa.String(length=255), nullable=False),
        sa.Column('subject', sa.String(length=1000), nullable=False),
        sa.Column('received_at', sa.DateTime(), nullable=True),
        sa.Column('snippet', sa.Text(), nullable=False),
        sa.Column('relevant_text', sa.Text(), nullable=False),
        sa.Column('automated', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('application_id', sa.Integer(),
                  sa.ForeignKey('applications.id', ondelete='SET NULL',
                                name='fk_application_email_messages_application_id'), nullable=True),
        sa.Column('match_score', sa.Float(), nullable=True),
        sa.Column('match_signals', sa.JSON(), nullable=True),
        sa.Column('match_method', sa.String(length=20), nullable=False),
        sa.Column('match_status', sa.String(length=20), nullable=False),
        sa.Column('match_candidates', sa.JSON(), nullable=True),
        sa.Column('analysis_status', sa.String(length=20), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'gmail_message_id', name='uq_application_email_messages_user_message'),
        sa.CheckConstraint(f"match_status IN ({_values(EMAIL_MATCH_STATUSES)})",
                           name='ck_application_email_messages_match_status'),
        sa.CheckConstraint(f"match_method IN ({_values(EMAIL_MATCH_METHODS)})",
                           name='ck_application_email_messages_match_method'),
        sa.CheckConstraint(f"analysis_status IN ({_values(EMAIL_ANALYSIS_STATUSES)})",
                           name='ck_application_email_messages_analysis_status'),
    )
    op.create_index('ix_application_email_messages_user_id', 'application_email_messages', ['user_id'])
    op.create_index('ix_application_email_messages_application_id', 'application_email_messages', ['application_id'])
    op.create_index('ix_application_email_messages_analysis', 'application_email_messages',
                    ['analysis_status', 'next_attempt_at'])
    op.create_index('ix_application_email_messages_thread', 'application_email_messages',
                    ['user_id', 'gmail_thread_id'])


def _create_application_communication_events() -> None:
    op.create_table(
        'application_communication_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('application_communication_events'),
        sa.Column('application_id', sa.Integer(),
                  sa.ForeignKey('applications.id', ondelete='CASCADE',
                                name='fk_application_communication_events_application_id'), nullable=True),
        sa.Column('message_id', sa.Integer(),
                  sa.ForeignKey('application_email_messages.id', ondelete='SET NULL',
                                name='fk_application_communication_events_message_id'), nullable=True),
        sa.Column('gmail_message_id', sa.String(length=64), nullable=False),
        sa.Column('gmail_thread_id', sa.String(length=64), nullable=False),
        sa.Column('event_type', sa.String(length=40), nullable=False),
        sa.Column('sender', sa.String(length=500), nullable=False),
        sa.Column('subject', sa.String(length=1000), nullable=False),
        sa.Column('received_at', sa.DateTime(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('requires_action', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('action_items', sa.JSON(), nullable=True),
        sa.Column('deadline', sa.DateTime(), nullable=True),
        sa.Column('interview', sa.JSON(), nullable=True),
        sa.Column('assessment', sa.JSON(), nullable=True),
        sa.Column('recruiter', sa.JSON(), nullable=True),
        sa.Column('important_facts', sa.JSON(), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('classification_method', sa.String(length=10), nullable=False),
        sa.Column('detected_stage', sa.String(length=40), nullable=True),
        sa.Column('recommended_tracker_action', sa.String(length=60), nullable=True),
        sa.Column('stage_decision', sa.String(length=20), nullable=False),
        sa.Column('stage_from', sa.String(length=50), nullable=True),
        sa.Column('stage_to', sa.String(length=50), nullable=True),
        sa.Column('stage_reason', sa.String(length=300), nullable=True),
        sa.Column('needs_review', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('review_status', sa.String(length=20), nullable=False),
        sa.Column('user_corrected', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('corrections', sa.JSON(), nullable=True),
        sa.Column('prompt_version', sa.String(length=20), nullable=True),
        sa.Column('model', sa.String(length=200), nullable=True),
        sa.Column('application_event_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'gmail_message_id', name='uq_application_communication_events_user_message'),
        sa.CheckConstraint(f"event_type IN ({_values(EMAIL_EVENT_TYPES)})",
                           name='ck_application_communication_events_event_type'),
        sa.CheckConstraint(f"stage_decision IN ({_values(EMAIL_STAGE_DECISIONS)})",
                           name='ck_application_communication_events_stage_decision'),
        sa.CheckConstraint(f"review_status IN ({_values(EMAIL_REVIEW_STATUSES)})",
                           name='ck_application_communication_events_review_status'),
    )
    op.create_index('ix_application_communication_events_user_id', 'application_communication_events', ['user_id'])
    op.create_index('ix_application_communication_events_application_id', 'application_communication_events',
                    ['application_id'])
    op.create_index('ix_application_communication_events_user_received', 'application_communication_events',
                    ['user_id', 'received_at'])


def _create_application_inbox_digests() -> None:
    op.create_table(
        'application_inbox_digests',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('application_inbox_digests'),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('applications_checked', sa.Integer(), nullable=False),
        sa.Column('messages_scanned', sa.Integer(), nullable=False),
        sa.Column('messages_matched', sa.Integer(), nullable=False),
        sa.Column('new_updates', sa.Integer(), nullable=False),
        sa.Column('needs_action_count', sa.Integer(), nullable=False),
        sa.Column('interviews', sa.Integer(), nullable=False),
        sa.Column('assessments', sa.Integer(), nullable=False),
        sa.Column('rejections', sa.Integer(), nullable=False),
        sa.Column('offers', sa.Integer(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('sections', sa.JSON(), nullable=True),
        sa.Column('sync_runs', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'date', name='uq_application_inbox_digests_user_date'),
    )
    op.create_index('ix_application_inbox_digests_user_id', 'application_inbox_digests', ['user_id'])


def downgrade() -> None:
    op.drop_table('application_inbox_digests')
    op.drop_table('application_communication_events')
    op.drop_table('application_email_messages')
    op.drop_table('gmail_connections')
