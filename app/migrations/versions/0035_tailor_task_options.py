"""Tailoring tasks remember how to tailor their job: {"pages": 1 | 2 | 3} (app.services.resume_length)

NULL means the account's default length, read when the task runs, exactly as tasks behaved before.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-08 14:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0035'
down_revision: str | None = '0034'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('tailor_tasks') as batch:
        batch.add_column(sa.Column('options', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tailor_tasks') as batch:
        batch.drop_column('options')
