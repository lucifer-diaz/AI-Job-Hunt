"""Isolate the test suite from a developer's .env before any app module is imported.

Settings are read once and cached, and .env may point at a real database, real API keys and
basic-auth credentials. Tests always use TEST_DATABASE_URL (or throwaway SQLite), the mock LLM,
no Apify token and no login.
"""
import os

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test_suite.db")
os.environ["LLM_PROVIDER"] = "mock"
for key in ("APP_USERNAME", "APP_PASSWORD", "APIFY_API_TOKEN", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[key] = ""

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

# The workflow-state tests wipe and re-migrate their database. They get their own schema so the
# app tests, which share TEST_DATABASE_URL through the public schema, keep their tables.
WORKFLOW_TEST_SCHEMA = "jobhunt_workflow_tests"


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database to run the workflow-state tests")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or "test" not in (parsed.database or ""):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL database whose name contains 'test'")
    return url


def reset_schema(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {WORKFLOW_TEST_SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {WORKFLOW_TEST_SCHEMA}"))


@pytest.fixture(scope="session")
def pg_engine(pg_url):
    from app.db_migrations import run_migrations

    engine = create_engine(pg_url, pool_size=20, max_overflow=20,
                           connect_args={"options": f"-csearch_path={WORKFLOW_TEST_SCHEMA}"})
    reset_schema(engine)
    run_migrations(engine)
    yield engine
    with engine.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {WORKFLOW_TEST_SCHEMA} CASCADE"))
    engine.dispose()


@pytest.fixture
def pg_sessions(pg_engine):
    """A session factory on a migrated PostgreSQL schema with an empty jobs table."""
    with pg_engine.begin() as connection:
        connection.execute(text("TRUNCATE jobs RESTART IDENTITY CASCADE"))
    return sessionmaker(bind=pg_engine, autoflush=False)
