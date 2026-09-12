"""T10's added scope: the HTTP 409 surface for `VersionConflict`.

T9 delivered the compare-and-swap (`repository_postgres.py`'s `replace()`)
and its own repository-level race test (`test_repository_postgres_cas.py`),
but nothing mapped `VersionConflict` to an HTTP response - it would have
surfaced as a bare 500. This file proves the mapping end to end, through a
real mutating route, not just at the repository layer that file already
covers.

**Why a monkeypatched `commands.run_command` stands in for a second thread.**
A genuine two-thread race against the *same* `PostgresRepository` instance
can never conflict: that instance's own `locked()` is a process-local
`threading.Lock` per game (`repository_postgres.py`'s module docstring), so
two requests through the same `store` just serialise, exactly the way
`InMemoryRepository`'s mutex does. A real conflict needs two *separate*
`PostgresRepository` instances - `test_repository_postgres_cas.py` builds
that with two real threads, each with its own instance, timed with a
`threading.Barrier`. Reusing that shape here, but through `app.py`'s single
route function rather than a bare `repo.replace()` call, would need two
independently-running copies of the FastAPI app; this repo has exactly one
process-wide `store` singleton (`store.py`), so there is nowhere to attach a
second one for the length of one test. Instead, this monkeypatches
`commands.run_command` - the one call `app._run_command` makes between its
own read (which captures `expected_version`) and its own `store.replace`
(which uses it) - so that a *second*, independent `PostgresRepository`
commits the conflicting write in that exact window, deterministically. This
reaches the identical code path a real second replica would: `app._run_
command` still reads first, still calls `commands.run_command` for real
afterward, and still calls the real `store.replace` with the version it
already had - only the *timing* of the other writer is pinned rather than
raced.

Skips under the plain `mise run test` (docker-free), fails loudly rather
than skipping under `mise run test:pg` - see `conftest.
require_postgres_database_url`, the same gate `test_repository_postgres_cas.
py` uses.
"""

from __future__ import annotations

import sys

import pytest
from conftest import require_postgres_database_url
from fastapi.testclient import TestClient
from sqlalchemy import text

from magic_cards_model_api import commands as commands_module
from magic_cards_model_api import store as store_module
from magic_cards_model_api.db import make_engine
from magic_cards_model_api.main import app
from magic_cards_model_api.repository_postgres import PostgresRepository

# `magic_cards_model_api/__init__.py` does `from magic_cards_model_api.app
# import app` - the FastAPI instance - which rebinds the *package's* own
# `app` attribute to that instance, so `import magic_cards_model_api.app`
# resolves to the FastAPI object, not the submodule, once the package has
# finished initialising. `sys.modules` is keyed by the full dotted name
# regardless, so this is the one reliable way to reach the submodule itself
# (needed here to monkeypatch its `store` name) rather than the app object
# `magic_cards_model_api.main` also exports it as.
app_module = sys.modules["magic_cards_model_api.app"]
game_access_module = sys.modules["magic_cards_model_api.game_access"]

_TEST_DATABASE_URL_ENV = "MCM_TEST_DATABASE_URL"


@pytest.fixture
def database_url() -> str:
    return require_postgres_database_url(_TEST_DATABASE_URL_ENV)


