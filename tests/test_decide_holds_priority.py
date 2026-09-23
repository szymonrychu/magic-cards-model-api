"""Answering a decision never passes priority on the seat's behalf.

**The defect (prod, 2026-09-23).** With the opponent's Dismember on the stack
targeting its Psychic Frog, a seat activated Polluted Delta in response, found
Watery Grave, and answered the shockland's "pay 2 life?" through `decide
(accept=true)`. `decide` then released the seat's priority *for it* - the
harness's old auto-pass after every action - the opponent passed, and
Dismember resolved before the seat could cast the Counterspell it had just
fetched the mana for.

The rules give that priority to the seat and leave the pass to it:

- CR 117.3b: "The active player receives priority after a spell or ability
  (other than a mana ability) resolves." The fetch resolved in the seat's own
  turn, so the seat was the one to receive priority when it finished.
- CR 117.3c: "If a player has priority when they cast a spell, activate an
  ability, or take a special action, that player receives priority afterward."
  A decision asked while the seat announces its own spell leaves it with that
  priority.
- CR 117.3d: "If a player has priority and chooses not to take any actions,
  that player passes." Passing is a choice the player makes, never one an
  answer to an unrelated question makes for it.

So `decide` (the MCP tool and `seat_cli decide`) now answers the question and
then only *checks* - it never posts a pass. Whoever the engine gives priority
to afterwards has it: the seat keeps it and passes explicitly with `pass` /
`pass_until`, or the opponent has it and the seat waits.

Every board here is a real board of real Scryfall printings played through the
real API; every CR number was read with `mise run rules:find` in the session
that wrote this file.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from conftest import add_card, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `httpx.Client` a `Seat` builds talks to the in-process app."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))


def mcp_call(game_id: str, tool: str, **kwargs: Any) -> dict[str, Any]:
    """Call one MCP tool and return its JSON answer, whichever wire shape it
    came back in (`input_required` carries it in `request_state`)."""
    server = build_server(Seats(api="http://testserver", game_id=game_id))
    result = asyncio.run(server.call_tool(tool, kwargs))
    text = result.request_state if hasattr(result, "request_state") else result.content[0].text
    return json.loads(str(text))


def cli_decide(
    game_id: str, uid: str, capsys: pytest.CaptureFixture, reasoning: str, *argv: str
) -> dict:
    code = seat_cli.run(
        ["--uid", uid, "--game", game_id, "--reasoning", reasoning, "decide", *argv]
    )
    out = capsys.readouterr().out
    assert code in (0, None), out
    return json.loads(out)


def pass_as(client: TestClient, game_id: str, player: str) -> dict[str, Any]:
    uid = seats_for(game_id)[player]
    response = client.post(
        f"/api/games/{game_id}/actions/pass-priority",
        params={"uid": uid},
        json={"player": player},
    )
    assert response.status_code == 200, response.text
    return response.json()


def ids_named(cards: list[dict], name: str) -> list[str]:
    return [card["id"] for card in cards if card["name"] == name]


# -- the reported scenario ----------------------------------------------------


def stage_the_response_window(client: TestClient, game_id: str) -> dict[str, Any]:
    """alice's precombat main. alice: Grizzly Bears, Polluted Delta, Island,
    Counterspell in hand, Watery Grave the only card in her library. bob:
    Mountain and Lightning Bolt. alice passes on the empty stack, bob Bolts
    the Bears and passes, and alice now holds priority with the Bolt on the
    stack - the moment the prod seat activated its fetch."""
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, "alice")
    add_card(client, game_id, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game_id, "Polluted Delta", "alice", "battlefield")
    add_card(client, game_id, "Island", "alice", "battlefield")
    add_card(client, game_id, "Watery Grave", "alice", "library")
    add_card(client, game_id, "Counterspell", "alice", "hand")
    add_card(client, game_id, "Mountain", "bob", "battlefield")
    add_card(client, game_id, "Lightning Bolt", "bob", "hand")
    board = state(client, game_id)
    bears = find(board["battlefield"], "Grizzly Bears")
    mountain = find(board["battlefield"], "Mountain")

    pass_as(client, game_id, "alice")
    bolt = client.post(
        f"/api/games/{game_id}/actions/cast",
        params={"uid": seats_for(game_id)["bob"]},
        json={
            "player": "bob",
            "card": "Lightning Bolt",
            "targets": [bears["id"]],
            "mana_sources": [mountain["id"]],
        },
    )
    assert bolt.status_code == 200, bolt.text
    assert bolt.json()["allowed"] is True, bolt.json()
    pass_as(client, game_id, "bob")
    board = state(client, game_id)
    assert board["priority_holder"] == "alice", board["priority_holder"]
    assert [entry["name"] for entry in board["stack"]] == ["Lightning Bolt"], board["stack"]
    return {
        "bears": bears,
        "delta": find(board["battlefield"], "Polluted Delta"),
        "island": find(board["battlefield"], "Island"),
    }


def fetch_into_the_pay_life(client: TestClient, game_id: str, staged: dict[str, Any]) -> None:
    """alice activates the fetch in response (releasing, as the prod seat's
    `activate` did), bob passes, the fetch resolves and asks alice which land
    to find. The shockland's `pay_life` comes after the search is answered."""
    activated = mcp_call(
        game_id,
        "activate",
        player_uid=seats_for(game_id)["alice"],
        card_id=staged["delta"]["id"],
        ability_index=0,
        reasoning="fetch the second blue source in response",
    )
    assert activated["outcome"]["allowed"] is True, activated
    assert state(client, game_id)["priority_holder"] == "bob"
    resolved = pass_as(client, game_id, "bob")
    assert resolved.get("paused") is True, resolved
    assert resolved["pending_decision"]["kind"] == "choose_cards", resolved


