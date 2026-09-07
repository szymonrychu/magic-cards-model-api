"""Poll-and-return: `mcp.wait_for_priority` and every `run_action`-backed tool
never block, and a pending decision addressed to the caller comes back as an
MCP 2026-07-28 `input_required` result (`InputRequiredResult`,
`result_type: "input_required"` on the wire) rather than plain text a caller
could ignore.

MCP protocol revision 2026-07-28 removed resumability (SEP-2577): a tool call
that blocks waiting for something to happen loses the request outright if the
underlying stream breaks mid-wait. `seatkit.block_until_wake` used to hold one
open for up to `max_seconds` (570s by default for `wait_for_priority`); it now
checks once and returns immediately either way (`seatkit.NOTHING_NEW_YET`),
and this file is what pins "immediately" - not merely "eventually, with a
shorter bound".
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi.testclient import TestClient
from mcp_types import InputRequiredResult
from test_decisions import advance_into_cleanup, seats_for, stuff_hand

from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats


def wired(client: TestClient, monkeypatch, game_id: str) -> tuple[Any, dict[str, str]]:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id), seats_for(client, game_id)


def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, arguments))


def test_wait_for_priority_returns_immediately_with_nothing_pending(client, game, monkeypatch):
    """No wake condition holds yet (alice already has priority and nothing
    has changed since she last looked - there is no `before` at all here, so
    every wake condition genuinely requires something this check cannot see
    on the pregame board): this must return fast, not after riding out
    `max_seconds`, and as an ordinary text result, not `input_required`."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)

    started = time.monotonic()
    result = call_tool(server, "wait_for_priority", {"player_uid": uids["bob"]})
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"wait_for_priority blocked for {elapsed:.1f}s - it must poll and return"
    assert not isinstance(result, InputRequiredResult)
    assert "NOTHING_NEW_YET" not in result.content[0].text  # sanity: real prose, not the const name
    assert "nothing new yet" in result.content[0].text


def test_wait_for_priority_returns_input_required_for_a_pending_decision(client, game, monkeypatch):
    """alice's own cleanup discard is pending: `wait_for_priority` must not
    just mention it in a wake_reason string a caller could skim past - it
    has to come back shaped as `input_required` on the wire, so an MCP client
    that understands the 2026-07-28 dual-result-type surface can tell "this
    call cannot proceed until you resolve something" apart from an ordinary
    text answer."""
    seats, uids = wired(client, monkeypatch, game)
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard: a real choice
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    paused = advance_into_cleanup(game)
    assert paused is not None, "the discard should have asked, not defaulted silently"

    server = build_server(seats)
    started = time.monotonic()
    result = call_tool(server, "wait_for_priority", {"player_uid": uids["alice"]})
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"wait_for_priority blocked for {elapsed:.1f}s instead of returning at once"
    )
    assert isinstance(result, InputRequiredResult), (
        f"expected an InputRequiredResult, got {type(result)}: {result!r}"
    )
    assert result.request_state is not None
    assert "choose_cards" in result.request_state


def test_wait_for_priority_does_not_shape_someone_elses_decision_as_input_required(
    client, game, monkeypatch
):
    """A decision pending for alice is not bob's `input_required` moment -
    bob's own `wait_for_priority` still has nothing new for him, plain
    text, not a claim that he must resolve something."""
    seats, uids = wired(client, monkeypatch, game)
    stuff_hand(client, game, "alice", 8)
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    paused = advance_into_cleanup(game)
    assert paused is not None

    server = build_server(seats)
    result = call_tool(server, "wait_for_priority", {"player_uid": uids["bob"]})
    assert not isinstance(result, InputRequiredResult)


def test_an_action_tool_also_surfaces_its_own_raised_decision_as_input_required(
    client, game, monkeypatch
):
    """Not only `wait_for_priority`: any `run_action`-backed tool (here,
    `pass_priority`) that crosses into a decision addressed to this very
    seat - alice's own pass closing the priority round and walking the turn
    into her own cleanup discard - surfaces the same way, through the shared
    `mcp._tool_result` helper, not a second, divergent implementation."""
    seats, uids = wired(client, monkeypatch, game)
    stuff_hand(client, game, "alice", 8)
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})

    # Hand priority to bob first so alice's own pass is the one that closes
    # the round and crosses into her own cleanup discard.
    from magic_cards_model_api.store import store

    real_game = store.get(game)
    assert real_game.priority is not None
    real_game.priority.holder_index = real_game.priority.players.index("bob")
    real_game.priority.passes = 0
    bobs_pass = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert bobs_pass.status_code == 200, bobs_pass.text

    server = build_server(seats)
    result = call_tool(server, "pass_priority", {"player_uid": uids["alice"]})
    assert isinstance(result, InputRequiredResult), (
        f"expected an InputRequiredResult, got {type(result)}: {result!r}"
    )
    assert result.request_state is not None
    assert "choose_cards" in result.request_state
