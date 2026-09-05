"""The API never hands back a state nobody is allowed to move.

The bug, as the user reported it: "I set autopassing priority until my
pre-combat-main and it's stuck", with the live game showing turn 1, step
untap. The engine had just been corrected to honour CR 502.4 - "No player
receives priority during the untap step" - so `priority_holder` there is
`None`, `pass-priority` is refused outright, and the browser's auto-pass
(`ui/app.js`'s `tickRun`, which bails unless priority is yours) had
nothing it could do. The page never moved again.

Nothing should have to ask a human to leave that step. CR 500.3: "A step in
which no players receive priority ends when all specified actions that take
place during that step are completed. The only such steps are the untap step
(see rule 502) and certain cleanup steps (see rule 514)."

The fix is `engine_api.settle_priorityless_steps`, driven from the two places
a live game walks into such a step: the last pregame call (CR 103.8's first
turn begins at untap) and a pass that ends a step (CR 117.4, 405.5 - the end
step's last pass lands in cleanup). It is deliberately *not* driven from
`POST /advance`, which is the caller explicitly asking for one step and is how
a harness observes those steps at all.

Every CR number in this module was verified against the Comprehensive Rules
with `knowledge_search` in the session that wrote it.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api import engine_api
from magic_cards_model_api.store import store

#: A legal sixty from recorded fixtures only, same shape `test_mulligans.py`
#: uses: basic lands are exempt from CR 100.2a's four-copy limit.
PLAIN_DECK = "60 Forest"


def boot_and_finish_the_pregame(client: TestClient) -> dict[str, Any]:
    """A real game from two real decklists, played through CR 103.5's mulligan
    declarations and CR 103.6's opening-hand actions by the seats themselves -
    exactly the sequence the browser UI posts. Returns the creation body."""
    response = client.post(
        "/api/ui/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": PLAIN_DECK, "ai": False},
                {"username": "bob", "decklist_text": PLAIN_DECK, "ai": False},
            ],
            "seed": 1,
        },
    )
    assert response.status_code == 200, response.text
    info = response.json()
    for player in ("alice", "bob"):
        kept = client.post(
            f"/api/games/{info['game_id']}/pregame/mulligan",
            params={"uid": info["seat_uids"][player]},
            json={"player": player, "take": False},
        )
        assert kept.status_code == 200, kept.text
    for player in ("alice", "bob"):
        done = client.post(
            f"/api/games/{info['game_id']}/pregame/opening-hand/finish",
            params={"uid": info["seat_uids"][player]},
            json={"player": player},
        )
        assert done.status_code == 200, done.text
    info["last_pregame_response"] = done.json()
    return info


def test_the_first_turn_does_not_park_in_the_untap_step(client: TestClient) -> None:
    """The reported freeze, end to end. When the last seat finishes CR 103.6,
    CR 103.8's first turn begins at its untap step - and nobody receives
    priority there (CR 502.4), so a game left there is one no client is
    allowed to move. CR 500.3 says that step ends of its own accord, so what
    the seat is handed is the upkeep, with a real priority holder."""
    info = boot_and_finish_the_pregame(client)
    try:
        body = info["last_pregame_response"]
        assert body["step"] == "upkeep"
        # The turn has already begun, resting at untap, so exactly one step is
        # crossed: out of untap and into the first one that grants priority.
        assert body["settled_through"] == ["upkeep"]

        board = client.get(
            f"/api/games/{info['game_id']}", params={"uid": info["seat_uids"]["alice"]}
        ).json()
        assert board["step"] == "upkeep"
        assert board["turn_number"] == 1
        assert board["priority_holder"] is not None, (
            "the game was handed back with nobody able to act - this is the reported freeze"
        )
        # The seat that is handed priority can really use it: the whole point
        # is that `pass-priority` is no longer refused (CR 502.4).
        passed = client.post(
            f"/api/games/{info['game_id']}/actions/pass-priority",
            json={"player": board["priority_holder"]},
        )
        assert passed.status_code == 200, passed.text
        assert passed.json()["allowed"] is True
    finally:
        client.delete(f"/api/games/{info['game_id']}")


def test_the_end_of_a_turn_does_not_park_in_cleanup_or_untap(client, game) -> None:
    """The other half of the same freeze, one turn later. The end step's last
    pass ends it (CR 117.4, 405.5) and lands in the cleanup step, where no
    player receives priority either (CR 514.3) unless CR 514.3a's exception
    applies - and nothing here is waiting. Left alone, that is the frozen page
    again, at the end of every single turn."""
    moved = client.put(f"/api/games/{game}/step", json={"step": "end_step", "player": "alice"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["priority_holder"] == "alice"

    first = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert first.json()["settled_through"] == [], "the end step grants priority - nothing to settle"

    last = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert last.status_code == 200, last.text
    assert last.json()["allowed"] is True
    assert last.json()["outcome"] == "end_step"
    assert last.json()["settled_through"] == ["untap", "upkeep"]

    board = state(client, game)
    assert board["step"] == "upkeep"
    assert board["priority_holder"] is not None, (
        "every turn would end in a state nobody is allowed to move"
    )


def test_a_step_with_something_genuinely_waiting_is_left_alone(client, game) -> None:
    """The guard: settling is for a step whose only remaining event is the
    step ending. A non-empty stack is something waiting on a player, not on
    the step, so nothing is crossed - and neither is a step that grants
    priority in the first place (CR 500.3's exceptions are the untap step and
    certain cleanup steps, and a main phase is neither)."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")
    bolt = board["hands"]["alice"][0]

    real_game = store.get(game)
    assert engine_api.settle_priorityless_steps(real_game) == [], (
        "a main phase grants priority - there is nothing to settle past"
    )

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "targets": ["bob"],
            "mana_sources": [mountain["id"]],
        },
    )
    assert cast.json()["allowed"] is True, cast.text
    assert len(state(client, game)["stack"]) == 1

    before = real_game.turn.step
    assert engine_api.settle_priorityless_steps(real_game) == []
    assert real_game.turn.step is before, "a waiting stack must not be stepped over"