@pytest.fixture
def postgres_app(monkeypatch: pytest.MonkeyPatch, database_url: str):
    """`store` swapped for a fresh `PostgresRepository` (`repo_a`, "this
    replica") for the length of one test, plus a `TestClient` over the real
    app and `repo_b`, a second, independent instance ("another replica")
    against the same database - the two `PostgresRepository` instances
    `test_repository_postgres_cas.py` already uses, just with one of them
    wired to the app the way `store.py`'s real singleton is.

    **Two separate monkeypatches, deliberately.** `app.py` does `from
    .store import store` once, at import time - a static binding this
    fixture must overwrite directly (`app_module.store`) for `app.py`'s own
    routes to see `repo_a`. `audit.py` (unlike `app.py`) re-imports `from
    .store import store` *lazily*, inside `record`/`entries` themselves
    (its own module docstring: "so tests that read the log directly ...
    keep working"), which means it reads whatever `magic_cards_model_api.
    store.store` currently is at each call - so patching `store_module.
    store` too is what makes `audit.record`'s own `store.append_record`/
    `fill_record`/`records` calls land on `repo_a` as well, not the
    process's real (untouched) `InMemoryRepository` singleton. Missing
    either one is exactly how this test was first found broken: `app.py`'s
    half alone produces a real 409 from the CAS, but `audit.record`'s
    follow-up `fill_record` call then silently hit the *other*, unpatched
    store and raised `KeyError` - a `store.py` wiring bug in the test, not
    in `repository_postgres.py`.
    """
    engine = make_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE records, games RESTART IDENTITY CASCADE"))
    repo_a = PostgresRepository(database_url, engine=make_engine(database_url))
    repo_b = PostgresRepository(database_url, engine=make_engine(database_url))
    monkeypatch.setattr(app_module, "store", repo_a)
    monkeypatch.setattr(store_module, "store", repo_a)
    # And a third, for the same reason as the first: `game_access.py` does
    # `from .store import store` at import time too, and it now *reads* that
    # store on every request - its positive credential rule asks whether the
    # uid opens a seat of this game and whether it is the admin key, where
    # before it only asked `owner_user_id` and took `None` for "unowned, let
    # it through". Left unpatched, the gate consults the process's real
    # (untouched) `InMemoryRepository`, finds no such game, and answers the
    # 404 it owes an unknown one - so the 409 under test never happens.
    monkeypatch.setattr(game_access_module, "store", repo_a)
    with TestClient(app) as client:
        yield client, repo_a, repo_b


