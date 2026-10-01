"""Automatic tailoring guards: a durable per-job decision, and one active tailoring task per job

jobs.auto_tailor_status keeps automatic tailoring's decision for a job (QUEUED, DECLINED) when the person clears
finished tasks, so a cancelled or failed job is never queued again. Existing jobs stay NULL (undecided); automatic
tailoring only ever considers jobs ranked after it was turned on.

uq_tailor_tasks_active_job allows one queued or running task per job. A job that has more than one today keeps the
running one (else the oldest); the others are cancelled first, as "Cancel" would.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-01 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0028'
down_revision: str | None = '0027'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE = "status IN ('queued', 'running')"


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('auto_tailor_status', sa.String(length=10), nullable=True))
    # Keep one active task per job: a running task, else the oldest queued one.
    op.execute(sa.text(f"""
        UPDATE tailor_tasks SET status = 'cancelled', stage = 'done', finished_at = CURRENT_TIMESTAMP,
               error = 'Cancelled: another task for this job was already queued'
        WHERE {ACTIVE} AND id <> (
            SELECT keep.id FROM tailor_tasks AS keep
            WHERE keep.job_id = tailor_tasks.job_id AND keep.{ACTIVE}
            ORDER BY CASE keep.status WHEN 'running' THEN 0 ELSE 1 END, keep.id
            LIMIT 1)
    """))
    op.create_index('uq_tailor_tasks_active_job', 'tailor_tasks', ['job_id'], unique=True,
                    postgresql_where=sa.text(ACTIVE), sqlite_where=sa.text(ACTIVE))


def downgrade() -> None:
    op.drop_index('uq_tailor_tasks_active_job', table_name='tailor_tasks')
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('auto_tailor_status')
