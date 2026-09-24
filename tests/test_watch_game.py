"""`scripts/watch_game.py` returns exactly one of its named edges as JSON -
see the module's own docstring. These tests pin that contract against the
defect that slipped through it: a 404 mid-watch (the game's row gone from
the server) used to raise a bare `KeyError: 'log'` instead of a named edge,
because the log fetch indexed `["log"]" straight off the response with no
regard for what actually came back.

Four more bare indexings survived that fix, all reachable on the same case:
the `--game` unset default path (`latest_game`, which bypassed `_get_json`
entirely and was called outside the loop's `try`), a `log` whose value is not
a list, a log row that is not an entry, and a dict row with no `action`. Each
has its own test below.

**DR8**: the `report` edge fires only for a `report_problem` row whose
`detail.category` is `engine` - or a legacy row with no `category` at all,
which counts as `engine` too. A `dsl` or `ux` row never stops the watch; it
is collected into `findings` (`{"seq", "player", "category", "text"}`)
instead, and `findings` rides along on whichever edge ends the watch
(`report`, `game_over`, `stalled`, `timeout`) - never on `game_gone`/
`watcher_error`, which abort before the log is read at all. `game_over` also
carries this game's per-seat `play_stats` (DR2's `GET .../review`) when
`--review-key` makes that reachable, or `None` with a `play_stats_reason`
when it is not.

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


def test_the_default_cli_path_with_no_game_named_reaches_a_named_edge_too() -> None:
    """`--game` unset is the usage the module docstring itself shows, and it
    was the one path with no named edge at all: `latest_game` indexed
    `client.get("/api/games").json()["games"]` with no `_get_json` between
    it and the response, and it is called OUTSIDE the loop's `try`. So a
    vanished game on the default path raised `KeyError: 'games'` - the bare
    indexing this module's own `WatchFailure` docstring says it exists to
    prevent, on exactly the case the edge was added for."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "game not found"})

    result = watch_game.watch(
        "http://game",
        None,
        0,
        max_seconds=5,
        stall_seconds=5,
        poll=0.01,
        transport=transport(handler),
    )
    assert result["edge"] == "game_gone"
    assert result["status"] == 404
    assert result["body"] == {"detail": "game not found"}
    assert result["game"] is None, "no game was ever resolved, so none is named"


def test_a_game_listing_of_the_wrong_shape_is_a_named_edge_not_a_keyerror() -> None:
    """The same fetch, the other way it surprises: a 200 whose body does not
    carry `games` at all."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    result = watch_game.watch(
        "http://game",
        None,
        0,
        max_seconds=5,
        stall_seconds=5,
        poll=0.01,
        transport=transport(handler),
    )
    assert result["edge"] == "watcher_error"
    assert result["status"] == 200
    assert result["body"] == {"unexpected": "shape"}


def test_a_log_that_is_not_a_list_is_a_named_edge_not_a_typeerror() -> None:
    """`_get_json`'s docstring promises "never a bare `KeyError`/`TypeError`
    from indexing an unexpected shape", and it validated only that the key
    was PRESENT. `entries[since:]` on a non-list is the `TypeError` half."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return httpx.Response(200, json={"log": {"0": {"action": "pass"}}})
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
    assert result["body"] == {"log": {"0": {"action": "pass"}}}


