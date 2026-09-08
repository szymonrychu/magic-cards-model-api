"""The alembic upgrade path itself, exercised against a real, freshly
created database - never the one the rest of the postgres suite shares.

Why this file exists: `records.game_id` used to carry an `ON DELETE CASCADE`
foreign key, and T10 needs it gone (`db.py`'s own docstring says why). That
change was first made by *editing* the already-shipped initial revision in
place, leaving its revision id untouched - which is a no-op on any database
already stamped with that id, since alembic never re-runs a revision it has
recorded. Such a database keeps the CASCADE forever while the code assumes
it is gone, and `append_record` for a game that no longer exists raises
`ForeignKeyViolation` there and nowhere else. The only honest fix is a new
revision, and the only honest proof is this: provision a database from the
initial revision, see the constraint, upgrade, see it gone.

Each test creates and drops its own database (`_scratch_database`), so it is
independent of whatever the shared `MCM_TEST_DATABASE_URL` database happens
to have been provisioned from - a cached docker volume included.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.config import Config
from conftest import require_postgres_database_url
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from alembic import command
from magic_cards_model_api.db import metadata

_TEST_DATABASE_URL_ENV = "MCM_TEST_DATABASE_URL"
_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The revision that first created both tables. Named here rather than
#: resolved as "the first one" so this test keeps testing the exact
#: constraint history it was written about, even after more revisions land.
INITIAL_REVISION = "374af1d25f73"

#: postgres's default name for a single-column foreign key: `<table>_<col>_fkey`.
RECORDS_FK = "records_game_id_fkey"

#: The revision that was head before `games.pause_generation` was added -
#: the state a database migrated before that column existed is stamped at.
PAUSE_GENERATION_PREDECESSOR = "8f1c47a0b6d2"


@contextmanager
def _scratch_database() -> Iterator[str]:
    """A brand-new, empty database on the same server as
    `MCM_TEST_DATABASE_URL`, dropped again on the way out.

    `CREATE DATABASE` cannot run inside a transaction block, hence the
    AUTOCOMMIT connection. The name is random so two of these can never
    collide, and so a leftover from a killed run never gets reused as if it
    were fresh - which is the whole point of this file.
    """
    base_url = require_postgres_database_url(_TEST_DATABASE_URL_ENV)
    name = f"mcm_migration_{uuid4().hex[:12]}"
    admin = create_engine(base_url, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        # `render_as_string(hide_password=False)`, never `str(url)`:
        # `URL.__str__` masks the password, and the masked string is a
        # perfectly well-formed URL that simply cannot authenticate.
        yield make_url(base_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _alembic_to(url: str, revision: str) -> None:
    """Run `alembic upgrade <revision>` against `url`.

    `alembic/env.py` reads the URL from `DATABASE_URL` in the environment
    (never from the committed ini), so that is what gets set here - restored
    afterward so one test can never leak its scratch database into another.
    """
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


def _foreign_keys(engine: Engine, table: str) -> list[dict[str, Any]]:
    return inspect(engine).get_foreign_keys(table)


def _schema_fingerprint(engine: Engine) -> dict[str, Any]:
    """Everything about this database's shape that a migration could get
    wrong, in a form two databases can be compared by: columns with their
    types/nullability/defaults, primary and unique constraints, foreign
    keys, and indexes. `alembic_version` is excluded - it records which
    revisions ran, which is exactly the thing the two paths differ in.
    """
    inspector = inspect(engine)
    fingerprint: dict[str, Any] = {}
    for table in sorted(inspector.get_table_names()):
        if table == "alembic_version":
            continue
        fingerprint[table] = {
            "columns": sorted(
                (
                    column["name"],
                    str(column["type"]),
                    bool(column["nullable"]),
                    str(column.get("default")),
                )
                for column in inspector.get_columns(table)
            ),
            "pk": sorted(inspector.get_pk_constraint(table).get("constrained_columns") or []),
            "unique": sorted(
                (constraint["name"], tuple(sorted(constraint["column_names"] or [])))
                for constraint in inspector.get_unique_constraints(table)
            ),
            "foreign_keys": sorted(
                (
                    tuple(sorted(fk["constrained_columns"])),
                    fk["referred_table"],
                    tuple(sorted(fk["referred_columns"])),
                    str((fk.get("options") or {}).get("ondelete")),
                )
                for fk in inspector.get_foreign_keys(table)
            ),
            "indexes": sorted(
                (index["name"], tuple(sorted(index["column_names"] or [])))
                for index in inspector.get_indexes(table)
            ),
        }
    return fingerprint


def test_the_initial_revision_still_creates_the_cascade_foreign_key() -> None:
    """A database provisioned from the initial revision alone has the
    CASCADE foreign key - because that is what shipped. Editing that
    revision to remove it would make this pass for the wrong reason (the
    constraint never created) while every already-migrated database out
    there still has it: the exact defect this file guards."""
    with _scratch_database() as url:
        _alembic_to(url, INITIAL_REVISION)
        engine = create_engine(url, future=True)
        try:
            keys = _foreign_keys(engine, "records")
            assert [key["name"] for key in keys] == [RECORDS_FK], (
                f"the initial revision must create {RECORDS_FK}; found {keys}"
            )
            assert (keys[0].get("options") or {}).get("ondelete") == "CASCADE"
        finally:
            engine.dispose()


def test_upgrade_head_drops_the_foreign_key_on_an_already_migrated_database() -> None:
    """The upgrade path, end to end: stamped at the initial revision with
    the constraint in place - the state every already-migrated database is
    in - `alembic upgrade head` removes it. Without a new revision this is
    a no-op and the constraint survives."""
    with _scratch_database() as url:
        _alembic_to(url, INITIAL_REVISION)
        _alembic_to(url, "head")
        engine = create_engine(url, future=True)
        try:
            assert _foreign_keys(engine, "records") == [], (
                "upgrade head must leave records.game_id with no foreign key - a "
                "database migrated before this revision existed still has the "
                "CASCADE otherwise"
            )
        finally:
            engine.dispose()


def test_an_orphaned_record_row_is_insertable_after_the_upgrade() -> None:
    """The behaviour the dropped constraint is *for*: `append_record` for a
    game that is not (or is no longer) in `games` must insert, not raise
    `ForeignKeyViolation`. Asserted against the upgraded database rather
    than a from-scratch one, since the upgraded one is where the constraint
    would have survived."""
    with _scratch_database() as url:
        _alembic_to(url, INITIAL_REVISION)
        _alembic_to(url, "head")
        engine = create_engine(url, future=True)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO records (game_id, seq, payload, created_at) "
                        "VALUES ('no-such-game', 1, '{}'::jsonb, now())"
                    )
                )
            with engine.connect() as conn:
                count = conn.execute(
                    text("SELECT count(*) FROM records WHERE game_id = 'no-such-game'")
                ).scalar_one()
            assert count == 1
        finally:
            engine.dispose()


def test_upgrade_head_adds_pause_generation_to_an_already_migrated_database() -> None:
    """`games.pause_generation` arrives on a database that predates it, with
    a real value for every row that already exists rather than a NULL.

    Same reasoning as the foreign key above: a column added by editing an
    already-shipped revision is a no-op on every database stamped with that
    id, and `pause_generation` is read on every state view - a missing
    column there is a 500 on the next read, not a subtle drift.
    """
    with _scratch_database() as url:
        _alembic_to(url, PAUSE_GENERATION_PREDECESSOR)
        engine = create_engine(url, future=True)
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO games (id, state, version, created_at, admin_key) "
                        "VALUES ('old', '{}'::jsonb, 0, now(), 'k')"
                    )
                )
            before = {column["name"] for column in inspect(engine).get_columns("games")}
            assert "pause_generation" not in before
        finally:
            engine.dispose()

        _alembic_to(url, "head")
        engine = create_engine(url, future=True)
        try:
            columns = {column["name"] for column in inspect(engine).get_columns("games")}
            assert "pause_generation" in columns
            with engine.connect() as conn:
                value = conn.execute(
                    text("SELECT pause_generation FROM games WHERE id = 'old'")
                ).scalar_one()
            assert value == 0, "a row that predates the column must start at generation 0"
        finally:
            engine.dispose()


def test_the_migrated_schema_matches_the_tables_the_code_defines() -> None:
    """The final schema `alembic upgrade head` produces is the schema
    `db.py`'s `metadata` describes - the definitions `repository_postgres`
    actually reads and writes through. This is what "the upgrade path and a
    from-scratch database agree" has to mean once the two share one linear
    revision history: the comparison worth making is against the code's own
    tables, not against a second run of the same migrations."""
    with _scratch_database() as migrated_url, _scratch_database() as declared_url:
        _alembic_to(migrated_url, "head")
        migrated = create_engine(migrated_url, future=True)
        declared = create_engine(declared_url, future=True)
        try:
            metadata.create_all(declared)
            assert _schema_fingerprint(migrated) == _schema_fingerprint(declared)
        finally:
            migrated.dispose()
            declared.dispose()


@pytest.mark.parametrize("start", [INITIAL_REVISION, "base"])
def test_every_upgrade_route_ends_at_the_same_schema(start: str) -> None:
    """Staged (stamped at the initial revision first, as a live database
    is) and straight-through (empty database, one `upgrade head`) end
    identical. Parameterised so a failure names which route diverged."""
    with _scratch_database() as staged_url, _scratch_database() as direct_url:
        if start != "base":
            _alembic_to(staged_url, start)
        _alembic_to(staged_url, "head")
        _alembic_to(direct_url, "head")
        staged = create_engine(staged_url, future=True)
        direct = create_engine(direct_url, future=True)
        try:
            assert _schema_fingerprint(staged) == _schema_fingerprint(direct)
        finally:
            staged.dispose()
            direct.dispose()
