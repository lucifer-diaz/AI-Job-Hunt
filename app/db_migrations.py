"""Small, idempotent compatibility migrations for existing single-user installs.

The project intentionally keeps deployment lightweight. New production deployments
should still take a database backup before upgrading.
"""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# table -> {column: DDL type}
ADDED_COLUMNS = {
    "applications": {"approved_resume_id": "INTEGER", "stage": "VARCHAR(50)"},
    "jobs": {"search_run_id": "INTEGER", "triaged_at": "TIMESTAMP"},
    "app_settings": {"preferences": "JSON"},
}


def migrate(engine: Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, added in ADDED_COLUMNS.items():
        if table not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl in added.items():
            if name not in columns:
                with engine.begin() as connection:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    if "applications" in tables:
        with engine.begin() as connection:
            connection.execute(text("UPDATE applications SET stage = 'applied' WHERE stage IS NULL"))
