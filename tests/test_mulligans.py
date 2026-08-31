"""The mulligan process (CR 103.5) and the opening-hand actions (CR 103.6),
as this API actually runs them.

Before `opening.py`, it ran neither: `decks.load_deck_into_game` called the
engine's `Match.start_game` - which deals the hands and deliberately stops -
and nothing ever called `Match.run_mulligans`. Every game served through
`/play/` or the MCP seat tools therefore began with seven unkept cards, and
CR 103.6's "some cards allow a player to take actions with them from their
opening hand" never fired at all: a Leyline in an opening hand simply sat
there for the rest of the game.

Both halves are covered here, and they are covered differently on purpose,
because they reach a live seat differently:

- CR 103.5's declaration is *asked* now, but no seat is ever shown it.
  `machines/decisions.py`'s `ask_mulligan` takes its answer from
  `DecisionScript.take` and then `default_answer`, never consulting
  `Game.decisions.provider` - this API's decision mailbox - so
  `pending_decision` can never carry a `mulligan` kind and `POST /decide`
  has nothing to answer. Every declaration takes the engine's own default for
  an unscripted player, keep the hand as dealt, and the audit log says so
  (`live: false`). Wiring a seat to it is an engine change, not an API one.
- CR 103.6's actions go through `Game.take_opening_hand_actions`'s
  `self.ask`, which *does* consult the provider. Run after the game and its
  seat uids are in the store (which is the whole reason `opening.py` runs on
  a thread of its own), the question really does land in a seat's
  `pending_decision` and really is answered by that seat through
  `POST /decide`. `test_a_live_seat_answers_the_opening_hand_action` is that
  path end to end, with a real seat uid and a real Leyline of the Void.

Card data is the recorded Scryfall fixtures `conftest.py` pins - Swamp,
Forest, Leyline of the Void - so every deck below is real printings and no
network is touched. Basic lands are exempt from CR 100.2a's four-copy limit,
so 56 Swamp + 4 Leyline is a legal sixty.

Every CR number in this module was verified against the Comprehensive Rules
with `knowledge_search` in the session that wrote it.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from magic_cards_model_api import opening
from magic_cards_model_api.store import store

#: 56 Swamp + 4 Leyline of the Void: a legal sixty (CR 100.2a exempts basic
#: lands from the four-copy limit), built only from recorded fixtures.
LEYLINE_DECK = "56 Swamp\n4 Leyline of the Void"
PLAIN_DECK = "60 Forest"

#: With this seed, `alice`'s dealt opening hand holds exactly one Leyline of
#: the Void, so CR 103.6's question is put to her and to nobody else. Seeded
#: staging of a real shuffle, not a hand-built hand: the library really is the
#: sixty printings above, shuffled by the engine (CR 103.3).
SEED_WITH_A_LEYLINE_IN_ALICES_HAND = 4


def boot(client: TestClient, *, alice_deck: str, bob_deck: str, seed: int) -> dict[str, Any]:
    """A real game from two real decklists, through the endpoint the browser
    UI itself posts to. Returns the creation response: game id, seat uids,
    admin key."""
    response = client.post(
        "/api/ui/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": alice_deck},
                {"username": "bob", "decklist_text": bob_deck},
            ],
            "seed": seed,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def settled(game_id: str) -> Any:
    """The game, once its CR 103.5/103.6 sequence has finished."""
    assert opening.wait_for_opening_sequence(game_id, timeout=10.0), (
        "the opening sequence never finished - opening.py's thread is stuck"
    )
    game = store.get(game_id)
    assert game is not None
    return game


def poll_for_decision(client: TestClient, game_id: str, uid: str, timeout: float = 5.0) -> Any:
    """Poll a seat's own state read until its `pending_decision` appears.

    The same read `seatkit.block_until_wake` polls, so this is what a live
    seat does, not a shortcut into the mailbox: it also marks the question
    delivered, which is what starts the answer clock (`DecisionMailbox`).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = client.get(f"/api/games/{game_id}", params={"uid": uid}).json()
        pending = view.get("pending_decision")
        if pending is not None:
            return pending
        time.sleep(0.02)
    return None


# -- CR 103.5: the mulligan process now runs at all ---------------------------


def test_a_game_booted_from_decklists_settles_its_opening_hands(client: TestClient) -> None:
    """CR 103.5 runs to completion: every player's hand is kept, which is the
    engine's own record that the declaration sequence finished. Before
    `opening.py` this was `False` for both seats for the whole game."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    game = settled(info["game_id"])
    assert [player.hand_kept for player in game.players] == [True, True]
    assert [len(player.hand) for player in game.players] == [7, 7]


def test_the_opening_hand_action_sequence_runs_once(client: TestClient) -> None:
    """CR 103.6's sequence is entered - `Match.run_mulligans` calls
    `Game.take_opening_hand_actions` at its end - and the engine's own
    once-per-game flag records it. Before `opening.py` it was never entered."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    game = settled(info["game_id"])
    assert game.opening_hand_actions_taken is True


