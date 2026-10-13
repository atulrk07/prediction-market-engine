"""Alembic uses the shared PostgreSQL connection and SQLAlchemy metadata."""

from logging.config import fileConfig

from alembic import context

from database import database_url, make_engine
from models import metadata


fileConfig(context.config.config_file_name)


def run_offline():
    """Print SQL without connecting; this does not apply the migration."""
    context.configure(url=database_url(), target_metadata=metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online():
    engine = make_engine()
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=metadata,
                              compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
