"""Approving a packet the reviewer scored below the account's minimum

The override is deliberate and recorded: `approval_override` says the minimum was waived, `approval_note`
keeps the reason. Existing approvals were all at or above the minimum, so the flag starts false.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-22 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0019'
down_revision: str | None = '0018'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('applications') as batch:
        batch.add_column(sa.Column('approval_override', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('approval_note', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('applications') as batch:
        batch.drop_column('approval_note')
        batch.drop_column('approval_override')
