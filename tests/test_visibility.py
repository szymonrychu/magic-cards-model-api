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


# -- the decklist each seat was booted from -----------------------------------
#
# `GET /api/games/{id}` never carries it at all - originally the bug that
# stopped the continuous agent ever learning which deck it was piloting, and
# so which matchup guides to read (`mtg_agent.driver.app`'s `seat_decklist`);
# now, since 2b's T8 (map's S6), deliberately so: `decklist` is lobby-owned
# state (`store.decklists`), and the api component reading it on every state
# poll was the hot reverse seam the six-way cut cannot carry. It lives on the
# lobby's own `GET /api/lobby/games/{id}/seats?uid=...` instead
# (`decks_api.SeatDetail`), and it is still hidden information there: CR
# 100.2a's constructed play is "a way of playing in which each player creates
# their own deck ahead of time", so a seat knows its own; CR 401.1 makes that
# deck the library, and CR 401.2 - "players can't look at or change the order
# of cards in a library" - is exactly what would be undone by naming the
# opponent's sixty cards. `decks_api.game_seats` redacts it the same way
# `visibility._redact_player` used to: present only on the entry matching the
# caller's own `uid` query parameter.

ALICE_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
BOB_DECK = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"


def _booted(client) -> dict:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": ALICE_DECK},
                {"username": "bob", "decklist_text": BOB_DECK},
            ],
            "seed": 7,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _blocks(view: dict) -> dict:
    return {player["username"]: player for player in view["players"]}


def _seats(client, game_id: str, uid: str | None = None) -> dict:
    params = {"uid": uid} if uid is not None else None
    response = client.get(f"/api/lobby/games/{game_id}/seats", params=params)
    assert response.status_code == 200, response.text
    return {seat["player"]: seat for seat in response.json()["seats"]}


def test_the_state_view_carries_no_decklist_for_any_viewer(client):
    """Not even the seat's own - the field left `GET /api/games/{id}`
    entirely (2b's T8, S6); see `test_a_seat_reads_its_own_decklist_and_never
    _the_opponents` below for where it actually lives now."""
    info = _booted(client)
    game_id = info["game_id"]
    try:
        for uid in (info["seat_uids"]["alice"], info["seat_uids"]["bob"], info["admin_key"], None):
            params = {"uid": uid} if uid is not None else None
            blocks = _blocks(client.get(f"/api/games/{game_id}", params=params).json())
            assert all("decklist" not in block for block in blocks.values())
    finally:
        client.delete(f"/api/games/{game_id}")


def test_a_seat_reads_its_own_decklist_and_never_the_opponents(client):
    info = _booted(client)
    game_id = info["game_id"]
    try:
        for seat, own, theirs in (("alice", ALICE_DECK, "bob"), ("bob", BOB_DECK, "alice")):
            seats = _seats(client, game_id, uid=info["seat_uids"][seat])
            assert seats[seat]["decklist"] == own
            assert not seats[theirs].get("decklist"), (
                f"{seat} was handed {theirs}'s decklist - that is the whole library, "
                "by name, which CR 401.2 keeps from every player including its owner"
            )
    finally:
        client.delete(f"/api/games/{game_id}")


def test_neither_a_stranger_nor_a_bystander_reads_any_decklist(client):
    """A caller with no uid, or one this game does not know, must never see
    more than a real seat does - the same rule the rest of this module keeps
    for hands and libraries. `info["admin_key"]` is included too: this route
    never learned an admin bypass, so it is exactly as much a stranger here
    as any other unrecognised uid."""
    info = _booted(client)
    game_id = info["game_id"]
    try:
        for uid in (None, "not-a-real-uid", info["admin_key"]):
            seats = _seats(client, game_id, uid=uid)
            assert all(not seat.get("decklist") for seat in seats.values())
    finally:
        client.delete(f"/api/games/{game_id}")


def test_a_board_built_game_reports_no_decklist_at_all(client, game):
    """`POST /api/games` builds a board with no decks registered, so there is
    no list to report - and an absent key, not an empty string, is the honest
    answer for "this seat never had a decklist". Checked on both the state
    view (never carried it) and the lobby's seats route (nothing registered
    for a game the lobby never booted)."""
    blocks = _blocks(state(client, game))
    assert all("decklist" not in block for block in blocks.values())

    from magic_cards_model_api.store import store

    uid = next(iter(store.seat_uids(game).keys()))
    seats = _seats(client, game, uid=uid)
    assert all(not seat.get("decklist") for seat in seats.values())


# ---------------------------------------------------------------------------
# the advisor credential: the human's own view, exactly
# ---------------------------------------------------------------------------


def advisors_for(game_id: str, player: str, uid: str = "advisor-uid") -> str:
    """Register `uid` as an advisor on `player`'s seat and hand it back - the
    credential a buddy pod is eventually given (its minting route is a later
    task; the credential and its resolution are this one)."""
    from magic_cards_model_api.repository import Advisor
    from magic_cards_model_api.store import store

    store.register_advisors(game_id, {uid: Advisor(player=player)})
    return uid


def test_an_advisor_uid_sees_exactly_what_its_human_sees(client, game):
    """`resolve_viewer` resolves an advisor uid to the human's *username*, so
    every redaction function downstream (`redact_view`, `redact_log_entry`,
    `redact_pending_decision`) filters for the human without knowing an
    advisor exists. Byte-identical is the assertion, deliberately: anything
    less would mean the advisor is being handed a second, subtly different
    view nobody is testing against the rules."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "hand")
    add_card(client, game, "Mountain", "alice", "library")
    uids = seats_for(client, game)
    advisor = advisors_for(game, "alice")

    as_alice = client.get(f"/api/games/{game}", params={"uid": uids["alice"]})
    as_advisor = client.get(f"/api/games/{game}", params={"uid": advisor})

    assert as_advisor.status_code == 200, as_advisor.text
    assert as_advisor.content == as_alice.content


def test_an_advisor_uid_reads_the_log_exactly_as_its_human_does(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "hand")
    uids = seats_for(client, game)
    advisor = advisors_for(game, "alice")

    as_alice = client.get(f"/api/games/{game}/log", params={"uid": uids["alice"]})
    as_advisor = client.get(f"/api/games/{game}/log", params={"uid": advisor})

    assert as_advisor.status_code == 200, as_advisor.text
    assert as_advisor.content == as_alice.content


def test_an_advisor_of_one_seat_does_not_see_the_other_seats_hand(client, game):
    """The advisor inherits its human's blind spots too - it is the human's
    view, not a privileged one."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "hand")
    advisor = advisors_for(game, "alice")

    board = state(client, game, uid=advisor)

    assert [c["name"] for c in board["hands"]["alice"]] == ["Lightning Bolt"]
    assert board["hands"]["bob"] == []
    assert "libraries" not in board


def test_an_advisor_uid_is_not_the_admin_key(client, game):
    """An advisor resolves to a player, never to `visibility.ADMIN` - the
    full board stays the operator's own eye."""
    add_card(client, game, "Mountain", "alice", "library")
    advisor = advisors_for(game, "alice")

    board = state(client, game, uid=advisor)

    assert "libraries" not in board
