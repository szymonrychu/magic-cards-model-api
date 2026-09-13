"""`/api/decks*` (api-owned deck resolution and legality, T5) and
`/api/lobby/games` (lobby-owned game boot): the UI's deck-loading and game-boot
endpoints, added because `serve.py` could previously only boot a game from
`--deck`/`--match` command-line arguments read once at process start (see
`decks_api.py`'s module docstring).

Deck validation used to live at `/api/ui/decks*`, in `decks_api.py` itself.
2b's T5 (map Finding 1) moved it to `api_decks.py`, mounted under plain
`/api`, because it needs `magic_cards_model.models.{Card, Deck, DeckIllegal}`
and `scryfall.py` - dependencies the lobby side of the eventual cut must not
carry. `start_game_from_decklists` (still here, still lobby-owned) now
reaches that endpoint through `api_client.ApiClient` rather than importing
its function directly - the seam the cut will later swap for `httpx`.

Card data comes only from the recorded Scryfall fixtures `conftest.py` pins
(`MCM_CACHE_DIR`) - Mountain, Forest, Grizzly Bears, Lightning Bolt - so every
deck built here stays inside that set. Mountain and Forest are basic lands,
exempt from the four-copy limit (CR 100.2a), so a legal sixty-card deck is
buildable from just these four names without touching the network.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from magic_cards_model_api.api_client import InProcessApiClient
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.store import store

# 52 Mountain (unlimited, basic) + 4 Grizzly Bears + 4 Lightning Bolt = 60,
# legal under CR 100.2a's four-copy limit.
LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"

# Five copies of a non-basic breaks the four-copy limit (CR 100.2a).
TOO_MANY_COPIES = "5 Lightning Bolt\n55 Mountain"

# Only twelve cards: fails CR 100.2a's sixty-card minimum.
TOO_FEW_CARDS = "4 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"


def test_list_bundled_decks_includes_the_repo_decklists(client: TestClient) -> None:
    response = client.get("/api/decks")
    assert response.status_code == 200
    decks = response.json()["decks"]
    assert "amulet_titan.txt" in decks
    assert all(name.endswith(".txt") for name in decks)


def test_the_old_ui_prefixed_deck_routes_are_gone(client: TestClient) -> None:
    """T5 moved deck resolution/legality off the lobby's `/api/ui` namespace
    entirely - proves the move happened rather than the route being merely
    duplicated at the new path. No route answers there any more, so the
    catch-all static mount at `/` (`test_ui_routes.py`'s own subject) is what
    picks these up: 404 for a GET, since it looks for a matching file and
    finds none, 405 for the POST, since the mount only serves GET/HEAD.
    """
    assert client.get("/api/ui/decks").status_code == 404
    assert client.get("/api/ui/decks/amulet_titan.txt").status_code == 404
    assert client.post("/api/ui/decks/validate", json={"text": LEGAL_DECK}).status_code == 405


def test_bundled_deck_text_is_readable(client: TestClient) -> None:
    response = client.get("/api/decks/amulet_titan.txt")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "amulet_titan.txt"
    assert "Amulet" in body["text"] or "Tron" in body["text"] or len(body["text"]) > 0


def test_bundled_deck_text_refuses_path_escape(client: TestClient) -> None:
    response = client.get("/api/decks/..%2Fpyproject.toml")
    assert response.status_code == 404


def test_bundled_deck_text_refuses_unknown_name(client: TestClient) -> None:
    response = client.get("/api/decks/not-a-real-deck.txt")
    assert response.status_code == 404


def test_validate_accepts_a_legal_deck(client: TestClient) -> None:
    response = client.post("/api/decks/validate", json={"text": LEGAL_DECK})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["errors"] == []
    assert body["mainboard_size"] == 60


def test_validate_rejects_too_many_copies_naming_the_card(client: TestClient) -> None:
    response = client.post("/api/decks/validate", json={"text": TOO_MANY_COPIES})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("Lightning Bolt" in e["message"] for e in body["errors"])


def test_validate_rejects_too_few_cards(client: TestClient) -> None:
    response = client.post("/api/decks/validate", json={"text": TOO_FEW_CARDS})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("at least 60" in e["message"] for e in body["errors"])


def test_validate_rejects_garbage_syntax_quoting_the_line(client: TestClient) -> None:
    response = client.post(
        "/api/decks/validate", json={"text": "4 Lightning Bolt\nnot a real line"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "not a real line" in body["errors"][0]["message"]


def test_the_in_process_api_client_matches_the_route(client: TestClient) -> None:
    """The lobby-side seam `start_game_from_decklists` now calls
    (`api_client.InProcessApiClient`) must answer exactly what `POST
    /api/decks/validate` answers - it is the same function underneath, called
    directly instead of over HTTP."""
    api_client = InProcessApiClient()
    direct = api_client.validate_decklist(LEGAL_DECK)
    routed = client.post("/api/decks/validate", json={"text": LEGAL_DECK}).json()
    assert direct == routed

    direct_bad = api_client.validate_decklist(TOO_FEW_CARDS)
    assert direct_bad["valid"] is False
    assert any("at least 60" in e["message"] for e in direct_bad["errors"])


def test_start_game_boots_a_real_game(client: TestClient) -> None:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": LEGAL_DECK, "ai": False},
                {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True},
            ],
            "seed": 7,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    try:
        assert set(body["seat_uids"]) == {"alice", "bob"}
        assert body["ai_players"] == ["bob"]

        state = client.get(
            f"/api/games/{body['game_id']}", params={"uid": body["seat_uids"]["alice"]}
        ).json()
        # The boot leaves the game *resting* in CR 103.5's mulligan step, before
        # CR 103.8's first turn: turn 0, and no phase at all, because CR 500.1
        # makes a phase part of a turn and no turn has begun. Nothing here
        # settles the mulligan on the seats' behalf - both seats still owe a
        # declaration, which is what `pregame` reports.
        assert state["turn_number"] == 0
        assert state["step"] == "mulligan"
        assert state["phase"] is None
        assert state["pregame"]["awaiting_mulligan"] == ["alice", "bob"]
        assert len(state["hands"]["alice"]) == 7
        # The opponent's hand stays hidden from alice's own filtered view.
        assert state["hands"]["bob"] == []
    finally:
        # Every game this suite boots is deleted afterward - the in-memory
        # store (`store.py`) is one module-level singleton shared by the
        # whole pytest process, and a game left behind changes which game
        # `GET /api/games` reports as "the live one" for tests elsewhere
        # (`test_mcp_resilience.py`) that rely on there being exactly one.
        client.delete(f"/api/games/{body['game_id']}")


def test_start_game_refuses_an_illegal_deck_naming_the_problem(client: TestClient) -> None:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": TOO_FEW_CARDS, "ai": False},
                {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True},
            ],
        },
    )
    assert response.status_code == 400
    assert "alice" in response.text


# ---------------------------------------------------------------------------
# `GET /api/lobby/games/{id}/seats`: a seat uid is a credential, not a label
# ---------------------------------------------------------------------------
#
# This route used to hand back `uid` for EVERY seat of the game to any
# caller, credential or not. That made every permitted read a way to obtain
# an acting credential: the mtg-buddy advisor, whose whole design is a uid
# the server refuses to let act, could ask this route for its human's seat
# uid and then act as that human with no gate involvement at all. The
# `decklist` field on this same route was already scoped to the caller's own
# seat (`SeatDetail.decklist`); `uid` now is too, and so is the `uid` echoed
# inside the `ai_seat` liveness block, which was a second copy of the same
# secret.


def _seat_uids(game_id: str) -> dict[str, str]:
    return {player: uid for uid, player in store.seat_uids(game_id).items()}


def test_game_seats_hides_every_uid_from_a_caller_with_no_credential(
    client: TestClient, game: str
) -> None:
    response = client.get(f"/api/lobby/games/{game}/seats")

    assert response.status_code == 200, response.text
    seats = response.json()["seats"]
    assert {seat["player"] for seat in seats} == {"alice", "bob"}
    assert [seat.get("uid") for seat in seats] == [None, None], response.text
    assert [(seat.get("ai_seat") or {}).get("uid") for seat in seats] == [None, None], response.text


def test_game_seats_shows_only_the_callers_own_uid(client: TestClient, game: str) -> None:
    """The same "own block only" rule `decklist` already followed."""
    uids = _seat_uids(game)

    response = client.get(f"/api/lobby/games/{game}/seats", params={"uid": uids["alice"]})

    assert response.status_code == 200, response.text
    shown = {seat["player"]: seat.get("uid") for seat in response.json()["seats"]}
    assert shown == {"alice": uids["alice"], "bob": None}, response.text


def test_game_seats_does_not_hand_an_advisor_its_humans_seat_uid(
    client: TestClient, game: str
) -> None:
    """The escalation this closes. An advisor uid is allowed every GET on
    purpose - it sees exactly what its human sees - so the only thing that
    can stop this route being a credential vending machine is the route."""
    store.register_advisors(game, {"advisor-uid": Advisor(player="alice")})

    response = client.get(f"/api/lobby/games/{game}/seats", params={"uid": "advisor-uid"})

    assert response.status_code == 200, response.text
    seats = response.json()["seats"]
    # Three rows, not two: B5 appends a row per advisor after the seats, so
    # the count is pinned rather than left implicit in the list below - an
    # empty answer must not be able to satisfy this test.
    assert len(seats) == 3, response.text
    assert [seat.get("uid") for seat in seats] == [None, None, None], response.text
    # And not by value either. An advisor row carries no `uid` key at all
    # (`SeatDetail.uid`), so the read above cannot tell "absent" from
    # "leaked under another key" - this can.
    assert "advisor-uid" not in response.text, response.text


def test_game_seats_still_answers_the_admin_key_in_full(
    client: TestClient, game: str, admin_key: str
) -> None:
    """The operator's own credential, the one identity in this api that is
    already trusted with the whole board (`visibility.ADMIN`), still gets
    every seat's uid - a harness that holds the admin key can drive both
    seats, and nothing about that changes."""
    uids = _seat_uids(game)

    response = client.get(f"/api/lobby/games/{game}/seats", params={"uid": admin_key})

    assert response.status_code == 200, response.text
    shown = {seat["player"]: seat.get("uid") for seat in response.json()["seats"]}
    assert shown == uids, response.text
