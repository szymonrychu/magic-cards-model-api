"""Forced decisions answered server-side on every path a seat meets them:
inside a `pass_until` run, inside `wait_for_priority`'s wait, and right after
an action tool raised one. `forced_decisions` decides what is forced (the
engine's own enumeration leaves exactly one legal answer); these tests pin
that each path answers it instead of stopping for the model, and says so in
`auto_answered`.

Real boards through the real API, real Scryfall printings.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from conftest import add_card, state
from fastapi.testclient import TestClient
from mcp_types import InputRequiredResult
from test_autopass import Opponent, set_step, stock_libraries
from test_decisions import seats_for
from test_forced_decisions import pause_on_declare_attackers

from magic_cards_model_api import mcp, seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats


def wired(client: TestClient, monkeypatch, game_id: str) -> tuple[Seats, dict[str, str]]:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id), seats_for(client, game_id)


def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, arguments))


def test_a_run_answers_a_forced_declare_attackers_and_still_stops_there(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")  # summoning sick
    set_step(client, game, "precombat_main")

    with Opponent(game, "bob"):
        resume = None
        for _ in range(50):
            result = seatkit.pass_until(
                alice,
                until_step="end_step",
                whose_turn="mine",
                stop_when_you_can_attack=True,
                resume=resume,
                max_seconds=2.0,
            )
            if result["finished"]:
                break
            resume = result["resume"]

    # The caller asked to look at combat: the stop still fires, with the
    # declaration that had one legal answer already made.
    assert result["stopped_because"].startswith("it is your declare-attackers step")
    assert result["stopped_because"].endswith("there is no legal attacker")
    assert result["state"]["step"] == "declare_attackers"
    assert result["state"].get("pending_decision") is None
    kinds = [entry["kind"] for entry in result.get("auto_answered") or []]
    assert kinds == ["declare_attackers"], result.get("auto_answered")


def test_a_run_not_stopping_for_combat_answers_it_and_carries_on(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")  # summoning sick
    set_step(client, game, "precombat_main")

    with Opponent(game, "bob"):
        resume = None
        for _ in range(50):
            result = seatkit.pass_until(
                alice, until_step="end_step", whose_turn="mine", resume=resume, max_seconds=2.0
            )
            if result["finished"]:
                break
            resume = result["resume"]

    assert result["stopped_because"] == "reached your end step", result["stopped_because"]
    kinds = [entry["kind"] for entry in result.get("auto_answered") or []]
    assert kinds == ["declare_attackers"], result.get("auto_answered")


def test_wait_for_priority_answers_a_forced_decision_and_reports_it(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")  # summoning sick
    pause_on_declare_attackers(client, game)
    assert state(client, game)["pending_decision"]["kind"] == "declare_attackers"
    server = mcp.build_server(seats)

    result = call_tool(server, "wait_for_priority", {"player_uid": uids["alice"]})

    assert not isinstance(result, InputRequiredResult), "a forced decision reached the model"
    answer = json.loads(result.content[0].text)
    assert [entry["kind"] for entry in answer["auto_answered"]] == ["declare_attackers"]
    assert (state(client, game).get("pending_decision") or {}).get("kind") != "declare_attackers"


def test_an_action_that_raises_a_forced_decision_answers_it(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    client.patch(f"/api/games/{game}/players/alice", json={"life": 1})
    add_card(client, game, "Steam Vents", "alice", "hand")
    server = mcp.build_server(seats)

    result = call_tool(
        server,
        "play_land",
        {"player_uid": uids["alice"], "card": "Steam Vents", "reasoning": "land drop"},
    )

    assert not isinstance(result, InputRequiredResult), "a forced decision reached the model"
    answer = json.loads(result.content[0].text)
    assert [entry["kind"] for entry in answer["auto_answered"]] == ["pay_life"]
    assert answer["auto_answered"][0]["answer"] == {"accept": False}
    assert state(client, game).get("pending_decision") is None


def test_a_decision_with_a_real_choice_still_reaches_the_seat(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    add_card(client, game, "Steam Vents", "alice", "hand")
    server = mcp.build_server(seats)

    result = call_tool(
        server,
        "play_land",
        {"player_uid": uids["alice"], "card": "Steam Vents", "reasoning": "land drop"},
    )

    assert isinstance(result, InputRequiredResult), "a real choice was answered for the seat"
