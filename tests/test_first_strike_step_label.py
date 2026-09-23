"""T9: the api's own name for CR 510.4's first-strike combat damage step.

The engine keeps one `Step.COMBAT_DAMAGE` for both occurrences of the combat
damage step - `Game.is_first_strike_damage_step`'s own docstring in
`magic_cards_model.models.game` explains why: CR 506.1/510.4 both call the
second one "a second combat damage step", never a step of its own. The api
tells the two occurrences apart with `first_strike_damage_step` on the state
view, and with a second position name, `"first_strike_damage"`, wherever it
compares, ranks, stamps or records "the current step" - `serialize.
step_position`/`step_position_of` for a `Game`, `seatkit.step_position` for a
state dict (a deliberate duplicate, not an import - see that module's own
header).

(a) is against the real engine, through the real HTTP routes, with real
Scryfall printings already in the engine's own bundled pool (`scryfall.
load_pooled`, no network, no new fixture file needed): Precinct Captain (CR
702.7, `{W}{W}` 2/2 first strike) and Grizzly Bears (`{1}{G}` 2/2, no
keywords) - both real Modern-legal printings.

(b)-(g) (seatkit/playscripts fake states, the UI's node-driven pattern) live
in their own modules per the plan: `tests/test_autopass.py`,
`tests/test_playscript_runtime.py`, `tests/test_ui_panels.py`,
`tests/test_ui_priority.py`, `tests/test_ui_ribbon.py`,
`tests/test_first_strike_step_position.py`. This file is the real-engine half
alone.
"""

from __future__ import annotations

from conftest import add_card, state
from fastapi.testclient import TestClient

from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def _boot_first_strike_combat(client: TestClient, game: str) -> dict[str, str]:
    """Alice's Precinct Captain (2/2 first strike) attacking, blocked by
    Bob's Grizzly Bears (2/2, no keywords) - a real CR 510.4 combat with
    exactly one blocker, so no `assign_combat_damage` question interrupts it.
    Returns with blockers declared, right before the first combat damage
    step. `captain` needs `summoning_sick: False` patched on - it has no
    haste, the same idiom `test_flow.py`'s `cast_and_resolve_bears` uses for
    a creature added straight to the battlefield rather than cast."""
    captain = add_card(client, game, "Precinct Captain", "alice", "battlefield")["added"]["card_id"]
    client.patch(f"/api/games/{game}/cards/{captain}", json={"summoning_sick": False})
    bears = add_card(client, game, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    uids = seats_for(game)

    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    declare_attackers = client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    assert declare_attackers.json()["pending_decision"]["kind"] == "declare_attackers", (
        declare_attackers.json()
    )
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": captain, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True, attack.json()

    declare_blockers = client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    assert declare_blockers.json()["pending_decision"]["kind"] == "declare_blockers", (
        declare_blockers.json()
    )
    blocks = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": [{"blocker": bears, "blocked": captain}]},
    )
    blocks_body = blocks.json()
    assert blocks_body.get("allowed", blocks_body.get("accepted")) is True, blocks_body
    return {"captain": captain, "bears": bears}


def test_the_first_combat_damage_step_is_labelled_first_strike(client, game) -> None:
    """CR 510.4/506.1: the first of the two occurrences shows `step ==
    "combat_damage"` (the engine's own, unchanged step name) alongside
    `first_strike_damage_step: True` - the api's own extra flag."""
    _boot_first_strike_combat(client, game)

    first = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert first.json()["allowed"] is True, first.json()

    board = state(client, game)
    assert board["step"] == "combat_damage", board["step"]
    assert board["first_strike_damage_step"] is True, board


def test_the_second_combat_damage_step_is_not_labelled_first_strike(client, game) -> None:
    """The second occurrence: same `step` value, flag now `False`."""
    _boot_first_strike_combat(client, game)
    first = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert first.json()["allowed"] is True, first.json()
    # Nothing landed on the stack (Precinct Captain dealt its first-strike
    # damage to a blocking creature, not to a player, so its "create a
    # token" trigger never fired) - the harness's own `/advance` can move
    # straight into the second occurrence, the same as
    # `test_flow.py::advance_collecting_steps` does for every other step.
    assert state(client, game)["stack"] == []

    second = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert second.json()["allowed"] is True, second.json()

    board = state(client, game)
    assert board["step"] == "combat_damage", board["step"]
    assert board["first_strike_damage_step"] is False, board


