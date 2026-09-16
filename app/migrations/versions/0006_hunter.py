"""Apify Hunter: saved search profiles, run tracking and the posting data each job now keeps

Jobs gain the employer's posting date, the salary and job facts the board stated, and the search
provenance (role, location, board, actor, run). Search runs gain the Hunter's own status and the
parameters their results are imported with.

Existing jobs are backfilled with the deduplication keys so postings found again are recognised.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-16 14:00:00
"""
import re
from collections.abc import Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_COLUMNS = (
    ('source_board', sa.String(length=50)), ('source_job_id', sa.String(length=200)),
    ('source_url', sa.String(length=1200)), ('direct_apply_url', sa.String(length=1200)),
    ('canonical_url', sa.String(length=1200)), ('canonical_apply_url', sa.String(length=1200)),
    ('dedupe_key', sa.String(length=700)), ('posted_at', sa.DateTime()),
    ('posted_date_raw', sa.String(length=200)), ('scraped_at', sa.DateTime()),
    ('job_type', sa.String(length=50)), ('remote_type', sa.String(length=20)),
    ('seniority', sa.String(length=100)), ('skills', sa.JSON()), ('experience', sa.String(length=200)),
    ('salary_min', sa.Float()), ('salary_max', sa.Float()), ('salary_currency', sa.String(length=10)),
    ('salary_interval', sa.String(length=20)), ('salary_source', sa.String(length=50)),
    ('matched_search_role', sa.String(length=300)), ('matched_search_location', sa.String(length=300)),
    ('apify_actor_id', sa.String(length=300)), ('apify_run_id', sa.String(length=200)),
    ('discoveries', sa.JSON()),
)
RUN_COLUMNS = (
    ('adapter', sa.String(length=50)), ('hunter_status', sa.String(length=30)), ('roles', sa.JSON()),
    ('location', sa.String(length=300)), ('sites', sa.JSON()), ('search_params', sa.JSON()), ('stats', sa.JSON()),
    ('started_at', sa.DateTime()), ('finished_at', sa.DateTime()), ('fallback_for_run_id', sa.Integer()),
)
JOB_INDEXES = (
    ('ix_jobs_user_board_job_id', ['user_id', 'source_board', 'source_job_id']),
    ('ix_jobs_user_canonical_url', ['user_id', 'canonical_url']),
    ('ix_jobs_user_canonical_apply_url', ['user_id', 'canonical_apply_url']),
    ('ix_jobs_user_dedupe_key', ['user_id', 'dedupe_key']),
    ('ix_jobs_posted_at', ['posted_at']),
)
# Frozen copies of app.services.apify_adapters, so the backfill keeps working when that code changes.
TRACKING_PARAM = re.compile(r"^(utm_|trk|refid$|ref$|src$|source$|from$|clickid|gclid$|fbclid$|mc_)", re.I)
COMPANY_SUFFIXES = re.compile(r"\b(incorporated|inc|llc|ltd|limited|pvt|private|plc|gmbh|corp|corporation|pte)\b")


def _utc_now():
    if op.get_bind().dialect.name == 'postgresql':
        return sa.text("(now() at time zone 'utc')")
    return sa.text('CURRENT_TIMESTAMP')


def _canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    query = sorted((k, v) for k, v in parse_qsl(parsed.query) if not TRACKING_PARAM.match(k))
    return urlunparse(('https', parsed.netloc.lower().removeprefix('www.'), parsed.path.rstrip('/') or '/', '',
                       urlencode(query), ''))[:1200]


def _dedupe_key(title: str, company: str, location: str) -> str:
    norm = lambda value: re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()  # noqa: E731
    company_name = re.sub(r"\s+", " ", COMPANY_SUFFIXES.sub(" ", norm(company))).strip()
    return f"{company_name}|{norm(title)}|{norm((location or '').split(',')[0])}"[:700]


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        'search_profiles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('roles', sa.JSON(), nullable=False),
        sa.Column('locations', sa.JSON(), nullable=False),
        sa.Column('sites', sa.JSON(), nullable=False),
        sa.Column('hours_old', sa.Integer(), nullable=True),
        sa.Column('max_results', sa.Integer(), nullable=False, server_default='20'),
        sa.Column('remote_only', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('job_type', sa.String(length=30), nullable=False, server_default=''),
        sa.Column('country', sa.String(length=60), nullable=False, server_default=''),
        sa.Column('mode', sa.String(length=20), nullable=False, server_default='primary'),
        sa.Column('require_posted_date', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('run_every_minutes', sa.Integer(), nullable=False, server_default='1440'),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=_utc_now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=_utc_now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE', name='fk_search_profiles_user_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'name', name='uq_search_profiles_user_name'),
    )
    op.create_index('ix_search_profiles_user_id', 'search_profiles', ['user_id'])
    op.create_index('ix_search_profiles_next_run_at', 'search_profiles', ['next_run_at'])

    with op.batch_alter_table('jobs') as batch:
        for name, column_type in JOB_COLUMNS:
            batch.add_column(sa.Column(name, column_type, nullable=True))
        batch.add_column(sa.Column('search_profile_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_jobs_search_profile_id', 'search_profiles', ['search_profile_id'], ['id'],
                                 ondelete='SET NULL')
    for name, columns in JOB_INDEXES:
        op.create_index(name, 'jobs', columns)
    op.create_index('ix_jobs_search_profile_id', 'jobs', ['search_profile_id'])

    with op.batch_alter_table('search_runs') as batch:
        for name, column_type in RUN_COLUMNS:
            batch.add_column(sa.Column(name, column_type, nullable=True))
        batch.add_column(sa.Column('search_profile_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_search_runs_search_profile_id', 'search_profiles', ['search_profile_id'], ['id'],
                                 ondelete='SET NULL')
    op.create_index('ix_search_runs_search_profile_id', 'search_runs', ['search_profile_id'])
    op.create_index('ix_search_runs_fallback_for_run_id', 'search_runs', ['fallback_for_run_id'])

    op.execute("UPDATE jobs SET source_board = substr(source, 7) WHERE source LIKE 'apify:%'")
    rows = bind.execute(sa.text("SELECT id, title, company, location, url FROM jobs")).all()
    updates = [{"row_id": row.id, "key": _dedupe_key(row.title, row.company, row.location),
                "url": _canonical_url(row.url)} for row in rows]
    for start in range(0, len(updates), 500):
        bind.execute(sa.text("UPDATE jobs SET dedupe_key = :key, canonical_url = :url WHERE id = :row_id"),
                     updates[start:start + 500])


def downgrade() -> None:
    op.drop_index('ix_search_runs_fallback_for_run_id', table_name='search_runs')
    op.drop_index('ix_search_runs_search_profile_id', table_name='search_runs')
    with op.batch_alter_table('search_runs') as batch:
        batch.drop_constraint('fk_search_runs_search_profile_id', type_='foreignkey')
        for name, _ in RUN_COLUMNS:
            batch.drop_column(name)
        batch.drop_column('search_profile_id')

    op.drop_index('ix_jobs_search_profile_id', table_name='jobs')
    for name, _ in JOB_INDEXES:
        op.drop_index(name, table_name='jobs')
    with op.batch_alter_table('jobs') as batch:
        batch.drop_constraint('fk_jobs_search_profile_id', type_='foreignkey')
        for name, _ in JOB_COLUMNS:
            batch.drop_column(name)
        batch.drop_column('search_profile_id')

    op.drop_index('ix_search_profiles_next_run_at', table_name='search_profiles')
    op.drop_index('ix_search_profiles_user_id', table_name='search_profiles')
    op.drop_table('search_profiles')
