"""An `input_required` answer must never cost a seat its whole tool call.

`mcp._tool_result` shapes a decision pending for the caller as an MCP
2026-07-28 `InputRequiredResult` carrying only `request_state` - no
`input_requests`, because nothing in it is for the client itself to fulfil.
A client's multi-round-trip driver (SEP-2322: `mcp.client._input_required`
here, the same loop in the typescript SDK Claude Code runs) can do only one
thing with that: sleep briefly and re-send the very same `tools/call`, with
the echoed `request_state`, and it gives up after
`DEFAULT_INPUT_REQUIRED_MAX_ROUNDS` (10) rounds - the live seat log's
`Multi-round-trip request 'tools/call' still required input after 10 rounds
(inputRequired.maxRounds)`. The decision stays pending across every retry
(only the seat itself can answer it, and it is stuck inside this call), so
before this fix every such call failed outright - after re-running its
action on each retry.

These tests drive the server through the real SDK client driver, not a
hand-rolled loop, so the cap they respect is the client's own.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from conftest import add_card, state
from fastapi.testclient import TestClient
from mcp.client import Client
from mcp.client import _input_required as client_driver
from mcp.server.mcpserver import Context
from mcp_types import InputRequiredResult, InputResponseRequestParams
from test_decisions import advance_into_cleanup, stuff_hand

from magic_cards_model_api import mcp as seat_mcp
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import INPUT_REQUIRED_MAX_ROUNDS, build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store

#: Twelve distinct nonland card names, one per Meddling Mage.
MAGE_NAMES = [
    "Lightning Bolt",
    "Shock",
    "Murder",
    "Duress",
    "Preordain",
    "Abrade",
    "Pacifism",
    "Mind Rot",
    "Treasure Cruise",
    "Lightning Helix",
    "Ephemerate",
    "Contingency Plan",
]


class _CountingClient(TestClient):
    """The in-process api client every `Seats` call goes through, counting
    each `POST .../decide` it sends - one per decision actually answered."""

    decide_posts: list[dict[str, Any]] = []

    def request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> Any:
        if method.upper() == "POST" and str(url).split("?")[0].endswith("/decide"):
            _CountingClient.decide_posts.append(kwargs.get("json") or {})
        return super().request(method, url, *args, **kwargs)


def _uids(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def _retry_context(server: Any, request_state: str) -> Context:
    """The `Context` the SDK builds for a client's retry round: the same
    tool call, carrying the echoed `request_state` (`MCPServer` hands the
    retry's params to the tool as `input_params`)."""
    from mcp.server.context import ServerRequestContext

    request_context = ServerRequestContext(
        session=object(), lifespan_context={}, protocol_version="2026-07-28", method="tools/call"
    )
    return Context(
        mcp_server=server,
        subscriptions=server._subscriptions,
        request_context=request_context,
        input_params=InputResponseRequestParams(request_state=request_state),
    )


def _text(result: Any) -> str:
    if isinstance(result, InputRequiredResult):
        assert result.request_state is not None
        return result.request_state
    return "".join(getattr(block, "text", "") for block in result.content)


def test_the_round_cap_sits_below_every_sdk_clients_own():
    assert 1 <= INPUT_REQUIRED_MAX_ROUNDS < client_driver.DEFAULT_INPUT_REQUIRED_MAX_ROUNDS


def test_retry_rounds_are_counted_and_end_in_plain_text_without_rerunning_the_action(
    client, game, monkeypatch
):
    """The round counter on its own: the client re-sends `pass` with the
    echoed `request_state` after each `input_required`. The server answers
    `input_required` for exactly `INPUT_REQUIRED_MAX_ROUNDS` rounds, then the
    same decision as ordinary text - and never runs the action again on a
    retry, so the one pass the seat asked for is the only one sent."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game)
    uids = _uids(game)
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard: a real choice
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    real_game = store.get(game)
    assert real_game.priority is not None
    real_game.priority.holder_index = real_game.priority.players.index("bob")
    real_game.priority.passes = 0
    assert (
        client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"}).status_code
        == 200
    )

    actions: list[str] = []
    real_run_action = seat_mcp.run_action

    def counting_run_action(*args: Any, **kwargs: Any) -> Any:
        actions.append("run")
        return real_run_action(*args, **kwargs)

    monkeypatch.setattr(seat_mcp, "run_action", counting_run_action)
    server = build_server(seats)

    result = asyncio.run(server.call_tool("pass", {"player_uid": uids["alice"]}))
    assert isinstance(result, InputRequiredResult), result
    first = json.loads(_text(result))
    first.pop(seat_mcp.INPUT_REQUIRED_ROUND_KEY)
    shapes = [result]
    while isinstance(result, InputRequiredResult):
        assert len(shapes) <= client_driver.DEFAULT_INPUT_REQUIRED_MAX_ROUNDS, "cap reached"
        result = asyncio.run(
            server.call_tool(
                "pass",
                {"player_uid": uids["alice"]},
                context=_retry_context(server, result.request_state or ""),
            )
        )
        shapes.append(result)

    rounds = sum(isinstance(shape, InputRequiredResult) for shape in shapes)
    assert rounds == INPUT_REQUIRED_MAX_ROUNDS
    assert actions == ["run"], "a retry round must never re-run the action"
    final = json.loads(_text(result))
    assert seat_mcp.INPUT_REQUIRED_ROUND_KEY not in final
    assert final == first, "the plain answer is the one the action produced"
    assert final["pending_decision"]["kind"] == "choose_cards"
    # The decision is still pending, exactly as the action left it.
    assert state(client, game)["pending_decision"]["kind"] == "choose_cards"


def test_a_retry_after_the_decision_was_answered_elsewhere_ends_at_once(client, game, monkeypatch):
    """A retry re-reads the board: once the decision is no longer this
    seat's (answered from another front end), there is nothing left to be
    `input_required` about, and the answer says so rather than repeating the
    stale decision."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game)
    uids = _uids(game)
    stuff_hand(client, game, "alice", 8)
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    assert advance_into_cleanup(game) is not None
    server = build_server(seats)

    result = asyncio.run(server.call_tool("wait_for_priority", {"player_uid": uids["alice"]}))
    assert isinstance(result, InputRequiredResult)
    pending = state(client, game, uid=uids["alice"])["pending_decision"]
    discard = pending["cards"][0]
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [discard]},
    )
    assert answered.status_code == 200, answered.text

    retried = asyncio.run(
        server.call_tool(
            "wait_for_priority",
            {"player_uid": uids["alice"]},
            context=_retry_context(server, result.request_state or ""),
        )
    )
    assert not isinstance(retried, InputRequiredResult)
    assert json.loads(_text(retried)).get("pending_decision") is None


