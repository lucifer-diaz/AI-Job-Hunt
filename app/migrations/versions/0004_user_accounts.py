"""user accounts, sign-in sessions, per-user ownership and encrypted API keys

Existing rows keep user_id NULL. The first account that signs up takes ownership of them
(app.auth.claim_existing_data), so a single-user install carries on unchanged.

Uniqueness that used to be global becomes per user: two users may import the same job posting, save
the same question, upload the same resume file or track the same Apify run.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-16 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OWNED_TABLES = ('candidate_profiles', 'app_settings', 'resume_documents', 'knowledge_answers', 'evidence', 'jobs',
                'search_runs')
ONE_PER_USER = ('candidate_profiles', 'app_settings')
KEY_COLUMNS = ('openrouter_api_key_enc', 'openai_api_key_enc', 'anthropic_api_key_enc', 'apify_api_token_enc')
SQLITE_UNIQUE_NAMES = {'uq': 'uq_%(table_name)s_%(column_0_name)s'}


def _utc_now():
    if op.get_bind().dialect.name == 'postgresql':
        return sa.text("(now() at time zone 'utc')")
    return sa.text('CURRENT_TIMESTAMP')


def _unnamed_checksum_unique_names(bind) -> list[str]:
    return [c['name'] for c in sa.inspect(bind).get_unique_constraints('resume_documents')
            if c['column_names'] == ['checksum'] and c['name']]


def upgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == 'postgresql'

    op.create_table('users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('password_hash', sa.String(length=300), nullable=False),
        sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=_utc_now()),
        sa.Column('last_login_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email', name='uq_users_email'),
    )
    op.create_table('user_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=_utc_now()),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False, server_default=_utc_now()),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE', name='fk_user_sessions_user_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash', name='uq_user_sessions_token_hash'),
    )
    op.create_index('ix_user_sessions_user_id', 'user_sessions', ['user_id'], unique=False)

    for table in OWNED_TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
            batch.create_foreign_key(f'fk_{table}_user_id', 'users', ['user_id'], ['id'], ondelete='CASCADE')
        op.create_index(f'ix_{table}_user_id', table, ['user_id'], unique=table in ONE_PER_USER)

    with op.batch_alter_table('app_settings') as batch:
        for column in KEY_COLUMNS:
            batch.add_column(sa.Column(column, sa.Text(), nullable=True))

    with op.batch_alter_table('jobs') as batch:
        batch.drop_constraint('uq_job_source_ref', type_='unique')
        batch.create_unique_constraint('uq_jobs_user_source_ref', ['user_id', 'source', 'source_ref'])
    with op.batch_alter_table('knowledge_answers') as batch:
        batch.drop_constraint('uq_knowledge_question_key', type_='unique')
        batch.create_unique_constraint('uq_knowledge_user_question_key', ['user_id', 'question_key'])

    if postgres:
        for name in _unnamed_checksum_unique_names(bind):
            op.drop_constraint(name, 'resume_documents', type_='unique')
        op.create_unique_constraint('uq_resume_documents_user_checksum', 'resume_documents', ['user_id', 'checksum'])
    else:  # SQLite reflects the baseline's unnamed constraint; name it so batch mode can drop it
        with op.batch_alter_table('resume_documents', naming_convention=SQLITE_UNIQUE_NAMES) as batch:
            batch.drop_constraint('uq_resume_documents_checksum', type_='unique')
            batch.create_unique_constraint('uq_resume_documents_user_checksum', ['user_id', 'checksum'])

    op.drop_index('ix_search_runs_external_run_id', table_name='search_runs')
    op.create_index('ix_search_runs_external_run_id', 'search_runs', ['external_run_id'], unique=False)
    with op.batch_alter_table('search_runs') as batch:
        batch.create_unique_constraint('uq_search_runs_user_external_run_id', ['user_id', 'external_run_id'])

    if postgres:
        # These tables used to hold a single row inserted with id=1, which never advanced their sequences.
        for table in ONE_PER_USER:
            op.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                       f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")


def downgrade() -> None:
    """Restores the single-user schema. Fails if two users now own rows that would collide."""
    postgres = op.get_bind().dialect.name == 'postgresql'
    with op.batch_alter_table('search_runs') as batch:
        batch.drop_constraint('uq_search_runs_user_external_run_id', type_='unique')
    op.drop_index('ix_search_runs_external_run_id', table_name='search_runs')
    op.create_index('ix_search_runs_external_run_id', 'search_runs', ['external_run_id'], unique=True)

    with op.batch_alter_table('resume_documents') as batch:
        batch.drop_constraint('uq_resume_documents_user_checksum', type_='unique')
        batch.create_unique_constraint('resume_documents_checksum_key' if postgres else 'uq_resume_documents_checksum',
                                       ['checksum'])
    with op.batch_alter_table('knowledge_answers') as batch:
        batch.drop_constraint('uq_knowledge_user_question_key', type_='unique')
        batch.create_unique_constraint('uq_knowledge_question_key', ['question_key'])
    with op.batch_alter_table('jobs') as batch:
        batch.drop_constraint('uq_jobs_user_source_ref', type_='unique')
        batch.create_unique_constraint('uq_job_source_ref', ['source', 'source_ref'])

    with op.batch_alter_table('app_settings') as batch:
        for column in reversed(KEY_COLUMNS):
            batch.drop_column(column)
    for table in reversed(OWNED_TABLES):
        op.drop_index(f'ix_{table}_user_id', table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f'fk_{table}_user_id', type_='foreignkey')
            batch.drop_column('user_id')

    op.drop_index('ix_user_sessions_user_id', table_name='user_sessions')
    op.drop_table('user_sessions')
    op.drop_table('users')
