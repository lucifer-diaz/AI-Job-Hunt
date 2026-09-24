"""External API monitoring: one telemetry row per third-party call, daily rollups and an operational log

Everything in this revision is written automatically by the instrumentation layer
(app.services.external_api); no existing row is changed. llm_request_logs gains a link to the generic
external-call row for the same request, and search_runs gains the usage figures Apify reports.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-21 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.models at this revision.
EXTERNAL_STATUSES = ('SUCCEEDED', 'FAILED')
EXTERNAL_ERROR_TYPES = ('TIMEOUT', 'CONNECTION', 'DNS', 'RATE_LIMIT', 'CLIENT_ERROR', 'SERVER_ERROR',
                        'INVALID_RESPONSE', 'RETRY_EXHAUSTED', 'CONFIGURATION', 'SDK_ERROR', 'OTHER')
SYSTEM_LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR')
SYSTEM_LOG_CATEGORIES = ('EXTERNAL_API', 'LLM', 'SCHEDULER', 'WORKFLOW', 'APIFY', 'APPLICATION', 'EMAIL', 'SYSTEM')


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        'external_api_request_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('request_id', sa.String(length=36), nullable=False),
        sa.Column('trace_id', sa.String(length=32), nullable=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_external_api_request_logs_user_id'),
                  nullable=True),
        sa.Column('provider', sa.String(length=30), nullable=False, server_default='OTHER'),
        sa.Column('service', sa.String(length=60), nullable=False, server_default=''),
        sa.Column('operation', sa.String(length=60), nullable=False, server_default=''),
        sa.Column('workflow_type', sa.String(length=30), nullable=True),
        sa.Column('workflow_entity_id', sa.Integer(), nullable=True),
        sa.Column('workflow', sa.String(length=60), nullable=True),
        sa.Column('job_id', sa.Integer(),
                  sa.ForeignKey('jobs.id', ondelete='SET NULL', name='fk_external_api_request_logs_job_id'), nullable=True),
        sa.Column('application_id', sa.Integer(),
                  sa.ForeignKey('applications.id', ondelete='SET NULL',
                                name='fk_external_api_request_logs_application_id'), nullable=True),
        sa.Column('http_method', sa.String(length=10), nullable=True),
        sa.Column('endpoint_host', sa.String(length=200), nullable=True),
        sa.Column('endpoint_path', sa.String(length=500), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='SUCCEEDED'),
        sa.Column('http_status_code', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('request_size_bytes', sa.Integer(), nullable=True),
        sa.Column('response_size_bytes', sa.Integer(), nullable=True),
        sa.Column('attempt', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('rate_limit_remaining', sa.Integer(), nullable=True),
        sa.Column('rate_limit_reset_at', sa.DateTime(), nullable=True),
        sa.Column('error_type', sa.String(length=30), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('estimated_cost', sa.Numeric(14, 8), nullable=True),
        sa.Column('cost_currency', sa.String(length=3), nullable=True),
        sa.Column('cost_source', sa.String(length=20), nullable=False, server_default='unknown'),
        sa.Column('request_metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"status IN ({_values(EXTERNAL_STATUSES)})", name='ck_external_api_request_logs_status'),
        sa.CheckConstraint(f"error_type IS NULL OR error_type IN ({_values(EXTERNAL_ERROR_TYPES)})",
                           name='ck_external_api_request_logs_error_type'),
        sa.UniqueConstraint('request_id', name='uq_external_api_request_logs_request_id'),
    )
    op.create_index('ix_external_api_request_logs_user_created', 'external_api_request_logs', ['user_id', 'created_at'])
    op.create_index('ix_external_api_request_logs_provider_created', 'external_api_request_logs', ['provider', 'created_at'])
    op.create_index('ix_external_api_request_logs_operation_created', 'external_api_request_logs',
                    ['service', 'operation', 'created_at'])
    op.create_index('ix_external_api_request_logs_status_created', 'external_api_request_logs', ['status', 'created_at'])
    op.create_index('ix_external_api_request_logs_created_at', 'external_api_request_logs', ['created_at'])
    op.create_index('ix_external_api_request_logs_trace_id', 'external_api_request_logs', ['trace_id'])
    op.create_index('ix_external_api_request_logs_workflow', 'external_api_request_logs',
                    ['workflow_type', 'workflow_entity_id'])
    op.create_index('ix_external_api_request_logs_job_id', 'external_api_request_logs', ['job_id'])
    op.create_index('ix_external_api_request_logs_application_id', 'external_api_request_logs', ['application_id'])

    op.create_table(
        'external_api_usage_daily',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_external_api_usage_daily_user_id'), nullable=True),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('provider', sa.String(length=30), nullable=False),
        sa.Column('service', sa.String(length=60), nullable=False, server_default=''),
        sa.Column('operation', sa.String(length=60), nullable=False, server_default=''),
        sa.Column('requests', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('retries', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('rate_limited', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('server_errors', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('timeouts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('latency_ms_sum', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('latency_ms_max', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('request_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('response_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('estimated_cost', sa.Numeric(14, 8), nullable=True),
        sa.Column('unpriced_requests', sa.Integer(), nullable=False, server_default='0'),
        sa.UniqueConstraint('user_id', 'day', 'provider', 'service', 'operation', name='uq_external_api_usage_daily_key'),
    )
    op.create_index('ix_external_api_usage_daily_user_day', 'external_api_usage_daily', ['user_id', 'day'])

    op.create_table(
        'system_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('level', sa.String(length=10), nullable=False, server_default='INFO'),
        sa.Column('category', sa.String(length=20), nullable=False, server_default='SYSTEM'),
        sa.Column('provider', sa.String(length=30), nullable=True),
        sa.Column('operation', sa.String(length=60), nullable=True),
        sa.Column('workflow', sa.String(length=60), nullable=True),
        sa.Column('trace_id', sa.String(length=32), nullable=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_system_logs_user_id'), nullable=True),
        sa.Column('entity_type', sa.String(length=30), nullable=True),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('message', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('status', sa.String(length=30), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('external_request_id', sa.String(length=36), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.CheckConstraint(f"level IN ({_values(SYSTEM_LOG_LEVELS)})", name='ck_system_logs_level'),
        sa.CheckConstraint(f"category IN ({_values(SYSTEM_LOG_CATEGORIES)})", name='ck_system_logs_category'),
    )
    for name, columns in (('ix_system_logs_user_created', ['user_id', 'created_at']),
                          ('ix_system_logs_category_created', ['category', 'created_at']),
                          ('ix_system_logs_level_created', ['level', 'created_at']),
                          ('ix_system_logs_created_at', ['created_at']),
                          ('ix_system_logs_trace_id', ['trace_id']),
                          ('ix_system_logs_external_request_id', ['external_request_id'])):
        op.create_index(name, 'system_logs', columns)

    with op.batch_alter_table('llm_request_logs') as batch:
        batch.add_column(sa.Column('external_request_id', sa.String(length=36), nullable=True))
        batch.add_column(sa.Column('trace_id', sa.String(length=32), nullable=True))
    op.create_index('ix_llm_request_logs_external_request_id', 'llm_request_logs', ['external_request_id'])
    op.create_index('ix_llm_request_logs_trace_id', 'llm_request_logs', ['trace_id'])

    with op.batch_alter_table('search_runs') as batch:
        batch.add_column(sa.Column('compute_units', sa.Float(), nullable=True))
        batch.add_column(sa.Column('usage_total_usd', sa.Numeric(14, 8), nullable=True))
        batch.add_column(sa.Column('memory_mb', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('dataset_item_count', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('search_runs') as batch:
        for column in ('dataset_item_count', 'memory_mb', 'usage_total_usd', 'compute_units'):
            batch.drop_column(column)
    op.drop_index('ix_llm_request_logs_trace_id', table_name='llm_request_logs')
    op.drop_index('ix_llm_request_logs_external_request_id', table_name='llm_request_logs')
    with op.batch_alter_table('llm_request_logs') as batch:
        batch.drop_column('trace_id')
        batch.drop_column('external_request_id')
    op.drop_table('system_logs')
    op.drop_table('external_api_usage_daily')
    op.drop_table('external_api_request_logs')
