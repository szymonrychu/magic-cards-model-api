"""`support_warnings` (flow 6, W2): the engine's `card_support` verdict,
surfaced at every place a decklist is submitted, validated, or loaded.

Card data is the recorded fixture cache (`tests/fixtures/scryfall`, tier 1)
plus the engine's own bundled printing corpus (tier 2,
`magic_cards_model/data/printings`), so every card here resolves offline -
`Lightning Bolt` is a real, fully `handled` printing, and `Keen-Eyed
Curator`/`Final Showdown`/`Alhammarret's Archive` are the engine's own fixture
pool of a `silent`, a `notice`, and an `unaudited` card
(`magic-cards-model/tests/test_card_support.py`). Both repos read the exact
same `card_support(card)` - this is the "at least one real-engine test" the
task asks for, run three times over, not stubbed once.

Legality (`valid`/`errors`) is asserted to stay exactly as it was before this
task, on the same requests these warnings ride along with - `support_warnings`
is additive, never a gate.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api import decks
from magic_cards_model_api.api_client import InProcessApiClient

# 52 Mountain (basic, unlimited under CR 100.2a) + 4 Lightning Bolt (a real,
# fully `handled` printing) = 60, legal.
HANDLED_ONLY_DECK = "56 Mountain\n4 Lightning Bolt"

# 52 Mountain + 4 Lightning Bolt (handled) + 4 Keen-Eyed Curator (the
# engine's own fixture pool example of a `silent` card - its "four or more
# card types" clause fires nowhere) = 60, legal.
SILENT_DECK = "52 Mountain\n4 Lightning Bolt\n4 Keen-Eyed Curator"

# Every one of the four support tiers in one legal sixty: Mountain (no rules
# text), Lightning Bolt (handled), Keen-Eyed Curator (silent), Final
# Showdown (notice: two of its three Spree modes fail closed), Alhammarret's
# Archive (unaudited: a static life-doubling line no support check audits).
MIXED_DECK = (
    "44 Mountain\n4 Lightning Bolt\n4 Keen-Eyed Curator\n4 Final Showdown\n4 Alhammarret's Archive"
)

# Only twelve cards: fails CR 100.2a's sixty-card minimum, same as
# `test_decks_api.py`'s `TOO_FEW_CARDS` - but with a silent card in it, to
# prove a warning survives an illegal deck too (legality and support are
# separate questions).
ILLEGAL_DECK_WITH_A_SILENT_CARD = "4 Mountain\n4 Lightning Bolt\n4 Keen-Eyed Curator"


@pytest.fixture(autouse=True)
def _clear_support_cache():
    """Every test starts from a cold `deck_support_warnings` cache, so one
    test's monkeypatched `card_support` can never answer for another's real
    call, and the caching test below can watch a cache actually fill."""
    decks.clear_deck_support_cache()
    yield
    decks.clear_deck_support_cache()


# ---------------------------------------------------------------------------
# The core computation - `decks.deck_support_warnings`
# ---------------------------------------------------------------------------


def test_a_fully_handled_deck_has_no_support_warnings():
    warnings = decks.deck_support_warnings(["Mountain", "Lightning Bolt"])
    assert warnings == []


def test_a_silent_card_is_reported_with_its_dead_clause():
    warnings = decks.deck_support_warnings(["Mountain", "Lightning Bolt", "Keen-Eyed Curator"])
    assert [w["card"] for w in warnings] == ["Keen-Eyed Curator"]
    (warning,) = warnings
    assert warning["status"] == "silent"
    assert warning["clauses"], "a silent card must name the clause that does nothing"
    assert any("four or more card types" in c["text"] for c in warning["clauses"])
    assert all(c["outcome"] and c["reason"] for c in warning["clauses"])
    # A handled clause is not a warning - only the ones the card is flagged for.
    assert all(c["outcome"] != "handled" for c in warning["clauses"])


def test_warnings_are_sorted_silent_first():
    names = [
        "Mountain",
        "Lightning Bolt",
        "Final Showdown",
        "Alhammarret's Archive",
        "Keen-Eyed Curator",
    ]
    warnings = decks.deck_support_warnings(names)
    statuses = [w["status"] for w in warnings]
    assert statuses == ["silent", "unaudited", "notice"]
    assert {w["card"] for w in warnings} == {
        "Keen-Eyed Curator",
        "Alhammarret's Archive",
        "Final Showdown",
    }


def test_a_duplicate_name_is_reported_once():
    warnings = decks.deck_support_warnings(
        ["Keen-Eyed Curator", "Keen-Eyed Curator", "Keen-Eyed Curator", "Keen-Eyed Curator"]
    )
    assert len(warnings) == 1


def test_caching_avoids_recomputing_the_same_card(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_card_support = decks.card_support

    def counting_card_support(card: Any) -> Any:
        calls.append(card.name)
        return real_card_support(card)

    monkeypatch.setattr(decks, "card_support", counting_card_support)

    decks.deck_support_warnings(["Keen-Eyed Curator", "Lightning Bolt"])
    assert calls == ["Keen-Eyed Curator", "Lightning Bolt"]

    # A second decklist naming the same two cards, plus one new one, must not
    # recompute either of the first two - `deck_support_warnings`'s own cache
    # is keyed by (name, engine version), independent of which decklist
    # asked. Only "Mountain" is new.
    decks.deck_support_warnings(["Lightning Bolt", "Keen-Eyed Curator", "Mountain"])
    assert calls == ["Keen-Eyed Curator", "Lightning Bolt", "Mountain"]


# ---------------------------------------------------------------------------
# `POST /api/decks/validate` - legality stays separate and unchanged
# ---------------------------------------------------------------------------


def test_validate_reports_support_warnings_for_a_legal_deck(client: TestClient) -> None:
    response = client.post("/api/decks/validate", json={"text": SILENT_DECK})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["errors"] == []
    warnings = body["support_warnings"]
    assert [w["card"] for w in warnings] == ["Keen-Eyed Curator"]
    assert warnings[0]["status"] == "silent"
    assert warnings[0]["clauses"]


def test_validate_reports_no_support_warnings_for_a_fully_handled_deck(client: TestClient) -> None:
    response = client.post("/api/decks/validate", json={"text": HANDLED_ONLY_DECK})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["support_warnings"] == []


def test_validate_support_warnings_never_block_or_change_legality(client: TestClient) -> None:
    """Same request, same `valid`/`errors`/`warnings`/sizes as before this
    task - `support_warnings` only adds a key, it changes nothing else."""
    response = client.post("/api/decks/validate", json={"text": SILENT_DECK})
    body = response.json()
    assert body["valid"] is True
    assert body["mainboard_size"] == 60
    assert body["sideboard_size"] == 0
    assert body["warnings"] == []  # the pre-existing Modern-legality warnings


def test_validate_reports_warnings_even_on_an_illegal_deck(client: TestClient) -> None:
    """Legality (`valid: False`) and support (`support_warnings`) are two
    separate questions about the same cards - an illegal deck still names
    the cards it is not fully supported on."""
    response = client.post("/api/decks/validate", json={"text": ILLEGAL_DECK_WITH_A_SILENT_CARD})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("at least 60" in e["message"] for e in body["errors"])
    assert [w["card"] for w in body["support_warnings"]] == ["Keen-Eyed Curator"]


def test_the_in_process_api_client_carries_support_warnings_through(client: TestClient) -> None:
    api_client = InProcessApiClient()
    direct = api_client.validate_decklist(SILENT_DECK)
    routed = client.post("/api/decks/validate", json={"text": SILENT_DECK}).json()
    assert direct == routed
    assert direct["support_warnings"]


# ---------------------------------------------------------------------------
# `POST /api/lobby/games` - the lobby's own submit
# ---------------------------------------------------------------------------


def test_start_game_returns_support_warnings_per_player(client: TestClient) -> None:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": SILENT_DECK, "ai": False},
                {"username": "bob", "decklist_text": HANDLED_ONLY_DECK, "ai": True},
            ],
            "seed": 7,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    try:
        assert [w["card"] for w in body["support_warnings"]["alice"]] == ["Keen-Eyed Curator"]
        assert body["support_warnings"]["bob"] == []
    finally:
        client.delete(f"/api/lobby/games/{body['game_id']}")


# ---------------------------------------------------------------------------
# `GET /api/lobby/games/{id}/seats` - a loaded/registered seat's own deck
# ---------------------------------------------------------------------------


def _booted(
    client: TestClient, alice_text: str = SILENT_DECK, bob_text: str = HANDLED_ONLY_DECK
) -> dict:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": alice_text, "ai": False},
                {"username": "bob", "decklist_text": bob_text, "ai": False},
            ],
            "seed": 7,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_game_seats_carries_support_warnings_for_the_callers_own_seat(client: TestClient) -> None:
    info = _booted(client)
    game_id = info["game_id"]
    try:
        seats = client.get(
            f"/api/lobby/games/{game_id}/seats", params={"uid": info["seat_uids"]["alice"]}
        ).json()["seats"]
        by_player = {s["player"]: s for s in seats}
        assert [w["card"] for w in by_player["alice"]["support_warnings"]] == ["Keen-Eyed Curator"]
        # Never the opponent's - the same "own block only" rule `decklist` follows.
        assert by_player["bob"].get("support_warnings") in (None, [])
    finally:
        client.delete(f"/api/lobby/games/{game_id}")


def test_game_seats_carries_no_support_warnings_with_no_credential(client: TestClient) -> None:
    info = _booted(client)
    game_id = info["game_id"]
    try:
        seats = client.get(f"/api/lobby/games/{game_id}/seats").json()["seats"]
        assert all(not seat.get("support_warnings") for seat in seats)
    finally:
        client.delete(f"/api/lobby/games/{game_id}")
