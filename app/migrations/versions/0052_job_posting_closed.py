"""Jobs: posting_closed_at / posting_closed_reason, the label the listing check puts on an approved job whose posting
closed (it is kept, never deleted)

First numbered 0050, alongside the application-form questions' 0050, which left two heads; renumbered after it. An
install that ran the old 0050 already has the columns, so the step checks first.

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-19 09:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0052'
down_revision: str | None = '0051'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if 'posting_closed_at' in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('jobs')}:
        return
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('posting_closed_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('posting_closed_reason', sa.String(40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('posting_closed_reason')
        batch.drop_column('posting_closed_at')
