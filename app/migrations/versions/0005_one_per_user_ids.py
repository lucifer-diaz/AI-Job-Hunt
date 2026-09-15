"""give candidate_profiles and app_settings auto-numbered ids on PostgreSQL

Databases created before migrations existed made both tables with a plain integer id (the model inserted
id=1), so there is no sequence behind it. 0004's setval() found no sequence and did nothing, and every
account after the first failed to get a profile or settings row. This adds the missing sequence.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-16 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ONE_PER_USER = ('candidate_profiles', 'app_settings')


def ensure_id_sequence(bind, table: str) -> None:
    """Back `table.id` with a sequence that starts after the highest existing id. Safe to run again."""
    sequence = bind.scalar(sa.text("SELECT pg_get_serial_sequence(:t, 'id')"), {'t': table})
    if sequence is None:
        sequence = f'{table}_id_seq'
        bind.execute(sa.text(f'CREATE SEQUENCE IF NOT EXISTS {sequence} OWNED BY {table}.id'))
        bind.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{sequence}')"))
    bind.execute(sa.text(f"SELECT setval(:s, COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"),
                 {'s': sequence})


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return  # SQLite numbers INTEGER PRIMARY KEY rows automatically
    for table in ONE_PER_USER:
        ensure_id_sequence(bind, table)


def downgrade() -> None:
    pass  # the sequence is harmless on the older schema
