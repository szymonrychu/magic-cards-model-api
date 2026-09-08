"""`cast_options` on a card in hand: the CR 118.9 keywords that replace a
spell's mana cost from the hand it is already legal to cast from.

The owner reported it from a live game: "I'm not able to evoke through webUI
(engine was supporting this before) ... I tried to evoke Wistfulness". The
engine did support it - `Casting.use_keyword`/`use_alternative_cost` announce
it, and `GET .../affordances/{card_id}` has listed it under
`alternative_costs` since Force of Negation went unpitched. What no surface
carried was the *cheap* half: `serialize._cast_options_of` stopped at
`Zone.GRAVEYARD`/`Zone.EXILE`, so the 1.2s board poll a client renders from
said nothing at all about a hand card's evoke, and a seat had to build a
`Casting` per hand card (a whole-game deep copy each) to find out that one of
them was evokable.

Seven keywords are the same shape, and all seven are read off the engine's
own `castable_from(card, Zone.HAND)` rather than named here: evoke
(CR 702.74a), dash (CR 702.109a), morph (CR 702.37a), megamorph
(CR 702.37b), disguise (CR 702.168a), bestow (CR 702.103a) and warp's
hand half (CR 702.185a). Every one is a bare CR 118.9 alternative cost paid
from the hand, and CR 601.3's permission question is already answered for the
hand by CR 402.3's own zone - which is exactly why they never needed a
`PlayPermission` and so never showed up anywhere a seat could read them.

The hidden-information half is what makes the hand different from a
graveyard: CR 402.3, "a player can't look at the cards in another player's
hand". `test_the_opponent_learns_nothing_*` below is the test that matters
most in this file.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_legal_actions_endpoint import seat_uid


def hand_card(client: TestClient, game: str, name: str, player: str = "alice") -> dict:
    return find(state(client, game)["hands"][player], name)


def options_of(card: dict) -> dict[str, dict[str, Any]]:
    return {entry["keyword"]: entry for entry in card.get("cast_options") or []}


# -- discovery on the board poll ---------------------------------------------


def test_wistfulness_publishes_its_evoke_option_from_hand(client, game):
    """CR 702.74a: "'Evoke [cost]' means 'You may cast this card by paying
    [cost] rather than paying its mana cost'". The card the owner could not
    evoke, and the whole of what the browser needs to badge it."""
    add_card(client, game, "Wistfulness", "alice", "hand")
    card = hand_card(client, game, "Wistfulness")

    evoke = options_of(card)["Evoke"]
    assert evoke["rule"] == "CR 702.74a"
    # The evoke cost, not the printed {3}{G/U}{G/U} - that is the point.
    assert evoke["cost"] == "{G/U}{G/U}"
    assert evoke["pays_mana_cost"] is False
    assert evoke["usable_now"] is True
    assert evoke["why_not"] is None
    # The printed cost is still on the same view, so a client can show both.
    assert card["mana_cost"] == "{3}{G/U}{G/U}"


def test_an_ordinary_hand_card_carries_no_cast_options_at_all(client, game):
    """Absent, not an empty list: a card with no keyword offering to replace
    its mana cost is cast the ordinary way and needs no row rendered."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    assert "cast_options" not in hand_card(client, game, "Lightning Bolt")


@pytest.mark.parametrize(
    ("card_name", "keyword", "rule"),
    [
        ("Wistfulness", "Evoke", "CR 702.74a"),
        ("Grief", "Evoke", "CR 702.74a"),
        ("Ragavan, Nimble Pilferer", "Dash", "CR 702.109a"),
        ("Brine Elemental", "Morph", "CR 702.37a"),
        ("Detective's Phoenix", "Bestow", "CR 702.103a"),
        ("Bygone Colossus", "Warp", "CR 702.185a"),
    ],
)
def test_every_hand_keyword_in_the_pool_is_published(client, game, card_name, keyword, rule):
    """The whole class, not just evoke. Megamorph (CR 702.37b) and disguise
    (CR 702.168a) have no printing in the engine's bundled pool to play, but
    they need no separate handling here: `_option_from_keyword` builds all
    three face-down keywords from one branch, and this surface reads
    `castable_from` rather than naming any keyword itself."""
    add_card(client, game, card_name, "alice", "hand")
    option = options_of(hand_card(client, game, card_name))[keyword]
    assert option["rule"] == rule
    assert option["pays_mana_cost"] is False


def test_a_keyword_that_casts_from_somewhere_else_is_not_offered_from_hand(client, game):
    """Detective's Phoenix prints bestow (from hand) and a CR 604.6 sentence
    letting it be cast from the graveyard using that same ability. In hand it
    is offered the hand option and not the graveyard one - `castable_from`
    filters on the zone the card is actually in, and this surface does not
    second-guess it."""
    add_card(client, game, "Detective's Phoenix", "alice", "hand")
    options = hand_card(client, game, "Detective's Phoenix")["cast_options"]
    assert [entry["keyword"] for entry in options] == ["Bestow"]
    assert all(entry["rule"] == "CR 702.103a" for entry in options)


# -- hidden information (CR 402.3) -------------------------------------------


def test_the_opponent_learns_nothing_about_a_hand_cards_cast_options(client, game):
    """CR 402.3: "A player can't look at the cards in another player's hand."
    The highest-risk half of publishing this field at all - a graveyard card's
    `cast_options` sits on a card whose identity is already public (CR 400.2),
    and a hand card's does not."""
    add_card(client, game, "Wistfulness", "alice", "hand")
    bob_view = state(client, game, uid=seat_uid(game, "bob"))

    assert bob_view["hands"]["alice"] == []
    assert bob_view["hands"]["bob"] == []
    # Nothing anywhere else in bob's whole view mentions the option either -
    # not a count, not a keyword, not a cost.
    assert "Evoke" not in _walk(bob_view)


