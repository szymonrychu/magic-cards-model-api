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
- the view model, by loading the shipped `ui/decisions.js` under Node the way
  `tests/test_card_split_labels.py` and `tests/test_ui_actions.py` do - the
  exact file the browser is served, no build step.

Every CR number in this module was verified with `knowledge_search(type="rules")`
in the session that wrote it.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, life_of, state
from fastapi.testclient import TestClient
from ui_node import requires_node, run_module

from magic_cards_model_api.store import store

# -- the API payload, and the fetch it drives --------------------------------


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def stock_the_fetch(client: TestClient, game_id: str) -> dict[str, Any]:
    """Wooded Foothills on alice's battlefield, and a library holding exactly
    the two cards its "Mountain or Forest card" clause can find: a basic
    Forest and Breeding Pool ("Land - Forest Island", so a Forest card by type
    even though it is not a basic and not green)."""
    add_card(client, game_id, "Wooded Foothills", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "library")
    add_card(client, game_id, "Breeding Pool", "alice", "library")
    board = state(client, game_id)
    return {"foothills": find(board["battlefield"], "Wooded Foothills")}


def activate_and_resolve(
    client: TestClient, game_id: str, uid: str, card_id: str
) -> dict[str, Any]:
    """Activate the fetch and resolve it - synchronously now: the search
    happens on resolution (CR 608.2d: the choices an effect offers are
    announced "while applying the effect"), and `resolve-top` returns the
    instant it crosses that question rather than parking a thread for an
    answer to land on. Returns `resolve-top`'s own paused response, `uid`'s
    own view so `pending_decision` carries the candidates and their labels."""
    activated = client.post(
        f"/api/games/{game_id}/actions/activate",
        json={"player": "alice", "card_id": card_id, "ability_index": 0},
    )
    assert activated.status_code == 200, activated.text
    resolved = client.post(f"/api/games/{game_id}/resolve-top", params={"uid": uid})
    assert resolved.status_code == 200, resolved.text
    return resolved.json()


def settle_the_shockland(
    client: TestClient, game_id: str, uid: str, result: dict[str, Any]
) -> None:
    """Breeding Pool's own "As this land enters, you may pay 2 life. If you
    don't, it enters tapped." - a `pay_life` question (CR 119.4) raised, in
    the same resolution, by the fetch putting it onto the battlefield: CR
    614's replacement effects apply as the permanent enters, with no priority
    pass in between, so the `decide` that answers the search resumes straight
    into this one. Declined here, so it enters tapped - the fetch is what
    this file is about. `result` is whatever answered the search, expected to
    carry this pause already merged in (`paused: True`, `pending_decision`)."""
    assert result.get("paused") is True, result
    pending = result["pending_decision"]
    assert pending["kind"] == "pay_life", pending
    answered = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uid},
        json={"player": "alice", "accept": False},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True, answered.json()


def test_the_search_offers_every_legal_find_by_name_in_one_question(client, game) -> None:
    """The reported bug, at the API boundary: one question, both candidates,
    each named - and the name is the only thing a seat has, because CR 400.2
    makes the library hidden and `redact_view` strips it."""
    uids = seats_for(game)
    board = stock_the_fetch(client, game)
    resolved = activate_and_resolve(client, game, uids["alice"], board["foothills"]["id"])
    assert resolved["paused"] is True, resolved
    pending = resolved["pending_decision"]
    assert pending["kind"] == "choose_cards", pending
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
    assert answered.status_code == 200, answered.text
    settle_the_shockland(client, game, uids["alice"], answered.json())

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
    resolved = activate_and_resolve(client, game, uids["alice"], board["foothills"]["id"])
    assert resolved["paused"] is True, resolved
    pending = resolved["pending_decision"]
    assert pending["kind"] == "choose_cards", pending
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": []},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True, answered.json()
    assert len(pending["cards"]) == 2

    after = state(client, game)
    assert after["battlefield"] == [], "nothing was found, so nothing entered"
    assert life_of(after, "alice") == 19, (
        "the cost was still paid - CR 701.23b lets the search find nothing, "
        "not the activation cost go unpaid"
    )
    library = store.get(game).player("alice").library
    assert sorted(card.name for card in library) == ["Breeding Pool", "Forest"]


