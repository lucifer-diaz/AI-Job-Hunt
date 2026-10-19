"""Repair: re-apply what an install can have skipped when the duplicate 0049 and 0050 were renumbered

Two pairs of migrations were both numbered 0049 / 0050 (Phase 4 preparation and the application-form questions; the
starred flag and posting_closed). The Phase 4 pair kept its numbers and the other became 0051 / 0052. An install that
ran the other pair already stood at revision '0050', so it never ran the new 0049 and 0050. This step runs each of
them where its schema is missing; on a database that has all of it this revision changes nothing.

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-19 20:00:00
"""
import importlib.util
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision: str = '0053'
down_revision: str | None = '0052'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _migration(name: str):
    spec = importlib.util.spec_from_file_location(f"_repair_{name}", Path(__file__).parent / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table('job_preparation_tasks'):
        _migration('0049_preparation_and_approval').upgrade()
    if 'scope' not in {c['name'] for c in inspector.get_columns('job_insight_tasks')}:
        _migration('0050_application_form_questions').upgrade()


def downgrade() -> None:
    """A repair: going back leaves the repaired schema as 0049 and 0050 define it."""
