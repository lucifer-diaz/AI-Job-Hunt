import os

# Settings and the engine are created once, when the first test module imports the app, so the
# environment has to be fixed here rather than in individual test modules.
os.environ["LLM_PROVIDER"] = "mock"
os.environ["DATABASE_URL"] = "sqlite:///./test_jobhunt.db"

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database to run the workflow-state tests")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or "test" not in (parsed.database or ""):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL database whose name contains 'test': it is wiped")
    return url


def reset_schema(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


@pytest.fixture(scope="session")
def pg_engine(pg_url):
    from app.db_migrations import run_migrations

    engine = create_engine(pg_url, pool_size=20, max_overflow=20)
    reset_schema(engine)
    run_migrations(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_sessions(pg_engine):
    """A session factory on a migrated PostgreSQL database with an empty jobs table."""
    with pg_engine.begin() as connection:
        connection.execute(text("TRUNCATE jobs RESTART IDENTITY CASCADE"))
    return sessionmaker(bind=pg_engine, autoflush=False)
