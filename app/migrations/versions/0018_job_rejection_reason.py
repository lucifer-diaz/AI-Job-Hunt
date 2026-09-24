"""Why a job was turned down in the approval queue

Rejecting a job from Approvals dismisses it from the pipeline. The reason is optional — a job can be rejected
without one — so all three columns are nullable and nothing is backfilled.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-22 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0018'
down_revision: str | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('rejection_reason', sa.String(length=40), nullable=True))
        batch.add_column(sa.Column('rejection_note', sa.Text(), nullable=True))
        batch.add_column(sa.Column('rejected_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        for column in ('rejected_at', 'rejection_note', 'rejection_reason'):
            batch.drop_column(column)
