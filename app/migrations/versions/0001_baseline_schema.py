"""baseline schema (v0.5, before workflow state)

Matches the tables that `Base.metadata.create_all()` produced before Alembic was introduced.
Every table is created only if it is missing, so an existing pre-Alembic install is brought to
this baseline without touching its data.

Revision ID: 0001
Revises:
Create Date: 2026-09-14 17:56:30.504781
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _missing(table: str) -> bool:
    return not sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    if _missing('app_settings'):
        op.create_table('app_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('llm_provider', sa.String(length=30), nullable=False),
        sa.Column('model', sa.String(length=300), nullable=False),
        sa.Column('application_mode', sa.String(length=30), nullable=False),
        sa.Column('min_review_score', sa.Integer(), nullable=False),
        sa.Column('preferences', sa.JSON(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
    if _missing('candidate_profiles'):
        op.create_table('candidate_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('email', sa.String(length=300), nullable=False),
        sa.Column('phone', sa.String(length=100), nullable=False),
        sa.Column('location', sa.String(length=200), nullable=False),
        sa.Column('linkedin_url', sa.String(length=500), nullable=False),
        sa.Column('github_url', sa.String(length=500), nullable=False),
        sa.Column('headline', sa.String(length=500), nullable=False),
        sa.Column('target_roles', sa.JSON(), nullable=False),
        sa.Column('preferred_locations', sa.JSON(), nullable=False),
        sa.Column('work_authorization', sa.Text(), nullable=False),
        sa.Column('compensation_notes', sa.Text(), nullable=False),
        sa.Column('master_resume', sa.Text(), nullable=False),
        sa.Column('search_preferences', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
    if _missing('evidence'):
        op.create_table('evidence',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('company', sa.String(length=200), nullable=False),
        sa.Column('role', sa.String(length=200), nullable=False),
        sa.Column('claim', sa.Text(), nullable=False),
        sa.Column('context', sa.Text(), nullable=False),
        sa.Column('skills', sa.JSON(), nullable=False),
        sa.Column('tags', sa.JSON(), nullable=False),
        sa.Column('verified', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )
    if _missing('jobs'):
        op.create_table('jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=50), nullable=False),
        sa.Column('source_ref', sa.String(length=500), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('company', sa.String(length=300), nullable=False),
        sa.Column('location', sa.String(length=300), nullable=False),
        sa.Column('url', sa.String(length=1200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('raw', sa.JSON(), nullable=False),
        sa.Column('discovered_at', sa.DateTime(), nullable=False),
        sa.Column('fit_score', sa.Float(), nullable=True),
        sa.Column('fit_analysis', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('search_run_id', sa.Integer(), nullable=True),
        sa.Column('triaged_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'source_ref', name='uq_job_source_ref')
        )
        op.create_index(op.f('ix_jobs_search_run_id'), 'jobs', ['search_run_id'], unique=False)
    if _missing('resume_documents'):
        op.create_table('resume_documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('original_filename', sa.String(length=500), nullable=False),
        sa.Column('media_type', sa.String(length=150), nullable=False),
        sa.Column('storage_path', sa.String(length=1000), nullable=False),
        sa.Column('checksum', sa.String(length=64), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('extracted_text', sa.Text(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('checksum')
        )
    if _missing('search_runs'):
        op.create_table('search_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('external_run_id', sa.String(length=200), nullable=False),
        sa.Column('actor_id', sa.String(length=300), nullable=False),
        sa.Column('input_json', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('dataset_id', sa.String(length=200), nullable=False),
        sa.Column('item_count', sa.Integer(), nullable=False),
        sa.Column('created_jobs', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_search_runs_external_run_id'), 'search_runs', ['external_run_id'], unique=True)
    if _missing('knowledge_answers'):
        op.create_table('knowledge_answers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('question_key', sa.String(length=64), nullable=False),
        sa.Column('answer', sa.Text(), nullable=False),
        sa.Column('category', sa.String(length=80), nullable=False),
        sa.Column('answer_type', sa.String(length=40), nullable=False),
        sa.Column('verified', sa.Boolean(), nullable=False),
        sa.Column('sensitive', sa.Boolean(), nullable=False),
        sa.Column('source_job_id', sa.Integer(), nullable=True),
        sa.Column('use_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['source_job_id'], ['jobs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('question_key', name='uq_knowledge_question_key')
        )
        op.create_index(op.f('ix_knowledge_answers_question_key'), 'knowledge_answers', ['question_key'], unique=False)
    if _missing('resume_variants'):
        op.create_table('resume_variants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('content', sa.JSON(), nullable=False),
        sa.Column('markdown', sa.Text(), nullable=False),
        sa.Column('cover_letter', sa.Text(), nullable=False),
        sa.Column('reviewer_score', sa.Float(), nullable=True),
        sa.Column('reviewer_notes', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_resume_variants_job_id'), 'resume_variants', ['job_id'], unique=False)
    if _missing('applications'):
        op.create_table('applications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('approved_at', sa.DateTime(), nullable=True),
        sa.Column('applied_at', sa.DateTime(), nullable=True),
        sa.Column('followup_due_at', sa.DateTime(), nullable=True),
        sa.Column('recruiter_name', sa.String(length=200), nullable=False),
        sa.Column('recruiter_email', sa.String(length=300), nullable=False),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('approved_resume_id', sa.Integer(), nullable=True),
        sa.Column('stage', sa.String(length=50), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['approved_resume_id'], ['resume_variants.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id')
        )
    if _missing('application_events'):
        op.create_table('application_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('application_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=40), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('detail', sa.Text(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_application_events_application_id'), 'application_events', ['application_id'], unique=False)
    if _missing('followups'):
        op.create_table('followups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('application_id', sa.Integer(), nullable=False),
        sa.Column('due_at', sa.DateTime(), nullable=False),
        sa.Column('channel', sa.String(length=30), nullable=False),
        sa.Column('draft', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_followups_application_id'), 'followups', ['application_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_followups_application_id'), table_name='followups')
    op.drop_table('followups')
    op.drop_index(op.f('ix_application_events_application_id'), table_name='application_events')
    op.drop_table('application_events')
    op.drop_table('applications')
    op.drop_index(op.f('ix_resume_variants_job_id'), table_name='resume_variants')
    op.drop_table('resume_variants')
    op.drop_index(op.f('ix_knowledge_answers_question_key'), table_name='knowledge_answers')
    op.drop_table('knowledge_answers')
    op.drop_index(op.f('ix_search_runs_external_run_id'), table_name='search_runs')
    op.drop_table('search_runs')
    op.drop_table('resume_documents')
    op.drop_index(op.f('ix_jobs_search_run_id'), table_name='jobs')
    op.drop_table('jobs')
    op.drop_table('evidence')
    op.drop_table('candidate_profiles')
    op.drop_table('app_settings')
