"""An activated ability whose own cost discards the card it is printed on
(CR 113.6j) - Waker of Waves' unkeyworded `{1}{U}, Discard this card: ...`
and the Channel-worded shape it shares with Boseiju, Who Endures and Otawara,
Soaring City - reached end to end through `POST .../actions/activate`.

The reported defect: an AI seat held a legal Waker of Waves and reached for
the `cycle` MCP tool, which refused it ("Waker of Waves has no cycling
ability number 0") because the ability is not cycling at all (CR 702.29a) -
it is CR 113.6j's more general "a cost that can't be paid on the battlefield
functions from any zone in which it can be". The engine's own enumeration
(`machines.legal_actions.legal_actions`) already reported the ability
correctly, through `ActionKind.ACTIVATE_OFF_ZONE`; what had no way to run it
was the api's execution surface - `/actions/activate` reached
`Game.begin_activating`/`Game.activate_ability`, which only ever activate a
*battlefield* permanent's ability, and `/actions/cycle` only ever reaches
`machines.keywords.cycling_activation`, CR 702.29a's own narrower shape.
Neither could reach `machines.keywords.off_zone_activation`, the general
entry point the engine repo's own `tests/scenarios/
test_off_battlefield_activation.py` already exercised directly.

`engine_api.activate_ability` now branches on the card's own zone: a
battlefield permanent keeps the exact path it always had, and a card
anywhere else (hand, graveyard, exile) is activated through
`off_zone_activation`, the single index space `machines.legal_actions.
_off_zone_actions` already reports through `Action.ability_index` - cycling
and a printed off-zone ability share one pool and one call.

Every CR number here was verified with `mise run rules:find` in the session
that wrote this file. Every card is a real Scryfall printing loaded through
`conftest.add_card`; no mocks (the api repo's own rule allows fakes, but a
real board proves the real defect and its real fix).
"""

from __future__ import annotations

from conftest import add_card, find, state
from test_card_split_labels import resolve_top_in_background, wait_for_pending
from test_legal_actions_endpoint import by_kind, legal_actions, seat_uid


def stock_waker_board(client, game_id: str) -> dict[str, str]:
    """Alice: two Islands, Waker of Waves in hand, Lightning Bolt then Forest
    on top of her library (added in that order so the second add lands on
    top - `engine_api.put_card_in_zone`'s own "no position means top" rule)."""
    add_card(client, game_id, "Island", "alice", "battlefield")
    add_card(client, game_id, "Island", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "library")
    add_card(client, game_id, "Lightning Bolt", "alice", "library")
    add_card(client, game_id, "Waker of Waves", "alice", "hand")
    board = state(client, game_id)
    waker = find(board["hands"]["alice"], "Waker of Waves")
    islands = [c["id"] for c in board["battlefield"] if c["name"] == "Island"]
    return {"waker_id": waker["id"], "islands": islands}


def test_waker_of_waves_is_offered_and_executes_end_to_end(client, game):
    """The seat's own two-step loop: read what is legal, then take it."""
    rig = stock_waker_board(client, game)
    uid = seat_uid(game, "alice")

    actions = legal_actions(client, game)
    (offer,) = [a for a in by_kind(actions, "activate_off_zone") if a["card_id"] == rig["waker_id"]]
    assert offer["rule"] == "CR 113.6j"
    assert offer["keyword"] is None, "Waker of Waves prints no ability word at all"
    assert offer["affordable"] == "yes"

    response = client.post(
        f"/api/games/{game}/actions/activate",
        params={"uid": uid},
        json={
            "player": "alice",
            "card_id": rig["waker_id"],
            "ability_index": offer["ability_index"],
            "mana_sources": rig["islands"],
        },
    )
    body = response.json()
    assert body["allowed"] is True, body

    # CR 113.7a: the ability exists on the stack independently of its source,
    # and the discard *was* the cost - Waker is already in the graveyard
    # before the ability has resolved at all.
    mid = state(client, game)
    assert [c["name"] for c in mid["graveyards"]["alice"]] == ["Waker of Waves"]
    assert mid["stack"][0]["kind"] == "ability"
    assert mid["stack"][0]["name"] == "Waker of Waves"

    thread = resolve_top_in_background(game)
    try:
        pending = wait_for_pending(client, game, uid)
        assert pending["kind"] == "scry"
        answered = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uid},
            json={"player": "alice", "moved": [pending["cards"][0]], "top_order": None},
        )
    finally:
        thread.join(timeout=15)
    assert not thread.is_alive()
    assert answered.status_code == 200, answered.text

    final = state(client, game)
    assert final["stack"] == []
    hand_names = {c["name"] for c in final["hands"]["alice"]}
    graveyard_names = {c["name"] for c in final["graveyards"]["alice"]}
    assert hand_names | graveyard_names >= {"Lightning Bolt", "Forest", "Waker of Waves"}
    assert len(hand_names & {"Lightning Bolt", "Forest"}) == 1
    assert len(graveyard_names & {"Lightning Bolt", "Forest"}) == 1


