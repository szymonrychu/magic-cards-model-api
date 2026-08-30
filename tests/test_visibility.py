"""Hidden-information filtering: server-side, uid-scoped (`visibility.py`).

The API used to hand back the full state to anyone who asked; a client
(mcp.py) computed the correct, filtered answer for players. These tests hit
the raw HTTP endpoints the way an agent bypassing mcp.py would, to prove the
server itself now enforces the boundary - not just its one well-behaved
client.
"""

from __future__ import annotations

from conftest import add_card, state


def seats_for(client, game_id: str) -> dict[str, str]:
    """The real per-seat uids for `game_id` - as if captured at dispatch."""
    from magic_cards_model_api.store import store

    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def test_a_seat_sees_its_own_hand_and_not_the_opponents(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "hand")
    uids = seats_for(client, game)

    as_alice = state(client, game, uid=uids["alice"])
    assert [c["name"] for c in as_alice["hands"]["alice"]] == ["Lightning Bolt"]
    assert as_alice["hands"]["bob"] == [], "the opponent's hand is never sent, not even trimmed"
    assert as_alice["you"] == "alice"

    as_bob = state(client, game, uid=uids["bob"])
    assert [c["name"] for c in as_bob["hands"]["bob"]] == ["Grizzly Bears"]
    assert as_bob["hands"]["alice"] == []


def test_a_seat_sees_neither_library(client, game):
    add_card(client, game, "Mountain", "alice", "library")
    add_card(client, game, "Forest", "bob", "library")
    uids = seats_for(client, game)

    as_alice = state(client, game, uid=uids["alice"])
    assert "libraries" not in as_alice, "not even the viewer's own library is sent"
    assert "sideboards" not in as_alice


def test_counts_of_hidden_zones_stay_visible_and_correct(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "hand")
    add_card(client, game, "Abrade", "bob", "hand")
    for _ in range(32):
        add_card(client, game, "Mountain", "bob", "library")
    uids = seats_for(client, game)

    as_alice = state(client, game, uid=uids["alice"])
    bob_row = next(p for p in as_alice["players"] if p["username"] == "bob")
    assert bob_row["hand_count"] == 2, "bob's hand size is public even though its cards are not"
    assert bob_row["library_count"] == 32
    alice_row = next(p for p in as_alice["players"] if p["username"] == "alice")
    assert alice_row["hand_count"] == 1


def test_unknown_uid_gets_nothing_hidden(client, game):
    """A uid that resolves to no seat is treated exactly like no uid at all -
    not as a half-trusted in-between."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "hand")

    board = state(client, game, uid="not-a-real-uid")
    assert board["hands"] == {"alice": [], "bob": []}
    assert "libraries" not in board
    assert "you" not in board


def test_no_uid_at_all_gets_nothing_hidden(client, game):
    """THE regression test: this is exactly the leak the assignment reports -
    a caller that talks to the API directly, with no uid, used to get both
    hands and both libraries in full."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "hand")
    add_card(client, game, "Mountain", "alice", "library")

    bare = client.get(f"/api/games/{game}")
    assert bare.status_code == 200
    board = bare.json()

    assert board["hands"] == {"alice": [], "bob": []}, "no uid must not see either hand"
    assert "libraries" not in board, "no uid must not see either library"
    assert "sideboards" not in board