def test_a_log_row_that_is_not_an_entry_is_a_named_edge_not_a_keyerror() -> None:
    """`e["action"]` at the report scan and again in the `stalled` tail. In
    the gauntlet a row without one degraded to
    `watcher_failed {"error": "'action'"}` through the blanket except in
    `scripts/eval_gauntlet.py` - the unreadable outcome these named edges
    were added to replace."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return httpx.Response(200, json={"log": ["not an entry at all"]})
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
    assert result["body"] == {"log": ["not an entry at all"]}


def test_dr8_a_dsl_report_problem_does_not_stop_the_watch_and_appears_in_findings() -> None:
    """A `dsl` category is development signal for the DSL/UX loop, not an
    engine problem - it must never fire the `report` edge, only ride along
    as a `findings` row on whatever edge the watch actually ends on."""
    entries = [
        {
            "seq": 1,
            "action": "report_problem",
            "detail": {"player": "bob", "text": "no move exists for foretell", "category": "dsl"},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log(entries)
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
    assert result["findings"] == [
        {"seq": 1, "player": "bob", "category": "dsl", "text": "no move exists for foretell"}
    ]


def test_dr8_a_ux_report_problem_does_not_stop_the_watch_and_appears_in_findings() -> None:
    entries = [
        {
            "seq": 2,
            "action": "report_problem",
            "detail": {"player": "alice", "text": "run_script needed four calls", "category": "ux"},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log(entries)
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
    assert result["findings"] == [
        {"seq": 2, "player": "alice", "category": "ux", "text": "run_script needed four calls"}
    ]


def test_dr8_an_engine_category_report_stops_the_watch_and_carries_no_findings() -> None:
    entries = [
        {
            "seq": 3,
            "action": "report_problem",
            "detail": {"player": "bob", "text": "trample math is wrong", "category": "engine"},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log(entries)
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
    assert result["edge"] == "report"
    assert result["reports"] == entries
    assert result["findings"] == []


def test_dr8_a_legacy_report_row_with_no_category_key_still_stops_the_watch() -> None:
    """A row written before `category` existed carries no `detail.category`
    key at all - `audit.problem_category`'s own fallback treats that as
    `engine`, and this watcher must match it exactly, not merely default a
    missing top-level key the way the pre-DR8 code accidentally did."""
    entries = [
        {
            "seq": 4,
            "action": "report_problem",
            "detail": {"player": "bob", "text": "a bug from before categories existed"},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log(entries)
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
    assert result["edge"] == "report"
    assert result["reports"] == entries
    assert result["findings"] == []


def test_dr8_game_over_carries_findings_and_play_stats_from_review_when_reachable() -> None:
    entries = [
        {
            "seq": 1,
            "action": "report_problem",
            "detail": {
                "player": "bob",
                "text": "DSL is missing a mulligan move",
                "category": "dsl",
            },
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log(entries)
        if request.url.path.endswith("/review"):
            assert request.url.params.get("key") == "admin-secret"
            return httpx.Response(
                200,
                json={"play_stats": {"alice": {"plays_total": 3}, "bob": {"plays_total": 1}}},
            )
        return _state(winner="alice", drawn=False, turn=8)

    result = watch_game.watch(
        "http://game",
        GAME,
        0,
        max_seconds=5,
        stall_seconds=5,
        poll=0.01,
        transport=transport(handler),
        review_key="admin-secret",
    )
    assert result["edge"] == "game_over"
    assert result["findings"] == [
        {"seq": 1, "player": "bob", "category": "dsl", "text": "DSL is missing a mulligan move"}
    ]
    assert result["play_stats"] == {"alice": {"plays_total": 3}, "bob": {"plays_total": 1}}
    assert result["play_stats_reason"] is None


def test_dr8_game_over_play_stats_is_null_with_a_reason_when_no_review_key_is_given() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log([])
        if request.url.path.endswith("/review"):
            raise AssertionError("GET /review must never be called with no --review-key")
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
    assert result["findings"] == []
    assert result["play_stats"] is None
    assert result["play_stats_reason"]


def test_dr8_game_over_play_stats_is_null_with_a_reason_when_review_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log([])
        if request.url.path.endswith("/review"):
            return httpx.Response(404, json={"detail": "game not found"})
        return _state(winner="alice", drawn=False, turn=8)

    result = watch_game.watch(
        "http://game",
        GAME,
        0,
        max_seconds=5,
        stall_seconds=5,
        poll=0.01,
        transport=transport(handler),
        review_key="admin-secret",
    )
    assert result["edge"] == "game_over"
    assert result["play_stats"] is None
    assert result["play_stats_reason"]


def test_dr8_stalled_edge_also_carries_findings() -> None:
    entries = [
        {
            "seq": 1,
            "action": "report_problem",
            "detail": {"player": "bob", "text": "guide push conflicted twice", "category": "ux"},
        },
        {"action": "pass"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log(entries)
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
    assert result["findings"] == [
        {"seq": 1, "player": "bob", "category": "ux", "text": "guide push conflicted twice"}
    ]


def test_a_log_row_with_no_action_still_watches_rather_than_crashing() -> None:
    """A dict row missing `action` is not a broken log - it is simply not a
    problem report and has no verb to show in a `stalled` tail. Both reads
    tolerate it, and the watch runs on to its real edge."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/log"):
            return _log([{"seq": 1}, {"action": "pass", "seq": 2}])
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
    assert result["tail"] == [None, "pass"]
