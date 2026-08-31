"""`affordances.py`: what an action asks for, answered by the engine.

The browser UI is context-sensitive only because the server can answer four
questions per action - does it target, how many, which objects are legal, and
is there an X or a mode to announce. This file pins those answers.

The motivating case is `test_karn_plus_one_*` below. Karn, the Great Creator
has two loyalty abilities: `+1` targets "up to one target noncreature
artifact", `-2` targets nothing. The old UI could not tell them apart, so it
sent `targets: []` for both - which the engine reads as CR 115.6's legal
answer of "no targets" rather than "the player has not chosen", and Karn's
`+1` went on the stack targeting nothing every time.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient


def affordances(
    client: TestClient, game: str, card_id: str, player: str = "alice", **params: Any
) -> dict:
    response = client.get(
        f"/api/games/{game}/affordances/{card_id}", params={"player": player, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def names(result: dict) -> set[str]:
    return {c["name"] for c in result["candidates"]}


# -- the ability that targets, and the one that does not --------------------


def test_karn_plus_one_reports_that_it_targets_and_the_minus_two_does_not(client, game):
    """CR 606.2's two loyalty abilities, told apart by the state view alone -
    no round trip, so the board can badge one and not the other."""
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    karn = find(state(client, game)["battlefield"], "Karn, the Great Creator")

    plus_one, minus_two = karn["abilities"]
    assert plus_one["loyalty"] == {"amount": 1, "label": "+1"}
    assert minus_two["loyalty"] == {"amount": -2, "label": "-2"}

    assert plus_one["targeting"]["targets_anything"] is True
    # "up to one" is CR 115.6: optional, so the floor is zero and the ceiling
    # is one. Both halves matter - the floor is what makes an empty answer
    # legal at all, and the ceiling is what stops a second target.
    assert plus_one["targeting"]["minimum"] == 0
    assert plus_one["targeting"]["maximum"] == 1

    assert minus_two["targeting"]["targets_anything"] is False


def test_karn_plus_one_offers_only_noncreature_artifacts(client, game):
    """CR 601.2c, answered against the real board: the candidate list is
    every object each `TargetRequirement` accepts, and nothing else."""
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "bob", "battlefield")
    add_card(client, game, "Mind Stone", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    karn = find(state(client, game)["battlefield"], "Karn, the Great Creator")

    result = affordances(client, game, karn["id"], ability_index=0)
    # Both artifacts, whoever controls them - "target noncreature artifact"
    # names no controller. Not the creature, not the land, not Karn.
    assert names(result) == {"Sol Ring", "Mind Stone"}
    assert result["requirements"][0]["phrase"] == "target noncreature artifact"


def test_the_minus_two_offers_no_candidates_at_all(client, game):
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "bob", "battlefield")
    karn = find(state(client, game)["battlefield"], "Karn, the Great Creator")

    result = affordances(client, game, karn["id"], ability_index=1)
    assert result["targets_anything"] is False
    assert result["candidates"] == []


# -- a spell's own targeting --------------------------------------------------


def test_lightning_bolt_offers_any_target(client, game):
    """ "Any target" is creatures, players, planeswalkers and battles - so
    both players and the planeswalker are candidates, and a noncreature
    artifact is not."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "bob", "battlefield")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")

    result = affordances(client, game, bolt["id"])
    assert names(result) == {"alice", "bob", "Grizzly Bears", "Karn, the Great Creator"}
    assert result["minimum"] == 1 and result["maximum"] == 1


