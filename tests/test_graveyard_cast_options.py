"""`cast_options` on a graveyard/exile card, and `available_actions`'s
`castable` offering them: the state-view surface over the non-hand halves of
CR 601.2b's keyword permissions (flashback, mayhem, plot, foretell, madness,
airbend, a granted keyword) - `machines/casting.py`'s
`keyword_cast_options`/`marker_cast_options`/`granted_cast_options`.

The reported defect this is the companion to: a player cast Faithful Mending
(mana cost `{W}{U}`, `Flashback {1}{W}{U}`, CR 702.34a) from their graveyard
through this API, and the engine wrongly charged the face cost and never
exiled it. The engine is being changed, separately, so a cast from any zone
other than the hand is refused unless the request names the keyword that
allows it (CR 601.3: "a player can begin to cast a spell only if a rule or
effect allows that player to cast it"). That fix on its own would make
flashback and friends uncastable through this API, because nothing here told
a seat the option existed or what `keyword=` to send - the gap closed here.

`_play_permission_view`/`play_permissions` (`test_play_permissions.py`)
already closed the same shape of gap for CR 601.3's *dynamic* grants
(suspend's free cast, cascade); this is its counterpart for the *keyword*
half, which needs no live `PlayPermission` object at all and so never showed
up there.

Faithful Mending is a real Scryfall printing, served from the engine's own
`tests/fixtures/cards/faithful_mending.json` fixture pool (`scryfall.py`'s
tier 2) - no network, no hand-written card data.
"""

from __future__ import annotations

from typing import Any

import httpx
from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats, available_actions
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def wired_seats(monkeypatch, game_id: str) -> tuple[Seats, dict[str, str]]:
    """`Seats` pointed at the in-process `TestClient`, the same trick
    `test_action_flow.py` uses so `available_actions`'s own dry-run HTTP
    calls land on the test app rather than the network."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id), seats_for(game_id)


# -- discovery: serialize.py's cast_options -----------------------------------


def test_faithful_mending_in_graveyard_names_its_flashback_option(client, game):
    add_card(client, game, "Faithful Mending", "alice", "graveyard")
    uids = seats_for(game)

    as_alice = state(client, game, uid=uids["alice"])
    mending = find(as_alice["graveyards"]["alice"], "Faithful Mending")

    (option,) = mending["cast_options"]
    assert option["keyword"] == "Flashback"
    assert option["rule"] == "CR 702.34a"
    assert option["pays_mana_cost"] is False
    assert option["cost"] == "{1}{W}{U}"
    assert option["usable_now"] is True
    assert option["why_not"] is None


def test_lightning_bolt_in_graveyard_offers_nothing(client, game):
    """A card with no CR 601.2b permission of its own gets no `cast_options`
    at all - the field is left off entirely, matching `special_actions` and
    `exile_pile`'s own "only when there is something to say" shape."""
    add_card(client, game, "Lightning Bolt", "alice", "graveyard")
    uids = seats_for(game)

    as_alice = state(client, game, uid=uids["alice"])
    bolt = find(as_alice["graveyards"]["alice"], "Lightning Bolt")
    assert "cast_options" not in bolt


def test_opponents_view_does_not_carry_the_cast_option(client, game):
    """Graveyards are public (CR 400.2) - bob sees Faithful Mending sitting in
    alice's graveyard just fine - but `cast_options` is the affordance of
    casting *alice's* card, which is not bob's to read off her row; the same
    "own block only" treatment `_redact_player` already gives `decklist`."""
    add_card(client, game, "Faithful Mending", "alice", "graveyard")
    uids = seats_for(game)

    as_bob = state(client, game, uid=uids["bob"])
    mending = find(as_bob["graveyards"]["alice"], "Faithful Mending")
    assert mending["name"] == "Faithful Mending"  # the card itself is not hidden
    assert "cast_options" not in mending


# -- affordances: seatkit.available_actions -----------------------------------


def test_available_actions_offers_the_flashback_cast_with_its_keyword(client, game, monkeypatch):
    add_card(client, game, "Faithful Mending", "alice", "graveyard")
    for _ in range(3):
        add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Plains", "alice", "battlefield")
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])

    as_alice = state(client, game, uid=uids["alice"])
    mending = find(as_alice["graveyards"]["alice"], "Faithful Mending")

    actions = available_actions(alice, as_alice)
    assert {"card_id": mending["id"], "keyword": "Flashback"} in actions["castable"]


