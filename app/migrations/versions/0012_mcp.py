"""Claude Desktop MCP: conversation action audit trail and MCP access tokens

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-17 18:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of app.models at this revision.
RESULT_STATUSES = ('OK', 'PARTIAL', 'REJECTED', 'ERROR')


def upgrade() -> None:
    op.create_table(
        'conversation_actions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_conversation_actions_user_id'), nullable=False),
        sa.Column('source', sa.String(length=30), nullable=False),
        sa.Column('tool_name', sa.String(length=80), nullable=False),
        sa.Column('entity_type', sa.String(length=40), nullable=True),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('operation_id', sa.String(length=36), nullable=True),
        sa.Column('arguments_summary', sa.JSON(), nullable=False),
        sa.Column('result_status', sa.String(length=20), nullable=False),
        sa.Column('result_summary', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint(f"result_status IN ({', '.join(repr(s) for s in RESULT_STATUSES)})",
                           name='ck_conversation_actions_result_status'),
    )
    op.create_index('ix_conversation_actions_user_id', 'conversation_actions', ['user_id'])
    op.create_index('ix_conversation_actions_user_created', 'conversation_actions', ['user_id', 'created_at'])
    op.create_index('ix_conversation_actions_operation_id', 'conversation_actions', ['operation_id'])

    op.create_table(
        'mcp_tokens',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_mcp_tokens_user_id'), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('token_hash', name='uq_mcp_tokens_token_hash'),
    )
    op.create_index('ix_mcp_tokens_user_id', 'mcp_tokens', ['user_id'])


def downgrade() -> None:
    op.drop_table('mcp_tokens')
    op.drop_table('conversation_actions')
