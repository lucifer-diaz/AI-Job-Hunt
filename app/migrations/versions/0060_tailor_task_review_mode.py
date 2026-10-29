"""tailor_tasks.mode 'review': review the latest saved packet without tailoring it again

A packet is saved before the Reviewer runs, so a review that failed (a model error, a rate limit, a restart) leaves a
tailored version with no review: it cannot reach Approvals, and only a new tailoring pass used to fix it. A review-only
task runs the Reviewer on that version through the same tailor worker.

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-29 10:00:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = '0060'
down_revision: str | None = '0059'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('tailor_tasks') as batch:
        batch.drop_constraint('ck_tailor_tasks_mode', type_='check')
        batch.create_check_constraint('ck_tailor_tasks_mode', "mode IN ('tailor', 'skip', 'review')")


def downgrade() -> None:
    op.execute("DELETE FROM tailor_tasks WHERE mode = 'review'")
    with op.batch_alter_table('tailor_tasks') as batch:
        batch.drop_constraint('ck_tailor_tasks_mode', type_='check')
        batch.create_check_constraint('ck_tailor_tasks_mode', "mode IN ('tailor', 'skip')")
