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
from magic_cards_model.models import Zone

from magic_cards_model_api import affordances as affordances_module
from magic_cards_model_api.store import store


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


# -- one answer, from the engine ---------------------------------------------
#
# `ability_hints`'s `mana_options` used to be read straight off the parsed
# ability: `mana.any_color` became a flat WUBRG list, `mana.options` became
# the set of colours the printed text can produce. That is a guess off printed
# text. `Game.mana_color_options` (CR 605.1a) is what `Game.activate_ability`
# would actually accept right now - the printed set narrowed by the
# board-derived CR 106.7 survey, the CR 614.12a chosen colour on the permanent
# itself, and Mox Amber's CR 106.5 "among legendary creatures and
# planeswalkers you control" reading - and it is the exact call
# `machines.legal_actions` builds `Action.color_options` from. Two readings of
# one rule is the shape this project has already paid for twice; there is one
# now, and this pins that the two surfaces cannot drift apart again.


def mana_sources_on_the_board(client: TestClient, game: str, admin_key: str) -> list[dict]:
    board = client.get(f"/api/games/{game}", params={"uid": admin_key}).json()["battlefield"]
    return [card for card in board if any(a["mana"] for a in card["abilities"])]


def test_mana_options_are_the_engines_own_color_options_not_a_second_reading(client, game):
    """Every mana ability on a real board, both surfaces, one comparison. The
    lands are picked to cover each shape the old reading handled separately: a
    Ravnica dual (two printed colours), an "any color" source that also prints
    a fixed colourless ability (Cavern of Souls, two abilities with different
    answers), a basic (no choice at all), and a pain-style dual whose cost is
    not just {T}."""
    from conftest import _ADMIN_KEYS

    admin_key = _ADMIN_KEYS[game]
    for name in ("Steam Vents", "Cavern of Souls", "Mountain", "Sunbaked Canyon"):
        add_card(client, game, name, "alice", "battlefield")

    actions = client.get(
        f"/api/games/{game}/legal-actions", params={"player": "alice", "uid": admin_key}
    ).json()["actions"]
    from_engine = {
        (action["card_id"], action["ability_index"]): sorted(action["color_options"])
        for action in actions
        if action["kind"] == "activate_mana"
    }
    assert from_engine, "the board must actually offer some mana abilities"

    seen = 0
    for card in mana_sources_on_the_board(client, game, admin_key):
        for ability in card["abilities"]:
            if not ability["mana"]:
                continue
            engine = from_engine.get((card["id"], ability["index"]))
            assert engine is not None, f"{card['name']} #{ability['index']} is not enumerated"
            seen += 1
            # `()` from the engine and `None` on the card view are the same
            # answer - "no real choice to make" - and `ui/mana.js` reads the
            # `None` shape, so the mapping is deliberate, not a mismatch.
            assert (ability["mana_options"] or []) == engine, (
                f"{card['name']} #{ability['index']}: the card view says "
                f"{ability['mana_options']}, the engine says {engine}"
            )
    assert seen >= 5, f"only compared {seen} abilities - the board did not cover the shapes"


def test_a_choice_offering_ability_still_names_its_colours(client, game):
    """The counterweight: folding onto the engine must not quietly answer
    `None` everywhere. A dual and an "any color" source each still name a real
    set for the UI to ask with, and a basic still names none."""
    from conftest import _ADMIN_KEYS

    for name in ("Steam Vents", "Cavern of Souls", "Mountain"):
        add_card(client, game, name, "alice", "battlefield")
    sources = {
        card["name"]: card for card in mana_sources_on_the_board(client, game, _ADMIN_KEYS[game])
    }

    (dual,) = [a for a in sources["Steam Vents"]["abilities"] if a["mana"]]
    assert dual["mana_options"] == ["R", "U"]

    any_color = [
        a for a in sources["Cavern of Souls"]["abilities"] if "any color" in (a["text"] or "")
    ]
    assert any_color and any_color[0]["mana_options"] == ["B", "G", "R", "U", "W"]

    (basic,) = [a for a in sources["Mountain"]["abilities"] if a["mana"]]
    assert basic["mana_options"] is None, "one printed colour is no choice to ask about"


