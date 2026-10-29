"""llm_request_logs.retrieval_ms / retrieval_mode: how long evidence retrieval took for the context a call was sent

RAG_RETRIEVAL_MODE=on measured retrieval (rag_retrieval.ContextPack.latency_ms) and then threw the figure away, so
Monitoring could not show it. The context builder now hands it to the call's telemetry like the other context
selection figures. NULL when the call's context had no retrieval (mode off, a fallback, a worker without evidence).

Revision ID: 0061
Revises: 0060
Create Date: 2026-10-29 14:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0061'
down_revision: str | None = '0060'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.add_column(sa.Column('retrieval_ms', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('retrieval_mode', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_column('retrieval_mode')
        batch.drop_column('retrieval_ms')
