"""Alembic environment: the database comes from DATABASE_URL (or the default SQLite file), the schema from the
SQLModel tables in billparse/db.py."""
from logging.config import fileConfig

from alembic import context
from sqlmodel import SQLModel, create_engine

from billparse import db  # noqa: F401 - registers the tables on SQLModel.metadata
from billparse.settings import data_dir, load_env

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
load_env()
target_metadata = SQLModel.metadata


def url() -> str:
    return config.get_main_option("sqlalchemy.url") or db.default_url(data_dir())


def run_migrations_offline() -> None:
    context.configure(url=url(), target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, render_as_batch=url().startswith("sqlite"))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url())          # not db.engine_for: that creates SQLite tables without migrations
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=url().startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
