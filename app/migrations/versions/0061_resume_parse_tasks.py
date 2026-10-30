"""resume_parse_tasks: AI structuring of uploaded resumes, queued like JD parsing

* resume_parse_tasks: one AI structuring run per resume version (app.services.resume_parsing): the text hash and the
  structured-data revision it was queued against, its claim and lease, the graph thread that ran it, the grounded
  model output between steps (`staged`) and what grounding dropped (`counts`).

Additive; nothing existing changes. Downgrade drops the table.

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-30 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0061'
down_revision: str | None = '0060'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE = "status IN ('PENDING', 'PROCESSING')"


def upgrade() -> None:
    op.create_table(
        'resume_parse_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_resume_parse_tasks_user_id'), nullable=False),
        sa.Column('resume_id', sa.Integer(),
                  sa.ForeignKey('resumes.id', ondelete='CASCADE', name='fk_resume_parse_tasks_resume_id'),
                  nullable=False),
        sa.Column('raw_text_hash', sa.String(64), nullable=False),
        sa.Column('base_revision', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING'),
        sa.Column('reason', sa.String(40), nullable=False, server_default='upload'),
        sa.Column('step', sa.String(40), nullable=True),
        sa.Column('outcome', sa.String(20), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('error_category', sa.String(40), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('run_now_at', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(200), nullable=True),
        sa.Column('lock_token', sa.String(36), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('agent_thread_id', sa.Integer(),
                  sa.ForeignKey('agent_threads.id', ondelete='SET NULL', name='fk_resume_parse_tasks_agent_thread_id'),
                  nullable=True),
        sa.Column('staged', sa.JSON(), nullable=True),
        sa.Column('counts', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')",
                           name='ck_resume_parse_tasks_status'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_resume_parse_tasks_attempt_count'),
    )
    op.create_index('ix_resume_parse_tasks_user_id', 'resume_parse_tasks', ['user_id'])
    op.create_index('ix_resume_parse_tasks_resume_id', 'resume_parse_tasks', ['resume_id'])
    op.create_index('ix_resume_parse_tasks_claim', 'resume_parse_tasks', ['status', 'next_attempt_at'])
    op.create_index('uq_resume_parse_tasks_active_resume', 'resume_parse_tasks', ['resume_id'], unique=True,
                    postgresql_where=sa.text(ACTIVE), sqlite_where=sa.text(ACTIVE))


def downgrade() -> None:
    op.drop_index('uq_resume_parse_tasks_active_resume', table_name='resume_parse_tasks')
    op.drop_index('ix_resume_parse_tasks_claim', table_name='resume_parse_tasks')
    op.drop_index('ix_resume_parse_tasks_resume_id', table_name='resume_parse_tasks')
    op.drop_index('ix_resume_parse_tasks_user_id', table_name='resume_parse_tasks')
    op.drop_table('resume_parse_tasks')
