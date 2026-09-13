"""Small, idempotent compatibility migrations for existing single-user installs.

The project intentionally keeps deployment lightweight. New production deployments
should still take a database backup before upgrading.
"""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def migrate(engine: Engine) -> None:
    inspector = inspect(engine)
    if "applications" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("applications")}
    if "approved_resume_id" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE applications ADD COLUMN approved_resume_id INTEGER"))
