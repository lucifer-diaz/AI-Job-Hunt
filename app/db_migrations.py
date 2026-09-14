"""Schema migrations. Alembic (app/migrations) owns the schema; this module runs it safely.

    python -m app.db_migrations        # upgrade the configured DATABASE_URL to the latest revision

The web app runs the same upgrade on startup. Pre-Alembic installs, whose tables were made by
`create_all()`, first get the column additions older versions applied at startup; the idempotent
0001 baseline then creates only missing tables, and later revisions run normally.
New production deployments should still take a database backup before upgrading.
"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Arbitrary constant: a PostgreSQL advisory lock so web, cron and workers never migrate concurrently.
MIGRATION_LOCK_KEY = 7_220_310_001

# Columns added before Alembic existed. table -> {column: DDL type}
LEGACY_ADDED_COLUMNS = {
    "applications": {"approved_resume_id": "INTEGER", "stage": "VARCHAR(50)"},
    "jobs": {"search_run_id": "INTEGER", "triaged_at": "TIMESTAMP"},
    "app_settings": {"preferences": "JSON"},
}


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def _bring_legacy_install_to_baseline(connection: Connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table, added in LEGACY_ADDED_COLUMNS.items():
        if table not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl in added.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    if "applications" in tables:
        connection.execute(text("UPDATE applications SET stage = 'applied' WHERE stage IS NULL"))


def run_migrations(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY})
        if not inspect(connection).has_table("alembic_version"):
            _bring_legacy_install_to_baseline(connection)
        command.upgrade(alembic_config(connection), revision)


if __name__ == "__main__":
    from app.db import engine

    run_migrations(engine)
    print(f"database upgraded to revision {head_revision()}")
