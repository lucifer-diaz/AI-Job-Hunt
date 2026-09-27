"""Resume tags: a domain and target roles per resume version, and how each job's resume was chosen

resumes.domain / resumes.roles tag a version ("Backend": "Lead Engineer", "Engineering Manager"); new jobs are
matched to the best-tagged resume (app.services.resume_matching). jobs.resume_choice says how jobs.resume_id was
chosen (AUTO, SETUP, USER, ACTIVE); existing rows stay NULL, which means the active resume, as before.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-28 16:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0027'
down_revision: str | None = '0026'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('resumes') as batch:
        batch.add_column(sa.Column('domain', sa.String(length=80), nullable=True))
        batch.add_column(sa.Column('roles', sa.JSON(), nullable=True))
    with op.batch_alter_table('jobs') as batch:
        batch.add_column(sa.Column('resume_choice', sa.String(length=10), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('resume_choice')
    with op.batch_alter_table('resumes') as batch:
        batch.drop_column('roles')
        batch.drop_column('domain')
