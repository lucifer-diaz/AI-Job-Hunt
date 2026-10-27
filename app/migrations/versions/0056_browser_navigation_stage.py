"""llm_request_logs.stage BROWSER_NAVIGATION, again

0055 allows the stage, but a database stamped 0055 by an earlier draft of it (before the stage was added) still has the
old check, and every Browser Navigator request row is then refused: the call happens, its telemetry is lost, and
Running workers shows "No model call" for the run. This re-creates the check with the same stages as 0055; on a
database that already has it, nothing changes.

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-26 21:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0056'
down_revision: str | None = '0055'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of app.models.LLM_STAGES at this revision (the same as 0055's NEW_STAGES).
STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'JD_PARSER', 'JD_RECOVERY', 'APPLICATION', 'FOLLOW_UP',
          'INBOX', 'COMPANY_INSIGHTS', 'INTERVIEW_INSIGHTS', 'ADVISOR', 'BROWSER_NAVIGATION', 'OTHER')


def upgrade() -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_constraint('ck_llm_request_logs_stage', type_='check')
        batch.create_check_constraint('ck_llm_request_logs_stage',
                                      "stage IN ({})".format(", ".join(repr(s) for s in STAGES)))


def downgrade() -> None:
    pass  # 0055 allows the same stages; its own downgrade removes BROWSER_NAVIGATION
