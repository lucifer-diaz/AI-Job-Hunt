"""LLM monitoring: request telemetry, daily usage rollups, AI optimization tasks and reports, Gemini API keys

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-17 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copies of app.models at this revision.
LLM_STAGES = ('RANKER', 'TAILOR', 'REVIEWER', 'RESUME_PARSER', 'APPLICATION', 'FOLLOW_UP', 'INBOX', 'COMPANY_INSIGHTS',
              'ADVISOR', 'OTHER')
LLM_ERROR_TYPES = ('TIMEOUT', 'RATE_LIMIT', 'INVALID_JSON', 'SCHEMA_VALIDATION', 'PROVIDER_ERROR', 'CONTEXT_OVERFLOW',
                   'CONFIGURATION', 'OTHER')
TASK_STATUSES = ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')


def _values(values: Sequence[str]) -> str:
    return ', '.join(f"'{v}'" for v in values)


def upgrade() -> None:
    with op.batch_alter_table('app_settings') as batch:
        batch.add_column(sa.Column('gemini_api_key_enc', sa.Text(), nullable=True))

    op.create_table(
        'llm_request_logs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('request_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_llm_request_logs_user_id'),
                  nullable=True),
        sa.Column('workflow_type', sa.String(length=30), nullable=True),
        sa.Column('workflow_entity_id', sa.Integer(), nullable=True),
        sa.Column('stage', sa.String(length=30), nullable=False),
        sa.Column('provider', sa.String(length=30), nullable=False),
        sa.Column('requested_model', sa.String(length=300), nullable=True),
        sa.Column('model', sa.String(length=300), nullable=True),
        sa.Column('prompt_name', sa.String(length=60), nullable=True),
        sa.Column('prompt_version', sa.String(length=20), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
        sa.Column('usage_source', sa.String(length=20), nullable=False),
        sa.Column('estimated_context_tokens', sa.Integer(), nullable=True),
        sa.Column('context_window_size', sa.Integer(), nullable=True),
        sa.Column('context_utilization_percent', sa.Float(), nullable=True),
        sa.Column('context_breakdown', sa.JSON(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('estimated_cost', sa.Numeric(14, 8), nullable=True),
        sa.Column('cost_source', sa.String(length=20), nullable=False),
        sa.Column('pricing_effective_from', sa.Date(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('error_type', sa.String(length=30), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('structured_output_valid', sa.Boolean(), nullable=True),
        sa.Column('structured_output_retry_count', sa.Integer(), nullable=False),
        sa.Column('cached', sa.Boolean(), nullable=False),
        sa.Column('cache_read_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_write_tokens', sa.Integer(), nullable=True),
        sa.Column('debug_prompt', sa.Text(), nullable=True),
        sa.Column('debug_response', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"stage IN ({_values(LLM_STAGES)})", name='ck_llm_request_logs_stage'),
        sa.CheckConstraint("status IN ('SUCCEEDED', 'FAILED')", name='ck_llm_request_logs_status'),
        sa.CheckConstraint(f"error_type IN ({_values(LLM_ERROR_TYPES)})", name='ck_llm_request_logs_error_type'),
        sa.UniqueConstraint('request_id', name='uq_llm_request_logs_request_id'),
    )
    op.create_index('ix_llm_request_logs_user_created', 'llm_request_logs', ['user_id', 'created_at'])
    op.create_index('ix_llm_request_logs_stage_created', 'llm_request_logs', ['stage', 'created_at'])
    op.create_index('ix_llm_request_logs_model_created', 'llm_request_logs', ['model', 'created_at'])
    op.create_index('ix_llm_request_logs_created_at', 'llm_request_logs', ['created_at'])

    op.create_table(
        'llm_usage_daily',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_llm_usage_daily_user_id'),
                  nullable=True),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('stage', sa.String(length=30), nullable=False),
        sa.Column('provider', sa.String(length=30), nullable=False),
        sa.Column('model', sa.String(length=300), nullable=False),
        sa.Column('prompt_version', sa.String(length=20), nullable=False),
        *[sa.Column(name, sa.Integer(), nullable=False) for name in (
            'requests', 'failures', 'retries', 'structured_output_failures', 'input_tokens', 'output_tokens')],
        sa.Column('estimated_cost', sa.Numeric(14, 8), nullable=True),
        *[sa.Column(name, sa.Integer(), nullable=False) for name in (
            'unpriced_requests', 'latency_ms_sum', 'latency_ms_max', 'context_tokens_sum')],
        sa.UniqueConstraint('user_id', 'day', 'stage', 'provider', 'model', 'prompt_version',
                            name='uq_llm_usage_daily_key'),
    )
    op.create_index('ix_llm_usage_daily_user_day', 'llm_usage_daily', ['user_id', 'day'])

    op.create_table(
        'ai_optimization_reports',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_ai_optimization_reports_user_id'), nullable=False),
        sa.Column('time_range_start', sa.DateTime(), nullable=False),
        sa.Column('time_range_end', sa.DateTime(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        *[sa.Column(name, sa.JSON(), nullable=False) for name in (
            'cost_findings', 'latency_findings', 'context_findings', 'model_findings', 'prompt_findings',
            'reliability_findings', 'recommendations')],
        sa.Column('estimated_savings', sa.String(length=200), nullable=True),
        sa.Column('aggregates', sa.JSON(), nullable=False),
        sa.Column('generated_by', sa.String(length=10), nullable=False),
        sa.Column('model_provider', sa.String(length=30), nullable=True),
        sa.Column('model_name', sa.String(length=300), nullable=True),
        sa.Column('prompt_version', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_ai_optimization_reports_user_id', 'ai_optimization_reports', ['user_id'])

    op.create_table(
        'ai_optimization_tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_ai_optimization_tasks_user_id'), nullable=False),
        sa.Column('time_range_start', sa.DateTime(), nullable=False),
        sa.Column('time_range_end', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('trigger', sa.String(length=20), nullable=False),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('locked_by', sa.String(length=200), nullable=True),
        sa.Column('lock_token', sa.String(length=36), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('report_id', sa.Integer(),
                  sa.ForeignKey('ai_optimization_reports.id', ondelete='SET NULL',
                                name='fk_ai_optimization_tasks_report_id'), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({_values(TASK_STATUSES)})", name='ck_ai_optimization_tasks_status'),
    )
    op.create_index('ix_ai_optimization_tasks_user_id', 'ai_optimization_tasks', ['user_id'])
    op.create_index('ix_ai_optimization_tasks_claim', 'ai_optimization_tasks', ['status', 'next_attempt_at'])
    op.create_index('uq_ai_optimization_tasks_active_user', 'ai_optimization_tasks', ['user_id'], unique=True,
                    postgresql_where=sa.text("status IN ('PENDING', 'PROCESSING')"),
                    sqlite_where=sa.text("status IN ('PENDING', 'PROCESSING')"))


def downgrade() -> None:
    op.drop_table('ai_optimization_tasks')
    op.drop_table('ai_optimization_reports')
    op.drop_table('llm_usage_daily')
    op.drop_table('llm_request_logs')
    with op.batch_alter_table('app_settings') as batch:
        batch.drop_column('gemini_api_key_enc')
