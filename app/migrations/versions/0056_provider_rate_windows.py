"""Per-model token windows for proactive rate-limit pacing

* provider_rate_windows: what a provider last said about one account's per-minute token window for one model
  (`x-ratelimit-limit/remaining/reset-tokens`), plus its request counters and the window they count. Read on the way
  into a model request so one that would certainly be refused for the minute waits for the reset
  (app.services.token_pacing).

Additive; nothing existing changes. Downgrade drops the table (it is a cache of provider headers).

Revision ID: 0056
Revises: 0054
Create Date: 2026-10-26 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0056'
down_revision: str | None = '0054'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'provider_rate_windows',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_provider_rate_windows_user_id'),
                  nullable=False),
        sa.Column('provider', sa.String(230), nullable=False),
        sa.Column('tokens_limit', sa.Integer(), nullable=True),
        sa.Column('tokens_remaining', sa.Integer(), nullable=True),
        sa.Column('tokens_reset_at', sa.DateTime(), nullable=True),
        sa.Column('requests_limit', sa.Integer(), nullable=True),
        sa.Column('requests_remaining', sa.Integer(), nullable=True),
        sa.Column('requests_reset_at', sa.DateTime(), nullable=True),
        sa.Column('requests_window', sa.String(10), nullable=True),
        sa.Column('observed_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'provider', name='uq_provider_rate_windows_user_provider'),
    )
    op.create_index('ix_provider_rate_windows_user_id', 'provider_rate_windows', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_provider_rate_windows_user_id', table_name='provider_rate_windows')
    op.drop_table('provider_rate_windows')
