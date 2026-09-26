import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from magic_cards_model_api.db import database_url_from_env, metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # `disable_existing_loggers=False` is load-bearing: the default (True)
    # switches off every logger this ini does not name, `mcm.audit`
    # included - so a migration run in-process silently kills the game
    # log and every action outcome for the rest of that process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# `db.py`'s `Table` definitions are the single source of truth for the
# schema - autogenerate diffs against these, and the initial migration was
# written by hand from them rather than the other way round.
target_metadata = metadata

# The environment overrides whatever `alembic.ini` has, which is a
# non-functional placeholder - secrets load through `mise`/`.env`, never
# through a committed ini file (see the api's `.env.example`).
# `database_url_from_env` takes `DATABASE_URL` when it is set and otherwise
# assembles one from the POSTGRES_* components, which is how the Kubernetes
# chart will pass CloudNativePG's Secret keys through.
if os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_HOST"):
    # `%%`, not `%`: `set_main_option` stores the value through
    # `configparser`, whose `BasicInterpolation` treats a lone `%` as the
    # start of an interpolation reference and raises on anything else - which
    # a percent-encoded URL (a password, or a query string like
    # `options=-csearch_path%3Dtest_gw0%2Cpublic`) can easily contain.
    # Doubling it here is exactly what interpolation undoes on the way back
    # out (`get_main_option`/`get_section`, both used below), so nothing
    # downstream ever sees the escaping - a URL with no `%` in it round-trips
    # identically to before.
    config.set_main_option("sqlalchemy.url", database_url_from_env().replace("%", "%%"))

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