@pytest.fixture()
def no_client_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK driver sleeps 50-250ms between state-only rounds; zero it so
    the thirteen-decision chain below does not spend ~20s asleep."""
    monkeypatch.setattr(client_driver, "_STATE_ONLY_BACKOFF_INITIAL_SECONDS", 0.0)
    monkeypatch.setattr(client_driver, "_STATE_ONLY_BACKOFF_CAP_SECONDS", 0.0)


def test_a_thirteen_decision_chain_completes_through_the_real_client_driver(
    client, game, monkeypatch, no_client_backoff
):
    """Shardless Agent cascades into Living End, which returns twelve
    Meddling Mages at once - thirteen chained decisions (the cascade's may,
    then one "choose a nonland card name" per Mage, CR 614.12a), every one
    of them addressed to alice and every one surfacing `input_required`.
    Driven end to end through `mcp.client.Client`, whose own driver raises
    once a call passes its 10-round cap: every call must complete, none may
    exceed `INPUT_REQUIRED_MAX_ROUNDS`, each decision must be answered
    exactly once, and the board must end exactly as the answers say."""
    _CountingClient.decide_posts = []
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _CountingClient(app))
    seats = Seats(api="http://testserver", game_id=game)
    uids = _uids(game)
    for name in ["Forest", "Island", "Island"]:
        add_card(client, game, name, "alice", "battlefield")
    for _ in MAGE_NAMES:
        add_card(client, game, "Meddling Mage", "alice", "graveyard")
    add_card(client, game, "Living End", "alice", "library")
    add_card(client, game, "Shardless Agent", "alice", "hand")

    server = build_server(seats)
    rounds_per_call: list[tuple[str, int]] = []
    real_call_tool = server.call_tool

    async def recording_call_tool(name: str, arguments: dict[str, Any], context=None):
        if context is None or context.request_state is None:
            rounds_per_call.append((name, 0))
        else:
            tool, rounds = rounds_per_call[-1]
            rounds_per_call[-1] = (tool, rounds + 1)
        return await real_call_tool(name, arguments, context)

    server.call_tool = recording_call_tool  # type: ignore[method-assign]

    async def play() -> list[dict[str, Any]]:
        answers: list[dict[str, Any]] = []
        async with Client(server) as mcp_client:

            async def call(name: str, **arguments: Any) -> dict[str, Any]:
                result = await mcp_client.call_tool(name, arguments)
                assert not result.is_error, result
                return json.loads(result.content[0].text)

            await call("cast", player_uid=uids["alice"], card="Shardless Agent")
            await call("pass", player_uid=uids["bob"])  # the cascade trigger resolves
            woken = await call("wait_for_priority", player_uid=uids["alice"])
            cascade = woken["pending_decision"]
            assert cascade["kind"] == "may", cascade
            await call(
                "decide",
                player_uid=uids["alice"],
                accept=True,
                generation=cascade["generation"],
            )
            await call("pass", player_uid=uids["alice"])
            await call("pass", player_uid=uids["bob"])  # Living End resolves
            pending = (await call("wait_for_priority", player_uid=uids["alice"]))[
                "pending_decision"
            ]
            for name in MAGE_NAMES:
                assert pending is not None and pending["kind"] == "choose_a_quality", pending
                answer = await call(
                    "decide",
                    player_uid=uids["alice"],
                    choice=name,
                    generation=pending["generation"],
                )
                answers.append(answer)
                assert answer["outcome"]["accepted"] is True, answer["outcome"]
                pending = answer.get("pending_decision")
            assert pending is None, pending
        return answers

    asyncio.run(play())

    retried = [rounds for _, rounds in rounds_per_call]
    assert max(retried) == INPUT_REQUIRED_MAX_ROUNDS, rounds_per_call
    assert all(rounds <= INPUT_REQUIRED_MAX_ROUNDS for rounds in retried)
    assert len(_CountingClient.decide_posts) == 1 + len(MAGE_NAMES), (
        "each decision answered exactly once - no retry re-sent a decide"
    )
    board = state(client, game)
    mages = [card for card in board["battlefield"] if card["name"] == "Meddling Mage"]
    assert sorted(card["choice"]["value"] for card in mages) == sorted(MAGE_NAMES)
    assert board["pending_decision"] is None
    assert [obj["name"] for obj in board["stack"]] == ["Shardless Agent"]
