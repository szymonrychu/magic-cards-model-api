"""Searching a library is one choice over every legal find - from the engine's
question to the payload a seat reads to the DOM it clicks.

**The bug, as reported from a real game, twice.** A seat activated Wooded
Foothills ("{T}, Pay 1 life, Sacrifice this land: Search your library for a
Mountain or Forest card, put it onto the battlefield, then shuffle." - oracle
text verified with `resolve_cards` in the session that wrote this file)
intending to fetch Breeding Pool, whose type line is "Land - Forest Island" and
which is therefore a legal find. It got a basic Forest. Separately, declining a
Hedge Maze cost two round trips, one candidate at a time.

Both are the same defect: the search was a *walk*, one yes/no question per
matching card in zone order, so the searcher could only decline what it was
shown and could never name what it wanted. CR 701.23a is not a walk - "To
search for a card in a zone, look at all cards in that zone (even if it's a
hidden zone) and find a card that matches the given description." The whole
zone is looked at, and one find is made.

The engine now asks it that way: one `choose_cards` question carrying every
legal candidate, `card_labels` naming them (CR 400.2 makes the library a
hidden zone, so nothing on a redacted board can name them for the client), and
the answer naming what the searcher wants. This file is that contract at the
two boundaries this repo owns.

- `exact` is False here, because CR 701.23b makes a stated-quality search of a
  hidden zone declinable: "If a player is searching a hidden zone for cards
  with a stated quality, such as a card with a certain card type or color,
  that player isn't required to find some or all of those cards even if
  they're present in that zone." Finding nothing is an answer, and it is one
  request, not one per card refused.
- CR 701.23d is the other branch, `exact` True: "If a player is searching a
  hidden zone simply for a quantity of cards, such as 'a card' or 'three
  cards,' that player must find that many cards (or as many as possible, if
  the zone doesn't contain that many)" - which, never whether.

Two halves, both real:

- the API payload and the fetch itself, from a real game on a real board with
  real Scryfall printings out of the engine's own fixture pool. Nothing is
  hand-written and nothing is mocked.
- the renderer, by loading the shipped `ui/app.js` under Node the way
  `tests/test_card_split_labels.py` and `tests/test_ui_priority_panel.py` do -
  the exact file the browser is served, no build step.

Every CR number in this module was verified with `knowledge_search(type="rules")`
in the session that wrote it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import add_card, find, life_of, state
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store

APP_JS = Path(__file__).resolve().parents[1] / "ui" / "app.js"


# -- the API payload, and the fetch it drives --------------------------------


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def stock_the_fetch(client: TestClient, game_id: str) -> dict[str, Any]:
    """Wooded Foothills on alice's battlefield, and a library holding exactly
    the two cards its "Mountain or Forest card" clause can find: a basic
    Forest and Breeding Pool ("Land - Forest Island", so a Forest card by type
    even though it is not a basic and not green)."""
    # A question this test fails to answer should fail the test, not park the
    # resolving thread for the deployment default (300s). Every question here
    # is answered within milliseconds of being published.
    store.get(game_id).decisions.provider.timeout_seconds = 20.0
    add_card(client, game_id, "Wooded Foothills", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "library")
    add_card(client, game_id, "Breeding Pool", "alice", "library")
    board = state(client, game_id)
    return {"foothills": find(board["battlefield"], "Wooded Foothills")}


def activate_and_resolve_in_background(game_id: str, card_id: str) -> threading.Thread:
    """Activate the fetch and resolve it off the answering thread.

    The search happens on resolution (CR 608.2d: the choices an effect offers
    are announced "while applying the effect"), and the ask parks the
    resolving request until an answer lands on the mailbox - so it cannot be
    the thread that is going to answer."""

    def call() -> None:
        stepping = TestClient(app)
        activated = stepping.post(
            f"/api/games/{game_id}/actions/activate",
            json={"player": "alice", "card_id": card_id, "ability_index": 0},
        )
        assert activated.status_code == 200, activated.text
        stepping.post(f"/api/games/{game_id}/resolve-top")

    thread = threading.Thread(target=call)
    thread.start()
    return thread


def wait_for_pending(client: TestClient, game_id: str, uid: str, kind: str) -> dict[str, Any]:
    for _ in range(1000):
        view = client.get(f"/api/games/{game_id}", params={"uid": uid}).json()
        pending = view.get("pending_decision")
        if pending is not None and pending["kind"] == kind:
            return pending
        time.sleep(0.01)
    raise AssertionError(f"no {kind} decision showed up in time")


def settle_the_shockland(client: TestClient, game_id: str, uid: str) -> None:
    """Breeding Pool's own "As this land enters, you may pay 2 life. If you
    don't, it enters tapped." - a `pay_life` question (CR 119.4) raised by the
    fetch putting it onto the battlefield. Declined here, so it enters tapped:
    the fetch is what this file is about, and an unanswered question would
    park the resolving thread until the provider's timeout and take the
    engine's default in place of a choice."""
    wait_for_pending(client, game_id, uid, "pay_life")
    answered = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uid},
        json={"player": "alice", "accept": False},
    )
    assert answered.status_code == 200, answered.text


