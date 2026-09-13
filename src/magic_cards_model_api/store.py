"""The process-wide game singleton, and the one decision about which
repository this deployment actually runs on.

`GameStore` is `InMemoryRepository` (`repository.py`) under its historical
name: everything that used to import `GameStore` from here keeps working
unchanged. The real content - the `GameRepository` protocol and its
in-memory implementation - lives in `repository.py`, with the postgres one
in `repository_postgres.py`.

**The selection is made here, once, at import, from the environment.** A
configured database URL (`DATABASE_URL`, or the `POSTGRES_*` components
`db.database_url_from_env` assembles one from - the shape CloudNativePG
hands a workload) selects `PostgresRepository`; nothing configured keeps
`InMemoryRepository`, so `mise run test` and a bare `mise run run` stay
docker-free with no environment set up at all. `db.configured_database_url`
is the whole rule, and a partial set of components counts as nothing
configured rather than as an error - see its docstring.

**`REQUIRE_POSTGRES` makes that same silence loud.** A production deployment
that only sets some `POSTGRES_*` vars, or points them at a Postgres that
isn't listening, would otherwise pass every check above and run happily on
`InMemoryRepository` (or, once reachability is the only thing wrong, on a
`PostgresRepository` whose first real query is the first anyone finds out) -
no error, no crash, just a pod that loses every game on its next restart
with nothing in any log to say why. With `REQUIRE_POSTGRES` truthy-set,
`select_store` refuses both of those: nothing/partial configured raises
`db.database_url_from_env`'s own `RuntimeError` instead of falling back, and
a configured-but-unreachable database is probed - connected to and
immediately disconnected from - before this function ever returns, so a bad
deployment crashes at startup rather than at its first game.

Until this existed, T9's postgres repository and T10's `records` table were
unreachable from the shipped application: this module said
`GameStore = InMemoryRepository; store = GameStore()` outright and nothing
anywhere named `PostgresRepository` outside tests. A postgres deployment
would have run entirely on the in-memory store and lost every game on
restart, with no error to say so.

**Why at import, and only at import.** Two names bind this singleton
independently: `app.py` does `from .store import store` once, at import
time, while `audit.py` re-imports it *lazily* inside `record`/`entries` so
it survives a module reload. Rebinding `store` after either has run leaves
them pointing at different objects - `audit.record`'s `fill_record` then
lands on a store that never saw the row `store.replace` wrote, and raises
`KeyError` for a row that genuinely exists. Choosing once, before anything
can bind, is what keeps the two views identical;
`tests/test_store_selection.py` boots a real subprocess and checks both
bindings resolve to the same object.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from uuid import uuid4

from magic_cards_model.models import Game

from .db import configured_database_url, database_url_from_env, make_engine
from .repository import GameRepository, InMemoryRepository

__all__ = [
    "GameRepository",
    "GameStore",
    "known_players",
    "new_seat_uids",
    "player_for_advisor_uid",
    "store",
]

GameStore = InMemoryRepository
"""Backward-compatible alias. Every existing `from .store import GameStore`
and every direct `GameStore()` construction in tests keeps working - this is
literally the same class, not a copy of it. It is deliberately NOT what
`store` below is necessarily an instance of any more: the singleton's class
is chosen from the environment, while this name keeps meaning what it always
meant, "the in-memory repository"."""


def select_store(env: Mapping[str, str] | None = None) -> GameRepository:
    """The repository this process runs on, per the environment.

    Imports `repository_postgres` only when it is actually going to be used,
    so nothing about a docker-free run pays for SQLAlchemy engine setup or
    fails on a driver it does not need.

    `env` follows `db.py`'s own convention (`configured_database_url`,
    `database_url_from_env`) so this stays testable without mutating
    `os.environ` - `os.environ` itself is still the real default.
    """
    env = os.environ if env is None else env
    require_postgres = bool(env.get("REQUIRE_POSTGRES"))

    database_url = configured_database_url(env)
    if database_url is None:
        if not require_postgres:
            return InMemoryRepository()
        # REQUIRE_POSTGRES turns "nothing, or only some POSTGRES_* vars, are
        # set" from configured_database_url's deliberate silent fallback
        # into a startup crash - database_url_from_env raises the RuntimeError
        # that names exactly what's missing; let it propagate uncaught.
        database_url = database_url_from_env(env)

    from .repository_postgres import PostgresRepository

    engine = None
    if require_postgres:
        # SQLAlchemy connects lazily, so a syntactically fine but unreachable
        # database_url would otherwise pass every check above and only fail
        # on the first real query - invisible to both this startup path and
        # to /api/health, which is a static dict with no DB check of its own.
        # Build the engine here and probe it before PostgresRepository ever
        # sees it, so a bad deployment crashes once, now, at import - and
        # hand the same engine to PostgresRepository below rather than
        # letting it open a second pool for the same URL.
        engine = make_engine(database_url)
        with engine.connect():
            pass

    return PostgresRepository(database_url, engine=engine)


store: GameRepository = select_store()
"""The one instance every request shares. See this module's docstring for
why nothing may rebind this name after import."""


def new_seat_uids(game: Game) -> dict[str, str]:
    """A fresh secret uid for each of the game's players: uid -> username."""
    return {uuid4().hex: player.username for player in game.players}


def player_for_advisor_uid(game_id: str, uid: str) -> str | None:
    """Whose view this advisor uid reads, or `None` when it is not one of
    this game's advisors (`repository.Advisor`).

    The mirror of `store.player_for_uid` for the other kind of credential,
    and deliberately a *separate* lookup rather than a widening of that one:
    `player_for_uid` answers "this uid may act as that player", which is
    exactly what an advisor uid may not do, and every existing caller of it
    reads it that way. Keeping them apart is what lets `game_access.py`
    decide with one unambiguous question which kind of credential it is
    holding.
    """
    advisor = store.advisor_uids(game_id).get(uid)
    return None if advisor is None else advisor.player


def known_players(game_id: str) -> list[str]:
    """This game's player names, read only from columns that need no decoding.

    `[p.username for p in game.players]` is the normal way to answer this
    and needs the `Game`. Every listing route also has to answer it for a
    game whose snapshot this build refuses to decode
    (`repository.GameUnloadable`), where there is no `Game` to ask - and
    "the game you were playing, with nobody in it" is a worse answer than
    no entry at all, which is what this exists to avoid.

    Both sources are registered at creation, beside the game and never from
    it: `seat_uids` (uid -> username) by every create path there is, and
    `decklists` (username -> text) by the lobby's. Sorted so a listing is
    stable rather than dependent on dict order.
    """
    names = set(store.seat_uids(game_id).values()) | set(store.decklists(game_id))
    return sorted(names)