def test_an_any_color_ability_reports_the_narrowed_set_the_engine_hands_it(client, game):
    """The fold, proved rather than merely pinned.

    `Game.mana_color_options` narrows an "any color" source by the live board
    (CR 106.7's survey, CR 614.12a's chosen colour, CR 106.5's "among
    legendary creatures and planeswalkers you control"), so it can legally
    answer with fewer colours than the printed text names. The old code could
    not: it read `mana.any_color` off the parsed ability and returned all five
    every time, whatever the board said, which is exactly the class of "the
    api reported it legal when it was not" this fold exists to end. Given a
    narrowed answer, `ability_hints` must report the narrowed answer.
    """
    from uuid import UUID

    from magic_cards_model.models import Color

    from magic_cards_model_api.affordances import ability_hints
    from magic_cards_model_api.store import store

    add_card(client, game, "Cavern of Souls", "alice", "battlefield")
    cavern = find(state(client, game)["battlefield"], "Cavern of Souls")
    real_game = store.get(game)
    permanent = real_game.find_by_id(UUID(cavern["id"]))
    (any_color_index, ability) = next(
        (index, ability)
        for index, ability in enumerate(real_game.activated_abilities(permanent))
        if getattr(ability, "mana", None) is not None and ability.mana.any_color
    )

    assert ability.mana.any_color, "the printed text really does say 'any color'"
    unnarrowed = real_game.mana_color_options(permanent, any_color_index)
    assert sorted(color.value for color in unnarrowed) == ["B", "G", "R", "U", "W"]

    narrowed = ability_hints(ability, (Color.RED, Color.WHITE))
    assert narrowed["mana_options"] == ["R", "W"], (
        "ability_hints re-derived the colours from the printed text instead of "
        "reporting the engine's own answer"
    )


# ---------------------------------------------------------------------------
# a face-down card is not named, wherever it is named from
# ---------------------------------------------------------------------------


def test_a_candidate_label_does_not_print_a_face_down_cards_name(
    client: TestClient, game: str
) -> None:
    """`_label` returned `thing.name` unconditionally, which is the printed
    name of the card - including a card that is face down.

    CR 708.2 (and CR 707.2 for a copy) is the rule: a face-down permanent
    has no name, and `visibility._redact_facedown_card` already exists for
    exactly this on the state view. `_label` is the other place a card's
    identity leaves this process, and `candidate_line` and `candidates`
    both go through it - newly reachable from `GET .../legal-actions
    ?candidates=true`, which walks the battlefield, graveyards and exile
    (`all_targetables`). Whether any current `TargetRequirement` actually
    accepts a face-down object is beside the point: the rule belongs inside
    the one function that turns an object into a name, so it is right
    however this is reached.
    """
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    live = store.get(game)
    bears = next(
        state for state in live.shared_zones[Zone.BATTLEFIELD] if state.card.name == "Grizzly Bears"
    )
    bears.face_up = False

    label = affordances_module._label(bears)
    line = affordances_module.candidate_line(bears)

    assert label["name"] == "a face-down card", label
    assert label["id"] == str(bears.id), label
    assert label["zone"] == "battlefield", label
    assert label["controller"] == "bob", label
    assert "Grizzly Bears" not in line, line
    assert str(bears.id) in line, line


def test_a_face_up_candidate_is_still_named(client: TestClient, game: str) -> None:
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    live = store.get(game)
    bears = next(
        state for state in live.shared_zones[Zone.BATTLEFIELD] if state.card.name == "Grizzly Bears"
    )

    assert affordances_module._label(bears)["name"] == "Grizzly Bears"
