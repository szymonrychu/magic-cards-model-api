"""The postgres schema (T9): `MetaData`/`Table` definitions shared by
`repository_postgres.py` and `alembic/env.py`, so the migration that creates
these tables and the code that reads/writes them can never drift apart -
there is exactly one place the columns are named.

Two tables. `games` is the CAS-versioned row `PostgresRepository` reads and
writes: `state` is `Game.model_dump(mode="json")`; `command`/`tape`/`pending`
are the paused-command bookkeeping `store_paused_command`/`paused_command`/
`clear_paused_command` need, `None` when nothing is paused; `version` is the
optimistic-concurrency counter `replace()` compares-and-swaps on. `records`
is the append-only audit trail (T9 built the table, T10 fills in its
payload and every write path that is not tied to a `games` commit -
`append_record`/`fill_record`).

**`records.game_id` carries no `ForeignKey`, deliberately (T10).** T9's
original schema had one, `ON DELETE CASCADE` - reasonable while every row's
payload was still `{}`, but wrong once this table became the actual audit
trail: `lobby.reset_game`'s whole point is that a deleted game's last
`records` row is the reset itself, so the transcript "ends with the reset
rather than trailing off" (its own docstring) - a `CASCADE` would delete
that very row the instant the game it describes is removed, silently
defeating the feature. `records` is an independent, append-only ledger that
outlives the game it describes; the two tables are linked only by the
`game_id` values matching, never by a constraint the database enforces.
The constraint is dropped by its own revision, `8f1c47a0b6d2` - never by
editing the initial one, which `alembic upgrade head` would not re-run
on any database already stamped with it. See `tests/test_migrations.py`.

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
as an opaque, exact `str`, never a `uuid.UUID` object), and native `uuid`
normalises on read-back to the canonical `8-4-4-4-12` dashed form - so a
value round-tripped through the column would no longer string-equal the
undashed hex this repository minted it as (`"...".join(...) in ui_games()`-
shaped checks would silently fail). A probe with a non-uuid-shaped sentinel
like `"no-such-game"` (the contract suite's "unknown id" case) is not a
second reason: `repository_postgres.py`'s `_looks_like_id` already turns
those away before any query reaches the driver, string column or not.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import quote

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
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
    # How many times this game's standing question has been replaced -
    # bumped by `store_paused_command` alone, never reset (not even by
    # `clear_paused_command`), so a number a client is holding can never be
    # reused by a later pause. See `repository.GameRepository
    # .pause_generation`. Added by revision `c4e91b7d2a58`.
    Column("pause_generation", Integer, nullable=False, server_default="0"),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("admin_key", String, nullable=False),
    # The OIDC subject (JWT `sub`) that created this game, `NULL` for one
    # created with no bearer token - every game today, and every game this
    # deployment ever creates until an OIDC issuer is actually configured
    # (see `oidc.py`). `game_access.py`'s ownership gate only restricts a
    # game that carries one; `NULL` behaves exactly as this column never
    # existed. Added by revision `f3a7c2e91b04`.
    Column("owner_user_id", String, nullable=True),
    Column("seats", JSONB, nullable=False, server_default="{}"),
    # uid -> `{"player": str, "agent": <AgentSettings wire dict or null>}`:
    # this game's advisor credentials (`repository.Advisor`). A separate map
    # from `seats` because an advisor uid must never be mistakable for a
    # seat uid - `game_access.py` refuses every mutating request presenting
    # one - and the agent settings live *inside* each entry rather than in
    # `agent_settings`, which is keyed by username and so cannot tell two
    # credentials on one seat apart. Added by revision `b8d24f6c1a07`.
    Column("advisors", JSONB, nullable=False, server_default="{}"),
    # uid -> `{"deck_slug", "guides_revision", "scripts", "registered_at"}`:
    # the play-script corpus each seat Job registered under its own uid
    # (`PUT /api/games/{id}/seats/{uid}/scripts`). One JSONB map rather than a
    # table, the same shape as `seats`/`advisors`: it is per-game, read by
    # uid, and gone with the game. Added by revision `d5b2e8f4a913`.
    Column("scripts", JSONB, nullable=False, server_default="{}"),
    Column("decklists", JSONB, nullable=False, server_default="{}"),
    Column("agent_settings", JSONB, nullable=False, server_default="{}"),
    Column("ai_players", JSONB, nullable=False, server_default="[]"),
    Column("ui_game", Boolean, nullable=False, server_default="false"),
    Column("ui_game_registered_at", DateTime(timezone=True), nullable=True),
    # When this game was ended, `NULL` while it is still live. `delete()` is
    # a soft delete: it stamps this column instead of removing the row, so
    # the board, hands and final state a post-game review reads are still
    # there after the lobby's End button, a concede, or a finished game -
    # the owner's requirement, "Games must be persisting in the database
    # even after are stopped/finished/conceded". Every other read filters on
    # `archived_at IS NULL`, so an ended game is exactly as unplayable as a
    # removed one used to be; `PostgresRepository.archived` is the single
    # accessor that looks past the filter. Added by revision `a2f6d9c31b57`.
    Column("archived_at", DateTime(timezone=True), nullable=True),
)

records = Table(
    "records",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    # No `ForeignKey` (T10, deliberate - see this module's own docstring):
    # this is an independent, append-only ledger, not a child row that
    # should disappear with its game. `game_id` is a plain string, matching
    # `games.id`'s own shape, but the two are linked only by convention.
    Column("game_id", String, nullable=False),
    Column("seq", Integer, nullable=False),
    # T10: what was done, what else was legal at that moment
    # (`records.legal_actions_before`), the engine's own outcome (allowed/
    # refused, its reason verbatim), and the seat's stated reasoning -
    # `audit.record`'s payload, verbatim. One row per audited action, not
    # one per `games` commit: a refusal that never touched `games` at all
    # still gets a row (`repository_postgres.PostgresRepository.
    # append_record`), which is why `seq` is this table's own append order
    # rather than borrowed from `games.version` - see `MEMORY.md`.
    Column("payload", JSONB, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("game_id", "seq", name="uq_records_game_id_seq"),
)


#: The component variables a DSN is assembled from when `DATABASE_URL` is not
#: set. They are named for what CloudNativePG publishes in a cluster's Secret
#: (`host`, `port`, `user`, `password`, `dbname`), so the Helm chart maps them
#: straight through with `valueFrom.secretKeyRef` and no values file ever has to
#: carry a URL.
_URL_COMPONENTS = ("POSTGRES_HOST", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")


def database_url_from_env(env: Mapping[str, str] | None = None) -> str:
    """The database URL, from `DATABASE_URL` or assembled from components.

    `DATABASE_URL` wins when it is set, so nothing that already passes one has
    to change. Otherwise the URL is built from `POSTGRES_HOST`, `POSTGRES_PORT`
    (default 5432), `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB`.

    Two reasons for the component path. Kubernetes is the first: CloudNativePG
    hands a workload its credentials as separate Secret keys, so assembling here
    means the chart never interpolates a URL and the password never appears in a
    values file. The second is that a committed `docker-compose.yml` carrying a
    `://user:password@host` shape is flagged by secret scanning even when every
    part of it is a variable reference, and a security check that is always red
    is a security check nobody reads.

    User and password are percent-encoded. A hand-set password containing `@`
    or `/` would otherwise silently redirect the connection to a different host
    or database rather than failing.
    """
    env = os.environ if env is None else env

    explicit = env.get("DATABASE_URL")
    if explicit:
        return explicit

    missing = [name for name in _URL_COMPONENTS if not env.get(name)]
    if missing:
        raise RuntimeError(
            "no database URL: set DATABASE_URL, or all of "
            f"{', '.join(_URL_COMPONENTS)} (missing: {', '.join(missing)})."
        )

    user = quote(env["POSTGRES_USER"], safe="")
    password = quote(env["POSTGRES_PASSWORD"], safe="")
    host = env["POSTGRES_HOST"]
    port = env.get("POSTGRES_PORT") or "5432"
    name = env["POSTGRES_DB"]
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"


def configured_database_url(env: Mapping[str, str] | None = None) -> str | None:
    """The database URL this environment configures, or `None` if it
    configures none.

    `database_url_from_env`'s question is "give me the URL, and complain if
    there is none" - right for `alembic/env.py`, which is only ever run
    against a database on purpose. This is the other question, the one
    `store.py` asks at import: "is a database configured at all?" A `None`
    here is not an error; it is the docker-free default this api has always
    had, and it must stay reachable with no environment set up whatsoever.

    A *partial* set of components (`POSTGRES_HOST` alone, say) counts as
    nothing configured rather than as an error. Guessing that a half-filled
    environment meant postgres and then failing to connect would turn a
    stray leftover variable in somebody's shell into a dead application;
    anyone who genuinely means postgres sets all of them, and
    `database_url_from_env` is still there to say exactly which one is
    missing for a caller that requires one.
    """
    env = os.environ if env is None else env
    if env.get("DATABASE_URL"):
        return env["DATABASE_URL"]
    if all(env.get(name) for name in _URL_COMPONENTS):
        return database_url_from_env(env)
    return None


def make_engine(database_url: str) -> Engine:
    """One `Engine` for `database_url` - `future=True` is the 2.0-style
    default already, kept explicit since this is the one place a repository
    instance's connection pool gets born.

    `pool_pre_ping=True`: a repository built here is built once and kept for
    the process's whole lifetime (`store.py`'s own pattern), so a pooled
    connection can easily outlive the far side of it - Postgres closing an
    idle connection, or a proxy in front of it dropping one. Without a
    pre-ping the pool only discovers that on the request that gets handed
    the dead connection, surfacing as a request failure; the pre-ping tests
    each connection with a cheap round trip before handing it out and
    transparently gets a fresh one instead."""
    return create_engine(database_url, future=True, pool_pre_ping=True)
