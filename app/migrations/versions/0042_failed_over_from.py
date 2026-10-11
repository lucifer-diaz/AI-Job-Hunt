"""The model a failed-over LLM request was moved off

* llm_request_logs.failed_over_from: recorded with routing_cause FAILOVER, so a finished task shows where its request
  really came from instead of today's configuration.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-11 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0042'
down_revision: str | None = '0041'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.add_column(sa.Column('failed_over_from', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_column('failed_over_from')
