"""AI model configuration: LiteLLM connection, model registry, tiers, worker assignments, routing, versions

Settings → AI Models keeps each account's desired AI configuration here, in PostgreSQL, instead of only in `.env`.
No configuration rows are created by this migration: each account's configuration is seeded from its saved stage
models and the RANK_MODEL / TAILOR_MODEL / … environment values the first time it is read
(app.services.ai_model_config.ensure_seeded), and recorded as version 1 with source MIGRATION, so nothing an
install already relies on is discarded. llm_request_logs gains the routing columns Monitoring shows; existing rows
keep NULL there (unknown), and `escalated` defaults to false.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-25 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0022'
down_revision: str | None = '0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AI_MODEL_TYPES = ("FAST", "BALANCED", "REASONING", "DEEP_REASONING", "EMBEDDING", "AUTO_ROUTER", "CUSTOM")
WORKER_MODEL_MODES = ("FIXED", "TIER", "AUTO_ROUTER")
REASONING_EFFORTS = ("low", "medium", "high")
LITELLM_CONNECTION_STATUSES = ("UNKNOWN", "CONNECTED", "DISCONNECTED", "AUTH_FAILED", "UNAVAILABLE")
LITELLM_SYNC_MODES = ("disabled", "admin_api", "config_file")
AI_SYNC_STATUSES = ("SYNCED", "PENDING", "APPLYING", "OUT_OF_SYNC", "FAILED")
AI_CONFIG_SOURCES = ("USER", "SYSTEM", "MIGRATION")
AUTO_ROUTER_CLASSIFIERS = ("DISABLED", "HEURISTIC", "HEURISTIC_V2", "LLM", "CUSTOM")
MODEL_HEALTH_STATUSES = ("UNKNOWN", "HEALTHY", "UNHEALTHY")
AI_AUDIT_RESULTS = ("SUCCEEDED", "FAILED", "REJECTED")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _owner(table: str) -> sa.Column:
    return sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name=f'fk_{table}_user_id'),
                     nullable=False)


def upgrade() -> None:
    op.create_table(
        'litellm_connections',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('litellm_connections'),
        sa.Column('base_url', sa.String(length=500), nullable=True),
        sa.Column('api_key_enc', sa.Text(), nullable=True),
        sa.Column('sync_mode', sa.String(length=20), nullable=False, server_default='disabled'),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='UNKNOWN'),
        sa.Column('litellm_version', sa.String(length=40), nullable=True),
        sa.Column('last_checked_at', sa.DateTime(), nullable=True),
        sa.Column('last_latency_ms', sa.Integer(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_discovery_at', sa.DateTime(), nullable=True),
        sa.Column('sync_status', sa.String(length=20), nullable=False, server_default='SYNCED'),
        sa.Column('last_sync_at', sa.DateTime(), nullable=True),
        sa.Column('last_sync_error', sa.Text(), nullable=True),
        sa.Column('applying_since', sa.DateTime(), nullable=True),
        sa.Column('draft_revision', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(_in('status', LITELLM_CONNECTION_STATUSES), name='ck_litellm_connections_status'),
        sa.CheckConstraint(_in('sync_status', AI_SYNC_STATUSES), name='ck_litellm_connections_sync_status'),
        sa.CheckConstraint(_in('sync_mode', LITELLM_SYNC_MODES), name='ck_litellm_connections_sync_mode'),
    )
    op.create_index('ix_litellm_connections_user_id', 'litellm_connections', ['user_id'], unique=True)

    op.create_table(
        'ai_models',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('ai_models'),
        sa.Column('display_name', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('litellm_model_name', sa.String(length=200), nullable=False),
        sa.Column('provider', sa.String(length=60), nullable=True),
        sa.Column('underlying_model', sa.String(length=300), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('model_type', sa.String(length=20), nullable=False, server_default='CUSTOM'),
        sa.Column('supports_structured_output', sa.Boolean(), nullable=True),
        sa.Column('supports_tools', sa.Boolean(), nullable=True),
        sa.Column('supports_reasoning', sa.Boolean(), nullable=True),
        sa.Column('supports_vision', sa.Boolean(), nullable=True),
        sa.Column('context_window', sa.Integer(), nullable=True),
        sa.Column('max_context_window', sa.Integer(), nullable=True),
        sa.Column('max_output_tokens', sa.Integer(), nullable=True),
        sa.Column('overrides', sa.JSON(), nullable=True),
        sa.Column('managed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('deployment_params', sa.JSON(), nullable=True),
        sa.Column('present_in_litellm', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('source', sa.String(length=20), nullable=False, server_default='DISCOVERED'),
        sa.Column('health_status', sa.String(length=20), nullable=False, server_default='UNKNOWN'),
        sa.Column('health_checked_at', sa.DateTime(), nullable=True),
        sa.Column('health_latency_ms', sa.Integer(), nullable=True),
        sa.Column('health_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('user_id', 'litellm_model_name', name='uq_ai_models_user_name'),
        sa.CheckConstraint(_in('model_type', AI_MODEL_TYPES), name='ck_ai_models_model_type'),
        sa.CheckConstraint(_in('health_status', MODEL_HEALTH_STATUSES), name='ck_ai_models_health_status'),
    )
    op.create_index('ix_ai_models_user_id', 'ai_models', ['user_id'])

    op.create_table(
        'model_tiers',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('model_tiers'),
        sa.Column('name', sa.String(length=60), nullable=False),
        sa.Column('description', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('litellm_model_name', sa.String(length=200), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'name', name='uq_model_tiers_user_name'),
    )
    op.create_index('ix_model_tiers_user_id', 'model_tiers', ['user_id'])

    op.create_table(
        'worker_model_configs',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('worker_model_configs'),
        sa.Column('worker_type', sa.String(length=40), nullable=False),
        sa.Column('model_mode', sa.String(length=20), nullable=False, server_default='TIER'),
        sa.Column('primary_model', sa.String(length=200), nullable=True),
        sa.Column('tier_name', sa.String(length=60), nullable=True),
        sa.Column('fallback_model', sa.String(length=200), nullable=True),
        sa.Column('temperature', sa.Float(), nullable=True),
        sa.Column('max_output_tokens', sa.Integer(), nullable=True),
        sa.Column('context_window', sa.Integer(), nullable=True),
        sa.Column('reasoning_effort', sa.String(length=10), nullable=True),
        sa.Column('timeout_seconds', sa.Integer(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=True),
        sa.Column('max_concurrency', sa.Integer(), nullable=True),
        sa.Column('use_structured_output', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('escalation', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'worker_type', name='uq_worker_model_configs_user_worker'),
        sa.CheckConstraint(_in('model_mode', WORKER_MODEL_MODES), name='ck_worker_model_configs_model_mode'),
        sa.CheckConstraint(_in('reasoning_effort', REASONING_EFFORTS), name='ck_worker_model_configs_reasoning_effort'),
    )
    op.create_index('ix_worker_model_configs_user_id', 'worker_model_configs', ['user_id'])

    op.create_table(
        'ai_routing_configs',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('ai_routing_configs'),
        sa.Column('auto_router_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('auto_router_model_name', sa.String(length=200), nullable=True),
        sa.Column('manage_auto_router', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('classifier', sa.String(length=20), nullable=False, server_default='HEURISTIC'),
        sa.Column('classifier_model', sa.String(length=200), nullable=True),
        sa.Column('classifier_plugin', sa.String(length=300), nullable=True),
        sa.Column('tier_models', sa.JSON(), nullable=True),
        sa.Column('default_model', sa.String(length=200), nullable=True),
        sa.Column('send_metadata', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(_in('classifier', AUTO_ROUTER_CLASSIFIERS), name='ck_ai_routing_configs_classifier'),
    )
    op.create_index('ix_ai_routing_configs_user_id', 'ai_routing_configs', ['user_id'], unique=True)

    op.create_table(
        'ai_config_versions',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('ai_config_versions'),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column('configuration_snapshot', sa.JSON(), nullable=False),
        sa.Column('changed_by', sa.String(length=320), nullable=False, server_default=''),
        sa.Column('source', sa.String(length=20), nullable=False, server_default='USER'),
        sa.Column('change_summary', sa.Text(), nullable=False, server_default=''),
        sa.Column('changes', sa.JSON(), nullable=False),
        sa.Column('sync_result', sa.JSON(), nullable=True),
        sa.Column('restored_from_version', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'version_number', name='uq_ai_config_versions_user_version'),
        sa.CheckConstraint(_in('source', AI_CONFIG_SOURCES), name='ck_ai_config_versions_source'),
    )
    op.create_index('ix_ai_config_versions_user_id', 'ai_config_versions', ['user_id'])

    op.create_table(
        'ai_config_audit',
        sa.Column('id', sa.Integer(), primary_key=True),
        _owner('ai_config_audit'),
        sa.Column('action', sa.String(length=30), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False, server_default='USER'),
        sa.Column('actor', sa.String(length=320), nullable=False, server_default=''),
        sa.Column('result', sa.String(length=20), nullable=False, server_default='SUCCEEDED'),
        sa.Column('version_number', sa.Integer(), nullable=True),
        sa.Column('changes', sa.JSON(), nullable=True),
        sa.Column('sync_result', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(_in('source', AI_CONFIG_SOURCES), name='ck_ai_config_audit_source'),
        sa.CheckConstraint(_in('result', AI_AUDIT_RESULTS), name='ck_ai_config_audit_result'),
    )
    op.create_index('ix_ai_config_audit_user_id', 'ai_config_audit', ['user_id'])
    op.create_index('ix_ai_config_audit_user_created', 'ai_config_audit', ['user_id', 'created_at'])

    with op.batch_alter_table('llm_request_logs') as batch:
        batch.add_column(sa.Column('worker_type', sa.String(length=40), nullable=True))
        batch.add_column(sa.Column('model_mode', sa.String(length=20), nullable=True))
        batch.add_column(sa.Column('configured_model', sa.String(length=200), nullable=True))
        batch.add_column(sa.Column('config_version', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('litellm_call_id', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('model_group', sa.String(length=200), nullable=True))
        batch.add_column(sa.Column('routing_tier', sa.String(length=30), nullable=True))
        batch.add_column(sa.Column('routing_cause', sa.String(length=60), nullable=True))
        batch.add_column(sa.Column('fallback_used', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('escalated', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('escalation_reason', sa.String(length=40), nullable=True))
        batch.add_column(sa.Column('confidence', sa.Float(), nullable=True))
    op.create_index('ix_llm_request_logs_litellm_call_id', 'llm_request_logs', ['litellm_call_id'])
    op.create_index('ix_llm_request_logs_worker_created', 'llm_request_logs', ['worker_type', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_llm_request_logs_worker_created', table_name='llm_request_logs')
    op.drop_index('ix_llm_request_logs_litellm_call_id', table_name='llm_request_logs')
    with op.batch_alter_table('llm_request_logs') as batch:
        for column in ('confidence', 'escalation_reason', 'escalated', 'fallback_used', 'routing_cause',
                       'routing_tier', 'model_group', 'litellm_call_id', 'config_version', 'configured_model',
                       'model_mode', 'worker_type'):
            batch.drop_column(column)
    op.drop_index('ix_ai_config_audit_user_created', table_name='ai_config_audit')
    op.drop_index('ix_ai_config_audit_user_id', table_name='ai_config_audit')
    op.drop_table('ai_config_audit')
    op.drop_index('ix_ai_config_versions_user_id', table_name='ai_config_versions')
    op.drop_table('ai_config_versions')
    op.drop_index('ix_ai_routing_configs_user_id', table_name='ai_routing_configs')
    op.drop_table('ai_routing_configs')
    op.drop_index('ix_worker_model_configs_user_id', table_name='worker_model_configs')
    op.drop_table('worker_model_configs')
    op.drop_index('ix_model_tiers_user_id', table_name='model_tiers')
    op.drop_table('model_tiers')
    op.drop_index('ix_ai_models_user_id', table_name='ai_models')
    op.drop_table('ai_models')
    op.drop_index('ix_litellm_connections_user_id', table_name='litellm_connections')
    op.drop_table('litellm_connections')
