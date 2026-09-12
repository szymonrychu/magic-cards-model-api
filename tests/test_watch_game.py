"""`scripts/watch_game.py` returns exactly one of its named edges as JSON -
see the module's own docstring. These tests pin that contract against the
defect that slipped through it: a 404 mid-watch (the game's row gone from
the server) used to raise a bare `KeyError: 'log'` instead of a named edge,
because the log fetch indexed `["log"]" straight off the response with no
regard for what actually came back.

Every test drives `watch_game.watch` against an `httpx.MockTransport` rather
than a real server, so the whole edge set - old and new - runs in-process and
fast.
"""

from __future__ import annotations

from typing import Any

import httpx

from scripts import watch_game

GAME = "g-1"


def _log(entries: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"log": entries})


def _state(**fields: Any) -> httpx.Response:
    return httpx.Response(200, json=fields)


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_regression_report_edge_fires_on_a_filed_problem_report() -> None:
    entries = [{"action": "report_problem", "detail": "engine bug"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log(entries)
        return _state(turn=3)

    result = watch_game.watch(
        "http://game",
        GAME,
        0,
        max_seconds=5,
        stall_seconds=5,
        poll=0.01,
        transport=transport(handler),
    )
    assert result["edge"] == "report"
    assert result["reports"] == entries


def test_regression_game_over_edge_fires_when_the_state_names_a_winner() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log([])
        return _state(winner="alice", drawn=False, turn=8)

    result = watch_game.watch(
        "http://game",
        GAME,
        0,
        max_seconds=5,
        stall_seconds=5,
        poll=0.01,
        transport=transport(handler),
    )
    assert result["edge"] == "game_over"
    assert result["winner"] == "alice"


def test_regression_stalled_edge_fires_after_quiet_seconds_with_no_new_log_entries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log([{"action": "pass"}])
        return _state(turn=2, step="main1", priority_holder="bob")

    result = watch_game.watch(
        "http://game",
        GAME,
        0,
        max_seconds=5,
        stall_seconds=0.05,
        poll=0.01,
        transport=transport(handler),
    )
    assert result["edge"] == "stalled"
    assert result["turn"] == 2


def test_regression_timeout_edge_fires_when_max_seconds_elapses_first() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log([])
        return _state(turn=1)

    result = watch_game.watch(
        "http://game",
        GAME,
        0,
        max_seconds=0.05,
        stall_seconds=5,
        poll=0.01,
        transport=transport(handler),
    )
    assert result["edge"] == "timeout"


def test_a_404_mid_watch_produces_a_named_edge_not_a_keyerror() -> None:
    """The real failure: a sibling test run truncated the shared postgres
    `games` table mid-gauntlet, and the watched games started 404ing. The old
    code indexed `response.json()["log"]` straight off that 404 body and blew
    up with `KeyError: 'log'`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "game not found"})

    result = watch_game.watch(
        "http://game",
        GAME,
        0,
        max_seconds=5,
        stall_seconds=5,
        poll=0.01,
        transport=transport(handler),
    )
    assert result["edge"] == "game_gone"
    assert result["status"] == 404
    assert result["body"] == {"detail": "game not found"}
    assert result["game"] == GAME


def test_an_unexpected_body_shape_produces_a_named_edge_not_a_crash() -> None:
    """A 200 whose body does not carry the key the watcher fetched it for -
    a server contract change, or a proxy swallowing the real response - must
    come back as a named outcome too, not a second flavour of `KeyError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return httpx.Response(200, json={"unexpected": "shape"})
        return _state(turn=1)

    result = watch_game.watch(
        "http://game",
        GAME,
        0,
        max_seconds=5,
        stall_seconds=5,
        poll=0.01,
        transport=transport(handler),
    )
    assert result["edge"] == "watcher_error"
    assert result["status"] == 200
    assert result["body"] == {"unexpected": "shape"}
    assert result["game"] == GAME


def test_watcher_error_also_covers_a_non_404_non_200_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    result = watch_game.watch(
        "http://game",
        GAME,
        0,
        max_seconds=5,
        stall_seconds=5,
        poll=0.01,
        transport=transport(handler),
    )
    assert result["edge"] == "watcher_error"
    assert result["status"] == 500
    assert result["body"] == {"detail": "boom"}
