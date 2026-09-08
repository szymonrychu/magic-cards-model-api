"""Where a look-then-split answer's two halves actually go, from the engine's
own words to the DOM the deciding player reads.

**The bug, as reported from a real game.** Devourer of Destiny's opening-hand
rider ("You may reveal this card from your opening hand. If you do, at the
beginning of your first upkeep, look at the top four cards of your library.
You may put one of those cards back on top of your library. Exile the rest."
- oracle text verified with `resolve_cards` in the session that wrote this
file) reaches a seat as a `scry` decision, because the engine reuses
`DecisionKind.SCRY` for the generalised look-then-split question. The browser
then told the player the cards they ticked went "to bottom" and that the
unticked ones "stay on top". Both are false, and in the worst possible way:
the ticked card is the one kept on TOP, and the unticked ones are EXILED
(CR 406.1, "the exile zone is essentially a holding area for objects").

The destinations are not a function of the kind. CR 701.22a fixes scry's pair
("look at the top N cards of your library, then put any number of them on the
bottom of your library in any order and the rest on top of your library in any
order") and CR 701.25a fixes surveil's ("put any number of them into your
graveyard and the rest on top of your library in any order"), but a card whose
own text names a different pair is answerable to its text, not to either rule.
So the engine stamps `Decision.moved_label` / `Decision.kept_label`, this API
carries them on `pending_decision`, and `ui/app.js` renders them.

Two halves, both covered here:

- the API payload, from a real game: a real decklist of real Scryfall
  printings, dealt by the engine's own seeded shuffle (CR 103.3), kept through
  CR 103.5's mulligan process, revealed through CR 103.6b's opening-hand
  action, and advanced to CR 103.7's first upkeep where CR 603.7's delayed
  trigger goes on the stack. Nothing is hand-built and nothing is mocked.
- the view model, by loading the shipped `ui/decisions.js` under Node the way
  `tests/test_ui_actions.py` does - the exact file the browser is served, no
  build step, no Python re-description.

Every CR number in this module was verified with `knowledge_search(type="rules")`
in the session that wrote it.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from fastapi.testclient import TestClient
from ui_node import requires_node, run_module

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store

#: 56 Swamp + 4 Devourer of Destiny: a legal sixty (CR 100.2a exempts basic
#: lands from the four-copy limit), every card a recorded Scryfall printing
#: from `tests/fixtures/scryfall`, and Devourer of Destiny is Modern legal
#: (checked with `resolve_cards(format="modern")` in this session).
DEVOURER_DECK = "56 Swamp\n4 Devourer of Destiny"
PLAIN_DECK = "60 Forest"

#: With this seed the engine's own shuffle deals alice exactly one Devourer of
#: Destiny in her opening seven and makes her the starting player, so CR 103.6b's
#: reveal is offered to her and "your first upkeep" is turn 1's. Seeded staging
#: of a real shuffle, not a hand-built hand.
SEED_WITH_A_DEVOURER_IN_ALICES_HAND = 4


# -- the API payload, from a real game ---------------------------------------


def boot(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": DEVOURER_DECK, "ai": False},
                {"username": "bob", "decklist_text": PLAIN_DECK, "ai": False},
            ],
            "seed": SEED_WITH_A_DEVOURER_IN_ALICES_HAND,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def reach_the_devourer_upkeep(client: TestClient, info: dict[str, Any]) -> None:
    """Keep both hands (CR 103.5), take CR 103.6b's reveal with the Devourer,
    and let both seats finish so the game settles into CR 103.7's first
    upkeep with the delayed trigger (CR 603.7) on the stack."""
    game_id = info["game_id"]
    uids = info["seat_uids"]
    for player in ("alice", "bob"):
        kept = client.post(
            f"/api/games/{game_id}/pregame/mulligan",
            params={"uid": uids[player]},
            json={"player": player, "take": False},
        )
        assert kept.status_code == 200, kept.text

    options = client.get(f"/api/games/{game_id}", params={"uid": uids["alice"]}).json()
    (offer,) = options["pregame"]["opening_hand_options"]["alice"]
    assert offer["source"] == "Devourer of Destiny"
    taken = client.post(
        f"/api/games/{game_id}/pregame/opening-hand/action",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_id": offer["card_id"]},
    )
    assert taken.status_code == 200, taken.text

    for player in ("alice", "bob"):
        done = client.post(
            f"/api/games/{game_id}/pregame/opening-hand/finish",
            params={"uid": uids[player]},
            json={"player": player},
        )
        assert done.status_code == 200, done.text
    assert done.json()["step"] == "upkeep"


def resolve_top_in_background(game_id: str) -> threading.Thread:
    """Resolving the trigger is what asks the question, and the ask blocks the
    resolving request until an answer lands on the mailbox - so it has to run
    off the thread that is going to answer it."""

    def post() -> None:
        stepping_client = TestClient(app)
        resolved = stepping_client.post(f"/api/games/{game_id}/resolve-top")
        assert resolved.status_code == 200, resolved.text

    thread = threading.Thread(target=post)
    thread.start()
    return thread


def wait_for_pending(client: TestClient, game_id: str, uid: str) -> dict[str, Any]:
    for _ in range(500):
        view = client.get(f"/api/games/{game_id}", params={"uid": uid}).json()
        if view.get("pending_decision") is not None:
            return view["pending_decision"]
        time.sleep(0.01)
    raise AssertionError("the Devourer trigger never published its decision")


def test_the_devourer_decision_says_exile_and_never_says_bottom(client: TestClient) -> None:
    """The reported bug, at the API boundary. The payload a seat reads must
    name the card's own two destinations - kept on top, rest exiled - and must
    not carry CR 701.22a's bottom-of-library anywhere, because this decision
    puts nothing on the bottom of anything."""
    info = boot(client)
    game_id, uids = info["game_id"], info["seat_uids"]
    try:
        reach_the_devourer_upkeep(client, info)
        thread = resolve_top_in_background(game_id)
        try:
            pending = wait_for_pending(client, game_id, uids["alice"])
            # Answer before asserting anything: the resolving request is parked
            # inside `Game.ask` holding this game's lock, so a failed assertion
            # left unanswered would hang the teardown behind a decision timeout
            # rather than reporting the failure.
            answered = client.post(
                f"/api/games/{game_id}/decide",
                params={"uid": uids["alice"]},
                json={"player": "alice", "moved": [pending["cards"][0]], "top_order": None},
            )
        finally:
            thread.join(timeout=15)
        assert not thread.is_alive()
        assert answered.status_code == 200, answered.text

        assert pending["kind"] == "scry", (
            "the engine reuses DecisionKind.SCRY for the generalised "
            "look-then-split, which is exactly why the kind cannot be read "
            "as CR 701.22a's pair of destinations"
        )
        assert pending["source"] == "Devourer of Destiny"
        assert len(pending["cards"]) == 4

        assert pending.get("moved_label") == "top of your library"
        assert "exile" in pending.get("kept_label", ""), (
            f"the cards not chosen are exiled (CR 406.1); the payload says "
            f"{pending.get('kept_label')!r}"
        )
        assert "bottom" not in (pending.get("moved_label", "") + pending.get("kept_label", "")), (
            "nothing about this decision puts a card on the bottom of a library"
        )

        # And the labels told the truth: the one card named stayed on top, the
        # other three were exiled (CR 406.1), none went to the bottom.
        game = store.get(game_id)
        alice = game.player("alice")
        # `game.exile`, the one shared exile zone (CR 400.1: "Each player has
        # their own library, hand, and graveyard. The other zones are shared by
        # all players"). Reading `alice.cards_in(Zone.EXILE)` here is what hid
        # the reported bug for as long as it did: the effect was exiling into a
        # per-player pile, which that read finds and every seat's board view
        # does not.
        exiled = [state.id for state in game.exile]
        assert len(exiled) == 3, f"three of the four should be exiled, got {len(exiled)}"
        assert str(alice.library[0].id) == pending["cards"][0], (
            "the card the answer named is the one kept on top, not one sent to the bottom"
        )
        assert {str(card_id) for card_id in exiled} == set(pending["cards"][1:])
    finally:
        client.delete(f"/api/games/{game_id}")


def test_declining_the_split_exiles_all_four_and_the_seat_can_see_them(
    client: TestClient,
) -> None:
    """The reported bug, end to end and in the browser's own words.

    "You may put one of those cards back on top of your library. Exile the
    rest." has exactly two outcomes: one card on top and three exiled, or -
    the choice declined (CR 608.2d, "the player announces these while applying
    the effect") - all four exiled. A player who picks nothing and presses
    Done posts `moved: []` with `top_order` naming the four cards in the order
    shown, which is what `ui/decisions.js`'s `split` builds from an untouched
    draft; this is that request, byte for byte.

    The assertion that matters is the *view*, not the engine's own bookkeeping:
    exile is a shared zone (CR 400.1) and the seat's board reads
    `Game.shared_zones[Zone.EXILE]`, so an effect that exiled into a
    per-player pile left the player looking at an empty exile with four cards
    gone from the top of their library - neither of the ability's two
    outcomes, from the only seat that can see the board.
    """
    info = boot(client)
    game_id, uids = info["game_id"], info["seat_uids"]
    try:
        reach_the_devourer_upkeep(client, info)
        # The browser's own route: both seats pass priority in CR 103.7's
        # first upkeep, and the trigger's resolution pauses on the question.
        pending = None
        for player in ("alice", "bob"):
            passed = client.post(
                f"/api/games/{game_id}/actions/pass-priority",
                params={"uid": uids[player]},
                json={"player": player},
            )
            assert passed.status_code == 200, passed.text
            if passed.json().get("pending_decision") is not None:
                pending = passed.json()["pending_decision"]
                break
        assert pending is not None, "the delayed trigger never asked its question"
        assert pending["source"] == "Devourer of Destiny"

        answered = client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "moved": [], "top_order": pending["cards"]},
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["accepted"] is True

        view = client.get(f"/api/games/{game_id}", params={"uid": uids["alice"]}).json()
        assert [card["id"] for card in view["exile"]] == pending["cards"], (
            "declining exiles all four (CR 406.2), and a seat has to be able to "
            f"see them: the board shows {view['exile']}"
        )
        (alice_view,) = [p for p in view["players"] if p["username"] == "alice"]
        assert alice_view["library_count"] == 49, "and none of the four went back on top"
        assert view["stack"] == []
    finally:
        client.delete(f"/api/games/{game_id}")


# -- the view model, running the shipped ui/decisions.js ---------------------


needs_node = requires_node


def render_split(pending: dict[str, Any], *, kind: str) -> dict[str, Any]:
    """The `split` control `ui/decisions.js`'s `viewModel` really produces for
    this pending decision - `kind` picks `scry` or `surveil`'s own fallback
    pair the same way the old renderer's explicit `toGraveyard` argument did,
    decoupled from whatever `pending["kind"]` already says."""
    vm = run_module("decisions.js", "m.viewModel(p, {})", p={**pending, "kind": kind})
    (control,) = [c for c in vm["controls"] if c["type"] == "split"]
    return control


def looked_at(**extra: Any) -> dict[str, Any]:
    """A pending look-then-split decision over two named cards."""
    return {
        "kind": "scry",
        "cards": ["card-1", "card-2"],
        "card_details": [
            {"id": "card-1", "name": "Swamp"},
            {"id": "card-2", "name": "Devourer of Destiny"},
        ],
        **extra,
    }


@needs_node
def test_the_browser_tags_each_card_with_the_destination_the_engine_named() -> None:
    """Devourer of Destiny's own pair, in the view model: every card carries
    the engine's own destination words, and neither of the two the old
    hardcoded renderer used ("bottom", "graveyard") may appear."""
    control = render_split(
        looked_at(moved_label="top of your library", kept_label="exiled"), kind="scry"
    )

    assert control["movedLabel"] == "top of your library"
    assert control["keptLabel"] == "exiled"
    assert [c["label"] for c in control["cards"]] == ["Swamp", "Devourer of Destiny"]
    assert "bottom" not in control["movedLabel"], "the old renderer's lie, still in the payload"
    assert "stay on top" not in control["keptLabel"], (
        "the unticked cards do not stay on top here - they are exiled"
    )


@needs_node
def test_a_plain_scry_renders_exactly_as_it_always_did() -> None:
    """No labels stamped - an older engine, or any decision CR 701.22a itself
    raises - falls back to the destination that has always been right for it.
    The view model carries the bare word (`"bottom"`); composing it into a
    full sentence ("...stay on top, in the order shown") is the screen's job,
    not this pure data layer's."""
    control = render_split(looked_at(), kind="scry")

    assert control["movedLabel"] == "bottom"
    assert control["keptLabel"] == "top"


@needs_node
def test_a_plain_surveil_renders_exactly_as_it_always_did() -> None:
    """CR 701.25a's own pair, on the same fallback path."""
    control = render_split(looked_at(kind="surveil"), kind="surveil")

    assert control["movedLabel"] == "graveyard"
    assert control["keptLabel"] == "top"


@needs_node
def test_each_label_falls_back_on_its_own() -> None:
    """A decision that stamps one label and not the other gets the stamped one
    and the fallback for the other - never a blank field."""
    only_moved = render_split(looked_at(moved_label="your hand", kept_label=""), kind="scry")
    assert only_moved["movedLabel"] == "your hand"
    assert only_moved["keptLabel"] == "top"

    only_kept = render_split(
        looked_at(moved_label="", kept_label="bottom of your library"), kind="surveil"
    )
    assert only_kept["movedLabel"] == "graveyard"
    assert only_kept["keptLabel"] == "bottom of your library"


@needs_node
def test_a_label_passes_through_verbatim_escaping_is_the_screens_job() -> None:
    """Labels are engine text; the view model is pure data and does not touch
    it, so it carries the label through verbatim - escaping for `innerHTML`
    is deferred to whatever screen renders this view model into the DOM."""
    control = render_split(
        looked_at(moved_label="<b>top</b>", kept_label="<i>exiled</i>"), kind="scry"
    )

    assert control["movedLabel"] == "<b>top</b>"
    assert control["keptLabel"] == "<i>exiled</i>"


@needs_node
def test_an_empty_look_still_renders_its_placeholder() -> None:
    """Nothing to look at (an empty library) is an empty card list, labels or
    no labels - a "(nothing to look at)" placeholder is the screen's job to
    show for it, not this pure data layer's."""
    empty = {"kind": "scry", "cards": [], "card_details": [], "moved_label": "top of your library"}
    assert render_split(empty, kind="scry")["cards"] == []
