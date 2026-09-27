"""CR 116.2's exile-it-now, cast-it-later special actions (suspend, plot,
foretell) and cycling (CR 702.29a, an activated ability that functions only
in hand) - the API surface over `MarkedExile`/`Casting.use_keyword`/
`Casting.use_permission`/`machines.keywords.cycling_activation`.

Every card here is a real Scryfall printing cached under
tests/fixtures/scryfall/ (Living End, Lupine Harbingers, Slickshot Show-Off,
Angelsong - copied from the engine's own `tests/fixtures/cards/`, which the
engine's own suspend/plot/foretell/cycling scenario tests already use). No
hand-written card data, per the project's "no mocks" rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from conftest import add_card, find, force_turn_position, resolve_whole_stack, state
from magic_cards_model.machines import Step
from magic_cards_model.models import Card, CardState, Zone

from magic_cards_model_api.store import store

FIXTURES = Path(__file__).parent / "fixtures" / "scryfall"


def seats_for(client, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def _bump_turn(game_id: str) -> None:
    """Move the turn counter forward a turn, directly on the engine's own
    `Turn` object (`turn_number` is a plain mutable field - see
    `machines/turn.py`) - the same "reach past the API, straight into the
    live engine object" already used for setup in `test_visibility.py`.

    Plot and foretell are only castable "on a later turn" (CR 702.170d, and
    foretell's own equivalent) - a fact the engine reads off `turn_number`,
    not off the number of `pass-priority`/`advance` calls made - so bumping
    it directly is a legitimate, non-mocking way to set up "some later turn"
    without re-driving the whole turn machine through the API one priority
    pass at a time.
    """
    game = store.get(game_id)
    assert game is not None and game.turn is not None
    game.turn.turn_number += 1


def test_suspending_living_end_exiles_it_with_three_time_counters(client, game):
    """CR 116.2f/702.62a: pays the suspend cost ({2}{B}{B}) and exiles Living
    End - a card with no printed mana cost - with 3 time counters."""
    add_card(client, game, "Living End", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Swamp", "alice", "battlefield")
    board = state(client, game)
    swamps = [c["id"] for c in board["battlefield"]]

    response = client.post(
        f"/api/games/{game}/actions/suspend",
        json={"player": "alice", "card": "Living End", "mana_sources": swamps},
    )
    body = response.json()
    assert body["allowed"] is True, body
    assert body["cost"] == "{2}{B}{B}"
    assert body["time_counters"] == 3
    assert isinstance(body.pop("log_seq"), int)

    board = state(client, game)
    (living_end,) = board["exile"]
    assert living_end["name"] == "Living End"
    assert living_end["counters"] == {"time": 3}
    assert living_end["exile_pile"] == {"marker": "suspended", "owner": "alice", "turn_exiled": 1}
    assert all(source["tapped"] for source in board["battlefield"])
    assert board["hands"]["alice"] == []


def test_suspend_discoverable_in_hand_state_before_it_is_taken(client, game):
    """Discoverability (task step 4): a hand card that could be suspended
    right now reports it, with the real suspend cost, before anyone acts."""
    add_card(client, game, "Living End", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Swamp", "alice", "battlefield")

    (living_end,) = state(client, game)["hands"]["alice"]
    assert living_end["special_actions"]["suspend"] == {"cost": "{2}{B}{B}", "time_counters": 3}
    assert "plot" not in living_end["special_actions"]
    assert "foretell" not in living_end["special_actions"]


def test_time_counters_come_off_each_upkeep_and_the_free_cast_reanimates(client, game):
    """CR 702.62a's second ability (one time counter per upkeep) and third
    (once the last comes off, a free cast - cast immediately while that
    third ability itself is still resolving, `Game.finish_suspend` via
    `Game.may_cast_during_resolution`, CR 608.2g; battle 8's funnel commit
    removed the standing `PlayPermission` this used to leave behind) are
    both live triggered abilities now: the engine puts a real ability on the
    stack for each at the owner's upkeep (`machines/keywords.
    _suspend_abilities`), the same as any other trigger, so nothing in this
    repo drives the countdown by hand any more - a seat has to let it
    resolve.

    `advance` and ordinary `pass-priority` play both reach the trigger
    through the identical `Game.advance` funnel (`pass_priority` calls it
    internally the moment both players pass on an empty stack, CR 117.4, and
    `commands._run_pass_priority`'s own `settle_priorityless_steps` carries
    it on through the untap step that follows) - `PUT .../step`'s raw
    teleport deliberately does not, since it is a harness-only board stager
    and never a seat verb, no more entitled to fire a turn-based trigger than
    it is to skip a draw or a combat declaration. This drives the same
    suspended card through one upkeep via each of the two real paths and
    checks the counter moves by exactly one on both, never two - the
    symmetry the deleted hand-driven code never had, which is why a live
    game (all `pass-priority`) never saw a counter move at all."""
    add_card(client, game, "Living End", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Swamp", "alice", "battlefield")
    swamps = [c["id"] for c in state(client, game)["battlefield"]]
    client.post(
        f"/api/games/{game}/actions/suspend",
        json={"player": "alice", "card": "Living End", "mana_sources": swamps},
    )

    # First upkeep, via `advance` (POST .../advance): CR 702.62a's second
    # ability triggers and goes on the stack as a real ability, resolved
    # here rather than read off a response field that no longer exists.
    force_turn_position(game, Step.UNTAP, "alice")
    advanced = client.post(f"/api/games/{game}/advance", json={"player": "alice"}).json()
    assert advanced["advanced_to"] == "upkeep", advanced
    assert resolve_whole_stack(client, game) == ["ability from Living End"]
    living_end = find(state(client, game)["exile"], "Living End")
    assert living_end["counters"].get("time", 0) == 2

    # Second upkeep, via ordinary priority-passing play instead of `advance`
    # - both players pass on an empty end step (CR 117.4), which crosses the
    # boundary into the priorityless untap step and on into upkeep entirely
    # inside `Game.advance`'s own funnel, the "third path" a live game
    # actually takes and the one the deleted driver never touched.
    force_turn_position(game, Step.END_STEP, "bob")
    passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"}).json()
    assert passed["allowed"] is True, passed
    passed = client.post(
        f"/api/games/{game}/actions/pass-priority", json={"player": "alice"}
    ).json()
    assert passed["allowed"] is True, passed
    assert state(client, game)["step"] == "upkeep", state(client, game)
    assert resolve_whole_stack(client, game) == ["ability from Living End"]
    living_end = find(state(client, game)["exile"], "Living End")
    assert living_end["counters"].get("time", 0) == 1

    # Third and last: the same `advance` path as the first. The last time
    # counter's own removal raises CR 702.62a's third trigger
    # (`finish_suspend`) as a *second* ability, resolved separately - and
    # resolving *that one* is where `finish_suspend` asks its own "you may
    # cast it" (`Game.may_cast_during_resolution`) and pauses, so
    # `resolve_whole_stack` (which cannot answer a decision) only drains the
    # first ability here.
    force_turn_position(game, Step.UNTAP, "alice")
    advanced = client.post(f"/api/games/{game}/advance", json={"player": "alice"}).json()
    assert advanced["advanced_to"] == "upkeep", advanced
    uids = seats_for(client, game)
    counted_down = client.post(
        f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]}
    ).json()
    assert counted_down.get("paused") is not True, counted_down
    assert counted_down["resolved"] == "ability from Living End"
    living_end = find(state(client, game)["exile"], "Living End")
    assert living_end["counters"].get("time", 0) == 0

    finishing = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]}).json()
    assert finishing.get("paused") is True, "finish_suspend's own may pauses (CR 608.2g)"
    pending = finishing["pending_decision"]
    assert pending["kind"] == "may"
    assert pending["source"] == "Living End"

    # Accepting casts Living End immediately, still within this same upkeep
    # (CR 117.1's preamble: a resolving ability's own instruction carries no
    # timing restriction) - no separate `actions/cast` call, and nothing is
    # left standing to discover or drive afterward.
    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["accepted"] is True
    assert body.get("paused") is not True, body
    mid = state(client, game)
    assert mid["exile"] == []
    assert mid["play_permissions"]["alice"] == []
    assert [entry["name"] for entry in mid["stack"]] == ["Living End"]

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Living End"
    final = state(client, game)
    assert final["exile"] == []
    assert [c["name"] for c in final["graveyards"]["alice"]] == ["Living End"]


def test_plotting_slickshot_show_off_and_casting_it_a_later_turn_for_free(client, game):
    """CR 116.2k/702.170b/d: exile with plot, refused the same turn, castable
    for nothing from exile on a later turn once announced with `keyword`."""
    add_card(client, game, "Slickshot Show-Off", "alice", "hand")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    mountains = [c["id"] for c in state(client, game)["battlefield"]]

    plotted = client.post(
        f"/api/games/{game}/actions/plot",
        json={"player": "alice", "card": "Slickshot Show-Off", "mana_sources": mountains},
    ).json()
    assert plotted["allowed"] is True, plotted

    exiled = find(state(client, game)["exile"], "Slickshot Show-Off")
    assert exiled["exile_pile"] == {"marker": "plotted", "owner": "alice", "turn_exiled": 1}

    # Same turn: refused (CR 702.170d, "any turn after the turn it became plotted").
    same_turn = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": exiled["id"],
            "from_zone": "exile",
            "keyword": "Plot",
        },
    ).json()
    assert same_turn["allowed"] is False
    assert "later turn" in same_turn["reason"]

    _bump_turn(game)
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": exiled["id"],
            "from_zone": "exile",
            "keyword": "Plot",
        },
    ).json()
    assert cast["allowed"] is True, cast
    assert cast["cost"] == ""

    bird = state(client, game)["stack"][0]
    assert bird["name"] == "Slickshot Show-Off"


def test_foretelling_lupine_harbingers_is_hidden_from_the_opponent(client, game):
    """CR 702.143a exiles it face down; visibility.py's own new rule is that a
    face-down card in a shared zone reads as a mystery to anyone but its
    controller and the admin - CR 406.3/CR 703's "no name, no text visible"."""
    add_card(client, game, "Lupine Harbingers", "alice", "hand")
    for _ in range(8):  # 2 for the {2} foretell cost, 6 more for the {4}{G}{G} cast later
        add_card(client, game, "Forest", "alice", "battlefield")
    forests = [c["id"] for c in state(client, game)["battlefield"]]
    uids = seats_for(client, game)

    foretold = client.post(
        f"/api/games/{game}/actions/foretell",
        json={
            "player": "alice",
            "card": "Lupine Harbingers",
            "mana_sources": forests[:2],
        },
    ).json()
    assert foretold["allowed"] is True, foretold
    assert foretold["cost"] == "{2}"

    as_alice = state(client, game, uid=uids["alice"])
    (own_view,) = as_alice["exile"]
    assert own_view["name"] == "Lupine Harbingers"
    assert own_view["face_up"] is False
    assert own_view["exile_pile"]["marker"] == "foretold"

    as_bob = state(client, game, uid=uids["bob"])
    (opponent_view,) = as_bob["exile"]
    assert opponent_view["name"] == "a face-down card"
    assert opponent_view.get("card_name") is None
    assert "oracle_text" not in opponent_view
    assert opponent_view["face_up"] is False
    assert opponent_view["controller"] == "alice"  # the pile's ownership stays public (CR 406.4)

    admin_view = state(client, game)  # the harness's own admin key: full visibility
    assert admin_view["exile"][0]["name"] == "Lupine Harbingers"

    _bump_turn(game)
    exiled_id = own_view["id"]
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": exiled_id,
            "from_zone": "exile",
            "keyword": "Foretell",
            "mana_sources": forests,
        },
    ).json()
    assert cast["allowed"] is True, cast
    assert cast["cost"] == "{4}{G}{G}"

    wolf = state(client, game)["stack"][0]
    assert wolf["name"] == "Lupine Harbingers"
    assert wolf["face_up"] is True


def test_discoverability_lists_plot_and_foretell_costs_in_hand(client, game):
    add_card(client, game, "Slickshot Show-Off", "alice", "hand")
    add_card(client, game, "Lupine Harbingers", "alice", "hand")
    for _ in range(6):
        add_card(client, game, "Mountain", "alice", "battlefield")
        add_card(client, game, "Forest", "alice", "battlefield")

    by_name = {c["name"]: c for c in state(client, game)["hands"]["alice"]}
    assert by_name["Slickshot Show-Off"]["special_actions"]["plot"]["cost"]
    assert by_name["Lupine Harbingers"]["special_actions"]["foretell"] == {"cost": "{2}"}


def test_cycling_angelsong_discards_and_draws_through_the_stack(client, game):
    """CR 702.29a: an activated ability that functions only in hand - not
    reachable through `actions/activate` (`Game.begin_activating` only ever
    activates a battlefield permanent's ability), so it gets its own endpoint.
    Puts the ability on the stack, resolved the ordinary way."""
    add_card(client, game, "Angelsong", "alice", "hand")
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "library")
    islands = [c["id"] for c in state(client, game)["battlefield"]]

    (angelsong,) = state(client, game)["hands"]["alice"]
    assert angelsong["special_actions"]["cycling"] == [
        {"index": 0, "keyword": "Cycling", "cost": "{2}, Discard this card"}
    ]

    cycled = client.post(
        f"/api/games/{game}/actions/cycle",
        json={"player": "alice", "card": "Angelsong", "mana_sources": islands},
    ).json()
    assert cycled["allowed"] is True, cycled
    assert cycled["cost"] == "{2}"

    mid_state = state(client, game)
    assert mid_state["hands"]["alice"] == []
    assert [c["name"] for c in mid_state["graveyards"]["alice"]] == ["Angelsong"]
    assert len(mid_state["stack"]) == 1  # on the stack, not resolved yet

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "ability from Angelsong"
    final = state(client, game)
    assert [c["name"] for c in final["hands"]["alice"]] == ["Mountain"]
    assert final["stack"] == []


def test_special_action_refused_for_a_card_without_the_ability(client, game):
    add_card(client, game, "Grizzly Bears", "alice", "hand")

    for action in ("suspend", "plot", "foretell"):
        response = client.post(
            f"/api/games/{game}/actions/{action}",
            json={"player": "alice", "card": "Grizzly Bears"},
        ).json()
        assert response["allowed"] is False
        assert "no " in response["reason"] and "ability" in response["reason"]

    cycle = client.post(
        f"/api/games/{game}/actions/cycle",
        json={"player": "alice", "card": "Grizzly Bears"},
    ).json()
    assert cycle["allowed"] is False


# -- CR 116.3: a special action does not cost you the rest of your phase -----


def test_the_seat_cli_keeps_priority_after_a_land_drop(monkeypatch, client, game):
    """CR 116.3: "If a player takes a special action, that player receives
    priority afterward." Playing a land is CR 116.2a's special action.

    Reported live: the agent played a Forest in its precombat main and then
    could not cast off it until the postcombat main. `seat_cli`'s shared
    release-and-wait flow passed priority after every action, so a land drop
    threw away the rest of the seat's own main phase - and nothing in either
    agent brief mentioned the `--hold` flag that would have avoided it.
    """
    import httpx

    from magic_cards_model_api import seat_cli

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: client)
    game_id = game
    uid = next(u for u, who in store._seats[game_id].items() if who == "alice")
    add_card(client, game_id, "Forest", "alice", "hand")

    rc = seat_cli.run(["--uid", uid, "--game", game_id, "play-land", "Forest"])

    assert rc == 0
    after = state(client, game_id, uid)
    assert after["priority_holder"] == "alice", (
        "CR 116.3 gives priority back after a special action - a land drop must "
        "not release it, or the seat loses the rest of its own main phase"
    )
    assert any(c["name"] == "Forest" for c in after["battlefield"])


# -- FD5 (5)/FD1: turn-face-up and companion -----------------------------


def stage_companion(client, game_id: str, player: str, card_name: str) -> str:
    """A real card added to `player`'s sideboard, then wired up as their
    chosen companion by reaching straight into the live engine object -
    `Player.companion_id` is only ever set at deck load for real, and there
    is no HTTP route for it, so this mirrors `_bump_turn`'s own "reach past
    the API for setup" pattern."""
    added = add_card(client, game_id, card_name, player, "sideboard")
    card_id = added["added"]["card_id"]
    game = store.get(game_id)
    assert game is not None
    from uuid import UUID

    game.player(player).companion_id = UUID(card_id)
    return card_id


def _face_down_morph_creature(game_id: str, player: str) -> str:
    """A synthetic 2/2-turned-3/3 creature with `Morph {2}`, staged directly
    on the live engine's own battlefield - `magic-cards-model-api` (unlike
    `magic-cards-model` itself) treats a test double as a legitimate tool
    for an API-level route test, and no real morph/manifest/disguise
    printing is cached under `tests/fixtures/scryfall/` for this route's
    happy path (no network access to fetch one - see the review response).
    Built by cloning the real, committed Grizzly Bears fixture and adding
    only the one line and keyword a morph creature's printing carries, so
    everything else about the `Card` stays a real, valid printing shape."""
    raw = json.loads((FIXTURES / "grizzly_bears.json").read_text())
    raw["name"] = "FD5 Test Morph Bears"
    raw["oracle_text"] = "Morph {2} (You may cast this card face down as a 2/2 creature for {3}...)"
    raw["keywords"] = [*raw.get("keywords", []), "Morph"]
    # A fresh id, distinct from the real Grizzly Bears printing this was
    # cloned from - `machines/abilities.py`'s own `_ability_cache` is a
    # process-global cache keyed by `Card.id`, so reusing the real id would
    # let whichever test (this one or a real-Grizzly-Bears one) runs first
    # in the same session poison the other's parsed abilities.
    raw["id"] = str(uuid4())
    raw["oracle_id"] = str(uuid4())
    card = Card.model_validate(raw)
    game = store.get(game_id)
    assert game is not None
    state_obj = CardState(
        card=card, zone=Zone.BATTLEFIELD, controller=player, owner=player, face_up=False
    )
    game.battlefield.append(state_obj)
    return str(state_obj.id)


def test_bringing_a_companion_to_hand_pays_three_and_moves_it(client, game):
    stage_companion(client, game, "alice", "Kaheera, the Orphanguard")
    for _ in range(3):
        add_card(client, game, "Mountain", "alice", "battlefield")
    mountains = [c["id"] for c in state(client, game)["battlefield"]]

    response = client.post(
        f"/api/games/{game}/actions/companion",
        json={"player": "alice", "mana_sources": mountains},
    )
    body = response.json()
    assert body["allowed"] is True, body
    assert body["cost"] == "{3}"
    assert isinstance(body.pop("log_seq"), int)

    after = state(client, game)
    assert any(c["name"] == "Kaheera, the Orphanguard" for c in after["hands"]["alice"])


def test_bringing_a_companion_to_hand_is_refused_with_no_companion_chosen(client, game):
    response = client.post(
        f"/api/games/{game}/actions/companion",
        json={"player": "alice"},
    )
    body = response.json()
    assert body["allowed"] is False
    assert "companion" in body["reason"]


def test_bringing_a_companion_to_hand_only_once_per_game(client, game):
    stage_companion(client, game, "alice", "Kaheera, the Orphanguard")
    for _ in range(6):
        add_card(client, game, "Mountain", "alice", "battlefield")
    mountains = [c["id"] for c in state(client, game)["battlefield"]]

    first = client.post(
        f"/api/games/{game}/actions/companion",
        json={"player": "alice", "mana_sources": mountains[:3]},
    ).json()
    assert first["allowed"] is True, first

    second = client.post(
        f"/api/games/{game}/actions/companion",
        json={"player": "alice", "mana_sources": mountains[3:]},
    ).json()
    assert second["allowed"] is False
    assert "already" in second["reason"]


def test_turning_a_permanent_face_up_pays_its_cost_and_reveals_it(client, game):
    card_id = _face_down_morph_creature(game, "alice")
    for _ in range(2):
        add_card(client, game, "Mountain", "alice", "battlefield")
    mountains = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Mountain"]

    response = client.post(
        f"/api/games/{game}/actions/turn-face-up",
        json={"player": "alice", "card_id": card_id, "mana_sources": mountains},
    )
    body = response.json()
    assert body["allowed"] is True, body
    assert body["cost"] == "{2}"
    assert isinstance(body.pop("log_seq"), int)

    after = state(client, game)
    turned = next(c for c in after["battlefield"] if c["id"] == card_id)
    assert turned["name"] == "FD5 Test Morph Bears"


def test_turning_a_non_morph_permanent_face_up_is_refused(client, game):
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    bear = next(c for c in state(client, game)["battlefield"] if c["name"] == "Grizzly Bears")

    response = client.post(
        f"/api/games/{game}/actions/turn-face-up",
        json={"player": "alice", "card_id": bear["id"]},
    )
    body = response.json()
    assert body["allowed"] is False
    assert body["reason"]