def assert_alice_still_holds_with_the_bolt_up(client: TestClient, game_id: str, answer: dict):
    assert "pass_outcome" not in answer, (
        f"decide passed priority on the seat's behalf: {answer.get('pass_outcome')}"
    )
    assert answer["held_priority"] is True, answer
    board = state(client, game_id)
    assert board.get("pending_decision") is None, board.get("pending_decision")
    assert board["priority_holder"] == "alice", (
        "CR 117.3b: the fetch resolved in alice's turn, so alice receives priority - "
        f"and nothing but alice may pass it; holder is {board['priority_holder']}"
    )
    assert [entry["name"] for entry in board["stack"]] == ["Lightning Bolt"], board["stack"]


def counter_the_bolt(client: TestClient, game_id: str, staged: dict[str, Any]) -> None:
    """The move the prod seat never got to make: Counterspell the Bolt with
    the Island and the Watery Grave that just entered untapped."""
    board = state(client, game_id)
    grave = find(board["battlefield"], "Watery Grave")
    assert grave["tapped"] is False, "2 life was paid, so it entered untapped"
    bolt = find(board["stack"], "Lightning Bolt")
    cast = mcp_call(
        game_id,
        "cast",
        player_uid=seats_for(game_id)["alice"],
        card="Counterspell",
        targets=bolt["id"],
        mana_sources=f"{staged['island']['id']},{grave['id']}",
        reasoning="counter the Bolt",
    )
    assert cast["outcome"]["allowed"] is True, cast
    pass_as(client, game_id, "bob")
    after = state(client, game_id)
    assert after["stack"] == [], after["stack"]
    assert find(after["battlefield"], "Grizzly Bears")["id"] == staged["bears"]["id"]
    assert "Lightning Bolt" in [card["name"] for card in after["graveyards"]["bob"]]


def test_mcp_decide_keeps_priority_after_the_fetched_shockland_so_the_seat_can_respond(
    client, game, monkeypatch
):
    """The prod scenario, through the MCP tools: search, pay 2 life, and alice
    still holds priority with the Bolt on the stack - then counters it."""
    wire(monkeypatch)
    staged = stage_the_response_window(client, game)
    fetch_into_the_pay_life(client, game, staged)
    uid = seats_for(game)["alice"]

    pending = state(client, game, uid=uid)["pending_decision"]
    grave_ids = [
        card_id
        for card_id, label in zip(pending["cards"], pending["card_labels"], strict=True)
        if label == "Watery Grave"
    ]
    searched = mcp_call(
        game,
        "decide",
        player_uid=uid,
        card_ids=grave_ids[0],
        generation=pending["generation"],
        reasoning="find the Watery Grave",
    )
    pay = searched["pending_decision"]
    assert pay["kind"] == "pay_life", searched
    answer = mcp_call(
        game,
        "decide",
        player_uid=uid,
        accept=True,
        generation=pay["generation"],
        reasoning="pay 2 so the Grave is untapped for Counterspell",
    )
    assert_alice_still_holds_with_the_bolt_up(client, game, answer)
    counter_the_bolt(client, game, staged)


def test_a_stale_hold_priority_argument_on_mcp_decide_is_harmless(client, game, monkeypatch):
    """A seat that learned the old workaround still sends `hold_priority=true`.
    The parameter is gone; the MCP server ignores the unknown argument, and
    the answer holds exactly as it does without it."""
    wire(monkeypatch)
    staged = stage_the_response_window(client, game)
    fetch_into_the_pay_life(client, game, staged)
    uid = seats_for(game)["alice"]
    pending = state(client, game, uid=uid)["pending_decision"]
    searched = mcp_call(
        game, "decide", player_uid=uid, card_ids=pending["cards"][0], hold_priority=True
    )
    pay = searched["pending_decision"]
    answer = mcp_call(
        game,
        "decide",
        player_uid=uid,
        accept=True,
        generation=pay["generation"],
        hold_priority=True,
    )
    assert_alice_still_holds_with_the_bolt_up(client, game, answer)


