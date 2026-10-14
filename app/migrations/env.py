"""Alembic environment. Runs against a connection handed over by app.db_migrations, or DATABASE_URL."""
from alembic import context
from sqlalchemy import create_engine, pool

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.db import Base
from app.settings import get_settings

config = context.config
target_metadata = Base.metadata
# LangGraph's PostgresSaver creates and migrates its own tables (app.agents.checkpoint); Alembic leaves them alone.
# The RAG vector table is created by app.services.vector_store.setup() when pgvector is available (derived data).
LIBRARY_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations", "rag_vectors")


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    return not (type_ == "table" and name in LIBRARY_TABLES)


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite can only change constraints by rebuilding a table.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(url=get_settings().database_url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        return
    engine = create_engine(config.get_main_option("sqlalchemy.url") or get_settings().database_url,
                           poolclass=pool.NullPool)
    with engine.connect() as connection:
        _configure(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
