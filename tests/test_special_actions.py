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

from conftest import add_card, find, state

from magic_cards_model_api.store import store


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
    """CR 702.62a's second ability (one time counter per upkeep, driven by
    `advance`/`step`'s hook into `engine_api.drive_suspend_upkeep`) and third
    (once the last comes off, a free cast - picked up automatically by
    `actions/cast` with no `keyword`, via the granted `PlayPermission`)."""
    add_card(client, game, "Living End", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Swamp", "alice", "battlefield")
    swamps = [c["id"] for c in state(client, game)["battlefield"]]
    client.post(
        f"/api/games/{game}/actions/suspend",
        json={"player": "alice", "card": "Living End", "mana_sources": swamps},
    )

    for expected_remaining in (2, 1, 0):
        response = client.put(f"/api/games/{game}/step", json={"step": "upkeep", "player": "alice"})
        body = response.json()
        (entry,) = body["suspend_upkeep"]
        assert entry["card"] == "Living End"
        assert entry["time_counters"] == expected_remaining
        if expected_remaining == 0:
            assert entry["cast_permission_granted"] is True
        else:
            assert "cast_permission_granted" not in entry

    living_end = find(state(client, game)["exile"], "Living End")
    assert living_end["counters"].get("time", 0) == 0

    # Free cast: CR 702.62a's third ability is a resolving trigger's own
    # instruction (CR 117.2e/608.2g), not a standing "until end of turn"
    # grant - `finish_suspend` hands it out `immediate=True`, good only for
    # the upkeep step it was granted in (`PlayPermission.granted_step`), so
    # the cast has to happen right here, still in that upkeep, and it works
    # despite this being nobody's main phase because an immediate permission
    # bypasses `why_cannot_cast_now`'s ordinary sorcery-speed check entirely.
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": living_end["id"], "from_zone": "exile"},
    )
    cast_body = cast.json()
    assert cast_body["allowed"] is True, cast_body
    assert cast_body["cost"] == ""

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
