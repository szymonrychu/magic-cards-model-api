"""The owner's own words, reproduced end to end: "I'm not able to evoke
through webUI ... I tried to evoke Wistfulness". `test_hand_cast_options.py`
already proved the announcement path itself works - the affordance endpoint,
`legal-actions`, and `POST .../actions/cast` all price the evoke cast
correctly, and the hand card's board-poll `cast_options` badges it. What was
still missing is the click: `ui/actions.js::needsExplicitFire` did not
consider `action.affordance.alternative_costs`, so a non-targeting cast like
Wistfulness (no target, no unbounded slot, no pinned mana) auto-fired at its
printed cost the instant the affordance landed - the player never got the
chance to click the evoke chip `ui/render/ribbon.js::renderAltCosts` draws.

This test drives both real halves rather than restating either: the real
engine, through the real HTTP affordance endpoint, computing Wistfulness's
real evoke option; and the real `ui/actions.js` module (`needsExplicitFire`),
fed that real payload, deciding whether the cast should wait for the player.
No mock stands in for either side."""

from __future__ import annotations

from conftest import add_card
from fastapi.testclient import TestClient
from ui_node import requires_node, run_module

pytestmark = requires_node


def _wistfulness_affordance(client: TestClient, game: str) -> dict:
    add_card(client, game, "Wistfulness", "alice", "hand")
    hand = client.get(f"/api/games/{game}", params={"uid": _alice_uid(client, game)}).json()[
        "hands"
    ]["alice"]
    (card,) = [c for c in hand if c["name"] == "Wistfulness"]
    response = client.get(f"/api/games/{game}/affordances/{card['id']}", params={"player": "alice"})
    assert response.status_code == 200, response.text
    return card, response.json()


def _alice_uid(client: TestClient, game: str) -> str:
    from test_legal_actions_endpoint import seat_uid

    return seat_uid(game, "alice")


def test_evoking_wistfulness_waits_for_the_player_to_choose(client, game):
    """The owner's exact card, the owner's exact keyword: reproduced through
    the real engine and the real frontend decision function, not restated by
    hand."""
    card, affordance = _wistfulness_affordance(client, game)

    evoke = next(alt for alt in affordance["alternative_costs"] if alt["keyword"] == "Evoke")
    assert evoke["available"] is True, affordance  # two Breeding Pools were never needed for this

    action_before_choice = {
        "pinned": [],
        "affordance": affordance,
        "slots": [],
        "altCost": None,
    }
    action_after_choice = {**action_before_choice, "altCost": evoke["index"]}

    result = run_module(
        "actions.js",
        "[m.needsExplicitFire(before), m.needsExplicitFire(after)]",
        before=action_before_choice,
        after=action_after_choice,
    )
    assert result == [
        True,  # the reported bug: this used to auto-fire at the printed cost
        False,  # once the player has actually chosen Evoke, it fires normally
    ]


def test_wistfulness_with_no_alternative_costs_offered_still_auto_fires(client, game):
    """The other half of the fix: a card with genuinely nothing to choose -
    no targets, no alternative cost - must not suddenly grow an extra click.
    Wistfulness always offers evoke, so this asserts on the shape rather than
    the card: an affordance with an empty `alternative_costs` list behaves
    exactly as it did before this fix."""
    card, affordance = _wistfulness_affordance(client, game)
    affordance = {**affordance, "alternative_costs": []}

    action = {"pinned": [], "affordance": affordance, "slots": [], "altCost": None}
    assert run_module("actions.js", "m.needsExplicitFire(action)", action=action) is False