def test_the_admin_key_still_sees_everything(client, game):
    """The harness's own credential (never handed to a player) is the one
    legitimate way to get the full, unfiltered board - the `state()` helper
    uses it implicitly via the `game` fixture."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "hand")
    add_card(client, game, "Mountain", "alice", "library")

    board = state(client, game)  # no explicit uid -> the fixture's admin key
    assert [c["name"] for c in board["hands"]["alice"]] == ["Lightning Bolt"]
    assert [c["name"] for c in board["hands"]["bob"]] == ["Grizzly Bears"]
    assert [c["name"] for c in board["libraries"]["alice"]] == ["Mountain"]


def test_log_endpoint_defaults_full_but_filters_for_a_real_seat(client, game):
    """Bullet 5's decision: the log is the debugger's eye by default (no uid,
    matching audit.py's own documented design and test_log_snapshots.py),
    but a real seat uid - the credential mcp.py's game_log tool now sends -
    gets the same redaction the state endpoint gives that seat."""
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    uids = seats_for(client, game)

    bare = client.get(f"/api/games/{game}/log")
    assert bare.status_code == 200
    full_hands = bare.json()["log"][-1]["snapshot"]["hands"]
    assert full_hands["alice"] == ["Grizzly Bears"], "no uid: still the full debugger's view"

    as_bob = client.get(f"/api/games/{game}/log", params={"uid": uids["bob"]})
    bob_hands = as_bob.json()["log"][-1]["snapshot"]["hands"]
    assert bob_hands["alice"] == ["<hidden>"], "bob's own view of the log hides alice's hand"
    assert bob_hands["bob"] == [], "bob's hand is empty in this snapshot, and stays visibly empty"

    as_alice = client.get(f"/api/games/{game}/log", params={"uid": uids["alice"]})
    alice_hands = as_alice.json()["log"][-1]["snapshot"]["hands"]
    assert alice_hands["alice"] == ["Grizzly Bears"], "alice's own view keeps her own hand"


def test_log_add_card_detail_is_blanked_for_the_other_seat(client, game):
    """The action detail itself (`add_card`'s `card` field), not only the
    snapshot, must not name a card dealt into a hidden zone that is not the
    reader's own."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    uids = seats_for(client, game)

    as_bob = client.get(f"/api/games/{game}/log", params={"uid": uids["bob"]}).json()["log"]
    entry = next(e for e in as_bob if e["action"] == "add_card")
    assert entry["detail"]["card"] == "<hidden>"

    as_alice = client.get(f"/api/games/{game}/log", params={"uid": uids["alice"]}).json()["log"]
    entry = next(e for e in as_alice if e["action"] == "add_card")
    assert entry["detail"]["card"] == "Lightning Bolt"


def test_a_seat_cannot_act_as_the_other_player(client, game):
    """Existing behaviour, unchanged by this work: mcp.py's tools take a
    player_uid and derive the acting player from it - a tool never accepts
    an arbitrary player name, so a seat cannot pose as the other seat.
    (The raw HTTP actions endpoints are the self-test harness's own escape
    hatch and were never uid-gated; see the report.)"""
    from magic_cards_model_api.mcp import Seat, Seats

    seats = Seats(api="http://testserver", game_id=game)
    seats.http = client  # FastAPI TestClient quacks like httpx for .get()

    uids = seats_for(client, game)
    alice_seat = seats.seat_for(uids["alice"])
    assert isinstance(alice_seat, Seat)
    assert alice_seat.player == "alice"
    assert alice_seat.uid == uids["alice"]
    # nothing about the tool surface lets a caller supply a different player
    # name than the one the uid resolved to - the seat *is* the identity.


def test_public_information_is_still_public(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "graveyard")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Abrade", "bob", "hand")
    client.patch(f"/api/games/{game}/players/bob", json={"life": 12})
    uids = seats_for(client, game)

    for uid in (None, uids["alice"], uids["bob"], "garbage"):
        params = {"uid": uid} if uid else None
        board = client.get(f"/api/games/{game}", params=params).json()
        assert [c["name"] for c in board["battlefield"]] == ["Mountain"]
        assert [c["name"] for c in board["graveyards"]["bob"]] == ["Grizzly Bears"]
        assert board["stack"] == []
        assert board["exile"] == []
        life = {p["username"]: p["life"] for p in board["players"]}
        assert life == {"alice": 20, "bob": 12}
        hand_counts = {p["username"]: p["hand_count"] for p in board["players"]}
        assert hand_counts == {"alice": 1, "bob": 1}
