"""Jobs: posting_closed_at / posting_closed_reason, the label the listing check puts on an approved job whose posting
closed (it is kept, never deleted)

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-19 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0050'
down_revision: str | None = '0049'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('posting_closed_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('posting_closed_reason', sa.String(40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('posting_closed_reason')
        batch.drop_column('posting_closed_at')
