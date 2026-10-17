"""Candidate knowledge retrieval (RAG): evidence ↔ resume tags, the embedding model's vector size, derived index

* ai_models.embedding_dimensions: the vector length of an EMBEDDING model (NULL = unknown).
* evidence_resume_links: which resume a piece of evidence belongs to (AUTO from verification, USER from a person;
  `excluded` keeps a person's removal of an AUTO link).
* rag_index_versions: one embedding index per account, model, vector size and document format.
* rag_index_entries: one source row (Evidence / KnowledgeAnswer) per index version, with its embedding work state.

The vectors themselves are kept in `rag_vectors`, created by app.services.vector_store.setup() when pgvector is
available (and as a JSON table on SQLite); Alembic does not manage it. Everything here is derived or additive:
downgrade drops it, and the Evidence / KnowledgeAnswer rows are never touched.

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-15 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0045'
down_revision: str | None = '0044'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORIGINS = ("AUTO", "USER")
COLLECTIONS = ("CANDIDATE_EVIDENCE", "ATS_ANSWER")
SOURCE_TYPES = ("evidence", "knowledge_answer")
INDEX_STATUSES = ("BUILDING", "SERVING", "RETIRED")
ENTRY_STATUSES = ("PENDING", "PROCESSING", "ACTIVE", "FAILED", "DEACTIVATED")


def _values(values: tuple[str, ...]) -> str:
    return ", ".join(repr(v) for v in values)


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'),
                     nullable=False)


def upgrade() -> None:
    with op.batch_alter_table('ai_models') as batch:
        batch.add_column(sa.Column('embedding_dimensions', sa.Integer(), nullable=True))

    op.create_table(
        'evidence_resume_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('evidence_resume_links'),
        sa.Column('evidence_id', sa.Integer(),
                  sa.ForeignKey('evidence.id', ondelete='CASCADE', name='fk_evidence_resume_links_evidence_id'),
                  nullable=False),
        sa.Column('resume_id', sa.Integer(),
                  sa.ForeignKey('resumes.id', ondelete='CASCADE', name='fk_evidence_resume_links_resume_id'),
                  nullable=False),
        sa.Column('origin', sa.String(10), nullable=False, server_default='AUTO'),
        sa.Column('excluded', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('evidence_id', 'resume_id', name='uq_evidence_resume_links_pair'),
        sa.CheckConstraint(f"origin IN ({_values(ORIGINS)})", name='ck_evidence_resume_links_origin'),
    )
    op.create_index('ix_evidence_resume_links_user_id', 'evidence_resume_links', ['user_id'])
    op.create_index('ix_evidence_resume_links_evidence_id', 'evidence_resume_links', ['evidence_id'])
    op.create_index('ix_evidence_resume_links_resume_id', 'evidence_resume_links', ['resume_id'])

    op.create_table(
        'rag_index_versions',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('rag_index_versions'),
        sa.Column('identity_hash', sa.String(64), nullable=False),
        sa.Column('provider', sa.String(30), nullable=False),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('underlying_model', sa.String(300), nullable=True),
        sa.Column('dimensions', sa.Integer(), nullable=False),
        sa.Column('doc_format_version', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='BUILDING'),
        sa.Column('eligible_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('embedded_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failed_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('serving_at', sa.DateTime(), nullable=True),
        sa.Column('retired_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('user_id', 'identity_hash', name='uq_rag_index_versions_identity'),
        sa.CheckConstraint(f"status IN ({_values(INDEX_STATUSES)})", name='ck_rag_index_versions_status'),
        sa.CheckConstraint('dimensions > 0', name='ck_rag_index_versions_dimensions'),
    )
    op.create_index('ix_rag_index_versions_user_id', 'rag_index_versions', ['user_id'])
    op.create_index('uq_rag_index_versions_serving', 'rag_index_versions', ['user_id'], unique=True,
                    postgresql_where=sa.text("status = 'SERVING'"), sqlite_where=sa.text("status = 'SERVING'"))

    op.create_table(
        'rag_index_entries',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('rag_index_entries'),
        sa.Column('index_version_id', sa.Integer(),
                  sa.ForeignKey('rag_index_versions.id', ondelete='CASCADE',
                                name='fk_rag_index_entries_index_version_id'), nullable=False),
        sa.Column('collection', sa.String(30), nullable=False),
        sa.Column('source_type', sa.String(30), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('verification_status', sa.String(20), nullable=False),
        sa.Column('sensitive', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('content_hash', sa.String(64), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('lock_token', sa.String(36), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('last_error', sa.String(300), nullable=True),
        sa.Column('embedded_at', sa.DateTime(), nullable=True),
        sa.Column('deactivated_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('index_version_id', 'source_type', 'source_id', name='uq_rag_index_entries_source'),
        sa.CheckConstraint(f"status IN ({_values(ENTRY_STATUSES)})", name='ck_rag_index_entries_status'),
        sa.CheckConstraint(f"collection IN ({_values(COLLECTIONS)})", name='ck_rag_index_entries_collection'),
        sa.CheckConstraint(f"source_type IN ({_values(SOURCE_TYPES)})", name='ck_rag_index_entries_source_type'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_rag_index_entries_attempt_count'),
    )
    op.create_index('ix_rag_index_entries_user_id', 'rag_index_entries', ['user_id'])
    op.create_index('ix_rag_index_entries_index_version_id', 'rag_index_entries', ['index_version_id'])
    op.create_index('ix_rag_index_entries_claim', 'rag_index_entries', ['status', 'next_attempt_at'])
    op.create_index('ix_rag_index_entries_lookup', 'rag_index_entries', ['user_id', 'source_type', 'source_id'])


def downgrade() -> None:
    # The runtime-managed vector table depends on rag_index_entries; it is derived data, so it goes too.
    op.execute('DROP TABLE IF EXISTS rag_vectors')
    op.drop_table('rag_index_entries')
    op.drop_index('uq_rag_index_versions_serving', table_name='rag_index_versions')
    op.drop_table('rag_index_versions')
    op.drop_table('evidence_resume_links')
    with op.batch_alter_table('ai_models') as batch:
        batch.drop_column('embedding_dimensions')
