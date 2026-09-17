"""Structured resume: versioned resumes with verification, change history, evidence provenance, context telemetry

Existing uploaded resumes and master resume text become resume versions. Their structured data is left empty
(the application parses it on first use) and nothing is marked verified: verification is always explicit.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-17 18:00:00
"""
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.models at this revision.
VERIFICATION_STATUSES = ('UNVERIFIED', 'PARTIALLY_VERIFIED', 'VERIFIED', 'NEEDS_REVIEW')
CHANGE_TYPES = ('ADD', 'UPDATE', 'REMOVE', 'REORDER', 'VERIFY', 'REJECT', 'UNVERIFY', 'PARSE', 'REPARSE_ACCEPT',
                'REPARSE_KEEP', 'VERIFY_RESUME')
CHANGE_SOURCES = ('PARSER', 'USER', 'SYSTEM')


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'),
                     nullable=False)


def _hash(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def upgrade() -> None:
    op.create_table(
        'resumes',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('resumes'),
        sa.Column('candidate_profile_id', sa.Integer(),
                  sa.ForeignKey('candidate_profiles.id', ondelete='CASCADE', name='fk_resumes_candidate_profile_id'),
                  nullable=False),
        sa.Column('resume_document_id', sa.Integer(),
                  sa.ForeignKey('resume_documents.id', ondelete='SET NULL', name='fk_resumes_resume_document_id'),
                  nullable=True),
        sa.Column('name', sa.String(length=300), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('source_filename', sa.String(length=500), nullable=True),
        sa.Column('source_media_type', sa.String(length=150), nullable=True),
        sa.Column('source_file_hash', sa.String(length=64), nullable=True),
        sa.Column('source_storage_path', sa.String(length=1000), nullable=True),
        sa.Column('raw_text_hash', sa.String(length=64), nullable=False),
        sa.Column('raw_text', sa.Text(), nullable=False),
        sa.Column('structured_data', sa.JSON(), nullable=True),
        sa.Column('parser', sa.String(length=20), nullable=True),
        sa.Column('parser_version', sa.String(length=20), nullable=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('verification_status', sa.String(length=20), nullable=False),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        sa.Column('verified_by', sa.Integer(), nullable=True),
        sa.Column('pending_reparse', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('candidate_profile_id', 'version', name='uq_resumes_profile_version'),
        sa.CheckConstraint(f"verification_status IN ({_values(VERIFICATION_STATUSES)})",
                           name='ck_resumes_verification_status'),
    )
    op.create_index('ix_resumes_user_id', 'resumes', ['user_id'])
    op.create_index('ix_resumes_candidate_profile_id', 'resumes', ['candidate_profile_id'])
    op.create_index('uq_resumes_active_profile', 'resumes', ['candidate_profile_id'], unique=True,
                    postgresql_where=sa.text('is_active IS TRUE'), sqlite_where=sa.text('is_active = 1'))
    op.create_index('uq_resumes_default_profile', 'resumes', ['candidate_profile_id'], unique=True,
                    postgresql_where=sa.text('is_default IS TRUE'), sqlite_where=sa.text('is_default = 1'))

    op.create_table(
        'resume_data_changes',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('resume_data_changes'),
        sa.Column('resume_id', sa.Integer(),
                  sa.ForeignKey('resumes.id', ondelete='CASCADE', name='fk_resume_data_changes_resume_id'), nullable=False),
        sa.Column('field_path', sa.String(length=300), nullable=False),
        sa.Column('item_id', sa.String(length=120), nullable=True),
        sa.Column('old_value', sa.JSON(), nullable=True),
        sa.Column('new_value', sa.JSON(), nullable=True),
        sa.Column('change_type', sa.String(length=20), nullable=False),
        sa.Column('source', sa.String(length=10), nullable=False),
        sa.Column('changed_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"change_type IN ({_values(CHANGE_TYPES)})", name='ck_resume_data_changes_change_type'),
        sa.CheckConstraint(f"source IN ({_values(CHANGE_SOURCES)})", name='ck_resume_data_changes_source'),
    )
    op.create_index('ix_resume_data_changes_user_id', 'resume_data_changes', ['user_id'])
    op.create_index('ix_resume_data_changes_resume_changed', 'resume_data_changes', ['resume_id', 'changed_at'])

    with op.batch_alter_table('evidence') as batch:
        batch.add_column(sa.Column('source', sa.String(length=20), nullable=False, server_default='manual'))
        batch.add_column(sa.Column('resume_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('structured_item_id', sa.String(length=120), nullable=True))
        batch.add_column(sa.Column('source_file_hash', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('fact_key', sa.String(length=64), nullable=True))
        batch.create_foreign_key('fk_evidence_resume_id', 'resumes', ['resume_id'], ['id'], ondelete='SET NULL')
        batch.create_index('ix_evidence_resume_id', ['resume_id'])
        batch.create_index('ix_evidence_fact_key', ['fact_key'])

    with op.batch_alter_table('llm_request_logs') as batch:
        batch.add_column(sa.Column('full_available_context_tokens', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('selected_context_tokens', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('context_reduction_percent', sa.Float(), nullable=True))

    _backfill(op.get_bind())


def _backfill(connection) -> None:
    """One resume version per uploaded file, plus the master text when it was edited by hand. Never VERIFIED."""
    now = datetime.now(UTC).replace(tzinfo=None)
    resumes = sa.table(
        'resumes', *[sa.column(name) for name in (
            'user_id', 'candidate_profile_id', 'resume_document_id', 'name', 'version', 'source', 'source_filename',
            'source_media_type', 'source_file_hash', 'source_storage_path', 'raw_text_hash', 'raw_text', 'revision',
            'is_active', 'is_default', 'verification_status', 'created_at', 'updated_at')])
    profiles = connection.execute(sa.text(
        'SELECT id, user_id, master_resume FROM candidate_profiles WHERE user_id IS NOT NULL ORDER BY id')).all()
    for profile in profiles:
        documents = connection.execute(sa.text(
            'SELECT id, original_filename, media_type, storage_path, checksum, extracted_text, active, created_at'
            ' FROM resume_documents WHERE user_id = :user ORDER BY id'), {'user': profile.user_id}).all()
        master = (profile.master_resume or '').strip()
        rows, active_index = [], None
        for document in documents:
            text = document.extracted_text or ''
            rows.append({'resume_document_id': document.id, 'name': document.original_filename, 'source': 'migration',
                         'source_filename': document.original_filename, 'source_media_type': document.media_type,
                         'source_file_hash': document.checksum, 'source_storage_path': document.storage_path,
                         'raw_text': text, 'created_at': document.created_at or now})
            if master and text.strip() == master or active_index is None and not master and document.active:
                active_index = len(rows) - 1
        if master and active_index is None:
            rows.append({'resume_document_id': None, 'name': 'Master resume text', 'source': 'manual_text',
                         'source_filename': None, 'source_media_type': None, 'source_file_hash': None,
                         'source_storage_path': None, 'raw_text': profile.master_resume, 'created_at': now})
            active_index = len(rows) - 1
        for index, row in enumerate(rows, start=1):
            chosen = index - 1 == active_index
            connection.execute(resumes.insert().values(
                user_id=profile.user_id, candidate_profile_id=profile.id, version=index,
                raw_text_hash=_hash(row['raw_text']), revision=0, is_active=chosen, is_default=chosen,
                verification_status='UNVERIFIED', updated_at=now, **row))


def downgrade() -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_column('context_reduction_percent')
        batch.drop_column('selected_context_tokens')
        batch.drop_column('full_available_context_tokens')
    with op.batch_alter_table('evidence') as batch:
        batch.drop_index('ix_evidence_fact_key')
        batch.drop_index('ix_evidence_resume_id')
        batch.drop_constraint('fk_evidence_resume_id', type_='foreignkey')
        batch.drop_column('fact_key')
        batch.drop_column('source_file_hash')
        batch.drop_column('structured_item_id')
        batch.drop_column('resume_id')
        batch.drop_column('source')
    op.drop_table('resume_data_changes')
    op.drop_table('resumes')
