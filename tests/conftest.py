"""Isolate the test suite from a developer's .env before any app module is imported.

Settings are read once and cached, and .env may point at a real database, real API keys and
basic-auth credentials. Tests always use TEST_DATABASE_URL (or throwaway SQLite), the mock LLM,
no Apify token and no login.
"""
import os

os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test_suite.db")
os.environ["LLM_PROVIDER"] = "mock"
os.environ["SCHEDULER_ENABLED"] = "false"  # tests start schedulers explicitly
os.environ["TAILOR_WORKER_ENABLED"] = "false"  # tests run tailoring tasks with tailor_tasks.run_pending
os.environ["SECRET_KEY"] = "test-secret-key-for-encrypting-api-keys"
for key in ("APIFY_API_TOKEN", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ADMIN_TOKEN"):
    os.environ[key] = ""

import pytest  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

TEST_USER_EMAIL = "tester@jobhunt.test"


def ensure_test_user():
    """The account that API tests act as unless they sign in or opt out with the `anonymous` fixture.

    Looked up (and recreated if a test dropped the tables) on each use, never cached.
    """
    from app.auth import CurrentUser
    from app.db import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == TEST_USER_EMAIL))
        if user is None:
            user = User(email=TEST_USER_EMAIL, password_hash="!", is_admin=True)  # "!" never matches a password
            db.add(user)
            db.commit()
            db.refresh(user)
        return CurrentUser(user.id, user.email, user.is_admin)


def make_user(sessions, email=None, is_admin=False) -> int:
    """Create an account in any database (e.g. the PostgreSQL workflow schema) and return its id."""
    import uuid

    from app.models import User

    with sessions() as db:
        user = User(email=email or f"{uuid.uuid4().hex[:10]}@jobhunt.test", password_hash="!", is_admin=is_admin)
        db.add(user)
        db.commit()
        return user.id


@pytest.fixture(autouse=True)
def signed_in_as_test_user(monkeypatch):
    """Requests without a session cookie act as the test account; real cookies still sign in normally."""
    from app import auth

    real = auth.session_user
    monkeypatch.setattr(auth, "authenticate", lambda request: real(request) or ensure_test_user())


@pytest.fixture
def anonymous(monkeypatch):
    """Turn off the test account: requests are signed in only by a real session cookie."""
    from app import auth

    monkeypatch.setattr(auth, "authenticate", auth.session_user)
    auth._failures.clear()

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
    """A session factory on a migrated PostgreSQL schema with empty jobs and scheduler tables."""
    with pg_engine.begin() as connection:
        connection.execute(text("TRUNCATE jobs, scheduler_jobs, users RESTART IDENTITY CASCADE"))
    return sessionmaker(bind=pg_engine, autoflush=False)
