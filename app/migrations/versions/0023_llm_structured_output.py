"""LLM structured output telemetry: schema enforcement, validation failure reason, context builder version

Every LLM request now records how the worker's output contract (app.prompts.contracts) was enforced, why an answer
failed it (including POSSIBLE_CROSS_WORKER_OUTPUT: valid JSON in another worker's shape), and the version of the
context builder that prepared its input. Existing rows keep NULL (unknown): nothing is backfilled or guessed.
Generation provenance on rankings and follow-ups is migration 0024: this revision is left as installs applied it.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-25 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0023'
down_revision: str | None = '0022'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (("schema_enforcement", 20), ("validation_failure_reason", 40), ("context_builder_version", 20))


def upgrade() -> None:
    with op.batch_alter_table("llm_request_logs") as batch:
        for name, length in COLUMNS:
            batch.add_column(sa.Column(name, sa.String(length), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("llm_request_logs") as batch:
        for name, _length in reversed(COLUMNS):
            batch.drop_column(name)
