"""Captured request/response bodies of external calls, and removing models from Job Hunt OS

* external_api_request_logs.payload: the redacted request and response, kept only on request and cleared after a week.
* ai_models.removed_at: a model a person removed (a sync leaves it removed; it can be restored).

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-10 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0041'
down_revision: str | None = '0040'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('external_api_request_logs') as batch:
        batch.add_column(sa.Column('payload', sa.JSON(), nullable=True))
    with op.batch_alter_table('ai_models') as batch:
        batch.add_column(sa.Column('removed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('ai_models') as batch:
        batch.drop_column('removed_at')
    with op.batch_alter_table('external_api_request_logs') as batch:
        batch.drop_column('payload')
