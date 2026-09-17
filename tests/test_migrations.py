"""Alembic migrations: fresh installs, legacy (pre-Alembic) installs, backfill and downgrade."""
import importlib.util
from datetime import datetime

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.db_migrations import MIGRATIONS_DIR, alembic_config, head_revision, run_migrations
from app.models import Job
from app.services.lifecycle import ErrorCategory, WorkflowState
from tests.conftest import reset_schema

DISCOVERED_AT = datetime(2026, 3, 1, 9, 30)

# (legacy status, fit_score, application stage, has reviewed resume) -> expected state
LEGACY_CASES = [
    ("discovered", None, None, False, "DISCOVERED"),
    ("discovered", 55.0, None, False, "RANKED"),
    ("maybe", 70.0, None, False, "RANKED"),
    ("shortlisted", 81.0, None, False, "QUALIFIED"),
    ("archived", 40.0, None, False, "ARCHIVED"),
    ("dismissed", None, None, False, "SKIPPED"),
    ("tailored", 80.0, None, False, "TAILORED"),
    ("tailored", 80.0, None, True, "REVIEWED"),
    ("pending_approval", 80.0, None, True, "REVIEWED"),
    ("approved", 80.0, None, True, "READY_TO_APPLY"),
    ("applied", 80.0, "screening", True, "APPLIED"),
    ("applied", 80.0, "interviewing", True, "INTERVIEW"),
    ("applied", 80.0, "offer", True, "OFFER"),
    ("applied", 80.0, "rejected", True, "REJECTED"),
    ("applied", 80.0, "closed", True, "ARCHIVED"),
    ("rejected", 80.0, None, True, "REJECTED"),
    ("something_unknown", None, None, False, "DISCOVERED"),
]


def _migration_module(name: str):
    spec = importlib.util.spec_from_file_location(name, MIGRATIONS_DIR / "versions" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_migration_values_match_the_lifecycle():
    latest = _migration_module("0010_job_ranker")
    assert latest.WORKFLOW_STATES == tuple(WorkflowState)
    assert latest.ERROR_CATEGORIES == tuple(ErrorCategory)
    original = _migration_module("0002_workflow_state")
    assert latest.PREVIOUS_WORKFLOW_STATES == original.WORKFLOW_STATES
    assert set(original.WORKFLOW_STATES) == set(WorkflowState) - {WorkflowState.RANK_PENDING, WorkflowState.RANKING}


@pytest.fixture
def migration_engine(pg_engine):
    reset_schema(pg_engine)
    yield pg_engine
    # Leave the shared test database at head for the other PostgreSQL tests.
    reset_schema(pg_engine)
    run_migrations(pg_engine)


def test_fresh_database_upgrades_to_head_and_matches_the_models(migration_engine):
    run_migrations(migration_engine)
    run_migrations(migration_engine)  # idempotent
    with migration_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head_revision()
        diff = compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def _seed_legacy_rows(connection) -> dict[str, int]:
    ids = {}
    for index, (status, fit, stage, reviewed, _) in enumerate(LEGACY_CASES):
        job_id = connection.scalar(text(
            "INSERT INTO jobs (source, source_ref, title, company, location, url, description, raw, discovered_at,"
            " fit_score, fit_analysis, status) VALUES ('legacy', :ref, 'Role', 'Acme', '', '', '', '{}', :at, :fit,"
            " '{}', :status) RETURNING id"), {"ref": f"ref-{index}", "at": DISCOVERED_AT, "fit": fit, "status": status})
        if reviewed:
            connection.execute(text(
                "INSERT INTO resume_variants (job_id, version, content, markdown, cover_letter, reviewer_score,"
                " reviewer_notes, created_at) VALUES (:job, 1, '{}', '', '', 86, '{}', :at)"),
                {"job": job_id, "at": DISCOVERED_AT})
        if stage:
            connection.execute(text(
                "INSERT INTO applications (job_id, status, recruiter_name, recruiter_email, notes, stage, created_at)"
                " VALUES (:job, 'applied', '', '', '', :stage, :at)"),
                {"job": job_id, "stage": stage, "at": DISCOVERED_AT})
        ids[f"ref-{index}"] = job_id
    return ids


def test_pre_alembic_install_is_upgraded_and_backfilled(migration_engine):
    with migration_engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0001")
        connection.execute(text("DROP TABLE alembic_version"))  # what a create_all() install looks like
        connection.execute(text("DROP TABLE followups"))  # a table an older install never had
        ids = _seed_legacy_rows(connection)

    run_migrations(migration_engine)

    inspector = inspect(migration_engine)
    assert inspector.has_table("followups") and inspector.has_table("workflow_transitions")
    with migration_engine.connect() as connection:
        rows = {row.source_ref: row for row in connection.execute(text(
            "SELECT source_ref, state, attempt_count, created_at, updated_at FROM jobs"))}
        for index, (status, _, stage, _, expected) in enumerate(LEGACY_CASES):
            row = rows[f"ref-{index}"]
            assert row.state == expected, (status, stage)
            assert row.attempt_count == 0 and row.created_at == row.updated_at == DISCOVERED_AT
        history = connection.execute(text(
            "SELECT job_id, from_state, to_state, actor FROM workflow_transitions ORDER BY id")).all()
    assert [(h.job_id, h.from_state, h.actor) for h in history] == [(i, None, "migration:0002") for i in ids.values()]
    assert [h.to_state for h in history] == [case[-1] for case in LEGACY_CASES]


def test_downgrade_to_baseline_and_upgrade_again(migration_engine):
    run_migrations(migration_engine)
    with migration_engine.begin() as connection:
        command.downgrade(alembic_config(connection), "0001")
    columns = {c["name"] for c in inspect(migration_engine).get_columns("jobs")}
    assert "state" not in columns and not inspect(migration_engine).has_table("workflow_transitions")
    run_migrations(migration_engine)
    assert "lock_token" in {c["name"] for c in inspect(migration_engine).get_columns("jobs")}


def test_sqlite_local_installs_migrate_too(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'local.db'}")
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0001")
        connection.execute(text("DROP TABLE alembic_version"))
        connection.execute(text(
            "INSERT INTO jobs (source, source_ref, title, company, location, url, description, raw, discovered_at,"
            " fit_score, fit_analysis, status) VALUES ('legacy', 'r1', 'Role', 'Acme', '', '', '', '{}',"
            " '2026-03-01 09:30:00', 90, '{}', 'shortlisted')"))
    run_migrations(engine)
    with Session(engine) as session:
        legacy = session.query(Job).one()
        assert legacy.state is WorkflowState.QUALIFIED
        session.add(Job(source="manual", source_ref="r2", title="New", company="Beta"))
        session.commit()
        assert session.query(Job).filter_by(source_ref="r2").one().state is WorkflowState.DISCOVERED
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("UPDATE jobs SET state = 'BOGUS'"))
    engine.dispose()