def test_a_hand_card_carries_its_cast_hints_on_the_state_view(client, game):
    """The cheap half rides along on every card view, so the UI can decide
    what to show without a round trip per card."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")

    assert bolt["cast"]["mana_cost"] == "{R}"
    # No X in the cost, so the UI shows no X field. This is the whole point:
    # the old UI showed one on every spell.
    assert bolt["cast"]["needs_x"] is False
    assert bolt["cast"]["targeting"]["targets_anything"] is True
    # Not modal, so no `modes` key at all rather than an empty one.
    assert "modes" not in bolt["cast"]


def test_a_land_in_hand_reports_no_targeting_and_no_x(client, game):
    add_card(client, game, "Forest", "alice", "hand")
    forest = find(state(client, game)["hands"]["alice"], "Forest")
    assert forest["cast"]["needs_x"] is False
    assert forest["cast"]["targeting"]["targets_anything"] is False


# -- candidates are read off the live board, not the printed card ------------


def test_a_candidate_list_follows_the_computed_board(client, game):
    """CR 613.5: a candidate list must read what an object *is* now, not what
    it was printed as. Karn's +1 makes Sol Ring an artifact creature, and the
    moment it does, Lightning Bolt's "any target" starts accepting it - which
    a printed-text reading would miss. `TargetRequirement.rejects` is the
    engine's own predicate, so the list cannot disagree with the announcement
    that follows it."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "bob", "battlefield")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")
    karn = find(state(client, game)["battlefield"], "Karn, the Great Creator")
    sol_ring = find(state(client, game)["battlefield"], "Sol Ring")

    # Printed, Sol Ring is a noncreature artifact: not "any target".
    assert "Sol Ring" not in names(affordances(client, game, bolt["id"]))

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": karn["id"],
            "ability_index": 0,
            "targets": [sol_ring["id"]],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True, response.json()
    assert client.post(f"/api/games/{game}/resolve-top").status_code == 200

    # It is a creature now, so the same question gets a different answer.
    after = state(client, game)
    assert find(after["battlefield"], "Sol Ring")["power"] is not None
    assert "Sol Ring" in names(affordances(client, game, bolt["id"]))


# -- CR 118.9 alternative costs ----------------------------------------------


def test_an_evoke_card_reports_its_alternative_cost(client, game):
    """A seat had no way to *find* an alternative cost before this, and so
    could not announce one - Force of Negation went unpitched twice in a real
    game for exactly that reason."""
    add_card(client, game, "Subtlety", "alice", "hand")
    subtlety = find(state(client, game)["hands"]["alice"], "Subtlety")

    result = affordances(client, game, subtlety["id"])
    options = result["alternative_costs"]
    assert options, "Subtlety prints an evoke cost and none was reported"
    evoke = options[0]
    assert evoke["keyword"] == "Evoke"
    assert evoke["requires"] == 1
    assert evoke["available"] is True


def test_asking_for_alternative_costs_leaves_the_card_in_hand(client, game):
    """`affordances.alternative_costs` builds a `Casting` to ask the engine
    what a card could be cast for, and CR 601.2a moves the card to the stack
    the moment a cast is proposed - so the question has to be rewound
    (CR 601.6, CR 733.1) or merely opening a card's row would cast it. The UI
    polls this endpoint, so the answer must be repeatable.
    """
    add_card(client, game, "Subtlety", "alice", "hand")
    subtlety = find(state(client, game)["hands"]["alice"], "Subtlety")

    for _ in range(3):
        assert affordances(client, game, subtlety["id"])["alternative_costs"]
        after = state(client, game)
        assert after["stack"] == []
        assert [card["name"] for card in after["hands"]["alice"]] == ["Subtlety"]


def test_a_card_with_no_alternative_cost_reports_an_empty_list(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")
    assert affordances(client, game, bolt["id"])["alternative_costs"] == []


def test_an_ability_is_never_asked_about_alternative_costs(client, game):
    """CR 118.9 is about casting a spell; an activated ability has no such
    thing, so the key is absent rather than an empty list a UI would render
    an empty row for."""
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    karn = find(state(client, game)["battlefield"], "Karn, the Great Creator")
    assert "alternative_costs" not in affordances(client, game, karn["id"], ability_index=0)


# -- CR 614.12a's chosen name, on the card that chose it ---------------------


def test_a_permanent_with_no_choice_carries_no_choice_field(client, game):
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    bears = find(state(client, game)["battlefield"], "Grizzly Bears")
    assert "choice" not in bears