def test_waker_of_waves_activation_is_refused_when_unaffordable(client, game):
    """CR 118.6: the activation is still legal, but not affordable - refused
    with the engine's own reason rather than silently absent."""
    add_card(client, game, "Waker of Waves", "alice", "hand")
    board = state(client, game)
    waker = find(board["hands"]["alice"], "Waker of Waves")
    uid = seat_uid(game, "alice")

    (offer,) = by_kind(legal_actions(client, game), "activate_off_zone")
    assert offer["affordable"] == "no"
    assert offer["unaffordable_reason"]

    response = client.post(
        f"/api/games/{game}/actions/activate",
        params={"uid": uid},
        json={"player": "alice", "card_id": waker["id"], "ability_index": 0},
    )
    body = response.json()
    assert body["allowed"] is False
    assert body["reason"]


def test_waker_of_waves_is_not_offered_from_the_battlefield(client, game):
    """CR 113.6j: the cost can only be paid in a hand, so a battlefield Waker
    offers no `activate_off_zone` action at all - and asking for it directly
    is refused with the engine's own zone reason."""
    add_card(client, game, "Waker of Waves", "alice", "battlefield")
    board = state(client, game)
    waker = find(board["battlefield"], "Waker of Waves")
    uid = seat_uid(game, "alice")

    actions = legal_actions(client, game)
    assert by_kind(actions, "activate_off_zone") == []

    response = client.post(
        f"/api/games/{game}/actions/activate",
        params={"uid": uid},
        json={"player": "alice", "card_id": waker["id"], "ability_index": 0},
    )
    body = response.json()
    assert body["allowed"] is False
    assert "hand" in body["reason"]
    assert "battlefield" in body["reason"]