def test_a_bystander_with_no_uid_sees_no_hand_cast_options(client, game):
    add_card(client, game, "Wistfulness", "alice", "hand")
    response = client.get(f"/api/games/{game}")
    assert response.status_code == 200, response.text
    anonymous = response.json()

    assert anonymous["hands"]["alice"] == []
    assert "Evoke" not in _walk(anonymous)


def test_the_seat_that_holds_the_card_does_see_it(client, game):
    add_card(client, game, "Wistfulness", "alice", "hand")
    alice_view = state(client, game, uid=seat_uid(game, "alice"))
    (card,) = alice_view["hands"]["alice"]
    assert "Evoke" in options_of(card)


def _walk(view: Any) -> str:
    """Every string anywhere in a response body, flattened - so a leak that
    hides in a nested key still trips the assertion above."""
    import json

    return json.dumps(view)


# -- announcing it (CR 601.2b) -----------------------------------------------


def two_untapped_duals(client: TestClient, game: str) -> list[str]:
    """Two Breeding Pools on alice's battlefield - `{T}: Add {G} or {U}`, so
    together they pay `{G/U}{G/U}` and nothing like `{3}{G/U}{G/U}`."""
    for _ in range(2):
        add_card(client, game, "Breeding Pool", "alice", "battlefield")
    return [
        card["id"] for card in state(client, game)["battlefield"] if card["name"] == "Breeding Pool"
    ]


def evoke_wistfulness(client: TestClient, game: str, **extra: Any) -> dict:
    add_card(client, game, "Wistfulness", "alice", "hand")
    sources = two_untapped_duals(client, game)
    card = hand_card(client, game, "Wistfulness")
    body = {
        "player": "alice",
        "card_id": card["id"],
        "mana_sources": sources,
        **extra,
    }
    response = client.post(f"/api/games/{game}/actions/cast", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_cast_route_accepts_the_keyword_the_hand_option_names(client, game):
    """The round trip the browser makes: read `cast_options[i].keyword` off
    the hand card, post it back as `keyword`."""
    outcome = evoke_wistfulness(client, game, keyword="Evoke")
    assert outcome["allowed"] is True, outcome
    assert [card["name"] for card in state(client, game)["stack"]] == ["Wistfulness"]


def test_the_cast_route_accepts_the_same_option_named_as_an_alternative_cost(client, game):
    """CR 118.9's own front door, for a seat that found the option through
    `GET .../affordances/{card_id}` instead - the two names are the same
    announcement (`Casting._announce_alternative` routes a KEYWORD option
    straight into `use_keyword`)."""
    outcome = evoke_wistfulness(client, game, alternative_cost="Evoke")
    assert outcome["allowed"] is True, outcome
    assert [card["name"] for card in state(client, game)["stack"]] == ["Wistfulness"]


def test_evoking_pays_the_evoke_cost_and_not_the_printed_one(client, game):
    """Two lands, and Wistfulness costs {3}{G/U}{G/U}: the cast can only have
    happened for {G/U}{G/U}, and the outcome says so in the engine's own
    words."""
    outcome = evoke_wistfulness(client, game, keyword="Evoke")
    assert outcome["allowed"] is True, outcome
    assert outcome["cost"] == "{G/U}{G/U}"
    board = state(client, game)
    assert [card["tapped"] for card in board["battlefield"]] == [True, True]
    assert len(board["battlefield"]) == 2


def test_an_evoked_creature_is_sacrificed_when_it_enters(client, game):
    """CR 702.74a's other half: "When this permanent enters, if its evoke cost
    was paid, its controller sacrifices it." The engine already models it; the
    point here is that the API path that announces evoke reaches it - an evoke
    that quietly skipped the sacrifice would be a much worse bug than not
    being able to evoke at all."""
    assert evoke_wistfulness(client, game, keyword="Evoke")["allowed"] is True
    uid = seat_uid(game, "alice")

    # Resolving the spell puts the permanent onto the battlefield and its
    # simultaneous triggers into CR 603.3b's ordering question, which alice
    # owes an answer to before anything else can happen.
    assert client.post(f"/api/games/{game}/resolve-top").status_code == 200
    pending = state(client, game, uid=uid)["pending_decision"]
    assert pending is not None and pending["kind"] == "order_triggers", pending
    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uid},
        json={"player": "alice", "order": pending["cards"]},
    )
    assert answer.status_code == 200, answer.text

    assert client.post(f"/api/games/{game}/resolve-top").status_code == 200
    after = state(client, game)
    assert [card["name"] for card in after["battlefield"]] == ["Breeding Pool", "Breeding Pool"]
    assert [card["name"] for card in after["graveyards"]["alice"]] == ["Wistfulness"]


def test_casting_wistfulness_the_ordinary_way_still_costs_its_printed_cost(client, game):
    """The alternative cost is optional (CR 118.9b) - publishing it must not
    make it the only way to cast the card."""
    add_card(client, game, "Wistfulness", "alice", "hand")
    sources = two_untapped_duals(client, game)
    card = hand_card(client, game, "Wistfulness")
    outcome = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": card["id"], "mana_sources": sources},
    ).json()
    assert outcome["allowed"] is False
    assert "{3}{G/U}{G/U}" in outcome["reason"]
