"""The retrieval indexer in Running workers

* rag_index_entries.started_at / completed_at: when an entry's latest attempt started and when it last finished, so
  Running workers can list, time and chart it like every other queue.
* rag_index_entries.run_now_at: a person's Run now; the indexer claims it first, past its retry wait and the
  provider cooldown, and clears it.

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-16 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0046'
down_revision: str | None = '0045'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('rag_index_entries') as batch:
        batch.add_column(sa.Column('started_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('completed_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('run_now_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('rag_index_entries') as batch:
        batch.drop_column('run_now_at')
        batch.drop_column('completed_at')
        batch.drop_column('started_at')
