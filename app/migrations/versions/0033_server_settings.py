"""Running workers: server-wide worker settings an admin can change without a restart

One row per setting (a scheduler interval, a batch size, a concurrency). A row overrides the environment value
(app.settings) until it is deleted; no row means the environment decides, exactly as before.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-04 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0033'
down_revision: str | None = '0032'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'server_settings',
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Integer(), nullable=False),
        sa.Column('updated_by', sa.String(length=200), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('name'),
    )


def downgrade() -> None:
    op.drop_table('server_settings')
