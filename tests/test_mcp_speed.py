"""AI-seat speed levers on the MCP surface, each one quality-neutral:

- every tool raises Claude Code's inline-result threshold
  (`_meta["anthropic/maxResultSizeChars"]`), so a large board is never
  spilled to a file the agent then has to parse with Bash;
- answers are compact JSON - the same fields, no indentation;
- a `pass_until` hand-back that is only waiting on the opponent keeps running
  in the background (`background_autopass`) until the seat takes it back;
- `then_pass` performs the `pass` the seat would otherwise send as a second
  call;
- every tool call logs its server time and the gap since the seat's previous
  call returned (the model's own step), so a before/after comparison is
  measurable from logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines.turn import Step
from test_autopass import Opponent, stock_libraries
from test_decisions import seats_for

from magic_cards_model_api import mcp
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats


def wired(client: TestClient, monkeypatch, game_id: str) -> tuple[Seats, dict[str, str]]:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id), seats_for(client, game_id)


def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, arguments))


def text_of(result: Any) -> str:
    return result.content[0].text


def test_every_tool_raises_the_inline_result_threshold():
    server = mcp.build_server(Seats(api="http://testserver", game_id="unused"))
    tools = asyncio.run(server.list_tools())
    assert tools
    for tool in tools:
        meta = tool.meta or {}
        assert meta.get("anthropic/maxResultSizeChars", 0) >= mcp.MAX_RESULT_SIZE_CHARS, tool.name
    assert mcp.MAX_RESULT_SIZE_CHARS >= 200_000


def test_answers_are_compact_json(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    for name, arguments in (
        ("battlefield_state", {"player_uid": uids["alice"]}),
        ("pass", {"player_uid": uids["alice"], "reasoning": "nothing to do"}),
    ):
        text = text_of(call_tool(server, name, arguments))
        assert "\n" not in text, (name, text[:200])
        json.loads(text)


def idle_run(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    stock_libraries(client, game)
    force_turn_position(game, Step.UPKEEP, "bob")
    server = mcp.build_server(seats)
    arguments = {
        "player_uid": uids["alice"],
        "step": "upkeep",
        "whose_turn": "mine",
        "stop_when_attacked": False,
        "reasoning": "nothing to do on their turn",
    }
    first = json.loads(text_of(call_tool(server, "pass_until", arguments)))
    assert first["finished"] is False, first["stopped_because"]
    return seats, uids, server, arguments, first


def test_an_idle_pass_until_keeps_running_in_the_background(client, game, monkeypatch):
    seats, uids, server, arguments, first = idle_run(client, game, monkeypatch)
    assert first["background"]["now_running"] is True, first.get("background")
    assert seats.background.status(uids["alice"]) == "running"

    with Opponent(game, "bob"):
        give_up = time.monotonic() + 30
        while seats.background.status(uids["alice"]) == "running":
            assert time.monotonic() < give_up
            # A read does not stop it.
            call_tool(server, "battlefield_state", {"player_uid": uids["alice"]})
            time.sleep(0.05)
        assert seats.background.status(uids["alice"]) == "halted"
        assert state(client, game)["active_player"] == "bob"

        resumed = {**arguments, "resume": json.dumps(first["resume"])}
        final = json.loads(text_of(call_tool(server, "pass_until", resumed)))

    assert final["stopped_because"] == "reached your upkeep", final["stopped_because"]
    assert final["background"]["passes"] > 0, final["background"]
    assert final["background"]["status"] == "halted"
    assert seats.background.status(uids["alice"]) is None


def test_an_acting_tool_stops_the_background_run(client, game, monkeypatch):
    seats, uids, server, _arguments, _first = idle_run(client, game, monkeypatch)
    assert seats.background.status(uids["alice"]) == "running"

    answer = json.loads(
        text_of(call_tool(server, "wait_for_priority", {"player_uid": uids["alice"]}))
    )

    assert seats.background.status(uids["alice"]) is None
    assert answer["background"]["status"] == "stopped", answer.get("background")


def test_then_pass_passes_exactly_once_after_the_action(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    add_card(client, game, "Plains", "alice", "hand")
    assert state(client, game)["priority_holder"] == "alice"

    answer = json.loads(
        text_of(
            call_tool(
                server,
                "play_land",
                {
                    "player_uid": uids["alice"],
                    "card": "Plains",
                    "then_pass": True,
                    "reasoning": "land, then nothing else this window",
                },
            )
        )
    )

    assert next(iter(answer)) == "outcome", list(answer)
    assert answer["first"]["outcome"]["allowed"] is True, answer["first"]
    board = state(client, game)
    assert any(card["name"] == "Plains" for card in board["battlefield"])
    assert board["priority_holder"] == "bob", "the pass after the land never happened"
    log = client.get(f"/api/games/{game}/log", params={"uid": uids["alice"]}).json()["log"]
    passes = [entry for entry in log if entry["action"] == "pass_priority"]
    assert len(passes) == 1, passes


def test_then_pass_does_not_pass_a_priority_the_seat_no_longer_holds(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    add_card(client, game, "Plains", "alice", "hand")

    answer = json.loads(
        text_of(
            call_tool(
                server,
                "play_land",
                {
                    "player_uid": uids["alice"],
                    "card": "Plains",
                    "hold_priority": False,
                    "then_pass": True,
                    "reasoning": "released already",
                },
            )
        )
    )

    assert "first" not in answer, "no second pass once priority was already released"


def test_every_call_logs_its_server_time_and_the_gap_before_it(client, game, monkeypatch, caplog):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    with caplog.at_level(logging.INFO, logger="mcm.mcp"):
        call_tool(server, "battlefield_state", {"player_uid": uids["alice"]})
        call_tool(server, "game_log", {"player_uid": uids["alice"]})
    lines = [record.getMessage() for record in caplog.records if record.name == "mcm.mcp"]
    timed = [line for line in lines if line.startswith("tool ")]
    assert len(timed) == 2, lines
    assert "name=battlefield_state" in timed[0] and "server_ms=" in timed[0]
    assert "gap_ms=" in timed[1] and "gap_ms=-" not in timed[1], timed[1]


class _PostCountingClient(TestClient):
    posts: list[str] = []

    def request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> Any:
        if method.upper() == "POST":
            _PostCountingClient.posts.append(str(url))
        return super().request(method, url, *args, **kwargs)


def test_no_tool_re_runs_on_an_input_required_retry(client, game, monkeypatch):
    """Lever 1: every tool that can answer `input_required` takes a client's
    retry as a request to re-read, never to act again - and hands the pending
    decision back as text on that first retry."""
    import inspect

    from test_decisions import advance_into_cleanup, stuff_hand
    from test_mcp_input_required_rounds import _retry_context

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: _PostCountingClient(app))
    seats = Seats(api="http://testserver", game_id=game)
    uids = seats_for(client, game)
    stuff_hand(client, game, "alice", 8)
    client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    assert advance_into_cleanup(game) is not None
    server = mcp.build_server(seats)
    pending = state(client, game)["pending_decision"]
    echoed = json.dumps({"outcome": None, "pending_decision": pending, "input_required_round": 1})

    retried = 0
    for name, tool in server._tool_manager._tools.items():
        if "ctx" not in inspect.signature(tool.fn).parameters:
            continue
        _PostCountingClient.posts = []
        answer = tool.fn(player_uid=uids["alice"], ctx=_retry_context(server, echoed))
        assert isinstance(answer, str), (name, answer)
        assert json.loads(answer)["pending_decision"]["kind"] == pending["kind"], name
        assert _PostCountingClient.posts == [], (name, _PostCountingClient.posts)
        retried += 1
    assert retried >= 15, retried
