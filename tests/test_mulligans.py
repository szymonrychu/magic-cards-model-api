"""The pregame over HTTP: CR 103.5's mulligan process, CR 103.6's opening-hand
actions, and the hard time bound both used to blow.

The engine now models the start of a game as two steps a game *rests* in -
`Step.MULLIGAN` and `Step.PREGAME_ACTIONS`, both before CR 103.8's first turn -
and this API drives them with three endpoints that block on nothing
(`app.py`'s `pregame/*`, `opening.py`). What this file covers:

- a booted game really does rest in CR 103.5's step, with `phase` null and
  `turn_number` 0, and a seat can read the board there without a 500;
- each seat declares for itself, the round resolves when the last declaration
  lands, and a seat that mulliganed pays CR 103.5's bottoming cost - choosing
  which cards, or letting the default choose;
- CR 103.6's actions reach the seat whose opening hand offers them, and only
  that seat may take them;
- **and all of it inside a hard wall-clock bound.** That bound is the
  regression cover kept from the stopgap branch this replaced: the old blocking
  path spent one full decision timeout per question per seat (a measured 1200 s
  for two decks) inside the game lock, which did not read as "slow" from
  outside - it read as a server that never answered again. A test that only
  asserts the right hands would pass in 1200 s.

Card data is the recorded Scryfall fixtures `conftest.py` pins - Swamp, Forest,
Leyline of the Void - so every deck below is real printings and no network is
touched. Basic lands are exempt from CR 100.2a's four-copy limit, so 56 Swamp +
4 Leyline is a legal sixty.

Every CR number in this module was verified against the Comprehensive Rules
with `knowledge_search` in the session that wrote it.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from magic_cards_model_api.store import store

#: 56 Swamp + 4 Leyline of the Void: a legal sixty (CR 100.2a exempts basic
#: lands from the four-copy limit), built only from recorded fixtures.
LEYLINE_DECK = "56 Swamp\n4 Leyline of the Void"
PLAIN_DECK = "60 Forest"

#: With this seed, `alice`'s dealt opening hand holds exactly one Leyline of
#: the Void, so CR 103.6's action is offered to her and to nobody else. Seeded
#: staging of a real shuffle, not a hand-built hand: the library really is the
#: sixty printings above, shuffled by the engine (CR 103.3).
SEED_WITH_A_LEYLINE_IN_ALICES_HAND = 4

#: The whole pregame, over HTTP, must finish inside this. Generous by two
#: orders of magnitude against what it actually costs (well under a second of
#: engine work) and by two orders against the bug (600-1200 s), so it can never
#: fail for being on a slow machine and can never pass with the bug back.
PREGAME_WITHIN_SECONDS = 20.0


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


def view(client: TestClient, info: dict[str, Any], player: str) -> dict[str, Any]:
    """One seat's own filtered read of the board."""
    response = client.get(
        f"/api/games/{info['game_id']}", params={"uid": info["seat_uids"][player]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def declare(
    client: TestClient,
    info: dict[str, Any],
    player: str,
    take: bool,
    bottom: list[str] | None = None,
    uid: str | None = None,
) -> Any:
    body: dict[str, Any] = {"player": player, "take": take}
    if bottom is not None:
        body["bottom"] = bottom
    return client.post(
        f"/api/games/{info['game_id']}/pregame/mulligan",
        params={"uid": uid or info["seat_uids"][player]},
        json=body,
    )


def finish_opening_hand(client: TestClient, info: dict[str, Any], player: str) -> Any:
    return client.post(
        f"/api/games/{info['game_id']}/pregame/opening-hand/finish",
        params={"uid": info["seat_uids"][player]},
        json={"player": player},
    )


def keep_both_and_start(client: TestClient, info: dict[str, Any]) -> str:
    """The whole pregame the way a pair of seats plays it: both keep, neither
    takes a CR 103.6 action. Returns the step the game ends in."""
    for player in ("alice", "bob"):
        assert declare(client, info, player, take=False).status_code == 200
    for player in ("alice", "bob"):
        answer = finish_opening_hand(client, info, player)
        assert answer.status_code == 200, answer.text
    return answer.json()["step"]


def delete(client: TestClient, info: dict[str, Any]) -> None:
    client.delete(f"/api/games/{info['game_id']}")


# -- the game rests in CR 103.5, and a seat can read it there ------------------


def test_a_booted_game_rests_in_the_mulligan_step(client: TestClient) -> None:
    """CR 103.5 is where `Match.start_game` leaves a dealt game, and it is
    before CR 103.8's "the starting player takes their first turn" - so
    `turn_number` is 0 and there is no phase, CR 500.1 making a phase part of a
    turn. Both seats owe a declaration and both are holding seven.

    The `phase is None` read is its own regression: `PHASE_OF` has no entry for
    either pregame step, and subscripting it turned this state read into a 500
    and the boot's own audit entry into an uncaught `KeyError`."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        state = view(client, info, "alice")
        assert state["turn_number"] == 0
        assert state["step"] == "mulligan"
        assert state["phase"] is None
        assert state["pregame"]["awaiting_mulligan"] == ["alice", "bob"]
        assert state["pregame"]["hand_kept"] == {"alice": False, "bob": False}
        assert len(state["hands"]["alice"]) == 7

        log = client.get(f"/api/games/{info['game_id']}/log")
        assert log.status_code == 200, log.text
        created = next(e for e in log.json()["log"] if e["action"] == "create_game")
        assert created["step"] == "mulligan"
        assert created["phase"] is None
    finally:
        delete(client, info)


def test_the_whole_pregame_runs_over_http_inside_its_bound(client: TestClient) -> None:
    """The bound, end to end: boot, both declarations, both CR 103.6 finishes,
    and the game is at CR 103.8's first turn - all of it well inside
    `PREGAME_WITHIN_SECONDS`.

    This is the regression test for the defect the engine's pregame steps
    replaced. The old path put every question through `Game.ask` behind a
    provider that blocks until a human answers, holding the game lock: one
    answer timeout per question per seat, measured at 1200 s. Asserting the
    outcome alone cannot catch that coming back - only the clock can."""
    started = time.monotonic()
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        assert keep_both_and_start(client, info) == "untap"
        elapsed = time.monotonic() - started
        assert elapsed < PREGAME_WITHIN_SECONDS, (
            f"the pregame took {elapsed:.1f}s - it must not block on a decision timeout"
        )
        game = store.get(info["game_id"])
        assert [player.hand_kept for player in game.players] == [True, True]
        assert [len(player.hand) for player in game.players] == [7, 7]
        assert game.opening_hand_actions_taken is True
        assert game.turn.turn_number == 1
        assert game.in_pregame is False
    finally:
        delete(client, info)


def test_the_next_mutating_request_is_not_queued_behind_the_pregame(
    client: TestClient,
) -> None:
    """The half of the old bug that made the whole server look dead: the
    settling thread held `store.locked(game_id)` for its whole 1200 s, so the
    *next* request to touch the game queued behind it. Nothing holds that lock
    across a wait any more, so a board-setup call right after the boot returns
    at once."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        started = time.monotonic()
        response = client.post(
            f"/api/games/{info['game_id']}/cards",
            json={"card": "Forest", "player": "alice", "zone": "battlefield"},
        )
        elapsed = time.monotonic() - started
        assert response.status_code == 200, response.text
        assert elapsed < PREGAME_WITHIN_SECONDS, f"a mutating request queued for {elapsed:.1f}s"
    finally:
        delete(client, info)


def test_the_serve_boot_path_rests_in_the_pregame_too(tmp_path) -> None:
    """`serve.py`'s `--deck` boot goes through the same `load_deck_into_game`
    and registers the game the same way, so it too hands back a game resting in
    CR 103.5 rather than one whose hands were settled by nobody."""
    from magic_cards_model_api import serve

    alice = tmp_path / "alice.txt"
    bob = tmp_path / "bob.txt"
    alice.write_text(PLAIN_DECK)
    bob.write_text(PLAIN_DECK)
    args = serve.parse_args(["--deck", f"alice={alice}", "--deck", f"bob={bob}", "--seed", "1"])
    started = time.monotonic()
    boot_result = serve.setup(args)
    try:
        assert time.monotonic() - started < PREGAME_WITHIN_SECONDS
        assert boot_result.game.turn.step.value == "mulligan"
        assert [player.hand_kept for player in boot_result.game.players] == [False, False]
        assert "mulligan step" in boot_result.summary()
    finally:
        store.delete(boot_result.game_id)


# -- CR 103.5: each seat declares for itself ----------------------------------


def test_a_seat_declares_its_own_mulligan_and_the_round_resolves(client: TestClient) -> None:
    """ "First, the starting player declares whether they will take a mulligan.
    Then each other player in turn order does the same" - and "once each player
    has made a declaration, all players who decided to take mulligans do so at
    the same time". So alice's declaration alone changes nothing; bob's is what
    resolves the round, and only then does alice have a new seven."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        first = declare(client, info, "alice", take=True)
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["step"] == "mulligan"
        assert body["pregame"]["awaiting_mulligan"] == ["bob"]
        game = store.get(info["game_id"])
        assert game.player("alice").mulligans_taken == 0, "the round had not resolved yet"

        second = declare(client, info, "bob", take=False)
        assert second.status_code == 200, second.text
        # bob kept, alice mulliganed, so a fresh round opens for alice alone.
        assert second.json()["pregame"]["awaiting_mulligan"] == ["alice"]
        assert game.player("alice").mulligans_taken == 1
        assert game.player("bob").hand_kept is True
        assert len(game.player("alice").hand) == 7
    finally:
        delete(client, info)


def test_a_seat_that_mulliganed_chooses_which_card_to_bottom(client: TestClient) -> None:
    """CR 103.5's cost for keeping: "puts a number of those cards equal to the
    number of times that player has taken a mulligan on the bottom of their
    library". The seat names the card, and it really is the bottom card.

    The engine asks this as a `CHOOSE_CARDS` question through `Game.ask`; this
    API pre-provides the answer from the declaration itself, so the request
    that keeps never parks in the mailbox waiting to be told what it was
    already handed (see `opening.py`)."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        assert declare(client, info, "alice", take=True).status_code == 200
        assert declare(client, info, "bob", take=False).status_code == 200

        state = view(client, info, "alice")
        assert state["pregame"]["cards_to_bottom"]["alice"] == 1
        chosen = state["hands"]["alice"][0]["id"]

        kept = declare(client, info, "alice", take=False, bottom=[chosen])
        assert kept.status_code == 200, kept.text
        assert kept.json()["step"] == "pregame_actions"

        game = store.get(info["game_id"])
        alice = game.player("alice")
        assert alice.mulligans_taken == 1
        assert len(alice.hand) == 6
        assert chosen not in [str(card.id) for card in alice.hand]
        assert str(alice.library[-1].id) == chosen

        entry = next(
            e
            for e in reversed(client.get(f"/api/games/{info['game_id']}/log").json()["log"])
            if e["action"] == "mulligan_declared"
        )
        assert entry["detail"]["bottom_chosen"] is True
        assert entry["detail"]["live"] is True
    finally:
        delete(client, info)


def test_an_unnamed_bottom_takes_the_engine_default_and_says_so(client: TestClient) -> None:
    """A seat that declines to choose gets the engine's own documented default
    for an exact `CHOOSE_CARDS` - the back of the hand, byte for byte what
    `Player.keep_hand(None)` bottoms - immediately rather than after a decision
    timeout. The audit log records `bottom_chosen: false`, so a transcript never
    implies the player picked it."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        assert declare(client, info, "alice", take=True).status_code == 200
        assert declare(client, info, "bob", take=False).status_code == 200
        back_of_hand = view(client, info, "alice")["hands"]["alice"][-1]["id"]

        assert declare(client, info, "alice", take=False).status_code == 200
        game = store.get(info["game_id"])
        assert str(game.player("alice").library[-1].id) == back_of_hand

        entry = next(
            e
            for e in reversed(client.get(f"/api/games/{info['game_id']}/log").json()["log"])
            if e["action"] == "mulligan_declared"
        )
        assert entry["detail"]["bottom_chosen"] is False
    finally:
        delete(client, info)


def test_a_wrong_sized_bottom_is_refused_and_the_declaration_stands_open(
    client: TestClient,
) -> None:
    """CR 103.5 names the quantity exactly. Naming two cards when one is owed is
    refused before the engine is touched, and the seat still owes its
    declaration afterwards - a bad answer must never cost a player their
    choice."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        assert declare(client, info, "alice", take=True).status_code == 200
        assert declare(client, info, "bob", take=False).status_code == 200
        hand = [card["id"] for card in view(client, info, "alice")["hands"]["alice"]]

        refused = declare(client, info, "alice", take=False, bottom=hand[:2])
        assert refused.status_code == 400, refused.text
        assert "not 2" in refused.json()["detail"]
        assert view(client, info, "alice")["pregame"]["awaiting_mulligan"] == ["alice"]
    finally:
        delete(client, info)


def test_the_opponents_uid_cannot_declare_your_mulligan(client: TestClient) -> None:
    """CR 103.5's declaration is each player's own. bob's uid does not make
    alice's choice, the same seat check `decide` applies to CR 608.2d."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        refused = declare(client, info, "alice", take=True, uid=info["seat_uids"]["bob"])
        assert refused.status_code == 400, refused.text
        assert "does not open alice's seat" in refused.json()["detail"]
        assert view(client, info, "bob")["pregame"]["awaiting_mulligan"] == ["alice", "bob"]
    finally:
        delete(client, info)


def test_no_seat_is_ever_shown_a_mulligan_pending_decision(client: TestClient) -> None:
    """`DecisionKind.MULLIGAN` stays in `decisions.SERVED_ELSEWHERE`: a live
    game here never goes through `Game.settle_pregame`, the only path that puts
    CR 103.5's declaration through `ask_mulligan` and so the only one that could
    publish it to this API's mailbox. Its transport is
    `POST .../pregame/mulligan`, and this is the standing check that nothing
    quietly starts routing it through `decide` instead."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        for player in ("alice", "bob"):
            pending = view(client, info, player).get("pending_decision")
            assert pending is None or pending["kind"] != "mulligan"
        declare(client, info, "alice", take=True)
        for player in ("alice", "bob"):
            pending = view(client, info, player).get("pending_decision")
            assert pending is None or pending["kind"] != "mulligan"
    finally:
        delete(client, info)


# -- CR 103.6: opening-hand actions -------------------------------------------


def test_a_seat_begins_the_game_with_a_leyline_on_the_battlefield(client: TestClient) -> None:
    """CR 103.6a end to end: Leyline of the Void's "if this card is in your
    opening hand, you may begin the game with it on the battlefield" is offered
    to alice as a `pregame.opening_hand_options` entry, she takes it by card id,
    and the engine puts the card onto the battlefield."""
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    try:
        for player in ("alice", "bob"):
            assert declare(client, info, player, take=False).status_code == 200

        pregame = view(client, info, "alice")["pregame"]
        assert pregame["step"] == "pregame_actions"
        assert pregame["opening_hand_actor"] == "alice"
        (offer,) = pregame["opening_hand_options"]["alice"]
        assert offer["source"] == "Leyline of the Void"

        taken = client.post(
            f"/api/games/{info['game_id']}/pregame/opening-hand/action",
            params={"uid": info["seat_uids"]["alice"]},
            json={"player": "alice", "card_id": offer["card_id"]},
        )
        assert taken.status_code == 200, taken.text

        game = store.get(info["game_id"])
        assert [card.name for card in game.battlefield] == ["Leyline of the Void"]
        assert len(game.player("alice").hand) == 6

        assert finish_opening_hand(client, info, "alice").status_code == 200
        assert finish_opening_hand(client, info, "bob").json()["step"] == "untap"
    finally:
        delete(client, info)


def test_declining_the_opening_hand_action_leaves_the_card_in_hand(client: TestClient) -> None:
    """ "May take any such actions" (CR 103.6) means no is a real answer:
    finishing without taking it leaves the Leyline in hand and the battlefield
    empty."""
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    try:
        assert keep_both_and_start(client, info) == "untap"
        game = store.get(info["game_id"])
        assert game.battlefield == []
        assert "Leyline of the Void" in [card.name for card in game.player("alice").hand]
        assert len(game.player("alice").hand) == 7
    finally:
        delete(client, info)


def test_the_other_seat_cannot_take_your_opening_hand_action(client: TestClient) -> None:
    """CR 103.6 puts the action to the player whose opening hand holds the card,
    and the engine orders the seats itself. bob can neither act with alice's
    card nor finish alice's turn."""
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    try:
        for player in ("alice", "bob"):
            assert declare(client, info, player, take=False).status_code == 200
        (offer,) = view(client, info, "alice")["pregame"]["opening_hand_options"]["alice"]

        wrong_seat = client.post(
            f"/api/games/{info['game_id']}/pregame/opening-hand/action",
            params={"uid": info["seat_uids"]["bob"]},
            json={"player": "alice", "card_id": offer["card_id"]},
        )
        assert wrong_seat.status_code == 400
        assert "does not open alice's seat" in wrong_seat.json()["detail"]

        out_of_turn = finish_opening_hand(client, info, "bob")
        assert out_of_turn.status_code == 400
        assert "alice's turn" in out_of_turn.json()["detail"]
        assert store.get(info["game_id"]).battlefield == []
    finally:
        delete(client, info)


def test_a_seat_cannot_see_the_opponents_opening_hand_options(client: TestClient) -> None:
    """CR 402.3: "a player can't look at the cards in another player's hand."
    The offer names a card sitting in alice's opening hand, so bob's own read
    shows him an empty list for her - while the admin key, the operator's own
    eye, sees both."""
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    try:
        for player in ("alice", "bob"):
            assert declare(client, info, player, take=False).status_code == 200

        bobs_read = view(client, info, "bob")["pregame"]
        assert bobs_read["opening_hand_options"]["alice"] == []
        assert bobs_read["opening_hand_actor"] == "alice", "whose turn it is stays public"

        admin = client.get(
            f"/api/games/{info['game_id']}", params={"uid": info["admin_key"]}
        ).json()
        assert admin["pregame"]["opening_hand_options"]["alice"]
    finally:
        delete(client, info)


# -- the headless path --------------------------------------------------------


def test_the_admin_can_settle_the_whole_pregame_at_once(client: TestClient) -> None:
    """`Game.settle_pregame` - unchanged engine behaviour for a harness that
    wants a game to play rather than a game to start correctly. Every seat keeps
    the dealt hand and declines every CR 103.6 action, immediately, because it
    consults nobody; the audit entry says `live: false` so a transcript never
    implies a player chose it."""
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    try:
        started = time.monotonic()
        settled = client.post(
            f"/api/games/{info['game_id']}/pregame/settle", params={"uid": info["admin_key"]}
        )
        assert settled.status_code == 200, settled.text
        assert time.monotonic() - started < PREGAME_WITHIN_SECONDS
        assert settled.json()["step"] == "untap"
        assert settled.json()["pregame"] is None

        game = store.get(info["game_id"])
        assert [player.hand_kept for player in game.players] == [True, True]
        assert game.battlefield == [], "an unscripted player declines CR 103.6's action"

        entry = next(
            e
            for e in client.get(f"/api/games/{info['game_id']}/log").json()["log"]
            if e["action"] == "pregame_settled"
        )
        assert entry["detail"]["live"] is False
    finally:
        delete(client, info)


def test_a_live_seat_answers_the_mulligan_declaration_through_decide(client: TestClient) -> None:
    """`DecisionKind.MULLIGAN` is answerable, and this is the path that makes
    it so.

    `Game.settle_pregame` puts each declaration through
    `machines/decisions.py`'s `ask_mulligan`, which consults
    `Game.decisions.provider` between the scripted answer and the default - so
    it really does land in this API's mailbox as a `mulligan`
    `pending_decision`, and a seat really does answer it with
    `POST .../decide`. CR 103.5's bottoming follows as an ordinary
    `choose_cards` question, and this answers it with the **front** card of the
    hand - deliberately not the engine's default of the back, so the assertion
    that it reached the bottom of the library can only pass if the answer was
    used.

    The settle call blocks while it asks, so it runs on its own thread; the
    poll-and-answer loop below is what a live seat's browser does.
    `POST .../decide` never takes the game lock, which is what lets it clear a
    request parked inside `Game.ask` (see `decisions.py`)."""
    import threading

    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    game_id = info["game_id"]
    # Every declaration below is answered within milliseconds, so the provider
    # never waits; a short timeout only bounds the failure mode.
    store.get(game_id).decisions.provider.timeout_seconds = 15.0
    answers: list[str] = []
    chosen: list[str] = []

    def settle() -> None:
        client.post(f"/api/games/{game_id}/pregame/settle", params={"uid": info["admin_key"]})

    def answer_everything() -> None:
        """Alice mulligans once, then keeps and bottoms her front card; bob
        keeps straight away."""
        deadline = time.monotonic() + PREGAME_WITHIN_SECONDS
        while time.monotonic() < deadline and len(answers) < 4:
            for player in ("alice", "bob"):
                pending = view(client, info, player).get("pending_decision")
                if pending is None or pending["player"] != player:
                    continue
                body: dict[str, Any] = {"player": player}
                if pending["kind"] == "mulligan":
                    # alice's first declaration is the only "yes".
                    body["accept"] = player == "alice" and "mulligan:alice" not in answers
                    answers.append(f"mulligan:{player}")
                elif pending["kind"] == "choose_cards":
                    chosen.append(pending["cards"][0])
                    body["card_ids"] = [pending["cards"][0]]
                    answers.append(f"choose_cards:{player}")
                else:
                    continue
                client.post(
                    f"/api/games/{game_id}/decide",
                    params={"uid": info["seat_uids"][player]},
                    json=body,
                )
            time.sleep(0.01)

    try:
        worker = threading.Thread(target=settle, name="settle-pregame")
        worker.start()
        answer_everything()
        worker.join(timeout=PREGAME_WITHIN_SECONDS)
        assert not worker.is_alive(), "the settle request never came back"

        assert answers == ["mulligan:alice", "mulligan:bob", "mulligan:alice", "choose_cards:alice"]
        game = store.get(game_id)
        alice = game.player("alice")
        assert alice.mulligans_taken == 1, "alice's 'yes' was really taken, not defaulted"
        assert len(alice.hand) == 6
        assert game.player("bob").mulligans_taken == 0
        # The front card, which the default would never have picked.
        assert str(alice.library[-1].id) == chosen[0]
        assert chosen[0] not in [str(card.id) for card in alice.hand]
    finally:
        delete(client, info)


def test_a_seat_cannot_settle_the_pregame_for_the_whole_table(client: TestClient) -> None:
    """Settling makes *both* seats' CR 103.5 declarations. That is the
    operator's call over a game they created, never one seat's over the
    other's."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        refused = client.post(
            f"/api/games/{info['game_id']}/pregame/settle",
            params={"uid": info["seat_uids"]["alice"]},
        )
        assert refused.status_code == 400
        assert "admin key" in refused.json()["detail"]
        assert view(client, info, "alice")["pregame"]["awaiting_mulligan"] == ["alice", "bob"]
    finally:
        delete(client, info)


# -- the seat CLI, which is the only pregame door an AI seat has -------------
#
# Everything above drives the HTTP endpoints directly. An AI seat cannot: its
# entire vocabulary is `seat_cli` (see `ai_seat.py`, which spawns a headless
# Claude whose one tool is that CLI). So a pregame that works over HTTP but has
# no CLI subcommand is a pregame an AI seat can never finish - and because the
# mulligan step is one every seat must declare in, that hangs the game for the
# HUMAN too: their own "Keep" lands fine and then waits forever on an opponent
# that has no way to answer. That is exactly the bug these two tests pin.


def run_cli(monkeypatch, client: TestClient, argv: list[str]) -> int:
    """`seat_cli.run` against the in-process test app."""
    import httpx

    from magic_cards_model_api import seat_cli

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    return seat_cli.run(argv)


def test_a_seat_can_declare_its_mulligan_through_the_cli(monkeypatch, client, capsys) -> None:
    """CR 103.5's declaration, reachable from the AI seat's only tool."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)

    rc = run_cli(
        monkeypatch,
        client,
        ["--uid", info["seat_uids"]["bob"], "--game", info["game_id"], "mulligan"],
    )

    assert rc == 0, capsys.readouterr().out
    assert view(client, info, "alice")["pregame"]["awaiting_mulligan"] == ["alice"], (
        "bob's CLI declaration should have left only alice to declare"
    )


def test_the_whole_pregame_is_reachable_from_the_cli_alone(monkeypatch, client, capsys) -> None:
    """Both seats, mulligan through CR 103.6, using nothing but `seat_cli`.

    CR 103.6 hands the actions to one seat at a time starting with the starting
    player, so `opening-hand-done` may land before this seat is the actor; the
    brief tells an AI seat to call it again, and this test does the same.
    """
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    game_id = info["game_id"]

    for player in ("alice", "bob"):
        assert (
            run_cli(
                monkeypatch,
                client,
                ["--uid", info["seat_uids"][player], "--game", game_id, "mulligan"],
            )
            == 0
        )

    for _ in range(4):
        if view(client, info, "alice")["pregame"] is None:
            break
        for player in ("alice", "bob"):
            run_cli(
                monkeypatch,
                client,
                ["--uid", info["seat_uids"][player], "--game", game_id, "opening-hand-done"],
            )

    state = view(client, info, "alice")
    assert state["pregame"] is None, "CR 103.8's first turn never began from the CLI alone"
    assert state["step"] == "untap"
    capsys.readouterr()