def test_the_serve_boot_path_settles_its_hands_too(tmp_path) -> None:
    """`serve.py`'s `--deck` boot goes through the same
    `opening.start_opening_sequence`, not only the HTTP creation endpoint - so
    a game booted at the command line is not left with an unkept seven either.
    The decklists are written out as real files because that is what `serve`
    reads; their contents are the same fixture-only sixties used above."""
    from magic_cards_model_api import serve

    alice = tmp_path / "alice.txt"
    bob = tmp_path / "bob.txt"
    alice.write_text(PLAIN_DECK)
    bob.write_text(PLAIN_DECK)
    args = serve.parse_args(["--deck", f"alice={alice}", "--deck", f"bob={bob}", "--seed", "1"])
    boot_result = serve.setup(args)
    try:
        game = settled(boot_result.game_id)
        assert [player.hand_kept for player in game.players] == [True, True]
        assert game.opening_hand_actions_taken is True
    finally:
        store.delete(boot_result.game_id)


def test_the_audit_log_says_the_mulligan_was_not_a_live_choice(client: TestClient) -> None:
    """The honest record of the intermediate: CR 103.5's declaration was put,
    but nobody was shown it, so the log marks it `live: false` rather than
    letting the transcript imply alice and bob chose to keep. CR 103.6's
    entry, on the same sequence, is `live: true` because that question really
    does reach a seat."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    game_id = info["game_id"]
    settled(game_id)
    log = client.get(f"/api/games/{game_id}/log").json()["log"]
    mulligans = next(entry for entry in log if entry["action"] == "mulligans_settled")
    assert mulligans["detail"]["live"] is False
    assert mulligans["detail"]["mulligans"] == "alice=0, bob=0"
    actions = next(entry for entry in log if entry["action"] == "opening_hand_actions")
    assert actions["detail"]["live"] is True


def test_no_seat_is_ever_shown_a_mulligan_decision(client: TestClient) -> None:
    """The gap this intermediate is honest about: `ask_mulligan` never
    publishes to the mailbox, so no seat's `pending_decision` can be a
    `mulligan`, before or after the sequence. If this ever starts failing,
    the engine grew the wire and `MULLIGAN` should leave
    `decisions.SERVED_ELSEWHERE`."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    game_id = info["game_id"]
    settled(game_id)
    for username, uid in info["seat_uids"].items():
        view = client.get(f"/api/games/{game_id}", params={"uid": uid}).json()
        pending = view.get("pending_decision")
        assert pending is None or pending["kind"] != "mulligan", (
            f"{username} was shown a mulligan decision - the engine now wires "
            "ask_mulligan to the provider, so MULLIGAN must move out of "
            "decisions.SERVED_ELSEWHERE and get a decide branch"
        )


# -- CR 103.6: a live seat really answers ------------------------------------


def test_a_live_seat_answers_the_opening_hand_action(client: TestClient) -> None:
    """CR 103.6a end to end, through the real transport.

    Leyline of the Void's "if this card is in your opening hand, you may begin
    the game with it on the battlefield" reaches alice's own state read as a
    `pending_decision` of kind `opening_hand_action`; she answers it with
    `POST /api/games/{id}/decide` using her seat uid; and the engine puts the
    card onto the battlefield (CR 103.6a: "the player taking this action puts
    that card onto the battlefield").
    """
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    game_id = info["game_id"]
    alice_uid = info["seat_uids"]["alice"]

    pending = poll_for_decision(client, game_id, alice_uid)
    assert pending is not None, "alice was never shown CR 103.6's question"
    assert pending["kind"] == "opening_hand_action"
    assert pending["player"] == "alice"
    assert pending["source"] == "Leyline of the Void"

    answer = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": alice_uid},
        json={"player": "alice", "accept": True},
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True, answer.text

    game = settled(game_id)
    assert [card.name for card in game.battlefield] == ["Leyline of the Void"]
    assert "Leyline of the Void" not in [card.name for card in game.player("alice").hand]
    assert len(game.player("alice").hand) == 6


def test_a_live_seat_can_decline_the_opening_hand_action(client: TestClient) -> None:
    """ "May take any such actions" (CR 103.6) means no is a real answer: the
    Leyline stays in the hand and the battlefield stays empty."""
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    game_id = info["game_id"]
    alice_uid = info["seat_uids"]["alice"]

    pending = poll_for_decision(client, game_id, alice_uid)
    assert pending is not None and pending["kind"] == "opening_hand_action"
    answer = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": alice_uid},
        json={"player": "alice", "accept": False},
    )
    assert answer.json()["accepted"] is True, answer.text

    game = settled(game_id)
    assert game.battlefield == []
    assert "Leyline of the Void" in [card.name for card in game.player("alice").hand]
    assert len(game.player("alice").hand) == 7


def test_the_opponent_cannot_answer_the_deciding_seats_question(client: TestClient) -> None:
    """CR 103.6 puts the action to the player whose opening hand holds the
    card. bob's uid does not open alice's decision, exactly as for every other
    kind - the seat check in `decide` is not bypassed just because no turn has
    begun."""
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    game_id = info["game_id"]
    assert poll_for_decision(client, game_id, info["seat_uids"]["alice"]) is not None

    refused = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": info["seat_uids"]["bob"]},
        json={"player": "alice", "accept": True},
    )
    assert refused.json()["accepted"] is False

    # Let alice finish, so the sequence does not have to time out.
    client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": info["seat_uids"]["alice"]},
        json={"player": "alice", "accept": False},
    )
    settled(game_id)
