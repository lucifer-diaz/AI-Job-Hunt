"""Jobs: a starred flag, so a person can mark jobs to come back to on the Approvals screen

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-18 22:45:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0049'
down_revision: str | None = '0048'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('starred', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('starred')
