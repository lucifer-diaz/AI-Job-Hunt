"""Model fallbacks: each model's own ordered fallback models and how long it rests after a failure

ai_models.fallback_models (JSON list) and ai_models.rest_seconds (NULL follows the account's Reliability setting).

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-10 16:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0040'
down_revision: str | None = '0039'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('ai_models') as batch:
        batch.add_column(sa.Column('fallback_models', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('rest_seconds', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('ai_models') as batch:
        batch.drop_column('rest_seconds')
        batch.drop_column('fallback_models')
