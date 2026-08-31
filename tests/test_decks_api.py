"""`/api/ui/decks*` and `/api/ui/games`: the UI's deck-loading and game-boot
endpoints, added because `serve.py` could previously only boot a game from
`--deck`/`--match` command-line arguments read once at process start (see
`decks_api.py`'s module docstring).

Card data comes only from the recorded Scryfall fixtures `conftest.py` pins
(`MCM_CACHE_DIR`) - Mountain, Forest, Grizzly Bears, Lightning Bolt - so every
deck built here stays inside that set. Mountain and Forest are basic lands,
exempt from the four-copy limit (CR 100.2a), so a legal sixty-card deck is
buildable from just these four names without touching the network.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

# 52 Mountain (unlimited, basic) + 4 Grizzly Bears + 4 Lightning Bolt = 60,
# legal under CR 100.2a's four-copy limit.
LEGAL_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
LEGAL_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"

# Five copies of a non-basic breaks the four-copy limit (CR 100.2a).
TOO_MANY_COPIES = "5 Lightning Bolt\n55 Mountain"

# Only twelve cards: fails CR 100.2a's sixty-card minimum.
TOO_FEW_CARDS = "4 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"


def test_list_bundled_decks_includes_the_repo_decklists(client: TestClient) -> None:
    response = client.get("/api/ui/decks")
    assert response.status_code == 200
    decks = response.json()["decks"]
    assert "amulet_titan.txt" in decks
    assert all(name.endswith(".txt") for name in decks)


def test_bundled_deck_text_is_readable(client: TestClient) -> None:
    response = client.get("/api/ui/decks/amulet_titan.txt")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "amulet_titan.txt"
    assert "Amulet" in body["text"] or "Tron" in body["text"] or len(body["text"]) > 0


def test_bundled_deck_text_refuses_path_escape(client: TestClient) -> None:
    response = client.get("/api/ui/decks/..%2Fpyproject.toml")
    assert response.status_code == 404


def test_bundled_deck_text_refuses_unknown_name(client: TestClient) -> None:
    response = client.get("/api/ui/decks/not-a-real-deck.txt")
    assert response.status_code == 404


def test_validate_accepts_a_legal_deck(client: TestClient) -> None:
    response = client.post("/api/ui/decks/validate", json={"text": LEGAL_DECK})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["errors"] == []
    assert body["mainboard_size"] == 60


def test_validate_rejects_too_many_copies_naming_the_card(client: TestClient) -> None:
    response = client.post("/api/ui/decks/validate", json={"text": TOO_MANY_COPIES})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("Lightning Bolt" in e["message"] for e in body["errors"])


def test_validate_rejects_too_few_cards(client: TestClient) -> None:
    response = client.post("/api/ui/decks/validate", json={"text": TOO_FEW_CARDS})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("at least 60" in e["message"] for e in body["errors"])


def test_validate_rejects_garbage_syntax_quoting_the_line(client: TestClient) -> None:
    response = client.post(
        "/api/ui/decks/validate", json={"text": "4 Lightning Bolt\nnot a real line"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "not a real line" in body["errors"][0]["message"]


def test_start_game_boots_a_real_game(client: TestClient) -> None:
    response = client.post(
        "/api/ui/games",
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
        assert state["turn_number"] == 1
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
        "/api/ui/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": TOO_FEW_CARDS, "ai": False},
                {"username": "bob", "decklist_text": LEGAL_DECK_B, "ai": True},
            ],
        },
    )
    assert response.status_code == 400
    assert "alice" in response.text
