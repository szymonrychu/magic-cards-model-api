"""Player counters in the state view while a decision is pending.

Battle 2, B2-16. Galvanic Discharge: "You get {E}{E}{E} (three energy
counters), then you may pay any amount of {E}." CR 107.14: "To pay {E}, a
player removes one energy counter from themselves", and CR 122.1 puts a
counter "on an object or player". While the `pay_energy_amount` question
stood, bob's own state showed `counters: {}` - the three energy the resolution
had just given him, and was asking him to spend, were nowhere on the board.

A paused command is never committed (`commands.py`): the stored game is the
one from before the command, and the half-resolved one is replayed from the
tape. The view has to read the player counters off that replay, the same
place the question itself (`amount: 3`) comes from.
"""

from __future__ import annotations

from conftest import add_card, find, state
from test_decision_kinds import cast_and_resolve, seats_for, wait_for_pending


def counters_of(view: dict, username: str) -> dict:
    return next(p for p in view["players"] if p["username"] == username)["counters"]


def pause_on_galvanic_discharge(client, game: str) -> dict[str, str]:
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Galvanic Discharge", "alice", "hand")
    bears = add_card(client, game, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    mountain = find(board["battlefield"], "Mountain")
    discharge = board["hands"]["alice"][0]
    resolved = cast_and_resolve(game, "alice", discharge["id"], [mountain["id"]], targets=[bears])
    assert resolved["paused"] is True
    return uids


def test_energy_is_in_the_view_while_pay_energy_is_pending(client, game):
    uids = pause_on_galvanic_discharge(client, game)

    own = wait_for_pending(client, game, uids["alice"])
    assert own["pending_decision"]["kind"] == "pay_energy_amount"
    assert own["pending_decision"]["amount"] == 3
    assert counters_of(own, "alice") == {"energy": 3}


def test_every_viewer_sees_the_same_player_counters(client, game):
    """Counters on a player are public, so the opponent and the operator see
    them too."""
    uids = pause_on_galvanic_discharge(client, game)

    assert counters_of(state(client, game, uid=uids["bob"]), "alice") == {"energy": 3}
    assert counters_of(state(client, game), "alice") == {"energy": 3}


def test_answering_leaves_the_committed_counters(client, game):
    uids = pause_on_galvanic_discharge(client, game)

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "energy_amount": 2},
    )
    assert answer.json()["accepted"] is True, answer.json()
    view = state(client, game, uid=uids["alice"])
    assert view["pending_decision"] is None
    assert counters_of(view, "alice") == {"energy": 1}