# -- the view model, running the shipped ui/decisions.js ---------------------


needs_node = requires_node


def render_choose_cards(
    pending: dict[str, Any], draft: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The `choose_cards` view model `ui/decisions.js`'s `viewModel` really
    produces for this pending decision."""
    return run_module("decisions.js", "m.viewModel(p, d)", p=pending, d=draft or {})


def choose_cards_options(pending: dict[str, Any]) -> list[dict[str, str]]:
    return run_module("decisions.js", "m.chooseCardsOptions(p)", p=pending)


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
    vm = render_choose_cards(FETCH_PENDING)
    (options,) = [c["items"] for c in vm["controls"] if c["type"] == "options"]
    labels = [item["label"] for item in options]

    assert "Breeding Pool" in labels
    assert "Forest" in labels
    assert len(options) == 2


@needs_node
def test_the_picker_says_which_card_is_searching() -> None:
    """`source` is the difference between an informed choice and a guess."""
    assert render_choose_cards(FETCH_PENDING)["title"] == "Wooded Foothills"


@needs_node
def test_a_declinable_search_offers_finding_nothing_as_a_real_answer() -> None:
    """CR 701.23b's decline needs a control of its own. Ticking nothing and
    pressing Confirm is the same answer, but a player cannot see that from a
    prompt that says only "choose up to 1"."""
    vm = render_choose_cards(FETCH_PENDING)

    assert vm["secondary"]["label"] == "Find nothing"
    assert vm["secondary"]["answer"] == {"card_ids": []}


@needs_node
def test_an_exact_choice_neither_says_up_to_nor_offers_a_decline() -> None:
    """CR 701.23d's branch, and the cleanup discard's: `amount` is a count.
    The old renderer said "choose up to N" for this too."""
    vm = render_choose_cards(
        {
            **FETCH_PENDING,
            "exact": True,
            "amount": 2,
            "prompt": "choose 2 card(s) to find while searching your library",
        }
    )

    assert vm["submit"]["label"] == "Choose 2"
    assert vm["secondary"] is None


@needs_node
def test_a_missing_exact_reads_as_the_engines_own_default() -> None:
    """An older server that never stamps `exact` gets `True`, the field's own
    engine-side default and the shape every `choose_cards` had before a search
    became one."""
    payload = {key: value for key, value in FETCH_PENDING.items() if key != "exact"}
    vm = render_choose_cards(payload)

    assert vm["submit"]["label"] == "Choose 1"
    assert vm["secondary"] is None


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
    options = choose_cards_options({**FETCH_PENDING, "card_labels": []})
    assert len(options) == 2


@needs_node
def test_a_label_passes_through_verbatim_escaping_is_the_screens_job() -> None:
    """`card_labels` and `source` are engine text; the view model is pure data
    and does not touch it, so both pass through verbatim - escaping for
    `innerHTML` is deferred to whatever screen renders this view model."""
    options = choose_cards_options({**FETCH_PENDING, "card_labels": ["<b>Bad</b>", "Forest"]})
    vm = render_choose_cards({**FETCH_PENDING, "source": "<i>Foothills</i>"})

    assert options[0]["label"] == "<b>Bad</b>"
    assert vm["title"] == "<i>Foothills</i>"


@needs_node
def test_confirm_refuses_to_send_fewer_than_an_exact_choice_needs() -> None:
    """CR 701.23d's count, caught in the view model before a submit ever
    happens: the server refuses it too (`decisions.validate_answer`), so what
    this buys is the submit control never reading as enabled for an answer
    the engine would reject - `ui/decisions.js` has no DOM to send a fetch
    from at all, that part of the old check moved to whatever screen wires
    this view model's `submit.enabled` to a button."""
    vm = render_choose_cards(
        {"kind": "choose_cards", "exact": True, "amount": 2, "cards": ["card-1", "card-2"]},
        draft={"card_ids": ["card-1"]},
    )

    assert vm["submit"]["enabled"] is False
    assert vm["submit"]["label"] == "Choose 2"
