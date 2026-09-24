"""Applying from Claude Desktop: questions waiting for a person, confirmations and blockers

`knowledge_answers.answer_options` keeps the choices an application form offered for a question saved for a
person to answer, and `request_context` which job and form field asked it. `applications.confirmation_id` is
the reference the employer's portal showed after a submission, `submitted_via` who submitted it, and
`apply_blocker` why the last attempt stopped. Nothing is backfilled: existing rows have none of these.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-24 12:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0021'
down_revision: str | None = '0020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('knowledge_answers') as batch:
        batch.add_column(sa.Column('answer_options', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('request_context', sa.JSON(), nullable=True))
    with op.batch_alter_table('applications') as batch:
        batch.add_column(sa.Column('confirmation_id', sa.String(length=200), nullable=True))
        batch.add_column(sa.Column('submitted_via', sa.String(length=40), nullable=True))
        batch.add_column(sa.Column('apply_blocker', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('applications') as batch:
        batch.drop_column('apply_blocker')
        batch.drop_column('submitted_via')
        batch.drop_column('confirmation_id')
    with op.batch_alter_table('knowledge_answers') as batch:
        batch.drop_column('request_context')
        batch.drop_column('answer_options')
