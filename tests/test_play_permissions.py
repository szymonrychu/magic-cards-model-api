"""`play_permissions` and `exile_pile`: the state-view surface over CR 601.3's
dynamic grants (`PlayPermission`, `machines/casting.py`) and CR 406.4's marked
exile piles (suspend, plot, foretell, cascade).

The reported defect: a player cast Violent Outburst, cascade offered "you may
cast Living End without paying its mana cost", the player accepted, the
engine granted the `PlayPermission` - and the seat then had no way to see it.
`GET /api/games/{id}` carried no `play_permissions` key at all, so the card
sat in exile until cleanup bottomed it (`Game._bottom_unused_cascade_
permissions`), and the player reported the engine as broken when the engine
had done its part correctly.

`engine_api.cast_card`'s exile-zone branch already drives
`Casting.use_permission()` automatically - no `keyword` needed - for any
card sitting in the zone its live permission names (suspend's own free cast
already proved this in `test_special_actions.
test_time_counters_come_off_each_upkeep_and_the_free_cast_reanimates`), and
`Game.play_land` does the same for a `PlayAction.PLAY` grant. So the gap
closed here is purely discoverability plus a place to find the marked exile
piles without scanning the whole `exile` list - `serialize._play_permissions_
view`/`_play_permission_view` and the `exile_pile` top-level key.

Every card here is a real Scryfall printing: Living End and the basics are
already cached under tests/fixtures/scryfall/ (copied for `test_special_
actions.py`); Shardless Agent is a real Modern-legal cascade creature, fetched
and cached the same way.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def run_in_background(post: Any) -> threading.Thread:
    thread = threading.Thread(target=post)
    thread.start()
    thread.join(timeout=0.5)
    assert thread.is_alive(), "expected this call to block on a CR 608.2d question"
    return thread


def wait_for_pending(client: TestClient, game_id: str, uid: str, tries: int = 300) -> dict:
    for _ in range(tries):
        view = state(client, game_id, uid=uid)
        if view.get("pending_decision") is not None:
            return view
        time.sleep(0.01)
    raise AssertionError("no pending decision showed up in time")


# -- suspend: the standing "when used" grant --------------------------------


def test_suspends_free_cast_permission_is_discoverable_before_it_is_used(client, game):
    """`play_permissions["alice"]` shows Living End's free-cast grant the
    moment the last time counter comes off - the exact fact a seat needs to
    know it may call `actions/cast` with no mana and no `keyword` - before it
    ever does so."""
    add_card(client, game, "Living End", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Swamp", "alice", "battlefield")
    swamps = [c["id"] for c in state(client, game)["battlefield"]]
    client.post(
        f"/api/games/{game}/actions/suspend",
        json={"player": "alice", "card": "Living End", "mana_sources": swamps},
    )

    for _ in range(3):
        client.put(f"/api/games/{game}/step", json={"step": "upkeep", "player": "alice"})

    living_end = find(state(client, game)["exile"], "Living End")

    # Discoverable before anybody drives it.
    permissions = state(client, game)["play_permissions"]["alice"]
    (permission,) = permissions
    assert permission["card"]["id"] == living_end["id"]
    assert permission["card"]["name"] == "Living End"
    assert permission["from_zone"] == "exile"
    assert permission["action"] == "cast"
    assert permission["pays_mana_cost"] is False
    assert permission["granted_by"] == "Living End's suspend"
    assert permission["rule"] == "CR 702.62a"
    # `finish_suspend` grants this through the same `Game.grant_play_permission`
    # every other grant uses, with `until_end_of_turn` left at its default
    # `True` - CR 702.62a's own text names no duration, but the engine still
    # drops the *permission* (not the card) at cleanup if it goes unused; the
    # card's own "if you don't, it remains exiled" is what leaves it sitting
    # there afterward with no route back, exactly like a cascade hit nobody
    # claims (`bottom_if_unused` is the one difference between the two).
    assert permission["until_end_of_turn"] is True
    assert permission["expires"] == "until end of turn (CR 514.2)"
    assert permission["usable_now"] is True
    assert permission["why_not"] is None

    client.put(f"/api/games/{game}/step", json={"step": "precombat_main", "player": "alice"})
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": living_end["id"], "from_zone": "exile"},
    ).json()
    assert cast["allowed"] is True, cast
    assert cast["cost"] == ""

    # Consumed once the spell has actually moved off the object it named
    # (CR 400.7) - the stack copy is a new object, so nothing still names it.
    assert state(client, game)["play_permissions"]["alice"] == []


def test_bobs_own_view_never_carries_alices_permission_under_the_wrong_key(client, game):
    """`play_permissions` is keyed by the player who may use it - bob never
    sees Living End's grant show up under his own name, and the underlying
    card is already public (CR 400.2's exile), so alice's own key is visible
    to bob too, matching real MTG's "everyone can see the exile pile"."""
    add_card(client, game, "Living End", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Swamp", "alice", "battlefield")
    swamps = [c["id"] for c in state(client, game)["battlefield"]]
    client.post(
        f"/api/games/{game}/actions/suspend",
        json={"player": "alice", "card": "Living End", "mana_sources": swamps},
    )
    for _ in range(3):
        client.put(f"/api/games/{game}/step", json={"step": "upkeep", "player": "alice"})

    uids = seats_for(client, game)
    as_bob = state(client, game, uid=uids["bob"])
    assert as_bob["play_permissions"]["bob"] == []
    assert len(as_bob["play_permissions"]["alice"]) == 1
    assert as_bob["play_permissions"]["alice"][0]["card"]["name"] == "Living End"


# -- exile_pile: the marked piles pulled out of the whole exile list --------


def test_exile_pile_surfaces_suspended_foretold_and_plotted_cards(client, game):
    add_card(client, game, "Living End", "alice", "hand")
    add_card(client, game, "Lupine Harbingers", "alice", "hand")
    add_card(client, game, "Slickshot Show-Off", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Swamp", "alice", "battlefield")
    for _ in range(8):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    board = state(client, game)
    swamps = [c["id"] for c in board["battlefield"] if c["name"] == "Swamp"]
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]
    mountains = [c["id"] for c in board["battlefield"] if c["name"] == "Mountain"]

    client.post(
        f"/api/games/{game}/actions/suspend",
        json={"player": "alice", "card": "Living End", "mana_sources": swamps},
    )
    client.post(
        f"/api/games/{game}/actions/foretell",
        json={"player": "alice", "card": "Lupine Harbingers", "mana_sources": forests[:2]},
    )
    client.post(
        f"/api/games/{game}/actions/plot",
        json={"player": "alice", "card": "Slickshot Show-Off", "mana_sources": mountains},
    )

    uids = seats_for(client, game)
    admin_pile = {card["name"]: card for card in state(client, game)["exile_pile"]}
    assert set(admin_pile) == {"Living End", "Lupine Harbingers", "Slickshot Show-Off"}
    assert admin_pile["Living End"]["exile_pile"]["marker"] == "suspended"
    assert admin_pile["Living End"]["counters"] == {"time": 3}
    assert admin_pile["Lupine Harbingers"]["exile_pile"]["marker"] == "foretold"
    assert admin_pile["Lupine Harbingers"]["face_up"] is False
    assert admin_pile["Slickshot Show-Off"]["exile_pile"]["marker"] == "plotted"

    # exile_pile is exactly the marked subset of exile, no more, no less.
    exile_names = {card["name"] for card in state(client, game)["exile"]}
    assert exile_names == set(admin_pile)

    # A foretold card's identity stays hidden from the opponent (CR 406.3)
    # even inside exile_pile - the same redaction `exile` already gets.
    as_bob = {c["name"]: c for c in state(client, game, uid=uids["bob"])["exile_pile"]}
    harbingers_id = admin_pile["Lupine Harbingers"]["id"]
    hidden = next(c for c in as_bob.values() if c["id"] == harbingers_id)
    assert hidden["name"] == "a face-down card"
    assert hidden["exile_pile"]["marker"] == "foretold"  # the pile fact stays public


# -- cascade: the reported case, end to end through the real API ------------


def _lands(client: TestClient, game_id: str) -> dict[str, list[str]]:
    board = state(client, game_id)
    by_name: dict[str, list[str]] = {}
    for card in board["battlefield"]:
        by_name.setdefault(card["name"], []).append(card["id"])
    return by_name


def test_cascade_into_living_end_is_seen_and_driven_through_the_permission(client, game):
    """The reported real-game defect, end to end: cast a cascade spell, accept
    the offered free cast through the ordinary `decide` flow, see the granted
    permission in the seat's own state (not just infer it from an empty
    exile-minus-the-spell), drive it with the ordinary `actions/cast` (no
    `keyword`, exactly the path suspend's free cast already uses), and watch
    Living End actually leave exile."""
    add_card(client, game, "Shardless Agent", "alice", "hand")
    add_card(client, game, "Living End", "alice", "library", position="top")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    uids = seats_for(client, game)
    lands = _lands(client, game)
    agent = state(client, game)["hands"]["alice"][0]

    def cast_agent() -> None:
        stepping_client = TestClient(app)
        cast = stepping_client.post(
            f"/api/games/{game}/actions/cast",
            json={
                "player": "alice",
                "card_id": agent["id"],
                "mana_sources": [lands["Forest"][0], lands["Island"][0], lands["Forest"][1]],
            },
        )
        assert cast.status_code == 200, cast.text
        assert cast.json()["allowed"] is True, cast.json()
        # The cascade trigger sits above Shardless Agent - resolve it, which
        # is where CR 608.2d's "you may cast it" question is actually asked.
        top = stepping_client.post(f"/api/games/{game}/resolve-top")
        assert top.status_code == 200, top.text

    thread = run_in_background(cast_agent)
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        pending = pending_view["pending_decision"]
        assert pending["kind"] == "may"
        assert pending["source"] == "Shardless Agent"

        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "accept": True},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive()

    # The trigger resolved: Living End is in exile, and the seat can now see
    # exactly the permission a real player needed and did not get before.
    living_end = find(state(client, game)["exile"], "Living End")
    as_alice = state(client, game, uid=uids["alice"])
    (permission,) = as_alice["play_permissions"]["alice"]
    assert permission["card"]["id"] == living_end["id"]
    assert permission["granted_by"] == "Shardless Agent's cascade"
    assert permission["pays_mana_cost"] is False
    assert permission["from_zone"] == "exile"
    assert permission["until_end_of_turn"] is True
    assert permission["expires"] == "until end of turn (CR 514.2)"
    assert permission["usable_now"] is True

    pile = find(state(client, game)["exile_pile"], "Living End")
    assert pile["exile_pile"]["marker"] == "cascade"

    # Shardless Agent itself is still on the stack - resolve it out of the
    # way, exactly as the engine's own end-to-end scenario does, so Living
    # End (a sorcery, CR 117.1a) is cast into an empty stack.
    resolved_agent = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved_agent["resolved"] == "Shardless Agent"

    # Drive the permission through the ordinary cast path a seat actually
    # has: no `keyword`, the same automatic pickup suspend's free cast uses.
    cast_living_end = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": living_end["id"], "from_zone": "exile"},
    ).json()
    assert cast_living_end["allowed"] is True, cast_living_end
    assert cast_living_end["cost"] == ""

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Living End"
    final = state(client, game)
    assert final["exile"] == []
    assert final["play_permissions"]["alice"] == []
    # Living End's own resolution (CR 702.62a's flagship combo, actually
    # driven through the real engine): exile graveyard creatures, *then*
    # sacrifice battlefield creatures, *then* return what was exiled - in
    # that order, Shardless Agent is still on the battlefield at the exile
    # step, so sacrificing it afterward leaves it in the graveyard rather
    # than bringing it back (see the engine's own
    # `test_battlefield_creatures_still_die_with_an_empty_graveyard`).
    # Alice had nothing else in her graveyard, so only the cast spells land
    # there: the sacrificed Shardless Agent and Living End itself.
    assert {c["name"] for c in final["graveyards"]["alice"]} == {"Shardless Agent", "Living End"}
    assert all(c["name"] != "Shardless Agent" for c in final["battlefield"])