def test_a_losing_concurrent_writer_gets_a_409_carrying_the_current_state(
    postgres_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repo_a, repo_b = postgres_app
    created = client.post(
        "/api/games",
        json={"players": [{"username": "alice"}, {"username": "bob"}], "active_player": "alice"},
    )
    assert created.status_code == 200, created.text
    game_id = created.json()["game_id"]
    admin_key = created.json()["admin_key"]
    assert created.json()["step"] == "precombat_main"

    real_run_command = commands_module.run_command
    landed = {"done": False}

    def racing_run_command(game, command):
        # Lands the conflicting commit in the exact window `app._run_command`
        # itself documents: after its own read (which already captured
        # `expected_version` into `repo_a`'s cache) and before its own
        # `store.replace` call - see this module's own docstring.
        if not landed["done"]:
            landed["done"] = True
            winner_game = repo_b.get(game_id)
            assert winner_game is not None
            winner_game = winner_game.model_copy(deep=True)
            # `repo_b.get` reads the row `store.create` wrote, which predates
            # `create_game`'s own `engine_api.set_step` call - a plain
            # in-place mutation on the object `store.create` already
            # returned, never persisted through a second `replace` (a
            # pre-existing gap, out of T10's scope, this works around
            # rather than exercises). `game` is `repo_a`'s own object, the
            # one this very function was handed - copying its step keeps
            # the race about the one field it deliberately changes (life),
            # not an unrelated side effect of that gap.
            winner_game.turn.step = game.turn.step
            winner_game.players[0].life = 1
            repo_b.replace(game_id, winner_game)
        return real_run_command(game, command)

    monkeypatch.setattr(commands_module, "run_command", racing_run_command)

    conflicted = client.post(
        f"/api/games/{game_id}/advance",
        params={"uid": admin_key},
        json={"player": "alice"},
    )
    assert conflicted.status_code == 409, conflicted.text
    body = conflicted.json()
    assert body["game_id"] == game_id
    # The body carries the state as it now actually stands - the winner's
    # committed life total, not whatever `advance` itself would have
    # produced from the stale read it started with.
    current_players = {p["username"]: p["life"] for p in body["current_state"]["players"]}
    assert current_players["alice"] == 1
    # `advance` never actually ran against the committed game: the step this
    # replica had is untouched, exactly like a `VersionConflict` from
    # `test_repository_postgres_cas.py`'s own repository-level test.
    assert body["current_state"]["step"] == "precombat_main"

    # No server-side retry happened - `_run_command` raised straight through
    # to the 409 handler rather than quietly reapplying the advance against
    # the now-current state. Confirmed independently, off `repo_b`'s own
    # committed row: the step never moved.
    refreshed_directly = repo_b.get(game_id)
    assert refreshed_directly is not None
    assert refreshed_directly.turn is not None
    assert refreshed_directly.turn.step.value == "precombat_main"


def test_the_loser_can_reread_and_reapply_through_the_same_route(
    postgres_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of a 409 over a 500: the loser is told, reads the current
    state back, and its very next call against the same route succeeds -
    without this test (or a caller) doing anything to force a retry."""
    client, repo_a, repo_b = postgres_app
    created = client.post(
        "/api/games",
        json={"players": [{"username": "alice"}, {"username": "bob"}], "active_player": "alice"},
    )
    game_id = created.json()["game_id"]
    admin_key = created.json()["admin_key"]

    real_run_command = commands_module.run_command
    landed = {"done": False}

    def racing_run_command(game, command):
        if not landed["done"]:
            landed["done"] = True
            winner_game = repo_b.get(game_id)
            assert winner_game is not None
            winner_game = winner_game.model_copy(deep=True)
            # `repo_b.get` reads the row `store.create` wrote, which predates
            # `create_game`'s own `engine_api.set_step` call - a plain
            # in-place mutation on the object `store.create` already
            # returned, never persisted through a second `replace` (a
            # pre-existing gap, out of T10's scope, this works around
            # rather than exercises). `game` is `repo_a`'s own object, the
            # one this very function was handed - copying its step keeps
            # the race about the one field it deliberately changes (life),
            # not an unrelated side effect of that gap.
            winner_game.turn.step = game.turn.step
            winner_game.players[0].life = 1
            repo_b.replace(game_id, winner_game)
        return real_run_command(game, command)

    monkeypatch.setattr(commands_module, "run_command", racing_run_command)

    conflicted = client.post(
        f"/api/games/{game_id}/advance", params={"uid": admin_key}, json={"player": "alice"}
    )
    assert conflicted.status_code == 409, conflicted.text

    # Un-patch: this next call is `repo_a`'s own genuine re-read-and-reapply,
    # exactly like any other request - `repo_a`'s cache was already refreshed
    # to the winner's committed row by the failed `replace()` above
    # (`repository_postgres.py`'s own conflict handling), so this is not a
    # hand-tuned retry, it is the ordinary next call succeeding.
    monkeypatch.setattr(commands_module, "run_command", real_run_command)
    retried = client.post(
        f"/api/games/{game_id}/advance", params={"uid": admin_key}, json={"player": "alice"}
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["allowed"] is True
    assert retried.json()["advanced_to"] == "beginning_of_combat"

    # The re-applied advance is on top of the winner's life total, not a
    # value this replica silently reverted.
    board = client.get(f"/api/games/{game_id}", params={"uid": admin_key})
    alice_life = next(p["life"] for p in board.json()["players"] if p["username"] == "alice")
    assert alice_life == 1

    # And repo_a's own log now carries both the winner's commit (recorded
    # through `repo_b` directly - this test never audited it) and this
    # replica's own successful advance, proving the log is the same shared
    # table both replicas write into.
    log = client.get(f"/api/games/{game_id}/log", params={"uid": admin_key}).json()["log"]
    advances = [entry for entry in log if entry["action"] == "advance"]
    assert len(advances) == 1
    assert advances[0]["detail"]["allowed"] is True
