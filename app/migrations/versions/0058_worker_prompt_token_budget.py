"""Worker model configs: prompt_token_budget, the tokens a worker's built prompt may use before its context is trimmed
(app.services.prompt_budget). Blank on every existing row: nothing is trimmed until a person sets it.

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-26 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0058'
down_revision: str | None = '0057'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('worker_model_configs') as batch:
        batch.add_column(sa.Column('prompt_token_budget', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('worker_model_configs') as batch:
        batch.drop_column('prompt_token_budget')