def test_the_search_offers_every_legal_find_by_name_in_one_question(client, game) -> None:
    """The reported bug, at the API boundary: one question, both candidates,
    each named - and the name is the only thing a seat has, because CR 400.2
    makes the library hidden and `redact_view` strips it."""
    uids = seats_for(game)
    board = stock_the_fetch(client, game)
    thread = activate_and_resolve_in_background(game, board["foothills"]["id"])
    try:
        pending = wait_for_pending(client, game, uids["alice"], "choose_cards")
        breeding = [
            card_id
            for card_id, name in zip(pending["cards"], pending["card_labels"], strict=True)
            if name == "Breeding Pool"
        ]
        answered = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_ids": breeding},
        )
        settle_the_shockland(client, game, uids["alice"])
    finally:
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert answered.status_code == 200, answered.text

    assert pending["source"] == "Wooded Foothills", (
        "the picker has to say which card is searching, or the choice is a guess"
    )
    assert pending["amount"] == 1
    assert pending["exact"] is False, (
        "CR 701.23b makes a stated-quality search of a hidden zone declinable, "
        "so this is 'up to 1', not 'exactly 1'"
    )
    assert len(pending["cards"]) == 2, (
        f"both legal finds belong in one question, not one walked at a time: {pending['cards']}"
    )
    assert sorted(pending["card_labels"]) == ["Breeding Pool", "Forest"], (
        "the searcher looks at the whole zone (CR 701.23a) and must be told what is in it by name"
    )
    assert pending["default_cards"] == [pending["cards"][0]], (
        "an unanswered search finds the first candidate in zone order, and the "
        "payload says so rather than leaving a seat to guess what silence costs"
    )

    # And the answer was honoured: the card the searcher named is the card it
    # got, not the one that happened to come first.
    after = state(client, game)
    assert find(after["battlefield"], "Breeding Pool")["tapped"] is True, (
        "the shockland's own payment was declined, so CR 614.1c's replacement brought it in tapped"
    )
    assert [card["name"] for card in after["battlefield"]] == ["Breeding Pool"], (
        "the basic Forest was never chosen and must still be in the library"
    )
    assert life_of(after, "alice") == 19, (
        "1 life for the fetch's own cost, and none for the shockland, whose payment was declined"
    )


def test_declining_the_whole_search_is_one_answer_not_one_refusal_per_card(client, game) -> None:
    """CR 701.23b's decline, in a single request. The Hedge Maze half of the
    report: the walk made "no" cost one round trip per candidate, and there is
    no shape of answer that says "none of these" at all."""
    uids = seats_for(game)
    board = stock_the_fetch(client, game)
    thread = activate_and_resolve_in_background(game, board["foothills"]["id"])
    try:
        pending = wait_for_pending(client, game, uids["alice"], "choose_cards")
        answered = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_ids": []},
        )
    finally:
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert answered.status_code == 200, answered.text
    assert len(pending["cards"]) == 2

    after = state(client, game)
    assert after["battlefield"] == [], "nothing was found, so nothing entered"
    assert life_of(after, "alice") == 19, (
        "the cost was still paid - CR 701.23b lets the search find nothing, "
        "not the activation cost go unpaid"
    )
    library = store.get(game).player("alice").library
    assert sorted(card.name for card in library) == ["Breeding Pool", "Forest"]


# -- the renderer, running the shipped ui/app.js ------------------------------


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def render_choose_cards(pending: dict[str, Any]) -> str:
    """The HTML `ui/app.js` really produces for this pending decision."""
    script = f"""
    const {{ renderChooseCardsDecision }} = require({json.dumps(str(APP_JS))});
    process.stdout.write(JSON.stringify(renderChooseCardsDecision({json.dumps(pending)})));
    """
    return json.loads(run_node(script))


def choose_cards_options(pending: dict[str, Any]) -> list[dict[str, str]]:
    script = f"""
    const {{ chooseCardsOptions }} = require({json.dumps(str(APP_JS))});
    process.stdout.write(JSON.stringify(chooseCardsOptions({json.dumps(pending)})));
    """
    return json.loads(run_node(script))


def run_node(script: str) -> str:
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return proc.stdout


#: What the fetch really publishes, minus `card_details`: a library card is
#: named for the deciding seat by `card_labels`, which is the field that had
#: no client reading it.
FETCH_PENDING = {
    "kind": "choose_cards",
    "player": "alice",
    "prompt": "choose up to 1 card(s) to find while searching your library",
    "source": "Wooded Foothills",
    "amount": 1,
    "exact": False,
    "cards": ["card-1", "card-2"],
    "card_labels": ["Breeding Pool", "Forest"],
}