def test_available_actions_does_not_offer_a_graveyard_card_with_no_option(
    client, game, monkeypatch
):
    add_card(client, game, "Lightning Bolt", "alice", "graveyard")
    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])

    as_alice = state(client, game, uid=uids["alice"])
    bolt = find(as_alice["graveyards"]["alice"], "Lightning Bolt")

    actions = available_actions(alice, as_alice)
    assert all(entry["card_id"] != bolt["id"] for entry in actions["castable"])


# -- the cast round trip: keyword="Flashback" from the graveyard --------------


def test_flashback_cast_from_the_graveyard_pays_the_flashback_cost_and_exiles(client, game):
    """Faithful Mending itself draws two cards then forces a discard of two
    (CR 608.2d, mid-resolution) - alice needs a library to draw from, or the
    draw alone would empty it and cost her the game (CR 104.3c) before this
    test ever gets to the fact under test. Nobody is polling this game as
    alice, so that discard decision would otherwise sit through a real
    `presence_seconds` probe (`decisions.py`) before defaulting; shrunk here
    the same way `test_transport_timeout.py`'s `_setup_pending_discard` does,
    since what this test cares about is the flashback mechanic, not the
    discard."""
    provider = store.get(game).decisions.provider
    assert provider is not None
    provider.timeout_seconds = 0.2
    provider.delivery_seconds = 0.2
    provider.presence_seconds = 0.2

    add_card(client, game, "Faithful Mending", "alice", "graveyard")
    for _ in range(3):
        add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "library")
    add_card(client, game, "Forest", "alice", "library")
    board = state(client, game)
    mending = find(board["graveyards"]["alice"], "Faithful Mending")
    sources = [card["id"] for card in board["battlefield"]]

    cast: dict[str, Any] = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": mending["id"],
            "keyword": "Flashback",
            "mana_sources": sources,
        },
    ).json()
    assert cast["allowed"] is True, cast
    assert cast["cost"] == "{1}{W}{U}"

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Faithful Mending"
    assert resolved["zone"] == "exile"

    final = state(client, game)
    # CR 702.34a: "cast it from the graveyard ... rather than paying its mana
    # cost. Then exile it." - not back in the graveyard, and not still
    # costing the mana cost `{W}{U}` it would have paid from hand. The
    # library cards drawn-then-discarded land in the graveyard in its place,
    # which is a fact about the draw/discard effect, not about flashback.
    assert all(card["name"] != "Faithful Mending" for card in final["graveyards"]["alice"])
    exiled = find(final["exile"], "Faithful Mending")
    assert exiled["id"] == mending["id"]
    assert final["winner"] is None  # alice's draw did not run her out of cards


# -- the refusal is an Outcome, not an uncaught 400 --------------------------


def test_a_graveyard_cast_with_no_keyword_is_refused_as_an_outcome(client, game):
    """CR 601.3, asked a second time once the announcement is complete: a cast
    from any zone but the hand has to have *claimed* what allows it, so this
    request is refused by the engine at `Casting.cast()` - the last step of
    CR 601.2, and the one step of `_drive_cast` that used to be unguarded.

    What matters here is the *shape* of the refusal, not that it happens. An
    uncaught `ValueError` leaves FastAPI to answer `{"detail": ...}` with no
    `allowed` key; `seatkit._refused` tests `outcome.get("allowed") is False`,
    so it reads that body as "not a refusal", `run_action` treats the call as
    having gone through, and the seat auto-passes priority on a spell that was
    never cast. A live game desynchronises and nothing about the attempt
    reaches the log.

    CR 601.6/CR 733.1's rewind is asserted too: the proposal moved the card to
    the stack, and a refused proposal owes the board exactly what it borrowed -
    the card back in the graveyard, the lands untapped.
    """
    add_card(client, game, "Faithful Mending", "alice", "graveyard")
    for _ in range(3):
        add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Plains", "alice", "battlefield")
    board = state(client, game)
    mending = find(board["graveyards"]["alice"], "Faithful Mending")
    sources = [card["id"] for card in board["battlefield"]]

    answer = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": mending["id"], "mana_sources": sources},
    )

    assert answer.status_code == 200, answer.text
    cast: dict[str, Any] = answer.json()
    assert cast["allowed"] is False, cast
    assert "Flashback" in str(cast)

    final = state(client, game)
    assert find(final["graveyards"]["alice"], "Faithful Mending")["id"] == mending["id"]
    assert final["stack"] == []
    assert all(not card["tapped"] for card in final["battlefield"])
