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

import asyncio
import sys
import threading
import time
from typing import Any

import httpx
from fastapi.testclient import TestClient

from magic_cards_model_api import ai_pregame, ai_seat, engine_api
from magic_cards_model_api.agent_settings import AgentSettings
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store

#: The route module itself, not the `FastAPI` instance the package re-exports
#: under the same name - `get_game` looks `state_view` up in these globals.
app_module = sys.modules["magic_cards_model_api.app"]

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


def boot(
    client: TestClient,
    *,
    alice_deck: str,
    bob_deck: str,
    seed: int,
    ai_players: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A real game from two real decklists, through the endpoint the browser
    UI itself posts to. Returns the creation response: game id, seat uids,
    admin key.

    `ai_players` sets the same `ai: true` flag the play UI sets on its
    opponent seat - the flag `ai_pregame.py`'s backstop keys off.
    """
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {
                    "username": "alice",
                    "decklist_text": alice_deck,
                    "ai": "alice" in ai_players,
                },
                {"username": "bob", "decklist_text": bob_deck, "ai": "bob" in ai_players},
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
    reasoning: str = "this hand can function",
) -> Any:
    # `bob` is this file's agent seat, and an agent seat's CR 103.5
    # declaration is gated on `reasoning` like every other action it takes
    # (T10) - so the harness gives one rather than the route being exempted
    # to spare it.
    body: dict[str, Any] = {"player": player, "take": take, "reasoning": reasoning}
    if bottom is not None:
        body["bottom"] = bottom
    return client.post(
        f"/api/games/{info['game_id']}/pregame/mulligan",
        params={"uid": uid or info["seat_uids"][player]},
        json=body,
    )


def finish_opening_hand(
    client: TestClient,
    info: dict[str, Any],
    player: str,
    reasoning: str = "nothing to do from my opening hand",
) -> Any:
    return client.post(
        f"/api/games/{info['game_id']}/pregame/opening-hand/finish",
        params={"uid": info["seat_uids"][player]},
        json={"player": player, "reasoning": reasoning},
    )


#: Where CR 103.8's first turn actually leaves a game somebody can play.
#: The turn begins at its untap step, but no player receives priority there
#: (CR 502.4) and CR 500.3 ends such a step of its own accord - so the API
#: settles straight on to the first step a seat can act in
#: (`engine_api.settle_priorityless_steps`), which is the upkeep.
FIRST_PLAYABLE_STEP = "upkeep"


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
        assert keep_both_and_start(client, info) == FIRST_PLAYABLE_STEP
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
        assert finish_opening_hand(client, info, "bob").json()["step"] == FIRST_PLAYABLE_STEP
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
        assert keep_both_and_start(client, info) == FIRST_PLAYABLE_STEP
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
    """`Game.settle_pregame`, driven with `use_defaults=True` - unchanged
    engine behaviour for a harness that wants a game to play rather than a
    game to start correctly. Every seat keeps the dealt hand and declines
    every CR 103.6 action, immediately, because it consults nobody; the
    audit entry says `live: false` so a transcript never implies a player
    chose it."""
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    try:
        started = time.monotonic()
        settled = client.post(
            f"/api/games/{info['game_id']}/pregame/settle",
            params={"uid": info["admin_key"]},
            json={"use_defaults": True},
        )
        assert settled.status_code == 200, settled.text
        assert time.monotonic() - started < PREGAME_WITHIN_SECONDS
        assert settled.json()["step"] == FIRST_PLAYABLE_STEP
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

    An admin `pregame/settle` call with `use_defaults` left `False` (the
    default) runs `Game.settle_pregame` as an ordinary command
    (`commands._run_pregame_settle`): the moment `ask_mulligan` finds no
    scripted answer it pauses, exactly like `advance` pauses on a combat
    declaration, and a seat answers with `POST .../decide` - no thread, no
    blocking request, `pending_decision` on every read in between. CR 103.5's
    bottoming follows as an ordinary `choose_cards` question, answered here
    with the **front** card of the hand - deliberately not the engine's
    default of the back, so the assertion that it reached the bottom of the
    library can only pass if alice's own answer was actually used, not the
    default."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    game_id = info["game_id"]
    answers: list[str] = []
    chosen: list[str] = []

    def pending_of(response: Any) -> dict[str, Any] | None:
        body = response.json()
        assert response.status_code == 200, body
        return body.get("pending_decision")

    def answer(player: str, **body: Any) -> Any:
        return client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": info["seat_uids"][player]},
            json={"player": player, **body},
        )

    try:
        started = time.monotonic()
        settled = client.post(
            f"/api/games/{game_id}/pregame/settle", params={"uid": info["admin_key"]}
        )
        pending = pending_of(settled)

        # alice mulligans once, then keeps and bottoms her front card; bob
        # keeps straight away. Driven purely by what each response's own
        # `pending_decision` says is next - never a fixed script - so this
        # fails loudly instead of silently passing if the order changes.
        while pending is not None and time.monotonic() < started + PREGAME_WITHIN_SECONDS:
            player = pending["player"]
            if pending["kind"] == "mulligan":
                take = player == "alice" and "mulligan:alice" not in answers
                answers.append(f"mulligan:{player}")
                pending = pending_of(answer(player, accept=take))
            elif pending["kind"] == "choose_cards":
                front = pending["cards"][0]
                chosen.append(front)
                answers.append(f"choose_cards:{player}")
                pending = pending_of(answer(player, card_ids=[front]))
            else:  # pragma: no cover - nothing else offered by this deck/seed
                raise AssertionError(f"unexpected pending decision: {pending}")

        assert pending is None, "the settle command never finished pausing and resuming"
        assert answers == ["mulligan:alice", "mulligan:bob", "mulligan:alice", "choose_cards:alice"]
        game = store.get(game_id)
        alice = game.player("alice")
        assert alice.mulligans_taken == 1, "alice's 'yes' was really taken, not defaulted"
        assert len(alice.hand) == 6
        assert game.player("bob").mulligans_taken == 0
        # The front card, which the default would never have picked.
        assert str(alice.library[-1].id) == chosen[0]
        assert chosen[0] not in [str(card.id) for card in alice.hand]
        assert game.turn is not None and game.turn.step.value == FIRST_PLAYABLE_STEP
    finally:
        delete(client, info)


def test_settling_with_defaults_overrides_its_own_standing_pregame_pause(
    client: TestClient,
) -> None:
    """The one deliberate exception to "a live pause blocks every other
    command" (`app.py::_pause_slot_guard`).

    A `use_defaults=False` settle leaves a live `mulligan` pause in the
    slot, waiting on a seat. `use_defaults=True` is the operator's headless
    escape hatch for exactly that situation - a seat has gone quiet and the
    game has to be made playable - so it answers every outstanding question
    with the engine's own default and walks over that pause rather than
    being refused by it. It is keyed on the stored command's *kind*, not on
    command equality: `use_defaults` is part of the payload, so the two
    settles are never the identical `Command`.

    A pause belonging to any other command is still refused here, the same
    as on every other route; there is no way to reach one during CR 103.5,
    which is why this pins the override rather than the refusal.
    """
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    game_id = info["game_id"]
    try:
        opened = client.post(
            f"/api/games/{game_id}/pregame/settle", params={"uid": info["admin_key"]}
        )
        assert opened.status_code == 200, opened.text
        assert opened.json()["pending_decision"]["kind"] == "mulligan", opened.json()

        forced = client.post(
            f"/api/games/{game_id}/pregame/settle",
            params={"uid": info["admin_key"]},
            json={"use_defaults": True},
        )
        assert forced.status_code == 200, forced.text
        assert forced.json().get("paused") is not True, forced.json()
        assert store.paused_command(game_id) is None, "the overridden pause must be retired"
        game = store.get(game_id)
        assert game.turn is not None and game.turn.step.value == FIRST_PLAYABLE_STEP
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
    assert state["step"] == FIRST_PLAYABLE_STEP
    capsys.readouterr()


def test_the_cli_pregame_carries_reasoning_for_an_agent_seat(monkeypatch, client, capsys) -> None:
    """`mulligan`/`opening-hand-done` through `seat_cli` must thread
    `--reasoning` onto the request body: an agent seat's declaration is gated
    on `reasoning` exactly like every other mutating action it takes (T10,
    `records.require_reasoning`), and the two CLI pregame tests above never
    caught a missing one because neither seat there is an agent seat - `boot`
    defaults `ai_players` to `()`. `bob` here is the agent seat, so an
    unreasoned CLI call must be refused (422) and a reasoned one must succeed.
    """
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))

    # `seat_cli`'s `mulligan` branch prints whatever the API answers with and
    # always returns 0 (unlike `cast`/`activate`, it never inspects the
    # answer for a refusal) - so the refusal shows up in stdout, not in `rc`.
    run_cli(
        monkeypatch,
        client,
        ["--uid", info["seat_uids"]["bob"], "--game", info["game_id"], "mulligan"],
    )
    printed = capsys.readouterr().out
    assert "needs `reasoning`" in printed, (
        f"an agent seat's declaration must be refused with no reasoning given: {printed}"
    )
    assert view(client, info, "alice")["pregame"]["awaiting_mulligan"] == ["alice", "bob"], (
        "the refused, unreasoned declaration must not have counted as bob declaring"
    )

    reasoned = run_cli(
        monkeypatch,
        client,
        [
            "--uid",
            info["seat_uids"]["bob"],
            "--game",
            info["game_id"],
            "--reasoning",
            "this hand can function",
            "mulligan",
        ],
    )
    assert reasoned == 0, capsys.readouterr().out
    assert view(client, info, "alice")["pregame"]["awaiting_mulligan"] == ["alice"], (
        "bob's reasoned CLI declaration should have left only alice to declare"
    )

    # alice is not an agent seat, so her own declaration needs no reasoning.
    declare(client, info, "alice", take=False)

    for player in ("alice", "bob"):
        rc = run_cli(
            monkeypatch,
            client,
            [
                "--uid",
                info["seat_uids"][player],
                "--game",
                info["game_id"],
                "--reasoning",
                "nothing to do from my opening hand",
                "opening-hand-done",
            ],
        )
        assert rc == 0, capsys.readouterr().out

    assert view(client, info, "alice")["pregame"] is None, (
        "CR 103.8's first turn never began - a reasoned opening-hand-done was still refused"
    )


# -- a bot seat that goes quiet must not hang the human ----------------------
#
# The seat CLI above gives an AI seat a *door* to the pregame. It does not
# make the AI walk through it. Driven live against a real server, the headless
# `claude` subprocess declared its CR 103.5 keep, then decided the pregame was
# "nothing to act on yet", exited 0, and left the game resting in
# `Step.PREGAME_ACTIONS` with `opening_hand_actor` naming a seat whose process
# was gone. CR 103.6 hands that window to one seat at a time, so nothing the
# human could press would ever move it: their panel said "waiting on claude"
# for as long as they cared to watch.
#
# `ai_pregame.py` is the backstop. These tests pin its three halves: it
# unblocks the human wherever a gone seat's move has one legal answer, it
# never plays the human's seat, and it never makes a gone seat's CHOICES -
# see "what the backstop will NOT do" at the end of this file for why the
# third one had to be added and what the human gets instead.


def play_alone_until_the_game_starts(
    client: TestClient, info: dict[str, Any], *, within: float
) -> dict[str, Any]:
    """Drive ONLY alice's seat - exactly the buttons the human's browser has -
    until the pregame is over or `within` seconds elapse. Returns her final
    view."""
    deadline = time.monotonic() + within
    state = view(client, info, "alice")
    while state["pregame"] is not None and time.monotonic() < deadline:
        pregame = state["pregame"]
        if "alice" in (pregame["awaiting_mulligan"] or []):
            declare(client, info, "alice", take=False)
        elif pregame["opening_hand_actor"] == "alice":
            finish_opening_hand(client, info, "alice")
        else:
            time.sleep(0.05)
        state = view(client, info, "alice")
    return state


def test_a_human_is_never_stranded_silently_by_an_ai_seat_that_never_moves(
    client: TestClient, monkeypatch
) -> None:
    """Nobody drives bob at all - no CLI, no subprocess, nothing. The human is
    never left looking at a panel that says "waiting on claude" with no button
    that moves and no explanation, which is the freeze `ai_pregame.py` exists
    for.

    What she is NOT given is bob's CR 103.5 declaration made up for him:
    keeping is one of two legal answers and no later play takes it back. She
    is told what the game is held on and how to move it, and the one decision
    that is a decision stays a decision - hers to make deliberately here,
    since nothing is playing that seat.
    """
    monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0")
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    try:
        assert wait_until(lambda: bool(stalls(client, info))), (
            "the human was left in the pregame with no word about what it is waiting on"
        )
        (stall,) = stalls(client, info)
        assert stall["player"] == "bob" and stall["kind"] == "mulligan"
        assert "no process or agent was ever started" in stall["reason"]
        assert "admin key" in stall["text"]

        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200
        state = play_alone_until_the_game_starts(client, info, within=PREGAME_WITHIN_SECONDS)

        assert state["pregame"] is None
        # Not "untap": CR 502.4 grants nobody priority there and CR 500.3 ends
        # the step of its own accord, so the API settles past it rather than
        # handing back a state no client may act on. The backstop making the
        # LAST pregame move is exactly the case that used to park here, because
        # ai_pregame calls opening.finish_opening_hand_actions directly rather
        # than through the HTTP handler where settling was first wired.
        assert state["step"] == "upkeep"
        assert state["priority_holder"] == "alice", "the human can actually act now"
        assert state["turn_number"] == 1
    finally:
        delete(client, info)


def test_the_backstop_never_declares_for_the_human(client: TestClient, monkeypatch) -> None:
    """With the grace period at zero and both seats owing CR 103.5, only the
    seat flagged `ai` is even looked at. Alice's declaration is hers to make,
    however long she takes over it - the backstop must never take it, and must
    never so much as report her seat as one the game is stuck on."""
    monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0")
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    try:
        assert wait_until(lambda: bool(stalls(client, info)))
        time.sleep(0.5)

        assert [stall["player"] for stall in stalls(client, info)] == ["bob"], (
            "the human's own seat was reported as one the backstop was waiting on"
        )
        pregame = view(client, info, "alice")["pregame"]
        assert pregame is not None, "the pregame ended without alice ever declaring"
        assert sorted(pregame["awaiting_mulligan"]) == ["alice", "bob"], (
            "somebody's CR 103.5 declaration was made without them"
        )
        assert pregame["mulligans_taken"] == {"alice": 0, "bob": 0}
        assert entries(client, info, "mulligan_declared") == []
    finally:
        delete(client, info)


def test_the_backstop_records_that_the_seat_did_not_choose(client: TestClient, monkeypatch) -> None:
    """A transcript must not claim the bot chose the move a backstop made for
    it. `opening.py` records a seat's own move as `live`; the backstop's is
    `automatic`.

    On the CR 103.6 pass, which is what the backstop still makes for a gone
    seat: bob's sixty Forests offer no opening-hand action, so passing the
    window is the only thing he could legally do with it. His CR 103.5
    declaration is a choice and is made for him by nobody - here, by alice
    with the admin key, which is recorded as the live decision it is."""
    monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0")
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    try:
        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200
        play_alone_until_the_game_starts(client, info, within=PREGAME_WITHIN_SECONDS)

        declarations = {
            entry["player"]: entry for entry in entries(client, info, "mulligan_declared")
        }
        assert declarations["alice"]["live"] is True
        assert declarations["alice"]["automatic"] is False
        assert declarations["bob"]["live"] is True, (
            "a CR 103.5 declaration nobody made was recorded as somebody's"
        )
        assert declarations["bob"]["automatic"] is False

        passes = {entry["player"]: entry for entry in entries(client, info, "opening_hand_done")}
        assert passes["alice"]["live"] is True
        assert passes["alice"]["automatic"] is False
        assert passes["bob"]["automatic"] is True
        assert passes["bob"]["live"] is False
    finally:
        delete(client, info)


def test_a_board_read_never_lands_inside_the_backstops_last_pregame_move(
    client: TestClient, monkeypatch
) -> None:
    """The settling of CR 500.3's untap step must be *unobservable*, not merely
    eventual.

    `opening.finish_opening_hand_actions` ends the pregame and only then
    settles past CR 103.8's untap step. The backstop runs that on its watcher
    thread under the game lock, but `GET /api/games/{id}` read the `Game`
    without taking the lock at all - so a poll landing between those two
    statements was handed exactly the frozen page the settling exists to
    prevent: `pregame: null`, `step: "untap"`, `priority_holder: null`, and no
    move any client is allowed to make. A browser polling the board hits the
    same window.

    The window is opened deliberately here rather than waited for: the
    watcher is stopped, this test makes the backstop's calls itself, and the
    settling is held open long enough that the read is certain to land inside
    it.
    """
    monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0")
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    game_id = info["game_id"]
    # The watcher would settle bob's moves on its own schedule; this test needs
    # to choose the moment, so it takes the backstop's job over.
    ai_pregame.stop(game_id)
    try:
        assert declare(client, info, "alice", take=False).status_code == 200
        # CR 103.5's keep/mulligan choice always has two legal answers, so the
        # backstop (`is_forced`) will not answer it - it is declared here with
        # the admin key exactly as a human at the panel would.
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200
        assert finish_opening_hand(client, info, "alice").status_code == 200

        # bob now owes the LAST pregame move - his CR 103.6 pass, which a
        # sixty-Forest hand offers no alternative to and which the backstop
        # therefore is entitled to make. This is the case the settling exists
        # for.
        inside = threading.Event()
        real_settle = engine_api.settle_priorityless_steps

        def slow_settle(game: Any) -> list[str]:
            inside.set()
            # Wide enough that the read below cannot miss the window, and
            # bounded so a reader that (correctly) waits for the lock is not
            # held up for long.
            time.sleep(0.5)
            return real_settle(game)

        monkeypatch.setattr(engine_api, "settle_priorityless_steps", slow_settle)

        settled: list[Any] = []
        worker = threading.Thread(
            target=lambda: settled.append(ai_pregame.settle_now(game_id, {"bob"})),
            daemon=True,
        )
        worker.start()
        assert inside.wait(10.0), "the backstop never reached the settling"

        state = view(client, info, "alice")

        worker.join(10.0)
        assert not worker.is_alive()
        assert settled and settled[0] is not None, "the backstop made no move"

        assert not (state["pregame"] is None and state["step"] == "untap"), (
            "a read landed inside the backstop's last pregame move and was handed "
            "the frozen untap page the settling exists to prevent"
        )
        assert state["step"] == FIRST_PLAYABLE_STEP
        assert state["priority_holder"] == "alice", "the human can actually act now"
        assert state["turn_number"] == 1
    finally:
        delete(client, info)


def test_a_board_read_that_passed_the_gate_first_still_never_sees_the_window(
    client: TestClient, monkeypatch
) -> None:
    """The other ordering, and the one a one-shot check cannot hold.

    `test_a_board_read_never_lands_inside_the_backstops_last_pregame_move`
    drives the case where the handoff is already open when the read arrives.
    A gate that is only consulted *before* the view is built does nothing for
    the reverse: a read that passes it while nothing is in flight, and is then
    overtaken - the pregame's last move opens its handoff and ends the pregame
    while this request is still assembling its answer - is handed exactly the
    frozen `untap` page out of the live `Game` it holds by reference.

    Driven deterministically: the read is held inside `state_view` until the
    backstop has ended the pregame, so it is guaranteed to build its view from
    the half-applied state.
    """
    monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0")
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    game_id = info["game_id"]
    ai_pregame.stop(game_id)
    try:
        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200
        assert finish_opening_hand(client, info, "alice").status_code == 200

        # bob owes the LAST pregame move. The read goes first, and is held
        # past the gate until that move is half applied.
        reading = threading.Event()
        window = threading.Event()
        real_state_view = app_module.state_view
        real_settle = engine_api.settle_priorityless_steps
        overtaken = threading.Event()

        def held_state_view(*args: Any, **kwargs: Any) -> Any:
            if not overtaken.is_set():
                overtaken.set()
                reading.set()
                assert window.wait(10.0), "the backstop never ended the pregame"
            return real_state_view(*args, **kwargs)

        def slow_settle(game: Any) -> list[str]:
            # The pregame is over and CR 103.8's untap step is not settled yet:
            # precisely the state no client may act on.
            window.set()
            time.sleep(0.3)
            return real_settle(game)

        monkeypatch.setattr(app_module, "state_view", held_state_view)
        monkeypatch.setattr(engine_api, "settle_priorityless_steps", slow_settle)

        read: list[dict[str, Any]] = []
        reader = threading.Thread(
            target=lambda: read.append(view(client, info, "alice")), daemon=True
        )
        reader.start()
        assert reading.wait(10.0), "the read never got past the gate"

        settled: list[Any] = []
        worker = threading.Thread(
            target=lambda: settled.append(ai_pregame.settle_now(game_id, {"bob"})),
            daemon=True,
        )
        worker.start()

        reader.join(20.0)
        assert not reader.is_alive(), "the board read never answered"
        worker.join(10.0)
        assert not worker.is_alive()
        assert settled and settled[0] is not None, "the backstop made no move"

        assert read, "the board read never answered"
        state = read[0]
        assert not (state["pregame"] is None and state["step"] == "untap"), (
            "a read that passed the gate before the window opened was still handed "
            "the frozen untap page the settling exists to prevent"
        )
        assert state["step"] == FIRST_PLAYABLE_STEP
        assert state["priority_holder"] == "alice", "the human can actually act now"
        assert state["turn_number"] == 1
    finally:
        delete(client, info)


def test_a_game_with_no_ai_seat_gets_no_backstop(client: TestClient, monkeypatch) -> None:
    """The flag is the whole trigger. Two human seats wait for each other for
    as long as it takes, exactly as CR 103.5 says they should."""
    monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0")
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        assert store.ai_players(info["game_id"]) == set()
        assert declare(client, info, "alice", take=False).status_code == 200
        time.sleep(1.0)

        assert view(client, info, "alice")["pregame"]["awaiting_mulligan"] == ["bob"], (
            "nobody declared for bob, and nobody should have"
        )
    finally:
        delete(client, info)


# -- the race the backstop used to lose --------------------------------------
#
# The tests above pin the backstop's *purpose*: a bot seat that has gone quiet
# must never strand the human. What they do not pin is its cost, which showed
# up live once the agent got slower than the grace period:
#
#     game   "POST /api/games/{id}/pregame/mulligan?uid=bf83..." 400 Bad Request
#     agent  agent calls mcp__mtg__mulligan {"player_uid": "bf83...", "take": false}
#
# The agent's prompts were rewritten to make it plan and investigate before
# acting, it stopped declaring inside 20 seconds, and the backstop began
# declaring first. That call happened to be a keep, matching the backstop's
# default, so the game looked fine; a *mulligan* would have been discarded and
# the seat would have played a hand it had rejected, with nothing in the audit
# log looking wrong.
#
# So the backstop no longer decides on a timer. It asks whether anything is
# actually playing the seat (`ai_seat.registry.activity`) and moves only a
# seat that is gone - with a hard ceiling so a live-but-silent seat still
# cannot strand a human, and a loud, explanatory refusal for the move that
# loses the race anyway.
#
# That refusal is now reachable only over a FORCED move, because a gone seat's
# choices are no longer made at all - so the race tests below run over
# CR 103.6's pass rather than CR 103.5's declaration.
#
# Every seat here is held by a REAL OS subprocess, exactly as `test_ai_seat.py`
# does it: `command_for` is pointed at a cheap deterministic command instead of
# the real `claude` CLI, so process liveness - the thing under test - is read
# off a process that is genuinely alive or genuinely dead.

#: `bob` holds exactly one Leyline of the Void in his dealt opening hand with
#: this seed, so CR 103.6's window really has something in it to lose. Seeded
#: staging of a real shuffle (CR 103.3), not a hand-built hand.
SEED_WITH_A_LEYLINE_IN_BOBS_HAND = 1

#: A process that stays up for the whole test without doing anything - a seat
#: that is thinking, from the only angle the server can observe one.
STILL_THINKING = ["python3", "-c", "import time; time.sleep(120)"]

#: A process that is over before it is looked at - the reported hang: the
#: subprocess declared nothing, exited 0, and left the seat unplayed.
ALREADY_EXITED = ["python3", "-c", "print('DONE')"]


def hold_seat(monkeypatch, info: dict[str, Any], player: str, command: list[str]) -> Any:
    """Give `player`'s seat a real subprocess, and hand back the registry it
    was started in. `ai_pregame` reads `ai_seat.registry` at call time, so
    swapping the module's registry is enough to isolate one test's seats."""
    registry = ai_seat.AiSeatRegistry()
    monkeypatch.setattr(ai_seat, "registry", registry)
    monkeypatch.setattr(ai_seat, "claude_available", lambda: True)
    monkeypatch.setattr(ai_seat, "command_for", lambda prompt, agent: command)
    registry.start(
        uid=info["seat_uids"][player],
        game_id=info["game_id"],
        player=player,
        api_base="http://testserver",
        agent=AgentSettings(model="sonnet"),
    )
    return registry


def watching(info: dict[str, Any]) -> threading.Thread:
    """The real backstop, on its own thread, for one game.

    Its two clocks come from the environment, exactly as the deployed pod's
    do, and nothing about the call names them - so reverting the source and
    re-running these tests exercises the old timer-only backstop rather than
    tripping over a keyword argument it never had.
    """
    thread = threading.Thread(
        target=ai_pregame.watch,
        args=(info["game_id"], store.ai_players(info["game_id"])),
        kwargs={"poll": 0.02},
        daemon=True,
    )
    thread.start()
    return thread


def wait_until(predicate, *, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_a_seat_that_answers_slowly_still_makes_its_own_declaration(
    client: TestClient, monkeypatch
) -> None:
    """THE BUG. A seat whose agent is alive and thinking keeps its own CR
    103.5 declaration, however far past the grace period it arrives - and the
    declaration it makes is the one that counts, mulligan included.

    The grace period here is a fiftieth of the deployed one and the seat is
    left to think through dozens of them. On the old timer-only backstop bob's
    hand is kept for him inside the first tenth of a second, and the mulligan
    below is refused."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    registry = hold_seat(monkeypatch, info, "bob", STILL_THINKING)
    try:
        monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0.05")
        monkeypatch.setenv("AI_PREGAME_MAX_WAIT_SECONDS", "60")
        watching(info)
        time.sleep(1.0)  # twenty grace periods with a live seat in the chair

        pregame = view(client, info, "alice")["pregame"]
        assert "bob" in pregame["awaiting_mulligan"], (
            "the backstop declared for a seat that was visibly still thinking"
        )

        assert declare(client, info, "alice", take=False).status_code == 200
        answer = declare(client, info, "bob", take=True)
        assert answer.status_code == 200, answer.text

        assert wait_until(
            lambda: view(client, info, "alice")["pregame"]["mulligans_taken"]["bob"] == 1
        ), "bob's own decision to mulligan was not the one the game played"
        log = client.get(
            f"/api/games/{info['game_id']}/log", params={"uid": info["admin_key"]}
        ).json()["log"]
        (bobs,) = [
            entry["detail"]
            for entry in log
            if entry["action"] == "mulligan_declared" and entry["detail"]["player"] == "bob"
        ]
        assert bobs["take"] is True
        assert bobs["live"] is True and bobs["automatic"] is False
    finally:
        registry.stop(info["seat_uids"]["bob"])
        delete(client, info)


def test_a_seat_whose_process_has_exited_is_still_moved_through_its_forced_moves(
    client: TestClient, monkeypatch
) -> None:
    """The half this module exists for, and the one that must never regress:
    the observed hang was a subprocess that declared nothing and exited 0.
    Liveness must not become an excuse to wait on it.

    What "moved along" means now is every move the seat had no choice about.
    Bob's sixty Forests offer no CR 103.6 action, so the only thing he can do
    with his opening-hand window is pass it - one legal answer, no decision,
    nothing taken from him by the server making it - and the human reaches
    CR 103.8's first turn."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    registry = hold_seat(monkeypatch, info, "bob", ALREADY_EXITED)
    uid = info["seat_uids"]["bob"]
    try:
        assert wait_until(lambda: registry.status(uid)["state"] == "stopped")
        monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0.05")
        monkeypatch.setenv("AI_PREGAME_MAX_WAIT_SECONDS", "60")
        watching(info)
        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200

        state = play_alone_until_the_game_starts(client, info, within=PREGAME_WITHIN_SECONDS)
        assert state["pregame"] is None, "a human was left waiting on a seat that had exited"
        assert state["step"] == FIRST_PLAYABLE_STEP
        (bobs,) = [
            entry
            for entry in entries(client, info, "opening_hand_done")
            if entry["player"] == "bob"
        ]
        assert bobs["automatic"] is True
    finally:
        delete(client, info)


def test_the_transcript_says_what_made_settling_for_a_seat_legitimate(
    client: TestClient, monkeypatch
) -> None:
    """`automatic: true` alone does not distinguish a crashed agent from a
    missing binary from an agent that was up and simply never spoke. Moving a
    seat along without it has to be justified in the record, even when the
    move it made was forced."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    registry = hold_seat(monkeypatch, info, "bob", ALREADY_EXITED)
    try:
        assert wait_until(lambda: registry.status(info["seat_uids"]["bob"])["state"] == "stopped")
        monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0.05")
        watching(info)
        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200
        play_alone_until_the_game_starts(client, info, within=PREGAME_WITHIN_SECONDS)

        (bobs,) = [
            entry
            for entry in entries(client, info, "opening_hand_done")
            if entry["player"] == "bob"
        ]
        assert "exited 0" in (bobs.get("reason") or "")
    finally:
        delete(client, info)


def test_a_live_seat_that_never_passes_a_forced_window_is_still_bounded(
    client: TestClient, monkeypatch
) -> None:
    """Liveness is not a licence to wait for ever. An agent that is up, and
    stays up, and simply never acts would strand the human exactly as a dead
    one does - so once `AI_PREGAME_MAX_WAIT_SECONDS` is out, its FORCED moves
    are made for it regardless of how alive it looks.

    Its choices are not: see `test_the_ceiling_goes_loud_rather_than_making_
    the_choice` for the other side of the same ceiling."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    registry = hold_seat(monkeypatch, info, "bob", STILL_THINKING)
    try:
        monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0.05")
        monkeypatch.setenv("AI_PREGAME_MAX_WAIT_SECONDS", "0.3")
        watching(info)
        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200

        state = play_alone_until_the_game_starts(client, info, within=PREGAME_WITHIN_SECONDS)
        assert state["pregame"] is None, "the human was stranded behind a seat that never spoke"
        (bobs,) = [
            entry
            for entry in entries(client, info, "opening_hand_done")
            if entry["player"] == "bob"
        ]
        assert bobs["automatic"] is True
        assert "AI_PREGAME_MAX_WAIT_SECONDS" in (bobs.get("reason") or "")
    finally:
        registry.stop(info["seat_uids"]["bob"])
        delete(client, info)


def test_a_pregame_move_that_lost_the_race_is_told_exactly_that(
    client: TestClient, monkeypatch
) -> None:
    """The loser of the race must fail loudly, not with a bare 400.

    The agent reads this response as its only feedback. The engine's own
    refusal is true of the game and false of the seat, and invites a retry
    that cannot work; the honest answer says a backstop moved for it and that
    a differing decision has been lost.

    The race is now only ever run over a FORCED move - the backstop makes no
    others - so this is CR 103.6's pass, on a hand (sixty Forests) that offers
    no opening-hand action to lose. CR 103.5's declaration cannot reach here
    at all any more, which is the point of the change; the mulligan a seat
    arrives late for is still its own (`test_a_seat_that_arrives_after_the_
    stall_still_declares_for_itself`)."""
    monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0")
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    try:
        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200
        play_alone_until_the_game_starts(client, info, within=PREGAME_WITHIN_SECONDS)

        answer = finish_opening_hand(client, info, "bob")
        assert answer.status_code == 409, answer.text
        detail = answer.json()["detail"]
        assert answer.json()["overruled"] is True
        assert "backstop" in detail
        assert "NOT applied" in detail
        assert "lost" in detail

        (overrule,) = entries(client, info, "pregame_overruled")
        assert overrule["player"] == "bob"
        assert overrule["allowed"] is False
        assert overrule["kind"] == "opening_hand"
    finally:
        delete(client, info)


def test_a_refused_declaration_leaves_no_answer_behind_for_the_next_question(
    client: TestClient, monkeypatch
) -> None:
    """A keep queues CR 103.5's bottoming answer into the game's script before
    the engine is told anything. When the declaration is then refused, that
    answer must come back out - left in, it is a live, unspent `CHOOSE_CARDS`
    waiting to silently answer whatever this player is asked next.

    The refusal here is a seat declaring after its own game owner already
    declared for it with the admin key - the way a game held on a seat nobody
    is playing gets moving again (`ai_pregame.stall_text`), and now the
    ordinary way a second declaration arrives too late."""
    monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0")
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    try:
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200
        assert declare(client, info, "bob", take=False).status_code == 400

        game = store.get(info["game_id"])
        assert game is not None
        assert game.decisions.answers == [], (
            "a refused mulligan left a scripted answer queued for the next question"
        )
    finally:
        delete(client, info)


def test_an_opening_hand_window_with_something_in_it_is_no_longer_shut(
    client: TestClient, monkeypatch
) -> None:
    """The loss `opening.py` calls the worst in that module, now closed.

    The backstop used to decline CR 103.6's window for a gone seat whatever
    was in it, so a seat that wanted to put Leyline of the Void onto the
    battlefield (CR 103.6a) found it shut - and nothing about the resulting
    board looked wrong, the card was simply still in hand. That is a choice
    (pass, or take this action), so the backstop does not make it: the window
    is HELD, said out loud, and still there when the seat turns up.

    And once the Leyline HAS been taken, passing is all that is left - one
    legal answer - so the backstop closes the window itself and the game
    starts without anybody waiting on a seat nothing is playing."""
    monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0")
    info = boot(
        client,
        alice_deck=PLAIN_DECK,
        bob_deck=LEYLINE_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_BOBS_HAND,
        ai_players=("bob",),
    )
    try:
        options = view(client, info, "bob")["pregame"]["opening_hand_options"]["bob"]
        assert options, "this seed is supposed to deal bob a CR 103.6 action"
        leyline = options[0]["card_id"]

        assert declare(client, info, "alice", take=False).status_code == 200
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200

        # CR 103.6 hands the window to one seat at a time starting with the
        # starting player, so alice's own may come first - she passes hers and
        # the window reaches bob, where it stops.
        def bobs_window_is_open() -> bool:
            pregame = view(client, info, "alice")["pregame"]
            if pregame is None:
                return False
            if pregame["opening_hand_actor"] == "alice":
                finish_opening_hand(client, info, "alice")
                return False
            return any(stall["kind"] == "opening_hand" for stall in stalls(client, info))

        assert wait_until(bobs_window_is_open), (
            "a CR 103.6 window with something in it was passed for a seat nobody was playing"
        )

        answer = client.post(
            f"/api/games/{info['game_id']}/pregame/opening-hand/action",
            params={"uid": info["seat_uids"]["bob"]},
            json={
                "player": "bob",
                "card_id": leyline,
                # An agent seat's pregame action is gated on `reasoning` (T10),
                # and that gate runs first - without one this would be a 422.
                "reasoning": "a graveyard-hate leyline is worth starting with",
            },
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["taken"] == "Leyline of the Void"

        state = play_alone_until_the_game_starts(client, info, within=PREGAME_WITHIN_SECONDS)
        assert state["pregame"] is None, "the now-forced CR 103.6 pass was never made"
        assert entries(client, info, "pregame_overruled") == []
        (leylines,) = [
            card for card in state["battlefield"] if card["name"] == "Leyline of the Void"
        ]
        assert leylines["controller"] == "bob"
    finally:
        delete(client, info)


def test_the_model_is_told_the_truth_by_the_mulligan_tool(client: TestClient, monkeypatch) -> None:
    """What an LLM seat actually reads. `mcp.mulligan` hands the model the
    response body verbatim, so the explanation has to survive the whole way
    out to the tool's text - a bare `HTTP 400` is what invited a retry.

    The declaration it loses to is the game owner's, made for a seat nothing
    was playing with this game's admin key; no backstop can take this one any
    more. The 409 `PregameOverruled` wording has its own cover on the one
    route a backstop can still win, CR 103.6's forced pass
    (`test_a_pregame_move_that_lost_the_race_is_told_exactly_that`)."""
    monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0")
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    try:
        assert declare(client, info, "bob", take=False, uid=info["admin_key"]).status_code == 200
        monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
        server = build_server(Seats(api="http://testserver", game_id=info["game_id"]))
        text = (
            asyncio.run(
                server.call_tool(
                    "mulligan",
                    {
                        "player_uid": info["seat_uids"]["bob"],
                        "take": True,
                        "reasoning": "no lands in this seven",
                    },
                )
            )
            .content[0]
            .text
        )

        assert "HTTP 400" in text
        assert "bob" in text
        assert "already declared" in text, "the model got a status code and no words to act on"
    finally:
        delete(client, info)


# -- what the backstop will NOT do -------------------------------------------
#
# Reported live on 2026-09-20 by the agent holding a seat, through
# `report_problem`:
#
#     Before this agent session started, an automatic backstop declared
#     'keep' (take=false) for seat claude during the mulligan step, reason
#     'nothing is playing this seat: driver unreachable' (log seq 4). This
#     locked in a 7-card opening hand with ZERO lands with no chance for the
#     real agent to mulligan it away.
#
# The zero-lander is the symptom. The defect is that the backstop was making
# that CLASS of decision at all: CR 103.5's declaration is the one move in a
# game of Magic that nothing later can undo, and it was being made on behalf
# of a seat *because nobody was there to make it* - which is the one
# circumstance in which a default is least defensible.
#
# So the rule is no longer "settle whatever a gone seat owes". It is: count
# the moves the engine says this seat could legally make right now, and act
# only when there is exactly one. One legal move is not a decision and the
# backstop makes it (CR 103.6's pass, for the overwhelmingly common hand that
# offers no opening-hand action at all). More than one is a choice, every
# pregame window closes for good once it passes, and the backstop fails
# closed with a notice instead - the standing preference everywhere else in
# this codebase (`understood=False`, `restriction_understood`, the unread
# trigger and unread spell notices).


def stalls(client: TestClient, info: dict[str, Any], player: str = "alice") -> list[dict[str, Any]]:
    """The pregame stall notices `player` can see on the board right now."""
    pregame = view(client, info, player)["pregame"]
    assert pregame is not None, "the pregame is over, so there is nothing to be stalled on"
    return pregame["stalled"]


def entries(client: TestClient, info: dict[str, Any], action: str) -> list[dict[str, Any]]:
    log = client.get(f"/api/games/{info['game_id']}/log", params={"uid": info["admin_key"]}).json()[
        "log"
    ]
    return [entry["detail"] for entry in log if entry["action"] == action]


def test_the_backstop_does_not_keep_a_hand_for_a_seat_nothing_is_playing(
    client: TestClient, monkeypatch
) -> None:
    """THE REPORTED BUG. bob's process has exited, so nothing is playing his
    seat - and his dealt seven is still his to keep or throw away.

    Both clocks are set far below anything real and the game is left running
    through dozens of each, so the old backstop has every chance to declare.
    It must not: keeping is one of two legal declarations here (CR 103.5, "a
    player who is dissatisfied with their initial hand may take a mulligan"),
    and nothing later in the game can give a kept hand back.
    """
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    registry = hold_seat(monkeypatch, info, "bob", ALREADY_EXITED)
    try:
        assert wait_until(lambda: registry.status(info["seat_uids"]["bob"])["state"] == "stopped")
        monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0.05")
        monkeypatch.setenv("AI_PREGAME_MAX_WAIT_SECONDS", "0.1")
        watching(info)
        assert wait_until(lambda: bool(stalls(client, info)))
        time.sleep(0.5)  # ten grace periods and five ceilings past the stall

        pregame = view(client, info, "alice")["pregame"]
        assert "bob" in pregame["awaiting_mulligan"], (
            "the backstop kept a hand for a seat that had no chance to mulligan it - "
            "this is the reported bug"
        )
        assert pregame["hand_kept"]["bob"] is False
        assert entries(client, info, "mulligan_declared") == [], (
            "a CR 103.5 declaration was recorded that no player made"
        )
    finally:
        delete(client, info)


def test_the_stall_says_who_what_why_and_what_to_do_about_it(
    client: TestClient, monkeypatch
) -> None:
    """Failing closed is only better than a wrong answer if it is loud. The
    notice has to name the seat, the move it owes, the evidence that nothing
    is playing it, and the ways out - otherwise this is the original reported
    freeze with better intentions."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    registry = hold_seat(monkeypatch, info, "bob", ALREADY_EXITED)
    try:
        assert wait_until(lambda: registry.status(info["seat_uids"]["bob"])["state"] == "stopped")
        monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0.05")
        watching(info)
        assert wait_until(lambda: bool(stalls(client, info)))

        (stall,) = stalls(client, info)
        assert stall["player"] == "bob"
        assert stall["kind"] == "mulligan"
        assert "exited 0" in stall["reason"], "the evidence for 'nothing is playing it' is missing"
        assert "CR 103.5" in stall["text"]
        assert "admin key" in stall["text"], "the human is not told how to get their game moving"

        (stalled,) = entries(client, info, "pregame_stalled")
        assert stalled["player"] == "bob"
        assert stalled["kind"] == "mulligan"
        assert stalled["allowed"] is False
        assert "exited 0" in stalled["reason"]
    finally:
        delete(client, info)


def test_the_stall_is_recorded_once_however_long_it_lasts(client: TestClient, monkeypatch) -> None:
    """The watcher re-reads liveness every grace period for as long as the
    obligation stands. One stall is one entry in the transcript, not one per
    poll - a log that scrolls a banner past a reader hides it as effectively
    as not writing it."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    registry = hold_seat(monkeypatch, info, "bob", ALREADY_EXITED)
    try:
        assert wait_until(lambda: registry.status(info["seat_uids"]["bob"])["state"] == "stopped")
        monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0.02")
        watching(info)
        assert wait_until(lambda: bool(stalls(client, info)))
        time.sleep(0.5)  # twenty-five more grace periods

        assert len(entries(client, info, "pregame_stalled")) == 1
        assert len(stalls(client, info)) == 1
    finally:
        delete(client, info)


def test_the_human_can_take_a_stuck_seats_declaration_and_start_the_game(
    client: TestClient, monkeypatch
) -> None:
    """The way out the notice names, exercised end to end. A game whose AI
    seat never comes up is not unplayable and is not silently mis-played: the
    person who created it decides, explicitly, with this game's admin key, and
    the transcript records their declaration as a real one (`live: true`)
    rather than as a default nobody chose."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    registry = hold_seat(monkeypatch, info, "bob", ALREADY_EXITED)
    try:
        assert wait_until(lambda: registry.status(info["seat_uids"]["bob"])["state"] == "stopped")
        monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0.05")
        watching(info)
        assert wait_until(lambda: bool(stalls(client, info)))

        answer = declare(
            client,
            info,
            "bob",
            take=True,
            uid=info["admin_key"],
            reasoning="nothing is playing this seat and I would rather it mulliganed",
        )
        assert answer.status_code == 200, answer.text

        assert wait_until(lambda: not stalls(client, info)), (
            "the notice outlived the obligation it was about"
        )
        # Her decision, and a different one from the default the old backstop
        # would have taken: bob really throws the hand away (CR 103.5).
        assert declare(client, info, "alice", take=False).status_code == 200
        assert wait_until(
            lambda: view(client, info, "alice")["pregame"]["mulligans_taken"]["bob"] == 1
        )
        # CR 103.5 deals bob a fresh seven and asks him again, so the stall
        # comes back for the new obligation - and is cleared again by the
        # keep she makes for him this time.
        assert wait_until(lambda: bool(stalls(client, info)))
        assert (
            declare(
                client, info, "bob", take=False, uid=info["admin_key"], reasoning="this seven works"
            ).status_code
            == 200
        )

        state = play_alone_until_the_game_starts(client, info, within=PREGAME_WITHIN_SECONDS)
        assert state["pregame"] is None
        assert state["step"] == FIRST_PLAYABLE_STEP
        bobs = [
            entry
            for entry in entries(client, info, "mulligan_declared")
            if entry["player"] == "bob"
        ]
        assert [entry["take"] for entry in bobs] == [True, False], (
            "the human's decisions for the stuck seat were not the ones played"
        )
        assert all(entry["live"] and not entry["automatic"] for entry in bobs)
    finally:
        delete(client, info)


def test_a_seat_that_arrives_after_the_stall_still_declares_for_itself(
    client: TestClient, monkeypatch
) -> None:
    """Why the wait is worth having at all. The agent in the reported game
    was not gone for good - it was late, and its session began after the
    backstop had already spent its mulligan. Held open, the seat's own
    declaration is still the one the game plays, and the notice clears."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    registry = hold_seat(monkeypatch, info, "bob", ALREADY_EXITED)
    try:
        assert wait_until(lambda: registry.status(info["seat_uids"]["bob"])["state"] == "stopped")
        monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0.05")
        monkeypatch.setenv("AI_PREGAME_MAX_WAIT_SECONDS", "0.1")
        watching(info)
        assert wait_until(lambda: bool(stalls(client, info)))
        time.sleep(0.3)

        answer = declare(client, info, "bob", take=True, reasoning="zero lands in this seven")
        assert answer.status_code == 200, answer.text
        assert not stalls(client, info)
        assert view(client, info, "alice")["pregame"]["mulligans_taken"]["bob"] == 0, (
            "CR 103.5 resolves the round only once every seat has declared"
        )
        assert declare(client, info, "alice", take=False).status_code == 200
        assert wait_until(
            lambda: view(client, info, "alice")["pregame"]["mulligans_taken"]["bob"] == 1
        ), "the seat's own decision to mulligan was not the one the game played"
    finally:
        delete(client, info)


def test_the_ceiling_goes_loud_rather_than_making_the_choice(
    client: TestClient, monkeypatch
) -> None:
    """`AI_PREGAME_MAX_WAIT_SECONDS` used to be the point at which a seat that
    was up and simply never declared had its hand kept for it. A seat being
    alive makes taking its decision away worse, not better - so the ceiling
    now bounds the silence, not the decision: it says out loud that a live
    seat is not answering, and still declines to answer for it."""
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1, ai_players=("bob",))
    registry = hold_seat(monkeypatch, info, "bob", STILL_THINKING)
    try:
        monkeypatch.setenv("AI_PREGAME_GRACE_SECONDS", "0.05")
        monkeypatch.setenv("AI_PREGAME_MAX_WAIT_SECONDS", "0.2")
        watching(info)
        assert wait_until(lambda: bool(stalls(client, info)))

        (stall,) = stalls(client, info)
        assert stall["player"] == "bob"
        assert stall["kind"] == "mulligan"
        assert "AI_PREGAME_MAX_WAIT_SECONDS" in stall["reason"]
        assert "bob" in view(client, info, "alice")["pregame"]["awaiting_mulligan"]
        assert entries(client, info, "mulligan_declared") == []
    finally:
        registry.stop(info["seat_uids"]["bob"])
        delete(client, info)
