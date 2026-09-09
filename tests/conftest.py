"""Shared fixtures. Card data comes only from the recorded Scryfall fixtures
in tests/fixtures/scryfall - the network is never touched."""

from __future__ import annotations

import os
from pathlib import Path

os.environ["MCM_CACHE_DIR"] = str(Path(__file__).parent / "fixtures" / "scryfall")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from magic_cards_model.machines import Step  # noqa: E402

from magic_cards_model_api import ai_seat, lobby  # noqa: E402
from magic_cards_model_api.main import app  # noqa: E402
from magic_cards_model_api.store import store  # noqa: E402

#: Set only by `mise run test:pg` - never by the plain `mise run test`, which
#: explicitly `unset`s it. With this set, a postgres-only test that cannot
#: get a database URL must fail loudly (`require_postgres_database_url`
#: below), not skip: a plain `pytest.skip` inside a fixture is invisible to
#: CI's pass/fail line, so a variable name that ever drifts between
#: mise.toml/CI and a test file would otherwise leave the entire postgres
#: suite silently unexecuted while `test:pg` still reports success.
MCM_REQUIRE_POSTGRES_TESTS = "MCM_REQUIRE_POSTGRES_TESTS"


def require_postgres_database_url(env_var: str) -> str:
    """The postgres DSN read from `env_var`, or a skip/hard-failure depending
    on whether this run is required to actually exercise postgres. Every
    postgres-only fixture (the contract suite's postgres parameterisation,
    the CAS race tests) calls this instead of inlining its own
    `pytest.skip`, so the fail-instead-of-skip behaviour lives in exactly one
    place."""
    url = os.environ.get(env_var)
    if url:
        return url
    if os.environ.get(MCM_REQUIRE_POSTGRES_TESTS):
        pytest.fail(
            f"{env_var} is not set, but {MCM_REQUIRE_POSTGRES_TESTS}=1 (set by "
            "`mise run test:pg`) - the postgres suite is required to actually "
            "run, not skip"
        )
    pytest.skip(
        f"{env_var} is not set - postgres tests are skipped (run `mise run "
        "test:pg` to exercise them)"
    )
    raise AssertionError("unreachable: pytest.skip/pytest.fail always raise")


