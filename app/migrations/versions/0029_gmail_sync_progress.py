"""Application Inbox: the progress of a Gmail sync, for the progress bar

gmail_connections.sync_progress holds where the running (or last) sync is — phase, counts and percent, never message
content. Existing rows stay NULL (no sync has reported progress yet).

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-01 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0029'
down_revision: str | None = '0028'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('gmail_connections') as batch:
        batch.add_column(sa.Column('sync_progress', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('gmail_connections') as batch:
        batch.drop_column('sync_progress')