def test_seat_cli_decide_keeps_priority_after_the_fetched_shockland(
    client, game, monkeypatch, capsys
):
    """The same scenario through `seat_cli decide`: the two front ends agree."""
    wire(monkeypatch)
    staged = stage_the_response_window(client, game)
    fetch_into_the_pay_life(client, game, staged)
    uid = seats_for(game)["alice"]
    pending = state(client, game, uid=uid)["pending_decision"]
    grave_ids = [
        card_id
        for card_id, label in zip(pending["cards"], pending["card_labels"], strict=True)
        if label == "Watery Grave"
    ]
    searched = cli_decide(game, uid, capsys, "find the Grave", grave_ids[0])
    pay = searched["state"]["pending_decision"]
    assert pay["kind"] == "pay_life", searched
    answer = cli_decide(
        game,
        uid,
        capsys,
        "pay 2 life",
        "--accept",
        "yes",
        "--generation",
        str(pay["generation"]),
    )
    assert_alice_still_holds_with_the_bolt_up(client, game, answer)
    counter_the_bolt(client, game, staged)


# -- a decision inside the seat's own spell announcement -----------------------


def test_a_decide_during_the_seats_own_cast_keeps_its_priority(client, game, monkeypatch):
    """CR 117.3c: alice cast Petrifying Meddler, so alice receives priority
    afterwards. Its cast trigger's target is asked mid-cast (CR 603.3d);
    answering it must leave alice with that priority - the Meddler and its
    trigger on the stack, and nobody's pass but alice's own to move them."""
    wire(monkeypatch)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    for _ in range(5):
        add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Petrifying Meddler", "alice", "hand")
    board = state(client, game)
    islands = ids_named(board["battlefield"], "Island")
    uid = seats_for(game)["alice"]

    cast = mcp_call(
        game,
        "cast",
        player_uid=uid,
        card="Petrifying Meddler",
        mana_sources=",".join(islands),
        reasoning="cast the Meddler",
    )
    assert cast.get("in_flight_action") is True, cast
    pending = cast["pending_decision"]
    assert pending["kind"] == "choose_targets", pending
    bears = find(board["battlefield"], "Grizzly Bears")
    answer = mcp_call(
        game,
        "decide",
        player_uid=uid,
        targets=bears["id"],
        generation=pending["generation"],
        reasoning="tap down the Bears",
    )
    assert "pass_outcome" not in answer, answer.get("pass_outcome")
    assert answer["held_priority"] is True, answer
    after = state(client, game)
    assert after["priority_holder"] == "alice", after["priority_holder"]
    assert len(after["stack"]) == 2, after["stack"]


# -- a decision inside the resolution of the opponent's spell ------------------


def test_a_decide_during_the_opponents_spell_leaves_priority_where_the_engine_puts_it(
    client, game, monkeypatch
):
    """bob's turn. alice Bolts bob in bob's main, bob Mana Leaks the Bolt,
    and Mana Leak's "unless its controller pays {3}" asks alice as it
    resolves. Answering it is not a pass by alice, and after Mana Leak
    finishes CR 117.3b gives priority to bob, the active player - so the
    answer says alice does NOT hold priority, and nobody passed for her."""
    wire(monkeypatch)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Island", "bob", "battlefield")
    add_card(client, game, "Island", "bob", "battlefield")
    add_card(client, game, "Mana Leak", "bob", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")
    bob_islands = ids_named(board["battlefield"], "Island")
    uids = seats_for(game)

    pass_as(client, game, "bob")
    bolt = client.post(
        f"/api/games/{game}/actions/cast",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "card": "Lightning Bolt",
            "targets": ["bob"],
            "mana_sources": [mountain["id"]],
        },
    )
    assert bolt.json()["allowed"] is True, bolt.json()
    pass_as(client, game, "alice")
    bolt_on_stack = find(state(client, game)["stack"], "Lightning Bolt")
    leak = client.post(
        f"/api/games/{game}/actions/cast",
        params={"uid": uids["bob"]},
        json={
            "player": "bob",
            "card": "Mana Leak",
            "targets": [bolt_on_stack["id"]],
            "mana_sources": bob_islands,
        },
    )
    assert leak.json()["allowed"] is True, leak.json()
    pass_as(client, game, "bob")
    resolving = pass_as(client, game, "alice")
    assert resolving.get("paused") is True, resolving
    pending = resolving["pending_decision"]
    assert pending["kind"] == "pay_mana", pending
    assert pending["player"] == "alice", pending

    answer = mcp_call(
        game,
        "decide",
        player_uid=uids["alice"],
        accept=False,
        generation=pending["generation"],
        reasoning="cannot pay three",
    )
    assert "pass_outcome" not in answer, answer.get("pass_outcome")
    assert answer["held_priority"] is False, answer
    after = state(client, game)
    assert after["priority_holder"] == "bob", after["priority_holder"]
    assert after["stack"] == [], after["stack"]
