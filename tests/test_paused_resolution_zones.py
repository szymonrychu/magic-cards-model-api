"""A seat reading the board mid-resolution sees the zones as they stand at the
question, not the board from before the resolution began.

**The defect (battle 5, B5-03).** Overlord of the Balemurk: "Whenever this
permanent enters or attacks, mill four cards, then you may return a non-Avatar
creature card or a planeswalker card from your graveyard to your hand." The
engine milled the four first and offered the milled creatures, but while the
return question stood every view still showed the graveyard and library from
before the mill - the player was asked to pick from a graveyard the board did
not show.

CR 608.2c has the controller follow the instructions "in the order written",
and CR 701.17a makes milling moving cards from the top of the library into the
graveyard, so the zones at the pause already carry the milled cards. A pause
is never committed (`commands.py`); the view has to read the zones off the
replayed board, the same place the question itself comes from. Every CR number
here was looked up with `mise run rules:find` in the session that wrote this
file.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.store import store

MILLED = ["Grizzly Bears", "Llanowar Elves", "Island", "Forest"]


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def library_count(view: dict[str, Any], player: str) -> int:
    return next(p for p in view["players"] if p["username"] == player)["library_count"]


def graveyard_names(view: dict[str, Any], player: str) -> list[str]:
    return sorted(card["name"] for card in view["graveyards"][player])


def pause_on_the_overlord(client: TestClient, game_id: str) -> dict[str, Any]:
    """Cast Overlord of the Balemurk over a known library, resolve the spell,
    then resolve its enters trigger - the call that mills four and pauses on
    the return question. Returns that paused response."""
    for _ in range(5):
        add_card(client, game_id, "Swamp", "alice", "battlefield")
    # Two more cards under the four that get milled, so the library does not
    # simply read empty at the pause.
    for name in ["Plains", "Mountain", *reversed(MILLED)]:
        add_card(client, game_id, name, "alice", "library")
    add_card(client, game_id, "Overlord of the Balemurk", "alice", "hand")
    board = state(client, game_id)
    swamps = [card["id"] for card in board["battlefield"] if card["name"] == "Swamp"]
    cast = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={"player": "alice", "card": "Overlord of the Balemurk", "mana_sources": swamps},
    )
    assert cast.status_code == 200, cast.text
    assert cast.json()["allowed"] is True, cast.json()
    spell = client.post(f"/api/games/{game_id}/resolve-top")
    assert spell.status_code == 200, spell.text
    assert spell.json().get("paused") is not True, spell.json()
    assert [entry["kind"] for entry in state(client, game_id)["stack"]] == ["ability"]
    trigger = client.post(f"/api/games/{game_id}/resolve-top")
    assert trigger.status_code == 200, trigger.text
    body = trigger.json()
    assert body.get("paused") is True, body
    return body


def assert_zones_at_the_pause(view: dict[str, Any]) -> None:
    assert library_count(view, "alice") == 2, (
        "CR 701.17a: the four milled cards have left the library at the pause"
    )
    assert graveyard_names(view, "alice") == sorted(MILLED), (
        f"CR 608.2c: the mill came first, so the graveyard holds it: {view['graveyards']}"
    )


def test_the_owner_sees_the_milled_zones_while_the_return_question_stands(client, game) -> None:
    pause_on_the_overlord(client, game)
    uids = seats_for(game)

    own = state(client, game, uid=uids["alice"])
    assert own["pending_decision"] is not None
    assert own["pending_decision"]["player"] == "alice"
    assert_zones_at_the_pause(own)


def test_the_opponent_sees_the_public_graveyard_and_library_count(client, game) -> None:
    """CR 400.2 makes the graveyard a public zone, and CR 401.3 lets any player
    count the cards in any library, so the opponent's redacted view carries
    both at the pause too - and still no library contents."""
    pause_on_the_overlord(client, game)
    uids = seats_for(game)

    other = state(client, game, uid=uids["bob"])
    assert_zones_at_the_pause(other)
    assert "libraries" not in other or not other["libraries"], other.get("libraries")
    assert other["pending_decision"]["player"] == "alice"


def test_the_operator_view_shows_the_library_minus_the_milled_cards(client, game) -> None:
    pause_on_the_overlord(client, game)

    full = state(client, game)
    assert_zones_at_the_pause(full)
    assert sorted(card["name"] for card in full["libraries"]["alice"]) == ["Mountain", "Plains"]


def test_the_paused_response_itself_shows_the_zones_at_the_pause(client, game) -> None:
    """The acting route's own paused board - what `resolve-top` hands back when
    it stops on the question - is the same board, not the pre-mill one."""
    paused = pause_on_the_overlord(client, game)
    assert_zones_at_the_pause(paused)


def test_the_resolving_trigger_stays_on_the_stack_at_the_pause(client, game) -> None:
    """CR 608.2n: "As the final part of an ability's resolution, the ability is
    removed from the stack" - so while it asks mid-resolution, the trigger is
    still there, for every viewer. The replayed board has already taken it off,
    which is why the stack is not read from it."""
    paused = pause_on_the_overlord(client, game)
    uids = seats_for(game)

    for view in (paused, state(client, game), state(client, game, uid=uids["bob"])):
        stack = [(entry["kind"], entry["name"]) for entry in view["stack"]]
        assert stack == [("ability", "Overlord of the Balemurk")], view["stack"]


def test_the_pause_does_not_move_the_turn_or_priority(client, game) -> None:
    """The zones come from the replay, and the step and turn come from the
    paused board (`serialize._paused_overlay`): a trigger resolving in the step
    it was put on the stack in leaves both where the committed game has them."""
    before = state(client, game)
    pause_on_the_overlord(client, game)
    during = state(client, game)
    assert during["step"] == before["step"]
    assert during["turn_number"] == before["turn_number"]


def test_answering_commits_the_board_the_pause_showed(client, game) -> None:
    pause_on_the_overlord(client, game)
    uids = seats_for(game)
    pending = state(client, game, uid=uids["alice"])["pending_decision"]
    assert pending["kind"] == "may", pending

    declined = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": False, "generation": pending["generation"]},
    )
    assert declined.status_code == 200, declined.text
    assert declined.json()["accepted"] is True, declined.json()

    board = state(client, game)
    assert board["pending_decision"] is None
    assert_zones_at_the_pause(board)
    assert find(board["battlefield"], "Overlord of the Balemurk")
