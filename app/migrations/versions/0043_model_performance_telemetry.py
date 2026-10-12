"""Per-request performance facts for model comparison

* llm_request_logs: ttft_ms, generation_ms, queue_wait_ms, gateway_overhead_ms, reasoning_tokens, service_tier.
  All nullable: NULL means the provider did not report it and Job Hunt OS could not measure it.
* ix_llm_request_logs_config_created: grouping by configuration version.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-11 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0043'
down_revision: str | None = '0042'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (('ttft_ms', sa.Integer()), ('generation_ms', sa.Integer()), ('queue_wait_ms', sa.Integer()),
           ('gateway_overhead_ms', sa.Integer()), ('reasoning_tokens', sa.Integer()),
           ('service_tier', sa.String(length=30)))


def upgrade() -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        for name, kind in COLUMNS:
            batch.add_column(sa.Column(name, kind, nullable=True))
    op.create_index('ix_llm_request_logs_config_created', 'llm_request_logs', ['config_version', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_llm_request_logs_config_created', table_name='llm_request_logs')
    with op.batch_alter_table('llm_request_logs') as batch:
        for name, _ in reversed(COLUMNS):
            batch.drop_column(name)