@needs_node
def test_the_picker_names_both_finds_when_only_the_engines_labels_have_them() -> None:
    """The renderer built its boxes out of `card_details` alone, so a
    candidate the board cannot resolve rendered as no box at all - and the
    seat was offered "(no candidates shown)" for a question with two legal
    answers."""
    html = render_choose_cards(FETCH_PENDING)

    assert "Breeding Pool" in html, html
    assert "Forest" in html, html
    assert "(no candidates shown)" not in html
    assert html.count('type="checkbox"') == 2, html
    assert "undefined" not in html


@needs_node
def test_the_picker_says_which_card_is_searching() -> None:
    """`source` is the difference between an informed choice and a guess."""
    assert "Wooded Foothills" in render_choose_cards(FETCH_PENDING)


@needs_node
def test_a_declinable_search_offers_finding_nothing_as_a_real_answer() -> None:
    """CR 701.23b's decline needs a control of its own. Ticking nothing and
    pressing Confirm is the same answer, but a player cannot see that from a
    box that says only "choose up to 1"."""
    html = render_choose_cards(FETCH_PENDING)

    assert "Find nothing" in html, html
    assert "up to 1" in html, html
    assert "declineChooseCards()" in html, html


@needs_node
def test_an_exact_choice_neither_says_up_to_nor_offers_a_decline() -> None:
    """CR 701.23d's branch, and the cleanup discard's: `amount` is a count.
    The old renderer said "choose up to N" for this too."""
    html = render_choose_cards(
        {
            **FETCH_PENDING,
            "exact": True,
            "amount": 2,
            "prompt": "choose 2 card(s) to find while searching your library",
        }
    )

    assert "exactly 2" in html, html
    assert "up to" not in html, html
    assert "Find nothing" not in html, html


@needs_node
def test_a_missing_exact_reads_as_the_engines_own_default() -> None:
    """An older server that never stamps `exact` gets `True`, the field's own
    engine-side default and the shape every `choose_cards` had before a search
    became one."""
    payload = {key: value for key, value in FETCH_PENDING.items() if key != "exact"}
    html = render_choose_cards(payload)

    assert "exactly 1" in html, html
    assert "Find nothing" not in html, html


@needs_node
def test_a_candidate_is_named_by_details_then_labels_then_its_id() -> None:
    """The precedence the trigger-order picker already uses. `card_details` is
    the server resolving the id against the board and carries the richer view,
    so it wins; `card_labels` is the engine's own naming for what the board
    cannot resolve; the raw id is the last resort that at least never drops a
    candidate silently."""
    options = choose_cards_options(
        {
            "cards": ["card-1", "card-2", "card-3"],
            "card_details": [{"id": "card-1", "name": "Detailed Forest"}],
            "card_labels": ["Labelled Forest", "Breeding Pool"],
        }
    )

    assert [option["label"] for option in options] == [
        "Detailed Forest",
        "Breeding Pool",
        "card-3",
    ]
    assert [option["id"] for option in options] == ["card-1", "card-2", "card-3"]


@needs_node
def test_every_candidate_keeps_a_box_even_with_no_naming_at_all() -> None:
    """No details, no labels: still one box per candidate. Dropping a legal
    find from the picker is how the walk misplayed in the first place."""
    html = render_choose_cards({**FETCH_PENDING, "card_labels": []})
    assert html.count('type="checkbox"') == 2, html


@needs_node
def test_a_label_is_escaped_like_every_other_string_in_the_box() -> None:
    """`card_labels` and `source` are engine text reaching innerHTML, so they
    go through the same escaping the rest of the box does."""
    html = render_choose_cards(
        {**FETCH_PENDING, "card_labels": ["<b>Bad</b>", "Forest"], "source": "<i>Foothills</i>"}
    )

    assert "<b>" not in html and "<i>" not in html, html
    assert "&lt;b&gt;Bad&lt;/b&gt;" in html
    assert "&lt;i&gt;Foothills&lt;/i&gt;" in html


@needs_node
def test_confirm_refuses_to_send_fewer_than_an_exact_choice_needs() -> None:
    """CR 701.23d's count, caught in the box. The server refuses it too
    (`decisions.validate_answer`), so what this buys is the player being told
    what is wrong with their answer instead of it bouncing off the API."""
    script = f"""
    const box = {{ innerHTML: "", hidden: true, className: "" }};
    let fetched = 0;
    globalThis.fetch = () => {{
      fetched += 1;
      return Promise.resolve({{ ok: true, json: async () => ({{}}) }});
    }};
    globalThis.document = {{
      activeElement: null,
      body: {{}},
      addEventListener() {{}},
      querySelector: () => box,
      querySelectorAll: () => [],
    }};
    const app = require({json.dumps(str(APP_JS))});
    app.game.state = {{
      you: "alice",
      pending_decision: {{ kind: "choose_cards", exact: true, amount: 2 }},
    }};
    app.submitChooseCards();
    process.stdout.write(JSON.stringify({{ notice: box.innerHTML, fetched: fetched }}));
    """
    result = json.loads(run_node(script))

    assert result["fetched"] == 0, "an answer the engine will reject was sent anyway"
    assert "exactly 2" in result["notice"], result["notice"]