def test_a_combat_with_no_first_or_double_striker_never_shows_the_flag(client, game) -> None:
    """The negative case the label exists to distinguish from: an ordinary
    Grizzly Bears attacking Grizzly Bears never sets the flag, in the one
    combat damage step it gets (CR 506.1's own "if any... has first strike
    ...or double strike" - neither creature here does)."""
    attacker = add_card(client, game, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    client.patch(f"/api/games/{game}/cards/{attacker}", json={"summoning_sick": False})
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    uids = seats_for(game)

    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": attacker, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True, attack.json()
    assert state(client, game)["first_strike_damage_step"] is False

    declare_blockers = client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    assert declare_blockers.json()["pending_decision"]["kind"] == "declare_blockers"
    assert state(client, game)["first_strike_damage_step"] is False
    blocks = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": []},
    )
    assert blocks.json().get("allowed", blocks.json().get("accepted")) is True, blocks.json()
    assert state(client, game)["first_strike_damage_step"] is False

    damage = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert damage.json()["allowed"] is True, damage.json()
    board = state(client, game)
    assert board["step"] == "combat_damage", board["step"]
    assert board["first_strike_damage_step"] is False, "no first striker: only one step ever runs"


def test_the_paused_overlay_carries_the_flag_too(client, game) -> None:
    """A command paused mid-way through the first-strike step - CR 510.1c's
    `assign_combat_damage` question, real trigger: two or more blockers -
    still shows `first_strike_damage_step: True` on the paused board, not the
    committed one from before the pause (`serialize._paused_overlay`, the
    same idiom `test_assign_combat_damage.py` exercises for `step`/`turn`
    itself).

    Precinct Captain (2/2 first strike) attacks, blocked by both Birds of
    Paradise (0/1) and Elvish Mystic (1/1): two blockers is CR 510.1c's own
    trigger for the question, no trample or lethal-math needed, and both are
    real Scryfall printings already in `tests/fixtures/scryfall`."""
    captain = add_card(client, game, "Precinct Captain", "alice", "battlefield")["added"]["card_id"]
    client.patch(f"/api/games/{game}/cards/{captain}", json={"summoning_sick": False})
    birds = add_card(client, game, "Birds of Paradise", "bob", "battlefield")["added"]["card_id"]
    mystic = add_card(client, game, "Elvish Mystic", "bob", "battlefield")["added"]["card_id"]
    uids = seats_for(game)

    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": captain, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True, attack.json()
    client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    blocks = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={
            "player": "bob",
            "blocks": [
                {"blocker": birds, "blocked": captain},
                {"blocker": mystic, "blocked": captain},
            ],
        },
    )
    assert blocks.json().get("allowed", blocks.json().get("accepted")) is True, blocks.json()

    paused = client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    body = paused.json()
    assert body["paused"] is True, body
    assert body["pending_decision"]["kind"] == "assign_combat_damage", body
    # The response of the paused call itself, and a fresh read straight after
    # it, both go through `serialize._paused_overlay` - both must carry the
    # flag, not only whichever one happens to compute it first.
    assert body["first_strike_damage_step"] is True, body
    board = state(client, game, uids["alice"])
    assert board["step"] == "combat_damage", board["step"]
    assert board["first_strike_damage_step"] is True, board


def test_put_step_refuses_first_strike_damage_naming_why(client, game) -> None:
    """`PUT .../step` moves the raw turn marker (`engine_api.set_step`,
    `Game.advance_to`), which takes a real `Step` - and `"first_strike_
    damage"` is not one (there is no `Step` member for it, CR 510.4/506.1's
    own "second combat damage step" wording). Refused with a 422 naming why,
    not a 500 from `Step("first_strike_damage")` raising, and not the bare
    400 an unrecognised `Step` value gets everywhere else in this api."""
    response = client.put(f"/api/games/{game}/step", json={"step": "first_strike_damage"})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "first_strike_damage" in detail
    assert "combat_damage" in detail


def test_creating_a_game_refuses_first_strike_damage_as_a_starting_step(client) -> None:
    """`POST /api/games` takes the identical `Step(req.step)` shape `PUT
    .../step` does (`NewGame.step`) - the same refusal, for the same
    reason."""
    response = client.post(
        "/api/games",
        json={
            "players": [{"username": "alice"}, {"username": "bob"}],
            "step": "first_strike_damage",
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "first_strike_damage" in detail
    assert "combat_damage" in detail
