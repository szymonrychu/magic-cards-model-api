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

from uuid import uuid4

from magic_cards_model.models import Game

from .db import configured_database_url
from .repository import GameRepository, InMemoryRepository

__all__ = ["GameRepository", "GameStore", "new_seat_uids", "store"]

GameStore = InMemoryRepository
"""Backward-compatible alias. Every existing `from .store import GameStore`
and every direct `GameStore()` construction in tests keeps working - this is
literally the same class, not a copy of it. It is deliberately NOT what
`store` below is necessarily an instance of any more: the singleton's class
is chosen from the environment, while this name keeps meaning what it always
meant, "the in-memory repository"."""


def select_store() -> GameRepository:
    """The repository this process runs on, per the environment.

    Imports `repository_postgres` only when it is actually going to be used,
    so nothing about a docker-free run pays for SQLAlchemy engine setup or
    fails on a driver it does not need.
    """
    database_url = configured_database_url()
    if database_url is None:
        return InMemoryRepository()
    from .repository_postgres import PostgresRepository

    return PostgresRepository(database_url)


store: GameRepository = select_store()
"""The one instance every request shares. See this module's docstring for
why nothing may rebind this name after import."""


def new_seat_uids(game: Game) -> dict[str, str]:
    """A fresh secret uid for each of the game's players: uid -> username."""
    return {uuid4().hex: player.username for player in game.players}