@pytest.fixture(autouse=True)
def _no_real_claude_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /api/lobby/games` now starts each `ai: true` seat itself, right
    after booting the game - which, in `subprocess` mode, means a game
    created in a test would otherwise spawn a *real* `claude` process the
    moment this dev machine happens to have one on PATH (it does).

    `AiSeatRegistry.start` already refuses to spawn when `claude_available()`
    is false, reporting a harmless fallback instead - so defaulting it to
    false here is already enough to keep every test safe without touching the
    mechanism itself. `command_for` is defaulted to a harmless stand-in too,
    as a second, independent line of defense at the actual spawn boundary -
    belt and suspenders against a future bug that calls `subprocess.Popen`
    without checking `claude_available()` first.

    Tests that exercise the real spawn path or the real argv/fallback text
    (`test_ai_seat.py`, `test_ai_seat_settings.py`, `test_lobby_reset.py`,
    `test_mulligans.py`) override one or both back themselves - `command_for`
    either to their own stand-in or to the real function captured at import
    time, before this fixture ever ran."""
    monkeypatch.setattr(ai_seat, "claude_available", lambda: False)
    monkeypatch.setattr(
        ai_seat, "command_for", lambda prompt, agent: ["python3", "-c", "print('DONE')"]
    )


def add_card(client: TestClient, game_id: str, name: str, player: str, zone: str, **kwargs):
    response = client.post(
        f"/api/games/{game_id}/cards",
        json={"card": name, "player": player, "zone": zone, **kwargs},
    )
    assert response.status_code == 200, response.text
    return response.json()


_ADMIN_KEYS: dict[str, str] = {}  # game_id -> admin key, stashed by the `game` fixture


def state(client: TestClient, game_id: str, uid: str | None = None) -> dict:
    """The board, filtered exactly as the API filters it for `uid`.

    With no `uid`, this reaches for the admin key the `game` fixture stashed
    at creation - the harness built this board, so it gets the harness's own
    full view, the same as any other operator holding that key. Tests that
    want the *filtered*, seat-scoped view (what a real player sees) pass a
    real seat uid explicitly.
    """
    if uid is None:
        uid = _ADMIN_KEYS.get(game_id)
    response = client.get(f"/api/games/{game_id}", params={"uid": uid} if uid else None)
    assert response.status_code == 200, response.text
    return response.json()


def find(cards: list[dict], name: str) -> dict:
    matches = [card for card in cards if card["name"] == name]
    assert matches, f"no {name} among {[card['name'] for card in cards]}"
    return matches[0]


def force_turn_position(game_id: str, step: Step, active_player: str) -> None:
    """Reach past the API into the live engine's own `Turn`/`PriorityMachine`
    to position a game at `step` for `active_player`, without replaying the
    turn machine to get there - legitimate test setup (the same "reach past
    the API, straight into the live engine object" idiom `test_special_
    actions.py`'s `_bump_turn` already used before this helper existed), and
    never a substitute for driving the real transition into the step under
    test, which every caller still does through the API afterwards.

    `PriorityMachine` tracks its own `active_index`/`holder_index` rather
    than reading `Turn`'s - `Game.advance` re-syncs it with `self.priority.
    reset(self.turn.active_index)` on every real turn rollover, so this does
    the same rather than only calling `sync_priority_with_step`, which
    grants or withholds for the step now in progress but never moves whose
    turn it resumes at (`PriorityMachine.grant`'s own docstring: "the seat
    it resumes at is unchanged").
    """
    game = store.get(game_id)
    assert game is not None and game.turn is not None and game.priority is not None
    game.turn.step = step
    game.turn.active_index = game.turn.players.index(active_player)
    game.priority.reset(game.turn.active_index)
    game.sync_priority_with_step()


def resolve_whole_stack(client: TestClient, game_id: str) -> list[str]:
    """Resolve every object on the stack, top down. Returns what resolved,
    in order - the caller's own way to drain triggers (CR 702.62a's suspend
    countdown among them, now a real triggered ability rather than a
    response field reporting a hand-driven removal)."""
    resolved: list[str] = []
    while state(client, game_id)["stack"]:
        body = client.post(f"/api/games/{game_id}/resolve-top").json()
        resolved.append(body["resolved"])
    return resolved


def drain_suspend_upkeeps(client: TestClient, game_id: str, player: str, count: int) -> None:
    """Walk `player` through `count` real upkeeps via `advance`, resolving
    CR 702.62a's countdown trigger (and, on the last one, its own free-cast
    grant) each time - the real path a suspended card's counters come off
    now that nothing in this repo drives the removal by hand. `PUT .../step`
    cannot stand in for this: it is a raw teleport that runs none of
    `Game.advance`'s own beginning-of-step triggers."""
    for _ in range(count):
        force_turn_position(game_id, Step.UNTAP, player)
        advanced = client.post(f"/api/games/{game_id}/advance", json={"player": player})
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["advanced_to"] == "upkeep", advanced.json()
        resolve_whole_stack(client, game_id)


def card_id(client: TestClient, game_id: str, name: str, player: str, zone: str) -> str:
    """Add `name` to `player`'s `zone` and return the id it was dealt.

    `hands` and `graveyards` in `state()` are per-player views; `battlefield`
    and `exile` are shared across both players, so only the former two are
    looked up by `player`.
    """
    add_card(client, game_id, name, player, zone)
    board = state(client, game_id)
    per_player = {"hand": board["hands"], "graveyard": board["graveyards"]}
    pool = per_player[zone][player] if zone in per_player else board[zone]
    return find(pool, name)["id"]


def life_of(state_dict: dict, username: str) -> int:
    return next(player for player in state_dict["players"] if player["username"] == username)[
        "life"
    ]


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client
    # `store` is a module-level singleton shared by the whole pytest process,
    # not reset between tests just because `TestClient`'s context manager
    # exited. Since `POST /api/lobby/games` no longer resets a previous lobby
    # game on its own (`lobby.py` - End game is the only reset now), a test
    # that boots one and forgets to end or delete it would otherwise leak a
    # live slot into every test that runs after it: `MAX_LIVE_GAMES`
    # eventually refuses a perfectly innocent later test's boot, and
    # `Seats`-style "the one live game" discovery starts pointing at a game
    # nobody in the current test created (`test_mcp_resilience.py`). This is
    # the safety net a well-behaved test does not need (it already deletes
    # what it created), and the one a forgetful test cannot do without: every
    # game still in the store when a test ends is cleared before the next one
    # starts, lobby-booted games through `lobby.reset_game` (so their
    # watcher threads and AI seats are released too, not just deleted out from
    # under them), everything else through a plain `store.delete`.
    for game_id in store.ui_games():
        lobby.reset_game(game_id, reason="test cleanup")
    for game_id in store.ids():
        store.delete(game_id)


@pytest.fixture()
def admin_key(game: str) -> str:
    """The `game` fixture's own admin key - the operator's full-view
    credential, for tests that need to assert what that view still shows."""
    return _ADMIN_KEYS[game]


@pytest.fixture()
def game(client: TestClient) -> str:
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    assert response.status_code == 200, response.text
    info = response.json()
    game_id = info["game_id"]
    admin_key = info["admin_key"]
    _ADMIN_KEYS[game_id] = admin_key
    yield game_id
    _ADMIN_KEYS.pop(game_id, None)
    client.delete(f"/api/games/{game_id}", params={"key": admin_key})
