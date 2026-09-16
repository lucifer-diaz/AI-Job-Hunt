"""several recruiters per application, and a time zone and contact on scheduled interviews

Applications gain `contacts` (a JSON list of name, email and phone), backfilled from the single
recruiter_name / recruiter_email pair, which stays as a mirror of the first contact. Application events
gain `meta` for an interview's time zone and optional contact details.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-16 20:00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('applications') as batch:
        batch.add_column(sa.Column('contacts', sa.JSON(), nullable=True))
    with op.batch_alter_table('application_events') as batch:
        batch.add_column(sa.Column('meta', sa.JSON(), nullable=True))

    bind = op.get_bind()
    applications = sa.table('applications', sa.column('id', sa.Integer()), sa.column('contacts', sa.JSON()))
    rows = bind.execute(sa.text("SELECT id, recruiter_name, recruiter_email FROM applications"
                                " WHERE COALESCE(recruiter_name, '') <> '' OR COALESCE(recruiter_email, '') <> ''")).all()
    for row in rows:
        contact = {"name": row.recruiter_name or "", "email": row.recruiter_email or "", "phone": ""}
        bind.execute(applications.update().where(applications.c.id == row.id).values(contacts=[contact]))


def downgrade() -> None:
    with op.batch_alter_table('application_events') as batch:
        batch.drop_column('meta')
    with op.batch_alter_table('applications') as batch:
        batch.drop_column('contacts')
