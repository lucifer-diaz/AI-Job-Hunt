"""How long to leave a provider alone after it rate limits an account

One row per account and provider, written only when a provider answers 429 and cleared on the next success
(app.services.backoff). Nothing is backfilled: no cooldown means no cooldown.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-22 14:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0020'
down_revision: str | None = '0019'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'provider_cooldowns',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_provider_cooldowns_user_id'), nullable=False),
        sa.Column('provider', sa.String(length=30), nullable=False),
        sa.Column('until', sa.DateTime(), nullable=True),
        sa.Column('consecutive', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('calls_skipped', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_limited_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'provider', name='uq_provider_cooldowns_user_provider'),
    )
    op.create_index('ix_provider_cooldowns_user_id', 'provider_cooldowns', ['user_id'])
    op.create_index('ix_provider_cooldowns_until', 'provider_cooldowns', ['until'])


def downgrade() -> None:
    op.drop_index('ix_provider_cooldowns_until', table_name='provider_cooldowns')
    op.drop_index('ix_provider_cooldowns_user_id', table_name='provider_cooldowns')
    op.drop_table('provider_cooldowns')
