"""Repair: re-apply what an install can have skipped when migrations 0034-0037 were renumbered, and 0038's widening

An install upgraded before the renumbering already stood at revision '0035' (then the tailor task options), so it
never ran the Job Insights '0035' (insight_observations.superseded_at, the INTERVIEW_INSIGHTS monitoring stage) and
every insights run failed on the missing column. An install that ran an early copy of 0038 kept a 30-character
provider_cooldowns.provider, too short for per-model LiteLLM cooldowns. Every step checks first, so on a database
that has all of it this revision changes nothing.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-10 15:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0039'
down_revision: str | None = '0038'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of app.models.LLM_STAGES as of revision 0035.
STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'JD_PARSER', 'JD_RECOVERY', 'APPLICATION', 'FOLLOW_UP',
          'INBOX', 'COMPANY_INSIGHTS', 'INTERVIEW_INSIGHTS', 'ADVISOR', 'OTHER')


def _columns(inspector: sa.Inspector, table: str) -> dict[str, dict]:
    return {c['name']: c for c in inspector.get_columns(table)}


def _stage_constraint_current(inspector: sa.Inspector) -> bool:
    for check in inspector.get_check_constraints('llm_request_logs'):
        if check.get('name') == 'ck_llm_request_logs_stage':
            return 'INTERVIEW_INSIGHTS' in (check.get('sqltext') or '')
    return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'superseded_at' not in _columns(inspector, 'insight_observations'):
        with op.batch_alter_table('insight_observations') as batch:
            batch.add_column(sa.Column('superseded_at', sa.DateTime(), nullable=True))
    if not _stage_constraint_current(inspector):
        values = ', '.join(f"'{v}'" for v in STAGES)
        with op.batch_alter_table('llm_request_logs') as batch:
            batch.drop_constraint('ck_llm_request_logs_stage', type_='check')
            batch.create_check_constraint('ck_llm_request_logs_stage', f"stage IN ({values})")
    op.execute(sa.text("UPDATE llm_request_logs SET stage = 'INTERVIEW_INSIGHTS' "
                       "WHERE prompt_name = 'interview_insights' AND stage = 'COMPANY_INSIGHTS'"))
    provider = _columns(sa.inspect(bind), 'provider_cooldowns').get('provider')
    if provider is not None and (getattr(provider['type'], 'length', None) or 230) < 230:
        with op.batch_alter_table('provider_cooldowns') as batch:
            batch.alter_column('provider', existing_type=sa.String(30), type_=sa.String(230), existing_nullable=False)


def downgrade() -> None:
    """A repair: going back leaves the repaired schema as 0035 and 0038 define it."""
