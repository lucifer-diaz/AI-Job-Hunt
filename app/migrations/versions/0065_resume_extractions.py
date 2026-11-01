"""resume_extractions, resume_extraction_feedback: how a resume version was read (RESUME_EXTRACTION_V2)

* resume_extractions: one per resume version read by app.services.resume_extraction: the reader, the quality report,
  the section map (each heading as written, its section and how it was placed) and the parse view the regex parser
  reads. Derived; Resume.raw_text is unchanged.
* resume_extraction_feedback: a person's decisions about how a resume was read (a heading assigned to a section, a
  correction accepted or rejected), per account.

Additive; nothing existing changes. A version read before this has no row, which means "unknown". Downgrade drops both.

Revision ID: 0065
Revises: 0064
Create Date: 2026-11-01 20:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0065'
down_revision: str | None = '0064'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'resume_extractions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_resume_extractions_user_id'), nullable=False),
        sa.Column('resume_id', sa.Integer(),
                  sa.ForeignKey('resumes.id', ondelete='CASCADE', name='fk_resume_extractions_resume_id'),
                  nullable=False, unique=True),
        sa.Column('pipeline_version', sa.String(20), nullable=False),
        sa.Column('source', sa.String(20), nullable=False),
        sa.Column('quality', sa.JSON(), nullable=False),
        sa.Column('sections', sa.JSON(), nullable=False),
        sa.Column('parse_view', sa.Text(), nullable=False),
        sa.Column('corrections', sa.JSON(), nullable=False),
        sa.Column('review', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_resume_extractions_user_id', 'resume_extractions', ['user_id'])
    op.create_table(
        'resume_extraction_feedback',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_resume_extraction_feedback_user_id'),
                  nullable=False),
        sa.Column('resume_id', sa.Integer(),
                  sa.ForeignKey('resumes.id', ondelete='SET NULL', name='fk_resume_extraction_feedback_resume_id'),
                  nullable=True),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('subject', sa.String(300), nullable=False),
        sa.Column('before', sa.JSON(), nullable=True),
        sa.Column('after', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_resume_extraction_feedback_user_id', 'resume_extraction_feedback', ['user_id'])
    op.create_index('ix_resume_extraction_feedback_user_kind', 'resume_extraction_feedback', ['user_id', 'kind'])


def downgrade() -> None:
    op.drop_index('ix_resume_extraction_feedback_user_kind', table_name='resume_extraction_feedback')
    op.drop_index('ix_resume_extraction_feedback_user_id', table_name='resume_extraction_feedback')
    op.drop_table('resume_extraction_feedback')
    op.drop_index('ix_resume_extractions_user_id', table_name='resume_extractions')
    op.drop_table('resume_extractions')
