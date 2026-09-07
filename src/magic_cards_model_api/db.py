"""The postgres schema (T9): `MetaData`/`Table` definitions shared by
`repository_postgres.py` and `alembic/env.py`, so the migration that creates
these tables and the code that reads/writes them can never drift apart -
there is exactly one place the columns are named.

Two tables. `games` is the CAS-versioned row `PostgresRepository` reads and
writes: `state` is `Game.model_dump(mode="json")`; `command`/`tape`/`pending`
are the paused-command bookkeeping `store_paused_command`/`paused_command`/
`clear_paused_command` need, `None` when nothing is paused; `version` is the
optimistic-concurrency counter `replace()` compares-and-swaps on. `records` is
the append-only audit trail T10 fills in - this task only owns the table and
the one-row-per-commit write path, not the payload shape.

`ui_game_registered_at` is separate from `created_at` on purpose:
`InMemoryRepository._ui_games` is a list appended to at whichever moment
`register_ui_game` (or `create_if_under(..., ui_game=True)`) actually runs,
not at game creation - a game can be created and registered as a ui game
later, and `ui_games()`/`live_ui_games()` promise "oldest first" by that
registration order, not by `created_at`.

`id` columns are plain `String`, not postgres's native `uuid` type, even
though every value in them is one: every id this repository hands out and
accepts is a literal `uuid4().hex` string (matching `InMemoryRepository` and
every existing caller, which treats `GameRepository.create()`'s return value
as an opaque, exact `str`, never a `uuid.UUID` object) - and native `uuid`
does two things that break that. It normalises on read-back to the canonical
`8-4-4-4-12` dashed form, so a value round-tripped through the column no
longer string-equals the undashed hex this repository minted it as
(`"...".join(...) in ui_games()`-shaped checks would silently fail). And it
rejects, at the driver, any string that is not valid uuid syntax at all -
which the contract suite deliberately probes with (`"no-such-game"`, an
"unknown id" sentinel that is not uuid-shaped), turning what every caller
here expects to be `None`/`False`/`KeyError` into an unhandled `DataError`.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

metadata = MetaData()

games = Table(
    "games",
    metadata,
    Column("id", String, primary_key=True),
    Column("state", JSONB, nullable=False),
    Column("command", JSONB, nullable=True),
    Column("tape", JSONB, nullable=True),
    Column("pending", JSONB, nullable=True),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("admin_key", String, nullable=False),
    Column("seats", JSONB, nullable=False, server_default="{}"),
    Column("decklists", JSONB, nullable=False, server_default="{}"),
    Column("agent_settings", JSONB, nullable=False, server_default="{}"),
    Column("ai_players", JSONB, nullable=False, server_default="[]"),
    Column("ui_game", Boolean, nullable=False, server_default="false"),
    Column("ui_game_registered_at", DateTime(timezone=True), nullable=True),
)

records = Table(
    "records",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "game_id",
        String,
        ForeignKey("games.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("seq", Integer, nullable=False),
    # T10's to fill: what was done, what else was legal, the engine's
    # outcome, the seat's stated reasoning. This task only owns the shape
    # of the write path - one row per committed game update, in the same
    # transaction as the `games` CAS - not the payload's content.
    Column("payload", JSONB, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("game_id", "seq", name="uq_records_game_id_seq"),
)


def make_engine(database_url: str) -> Engine:
    """One `Engine` for `database_url` - `future=True` is the 2.0-style
    default already, kept explicit since this is the one place a repository
    instance's connection pool gets born."""
    return create_engine(database_url, future=True)
