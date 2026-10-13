"""Agent graphs (LangGraph pilot): thread ownership and leases, and once-only side effects

* agent_threads: one LangGraph thread per row: its owner, server-built thread id, status and lease. The graph's own
  checkpoints are kept by LangGraph's PostgresSaver in its own tables (created by `setup()`, not by Alembic).
* agent_effects: a side effect a graph made, unique per thread and key, so a node replayed after a crash finds it.

Both are additive; nothing existing changes. Downgrade drops them (and any graph history they held).

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-13 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0044'
down_revision: str | None = '0043'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = ("QUEUED", "RUNNING", "INTERRUPTED", "COMPLETED", "FAILED", "STOPPED")


def upgrade() -> None:
    op.create_table(
        'agent_threads',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_agent_threads_user_id'), nullable=False),
        sa.Column('thread_id', sa.String(200), nullable=False),
        sa.Column('graph', sa.String(60), nullable=False),
        sa.Column('graph_version', sa.String(20), nullable=False),
        sa.Column('subject', sa.String(64), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='QUEUED'),
        sa.Column('input', sa.JSON(), nullable=True),
        sa.Column('interrupt', sa.JSON(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('trace_id', sa.String(64), nullable=True),
        sa.Column('locked_by', sa.String(200), nullable=True),
        sa.Column('lock_token', sa.String(36), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('checkpoint_pruned_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('thread_id', name='uq_agent_threads_thread_id'),
        sa.UniqueConstraint('user_id', 'graph', 'subject', name='uq_agent_threads_subject'),
        sa.CheckConstraint(f"status IN ({', '.join(repr(s) for s in STATUSES)})", name='ck_agent_threads_status'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_agent_threads_attempt_count'),
    )
    op.create_index('ix_agent_threads_user_id', 'agent_threads', ['user_id'])
    op.create_index('ix_agent_threads_claim', 'agent_threads', ['status', 'lock_expires_at'])
    op.create_table(
        'agent_effects',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_agent_effects_user_id'), nullable=False),
        sa.Column('agent_thread_id', sa.Integer(),
                  sa.ForeignKey('agent_threads.id', ondelete='CASCADE', name='fk_agent_effects_agent_thread_id'),
                  nullable=False),
        sa.Column('effect_key', sa.String(120), nullable=False),
        sa.Column('summary', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('agent_thread_id', 'effect_key', name='uq_agent_effects_key'),
    )
    op.create_index('ix_agent_effects_user_id', 'agent_effects', ['user_id'])
    op.create_index('ix_agent_effects_agent_thread_id', 'agent_effects', ['agent_thread_id'])


def downgrade() -> None:
    op.drop_table('agent_effects')
    op.drop_table('agent_threads')
