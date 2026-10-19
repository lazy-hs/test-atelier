from logging.config import fileConfig

from alembic import context
from sqlalchemy import text

from api.db import Base, engine

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=str(engine.url), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        # Acquire a transaction-scoped advisory lock after Alembic starts its
        # migration transaction. This avoids an implicit SQLAlchemy transaction
        # before ``context.begin_transaction`` and releases the lock atomically
        # with the schema commit or rollback.
        with context.begin_transaction():
            if connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                    {"lock_key": "test_atelier.alembic.migrations"},
                )
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
