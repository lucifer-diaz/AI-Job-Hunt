"""Job Insights: keep superseded observations that kept sections still cite, and a monitoring stage for interview prep

* insight_observations.superseded_at: a refresh no longer deletes the rows a retained section cites.
* llm_request_logs.stage gains INTERVIEW_INSIGHTS, so interview preparation is measured apart from company research.
  Requests already recorded under COMPANY_INSIGHTS for the interview_insights prompt are re-labelled.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-08 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0035'
down_revision: str | None = '0034'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.models.LLM_STAGES before and after this revision.
OLD_STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'JD_PARSER', 'JD_RECOVERY', 'APPLICATION', 'FOLLOW_UP',
              'INBOX', 'COMPANY_INSIGHTS', 'ADVISOR', 'OTHER')
NEW_STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'JD_PARSER', 'JD_RECOVERY', 'APPLICATION', 'FOLLOW_UP',
              'INBOX', 'COMPANY_INSIGHTS', 'INTERVIEW_INSIGHTS', 'ADVISOR', 'OTHER')


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def _constraint(stages: Sequence[str]) -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_constraint('ck_llm_request_logs_stage', type_='check')
        batch.create_check_constraint('ck_llm_request_logs_stage', f"stage IN ({_values(stages)})")


def upgrade() -> None:
    with op.batch_alter_table('insight_observations') as batch:
        batch.add_column(sa.Column('superseded_at', sa.DateTime(), nullable=True))
    _constraint(NEW_STAGES)
    op.execute(sa.text("UPDATE llm_request_logs SET stage = 'INTERVIEW_INSIGHTS' "
                       "WHERE prompt_name = 'interview_insights' AND stage = 'COMPANY_INSIGHTS'"))


def downgrade() -> None:
    op.execute(sa.text("UPDATE llm_request_logs SET stage = 'COMPANY_INSIGHTS' WHERE stage = 'INTERVIEW_INSIGHTS'"))
    _constraint(OLD_STAGES)
    with op.batch_alter_table('insight_observations') as batch:
        batch.drop_column('superseded_at')
