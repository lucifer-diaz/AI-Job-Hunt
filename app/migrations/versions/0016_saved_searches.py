"""Saved Discovery search setups: many named search profiles per account, each with its own schedule

The one profile accounts had until now lives in candidate_profiles.search_preferences; it is copied
into a setup named "My search" lazily, the first time the account reads its setups
(app.services.saved_searches.ensure_saved_searches), so this migration only makes room for it.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-20 10:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of app.models.SCHEDULE_KINDS at this revision.
SCHEDULE_KINDS = ('manual', 'interval', 'daily', 'weekly')


def upgrade() -> None:
    op.create_table(
        'saved_searches',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE', name='fk_saved_searches_user_id'), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('settings', sa.JSON(), nullable=False),
        sa.Column('schedule_kind', sa.String(length=20), nullable=False, server_default='manual'),
        sa.Column('every_hours', sa.Integer(), nullable=False, server_default='24'),
        sa.Column('time_of_day', sa.String(length=5), nullable=False, server_default='07:00'),
        sa.Column('weekdays', sa.JSON(), nullable=False),
        sa.Column('timezone', sa.String(length=60), nullable=False, server_default='UTC'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'name', name='uq_saved_searches_user_name'),
        sa.CheckConstraint(f"schedule_kind IN ({', '.join(repr(k) for k in SCHEDULE_KINDS)})",
                           name='ck_saved_searches_schedule_kind'),
    )
    op.create_index('ix_saved_searches_user_id', 'saved_searches', ['user_id'])
    op.create_index('ix_saved_searches_next_run_at', 'saved_searches', ['next_run_at'])

    with op.batch_alter_table('search_runs') as batch:
        batch.add_column(sa.Column('saved_search_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_search_runs_saved_search_id', 'saved_searches', ['saved_search_id'], ['id'],
                                 ondelete='SET NULL')
    op.create_index('ix_search_runs_saved_search_id', 'search_runs', ['saved_search_id'])


def downgrade() -> None:
    op.drop_index('ix_search_runs_saved_search_id', table_name='search_runs')
    with op.batch_alter_table('search_runs') as batch:
        batch.drop_constraint('fk_search_runs_saved_search_id', type_='foreignkey')
        batch.drop_column('saved_search_id')
    op.drop_index('ix_saved_searches_next_run_at', table_name='saved_searches')
    op.drop_index('ix_saved_searches_user_id', table_name='saved_searches')
    op.drop_table('saved_searches')
