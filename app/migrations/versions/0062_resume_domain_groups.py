"""Resume tags: several domains per resume version, each with its own target roles

resumes.domains holds [{"domain": "AI / ML", "roles": [...]}, ...] (app.services.resume_matching). resumes.domain and
resumes.roles stay as mirrors (the first domain, every group's roles). Existing rows keep domains NULL, which reads as
the one group domain + roles, so nothing is backfilled.

Revision ID: 0062
Revises: 0061
Create Date: 2026-10-30 14:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0062'
down_revision: str | None = '0061'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('resumes') as batch:
        batch.add_column(sa.Column('domains', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('resumes') as batch:
        batch.drop_column('domains')
