"""llm_request_logs.retrieval_ms / retrieval_mode: how long evidence retrieval took for the context a call was sent

RAG_RETRIEVAL_MODE=on measured retrieval (rag_retrieval.ContextPack.latency_ms) and then threw the figure away, so
Monitoring could not show it. The context builder now hands it to the call's telemetry like the other context
selection figures. NULL when the call's context had no retrieval (mode off, a fallback, a worker without evidence).

This migration was first numbered 0062, beside main's 0062_resume_domain_groups. An install that ran it under that
number stands at revision '0062' with these columns and without resumes.domains, so this step adds only what is
missing, and runs 0062 where its column is missing. On a database that has neither it is the plain migration.

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-29 14:00:00
"""
import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = '0063'
down_revision: str | None = '0062'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _migration(name: str):
    spec = importlib.util.spec_from_file_location(f"_repair_{name}", Path(__file__).parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if 'domains' not in {c['name'] for c in inspector.get_columns('resumes')}:
        _migration('0062_resume_domain_groups').upgrade()
    existing = {c['name'] for c in inspector.get_columns('llm_request_logs')}
    missing = [column for column in (sa.Column('retrieval_ms', sa.Integer(), nullable=True),
                                     sa.Column('retrieval_mode', sa.String(length=20), nullable=True))
               if column.name not in existing]
    if missing:
        with op.batch_alter_table('llm_request_logs') as batch:
            for column in missing:
                batch.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_column('retrieval_mode')
        batch.drop_column('retrieval_ms')