def test_channel_reaches_the_stack_and_resolves_through_activate(client, game):
    """The ability-word shape (CR 207.2c) that shares CR 113.6j with Waker's
    plain one: Boseiju, Who Endures' Channel, end to end through the exact
    same endpoint, proving the fix is general rather than a Waker-only
    special case. Its own effect - "Destroy target artifact, enchantment, or
    nonbasic land an opponent controls. That player may search their library
    for a land card with a basic land type..." - raises its own `may`
    decision for the opponent mid-resolution, answered here the same way any
    other live decision is."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Boseiju, Who Endures", "alice", "hand")
    add_card(client, game, "Sol Ring", "bob", "battlefield")
    board = state(client, game)
    boseiju = find(board["hands"]["alice"], "Boseiju, Who Endures")
    sol_ring = find(board["battlefield"], "Sol Ring")
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]
    alice_uid = seat_uid(game, "alice")
    bob_uid = seat_uid(game, "bob")

    (offer,) = [a for a in by_kind(legal_actions(client, game), "activate_off_zone")]
    assert offer["keyword"] == "Channel"
    assert offer["rule"] == "CR 113.6j"

    response = client.post(
        f"/api/games/{game}/actions/activate",
        params={"uid": alice_uid},
        json={
            "player": "alice",
            "card_id": boseiju["id"],
            "ability_index": offer["ability_index"],
            "targets": [sol_ring["id"]],
            "mana_sources": forests,
        },
    )
    body = response.json()
    assert body["allowed"] is True, body
    assert state(client, game)["graveyards"]["alice"][0]["name"] == "Boseiju, Who Endures"

    thread = resolve_top_in_background(game)
    try:
        pending = wait_for_pending(client, game, bob_uid)
        assert pending["kind"] == "may"
        answered = client.post(
            f"/api/games/{game}/decide",
            params={"uid": bob_uid},
            json={"player": "bob", "accept": False},
        )
    finally:
        thread.join(timeout=15)
    assert not thread.is_alive()
    assert answered.status_code == 200, answered.text

    final = state(client, game)
    assert final["stack"] == []
    assert any(c["name"] == "Sol Ring" for c in final["graveyards"]["bob"])


def test_cycling_reaches_the_stack_through_the_same_activate_endpoint(client, game):
    """Cycling (CR 702.29a) shares the same unified pool and the same
    endpoint now - the shape fix does not narrow what `/actions/cycle`
    already did, it widens `/actions/activate` to reach it too."""
    add_card(client, game, "Street Wraith", "alice", "hand")
    add_card(client, game, "Lightning Bolt", "alice", "library")
    board = state(client, game)
    wraith = find(board["hands"]["alice"], "Street Wraith")
    uid = seat_uid(game, "alice")

    (offer,) = by_kind(legal_actions(client, game), "activate_off_zone")
    assert offer["rule"] == "CR 702.29a"
    assert offer["keyword"] == "Cycling"

    response = client.post(
        f"/api/games/{game}/actions/activate",
        params={"uid": uid},
        json={"player": "alice", "card_id": wraith["id"], "ability_index": offer["ability_index"]},
    )
    body = response.json()
    assert body["allowed"] is True, body

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved
    final = state(client, game)
    assert [c["name"] for c in final["graveyards"]["alice"]] == ["Street Wraith"]
    assert [c["name"] for c in final["hands"]["alice"]] == ["Lightning Bolt"]


def test_the_old_cycle_endpoint_is_unaffected(client, game):
    """Regression guard: `/actions/cycle` (CR 702.29a's own narrower
    endpoint, used by the `cycle` MCP tool and `seat_cli cycle`) still works
    exactly as it did - the fix widens `/actions/activate`, it does not
    touch this path."""
    add_card(client, game, "Street Wraith", "alice", "hand")
    add_card(client, game, "Lightning Bolt", "alice", "library")
    board = state(client, game)
    wraith = find(board["hands"]["alice"], "Street Wraith")
    uid = seat_uid(game, "alice")

    response = client.post(
        f"/api/games/{game}/actions/cycle",
        params={"uid": uid},
        json={"player": "alice", "card_id": wraith["id"]},
    )
    assert response.json()["allowed"] is True, response.text


def test_a_hand_cards_own_ability_list_reports_the_off_zone_pool_index(client, game):
    """`engine_api.list_abilities` used to read `Game.activated_abilities`
    unconditionally, which is not zone-restricted at all - Boseiju, Who
    Endures prints `{T}: Add {G}` first and Channel second, so a card view
    for a Boseiju in hand reported Channel at index 1, the ordinary
    battlefield ability list's own position. `/actions/activate` for a card
    not on the battlefield now reads `machines.keywords.off_zone_abilities`'
    pool instead (this file's other tests), where Channel is the *only*
    entry, at index 0 - so a seat that took the state view's own number
    literally would call the wrong index and be refused. `list_abilities`
    now reads the same pool for a card anywhere but the battlefield, so the
    number in a card's own view is always the number `/actions/activate`
    actually wants.
    """
    add_card(client, game, "Boseiju, Who Endures", "alice", "hand")
    hand_view = find(state(client, game)["hands"]["alice"], "Boseiju, Who Endures")
    (only_ability,) = hand_view["abilities"]
    assert only_ability["index"] == 0
    assert "Discard this card" in only_ability["cost"]

    # Unaffected: a Boseiju already on the battlefield keeps its ordinary,
    # unfiltered ability list - the mana ability at index 0, Channel at
    # index 1 - since that list is what `/actions/activate` reads for a
    # battlefield permanent, unchanged by this fix.
    add_card(client, game, "Boseiju, Who Endures", "bob", "battlefield")
    battlefield_view = find(state(client, game)["battlefield"], "Boseiju, Who Endures")
    assert [a["index"] for a in battlefield_view["abilities"]] == [0, 1]
    assert battlefield_view["abilities"][1]["cost"] == "{1}{G}, Discard this card"
