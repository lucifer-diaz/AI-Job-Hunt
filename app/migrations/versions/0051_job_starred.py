"""Jobs: a starred flag, so a person can mark jobs to come back to on the Approvals screen

First numbered 0049, alongside Phase 4's 0049, which left two heads; renumbered after it. An install that ran the old
0049 already has the column, so the step checks first.

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-18 22:45:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0051'
down_revision: str | None = '0050'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if 'starred' in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('jobs')}:
        return
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('starred', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('starred')