def _insert(connection, table, **values):
    """Insert a row filling every other NOT NULL column with an empty value of its type."""
    import json as _json

    from sqlalchemy import JSON, Boolean, DateTime, Integer, MetaData, Numeric
    from sqlalchemy import Table as _Table

    reflected = _Table(table, MetaData(), autoload_with=connection)
    row = {}
    for column in reflected.columns:
        if column.name in values or column.nullable or column.primary_key or column.server_default is not None:
            continue
        kind = column.type
        row[column.name] = (False if isinstance(kind, Boolean) else 0 if isinstance(kind, (Integer, Numeric))
                            else DISCOVERED_AT if isinstance(kind, DateTime) else _json.dumps([]) if isinstance(kind, JSON)
                            else "")
    return connection.execute(reflected.insert().values(**row, **values)).inserted_primary_key[0]


def test_0012_turns_existing_resumes_into_unverified_versions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'resumes.db'}")
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0011")
        user = _insert(connection, "users", email="legacy@example.com", password_hash="!")
        _insert(connection, "candidate_profiles", user_id=user, master_resume="Hand edited resume text")
        old = _insert(connection, "resume_documents", user_id=user, original_filename="old.pdf", media_type="application/pdf",
                      storage_path="/files/old", checksum="a" * 64, size=1, extracted_text="Old resume", active=False)
        _insert(connection, "resume_documents", user_id=user, original_filename="new.pdf", media_type="application/pdf",
                storage_path="/files/new", checksum="b" * 64, size=1, extracted_text="New resume", active=True)
        evidence = _insert(connection, "evidence", user_id=user, claim="Verified by hand", verified=True)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0012")
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT version, source, resume_document_id, source_file_hash, raw_text, is_active, is_default,"
            " verification_status, structured_data FROM resumes ORDER BY version")).all()
        kept = connection.execute(text("SELECT source, verified FROM evidence WHERE id = :id"), {"id": evidence}).one()
        master = connection.scalar(text("SELECT master_resume FROM candidate_profiles"))
    assert [(r.version, r.source, r.raw_text, bool(r.is_active)) for r in rows] == [
        (1, "migration", "Old resume", False), (2, "migration", "New resume", False),
        (3, "manual_text", "Hand edited resume text", True)]
    assert rows[0].resume_document_id == old and rows[0].source_file_hash == "a" * 64
    assert {r.verification_status for r in rows} == {"UNVERIFIED"} and all(r.structured_data is None for r in rows)
    assert [bool(r.is_default) for r in rows] == [False, False, True]
    assert (kept.source, bool(kept.verified)) == ("manual", True) and master == "Hand edited resume text"
    with engine.begin() as connection:
        command.downgrade(alembic_config(connection), "0011")
    assert not inspect(engine).has_table("resumes")
    assert "fact_key" not in {c["name"] for c in inspect(engine).get_columns("evidence")}
    engine.dispose()
