"""applications.archived_at: when the person archived the application off the tracker board

Archiving (Tracker → Archive old…) puts an application out of sight without losing its stage: a live one is also
moved to Closed, a Rejected one stays Rejected. NULL means it is on the board.

Revision ID: 0064
Revises: 0063
Create Date: 2026-11-01 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0064'
down_revision: str | None = '0063'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('applications') as batch:
        batch.add_column(sa.Column('archived_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('applications') as batch:
        batch.drop_column('archived_at')
