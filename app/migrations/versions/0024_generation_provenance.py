"""Generation provenance: the AI config version of a ranking, and what produced a follow-up draft

job_rankings.config_version records the AI configuration version a ranking ran under; followups.generation records
the prompt version, provider, model and config version behind a draft. Existing rows keep NULL (unknown).

These columns were briefly part of 0023 after some installs had already applied it, so each is added only when it
is missing: an install at either form of 0023 upgrades to the same schema.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-26 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0024'
down_revision: str | None = '0023'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (("job_rankings", "config_version", sa.Integer), ("followups", "generation", sa.JSON))


def _has(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for table, column, kind in COLUMNS:
        if not _has(table, column):
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column(column, kind(), nullable=True))


def downgrade() -> None:
    for table, column, _kind in reversed(COLUMNS):
        if _has(table, column):
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)
