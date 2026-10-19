"""Application-form questions on Job Insights

* job_insights.application_questions: the questions an application form is likely to ask (and the ones the person
  pasted), each with the person's library answer, a draft grounded in verified evidence, or NEEDS_INPUT.
* job_insights.application_form_input: the questions the person pasted from the real form.
* job_insight_tasks.scope: FULL research, or APPLICATION_FORM (only the drafts for newly pasted questions).

Additive; nothing existing changes. Downgrade drops the three columns.

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-19 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0050'
down_revision: str | None = '0049'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('job_insights') as batch:
        batch.add_column(sa.Column('application_questions', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('application_form_input', sa.JSON(), nullable=True))
    with op.batch_alter_table('job_insight_tasks') as batch:
        batch.add_column(sa.Column('scope', sa.String(20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('job_insight_tasks') as batch:
        batch.drop_column('scope')
    with op.batch_alter_table('job_insights') as batch:
        batch.drop_column('application_form_input')
        batch.drop_column('application_questions')
