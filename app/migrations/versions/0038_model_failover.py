"""Model failover: per-route health overlay, failover events and a worker's secondary models

model_route_states is the runtime health of a model (overall or per worker); failover_events is the notification feed;
worker_model_configs.secondary_models holds the ordered models tried after the fallback model.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-10 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0038'
down_revision: str | None = '0037'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('provider_cooldowns') as batch:  # LITELLM:<model> cooldowns are kept per model
        batch.alter_column('provider', existing_type=sa.String(30), type_=sa.String(230), existing_nullable=False)
    with op.batch_alter_table('worker_model_configs') as batch:
        batch.add_column(sa.Column('secondary_models', sa.JSON(), nullable=True))
    op.create_table(
        'model_route_states',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_model_route_states_user_id'), nullable=False),
        sa.Column('worker_type', sa.String(40), nullable=False, server_default=''),
        sa.Column('model', sa.String(200), nullable=False),
        sa.Column('state', sa.String(12), nullable=False, server_default='HEALTHY'),
        sa.Column('score', sa.Float(), nullable=True),
        sa.Column('stats', sa.JSON(), nullable=True),
        sa.Column('reason', sa.String(300), nullable=True),
        sa.Column('cooling_until', sa.DateTime(), nullable=True),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('probe_successes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_changed_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'worker_type', 'model', name='uq_model_route_states_route'),
        sa.CheckConstraint("state IN ('HEALTHY', 'DEGRADED', 'COOLING')", name='ck_model_route_states_state'),
    )
    op.create_index('ix_model_route_states_user_id', 'model_route_states', ['user_id'])
    op.create_table(
        'failover_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_failover_events_user_id'), nullable=False),
        sa.Column('worker_type', sa.String(40), nullable=False, server_default=''),
        sa.Column('from_model', sa.String(200), nullable=False),
        sa.Column('to_model', sa.String(200), nullable=True),
        sa.Column('cause', sa.String(16), nullable=False),
        sa.Column('algorithm', sa.String(20), nullable=True),
        sa.Column('message', sa.String(400), nullable=False),
        sa.Column('metrics', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "cause IN ('RATE_LIMIT', 'ERROR_RATE', 'LATENCY', 'SCHEMA', 'MANUAL', 'RECOVERED')",
            name='ck_failover_events_cause'),
    )
    op.create_index('ix_failover_events_user_id', 'failover_events', ['user_id'])
    op.create_index('ix_failover_events_user_created', 'failover_events', ['user_id', 'created_at'])


def downgrade() -> None:
    op.execute("DELETE FROM provider_cooldowns WHERE provider LIKE 'LITELLM:%'")
    with op.batch_alter_table('provider_cooldowns') as batch:
        batch.alter_column('provider', existing_type=sa.String(230), type_=sa.String(30), existing_nullable=False)
    op.drop_table('failover_events')
    op.drop_table('model_route_states')
    with op.batch_alter_table('worker_model_configs') as batch:
        batch.drop_column('secondary_models')
