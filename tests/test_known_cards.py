"""`known_cards`: the state-view surface over `Game.known_to` (CR 400.7,
401.2, 401.4) - what a player knows about a hidden card's identity and, for a
library, its live position.

Nothing in this api ever exposed this before. `libraries`/`sideboards` are
admin-only (nobody reads their own library, matching the real game), so the
engine's own tracking of public knowledge - a card revealed to hand, or, as
of battle 8 (B8-03), a single card put into a library from a public zone -
had no reader here at all.

The reproducer: alice casts Lightning Bolt at bob, bob counters it with
Memory Lapse, which puts the countered spell on top of its owner's library
instead of the graveyard (CR 701.6a's destination, replaced by the spell's
own text) - a spell leaving the stack for a library is as public a move as
CR 400.2 has, so CR 400.2/401.4 make its identity and position known to
*every* player, not just its owner. `known_cards["alice"]["library"]` is
alice's own record of her own top card - information `libraries` never
carries to any seat, hers included - and it is present the same way whether
the request is answered as alice or as bob, because each is reading its own
entry, and both players know the identical fact.
"""

from __future__ import annotations

from conftest import add_card, find, state

from magic_cards_model_api.store import store


def test_a_countered_spell_put_on_top_of_its_owners_library_is_known_to_both_players(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Forest", "alice", "library")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Memory Lapse", "bob", "hand")
    add_card(client, game, "Forest", "bob", "library")
    add_card(client, game, "Island", "bob", "battlefield")
    add_card(client, game, "Island", "bob", "battlefield")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")["id"]
    islands = [c["id"] for c in board["battlefield"] if c["name"] == "Island"]
    bolt = board["hands"]["alice"][0]

    cast_bolt = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "mana_sources": [mountain],
            "targets": ["bob"],
        },
    )
    assert cast_bolt.status_code == 200, cast_bolt.text
    assert cast_bolt.json()["allowed"] is True, cast_bolt.json()
    passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert passed.json()["allowed"] is True, passed.json()

    lapse = state(client, game)["hands"]["bob"][0]
    cast_lapse = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "bob",
            "card_id": lapse["id"],
            "mana_sources": islands,
            "targets": [bolt["id"]],
        },
    )
    assert cast_lapse.status_code == 200, cast_lapse.text
    assert cast_lapse.json()["allowed"] is True, cast_lapse.json()

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Memory Lapse"

    # Before the countered spell's own knowledge check: it left the stack for
    # alice's library, never her graveyard (CR 701.6a's replaced destination).
    admin_view = state(client, game)
    assert admin_view["libraries"]["alice"][0]["name"] == "Lightning Bolt"
    assert admin_view["graveyards"]["alice"] == []
    assert admin_view["players"][0]["life"] == 20 or admin_view["players"][1]["life"] == 20

    uids = {"alice": None, "bob": None}
    for uid, username in store.seat_uids(game).items():
        uids[username] = uid

    for viewer in ("alice", "bob"):
        seat_view = state(client, game, uid=uids[viewer])
        # A viewer never reads another seat's `known_cards` entry (own-block-
        # only redaction, the same shape `hands` already gets) - here that
        # means only the requesting viewer's own key is present at all.
        assert set(seat_view["known_cards"]) <= {viewer}
        own = seat_view["known_cards"][viewer]
        assert own["hand"] == []
        assert own["sideboard"] == []
        (record,) = own["library"]
        assert record["index_from_top"] == 0
        assert record["card"]["name"] == "Lightning Bolt"
        # `libraries` itself stays fully hidden even to alice, its owner -
        # `known_cards` is the one place this position is ever readable.
        assert "libraries" not in seat_view


def test_the_known_library_card_is_no_longer_listed_once_drawn(client, game):
    """CR 400.7: once the known card changes zone by leaving the library, the
    library record ends (it is a new object in a new zone) - `known_to`
    itself is proven by the engine's own scenario tests; this only checks the
    api's view empties out the way the engine's answer says it should."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Forest", "alice", "library")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Memory Lapse", "bob", "hand")
    add_card(client, game, "Forest", "bob", "library")
    add_card(client, game, "Island", "bob", "battlefield")
    add_card(client, game, "Island", "bob", "battlefield")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")["id"]
    islands = [c["id"] for c in board["battlefield"] if c["name"] == "Island"]
    bolt = board["hands"]["alice"][0]
    client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "mana_sources": [mountain],
            "targets": ["bob"],
        },
    )
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    lapse = state(client, game)["hands"]["bob"][0]
    client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "bob",
            "card_id": lapse["id"],
            "mana_sources": islands,
            "targets": [bolt["id"]],
        },
    )
    client.post(f"/api/games/{game}/resolve-top")

    game_obj = store.get(game)
    assert game_obj is not None
    game_obj.draw_cards("alice")

    admin_view = state(client, game)
    assert admin_view["hands"]["alice"][0]["name"] == "Lightning Bolt"
    assert admin_view["known_cards"]["alice"]["library"] == []
