"""Distinguish a packet review sent back to be tailored again from a job never tailored

A RETAILOR review used to leave the job as 'shortlisted', which is also what a job that has never been
tailored looks like. Those jobs are exactly the shortlisted ones that already have a tailored version,
so they become 'needs_retailor'.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-18 10:00:00
"""
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = '0013'
down_revision: str | None = '0012'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(text(
        "UPDATE jobs SET status = 'needs_retailor' WHERE status = 'shortlisted'"
        " AND EXISTS (SELECT 1 FROM resume_variants WHERE resume_variants.job_id = jobs.id)"))


def downgrade() -> None:
    op.execute(text("UPDATE jobs SET status = 'shortlisted' WHERE status = 'needs_retailor'"))
