"""LLM monitoring: job description parsing and recovery get their own stages

Model calls for the JD parser and JD recovery were recorded under OTHER, so Monitoring could not show them. They are
now JD_PARSER and JD_RECOVERY; existing request rows are re-labelled from their prompt name. Daily roll-ups made
before this revision stay under OTHER (they do not keep the prompt name).

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-01 19:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0031'
down_revision: str | None = '0030'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.models.LLM_STAGES before and after this revision.
OLD_STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'APPLICATION', 'FOLLOW_UP', 'INBOX', 'COMPANY_INSIGHTS',
              'ADVISOR', 'OTHER')
NEW_STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'JD_PARSER', 'JD_RECOVERY', 'APPLICATION', 'FOLLOW_UP',
              'INBOX', 'COMPANY_INSIGHTS', 'ADVISOR', 'OTHER')
RELABEL = (('job_description_parser', 'JD_PARSER'), ('job_description_recovery', 'JD_RECOVERY'))


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def _constraint(stages: Sequence[str]) -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_constraint('ck_llm_request_logs_stage', type_='check')
        batch.create_check_constraint('ck_llm_request_logs_stage', f"stage IN ({_values(stages)})")


def upgrade() -> None:
    _constraint(NEW_STAGES)
    for prompt, stage in RELABEL:
        op.execute(sa.text("UPDATE llm_request_logs SET stage = :stage WHERE prompt_name = :prompt AND stage = 'OTHER'")
                   .bindparams(stage=stage, prompt=prompt))


def downgrade() -> None:
    op.execute(sa.text("UPDATE llm_request_logs SET stage = 'OTHER' WHERE stage IN ('JD_PARSER', 'JD_RECOVERY')"))
    _constraint(OLD_STAGES)
