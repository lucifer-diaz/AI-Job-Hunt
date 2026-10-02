"""Application Inbox: the progress of a Gmail sync, for the progress bar

gmail_connections.sync_progress holds where the running (or last) sync is — phase, counts and percent, never message
content. Existing rows stay NULL (no sync has reported progress yet).

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-01 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0030'
down_revision: str | None = '0029'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Already there on installs that ran this revision while it was numbered 0029.
    if 'sync_progress' in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('gmail_connections')}:
        return
    with op.batch_alter_table('gmail_connections') as batch:
        batch.add_column(sa.Column('sync_progress', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('gmail_connections') as batch:
        batch.drop_column('sync_progress')
