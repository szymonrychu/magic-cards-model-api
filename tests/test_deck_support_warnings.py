"""`support_warnings` (flow 6, W2): the engine's `card_support` verdict,
surfaced at every place a decklist is submitted, validated, or loaded.

Card data is the recorded fixture cache (`tests/fixtures/scryfall`, tier 1)
plus the engine's own bundled printing corpus (tier 2,
`magic_cards_model/data/printings`), so every real card here resolves
offline - `Mountain` and `Lightning Bolt` are real, fully `handled`
printings, and `Final Showdown`/`Alhammarret's Archive` are the engine's own
fixture pool example of a `notice` and an `unaudited` card
(`magic-cards-model/tests/test_card_support.py`). Both repos read the exact
same `card_support(card)` - this is the "at least one real-engine test" the
task asks for, run four times over, not stubbed once.

The `silent` example is deliberately **not** a real card. It used to be
`Keen-Eyed Curator` - the engine's own fixture pool example of a `silent`
card - right up until the engine's silent-card campaign taught
`card_support` to read the card's linked-exile threshold static (S09), and
every one of this file's 8 tests broke on the same day: nothing here
changed, the engine simply got better. Any real card will eventually stop
being silent (the whole point of the campaign is zero left), so this file's
own example cannot be one. `SILENT_CARD_RAW` below is a `Card` built for
this test alone, with a made-up sentence
(`"Whenever this creature glimmers, frobnicate target zorblax."`) no parser
in `magic_cards_model` recognises - guarded explicitly by
`test_the_test_fixture_card_is_actually_silent` below, so a future reader
that starts parsing gibberish fails loudly instead of quietly invalidating
every test that relies on it. It is injected through the exact seam a real
decklist name resolves through - `scryfall.fetch_card` - the same seam
`_resolve_card` (`magic_cards_model_api.decks`) and `_resolve_all`
(`magic_cards_model_api.api_decks`) both call to build the deck pool;
`card_support` itself is never mocked, for this card or any other.

Legality (`valid`/`errors`) is asserted to stay exactly as it was before this
task, on the same requests these warnings ride along with - `support_warnings`
is additive, never a gate.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from magic_cards_model.models import Card
from magic_cards_model.support import card_support

from magic_cards_model_api import decks, scryfall
from magic_cards_model_api.api_client import InProcessApiClient

#: A `Card` built for this test alone - see the module docstring for why a
#: real card cannot stand in this role. Legal in all three tracked formats
#: so it never trips the *separate* Modern-legality `warnings` list
#: `validate_decklist_text` computes alongside `support_warnings` - that
#: list is asserted to stay empty by `test_validate_support_warnings_never_
#: block_or_change_legality` below and must not gain an unrelated entry.
SILENT_CARD_NAME = "Inscrutable Husk"

SILENT_CARD_RAW: dict[str, Any] = {
    "id": "ffffffff-0000-0000-0000-000000000001",
    "oracle_id": "ffffffff-0000-0000-0000-000000000002",
    "name": SILENT_CARD_NAME,
    "type_line": "Creature — Test Fixture",
    "mana_cost": "{2}{G}",
    "cmc": 3.0,
    "oracle_text": "Whenever this creature glimmers, frobnicate target zorblax.",
    "colors": ["G"],
    "color_identity": ["G"],
    "keywords": [],
    "power": "2",
    "toughness": "2",
    "layout": "normal",
    "legalities": {"standard": "legal", "pioneer": "legal", "modern": "legal"},
    "rarity": "common",
    "set": "tst",
}


@pytest.fixture(autouse=True)
def _inject_silent_test_fixture_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """Makes `SILENT_CARD_NAME` resolve to `SILENT_CARD_RAW` through the
    same `scryfall.fetch_card` seam a real decklist name resolves through -
    every other name in this file (Mountain, Lightning Bolt, Final Showdown,
    Alhammarret's Archive) still reaches the real tier-1/tier-2/network
    lookup unchanged. `card_support` is never patched: this only stands the
    synthetic card in for a Scryfall answer, so it is classified by the
    real, live `card_support`, exactly like every real card in this file is.
    """
    real_fetch_card = scryfall.fetch_card

    def fetch_card_or_synthetic_fixture(name: str) -> dict[str, Any]:
        if name == SILENT_CARD_NAME:
            return SILENT_CARD_RAW
        return real_fetch_card(name)

    monkeypatch.setattr(scryfall, "fetch_card", fetch_card_or_synthetic_fixture)


# 52 Mountain (basic, unlimited under CR 100.2a) + 4 Lightning Bolt (a real,
# fully `handled` printing) = 60, legal.
HANDLED_ONLY_DECK = "56 Mountain\n4 Lightning Bolt"

# 52 Mountain + 4 Lightning Bolt (handled) + 4 Inscrutable Husk (this file's
# own synthetic `silent` fixture card - its made-up trigger condition fires
# nowhere) = 60, legal.
SILENT_DECK = f"52 Mountain\n4 Lightning Bolt\n4 {SILENT_CARD_NAME}"

# Every one of the four support tiers in one legal sixty: Mountain (no rules
# text), Lightning Bolt (handled), Inscrutable Husk (silent, synthetic),
# Final Showdown (notice: two of its three Spree modes fail closed), and
# Alhammarret's Archive (unaudited: a static life-doubling line no support
# check audits).
MIXED_DECK = (
    f"44 Mountain\n4 Lightning Bolt\n4 {SILENT_CARD_NAME}\n4 Final Showdown\n"
    "4 Alhammarret's Archive"
)

# Only twelve cards: fails CR 100.2a's sixty-card minimum, same as
# `test_decks_api.py`'s `TOO_FEW_CARDS` - but with the silent fixture card in
# it, to prove a warning survives an illegal deck too (legality and support
# are separate questions).
ILLEGAL_DECK_WITH_A_SILENT_CARD = f"4 Mountain\n4 Lightning Bolt\n4 {SILENT_CARD_NAME}"


@pytest.fixture(autouse=True)
def _clear_support_cache():
    """Every test starts from a cold `deck_support_warnings` cache, so one
    test's monkeypatched `card_support` can never answer for another's real
    call, and the caching test below can watch a cache actually fill."""
    decks.clear_deck_support_cache()
    yield
    decks.clear_deck_support_cache()


def test_the_test_fixture_card_is_actually_silent() -> None:
    """This file's whole premise, asserted explicitly: `SILENT_CARD_RAW`'s
    made-up oracle text must never be read by any real dispatcher in
    `magic_cards_model`, or every test below that treats it as *the*
    unsupported card would be proving nothing.

    This is exactly the failure this file is a fix for - `Keen-Eyed
    Curator` played this same role until the engine's silent-card campaign
    (S09) taught `card_support` to read its linked-exile threshold static,
    and all 8 tests in this file passed for the wrong reason right up until
    the day they all failed at once. If this assertion ever fails, the fix
    is a new made-up sentence for `SILENT_CARD_RAW["oracle_text"]` that no
    parser recognises - never reaching for another real card.
    """
    support = card_support(Card.model_validate(SILENT_CARD_RAW))
    assert support.status == "silent", (
        f"the test fixture card {SILENT_CARD_NAME!r} is no longer silent - "
        f"the engine now classifies it {support.status!r}. Its made-up "
        "oracle text must be replaced with a new sentence no parser in "
        "magic_cards_model recognises; do not swap in a real card."
    )
    assert support.clauses and support.clauses[0].status == "silent"


# ---------------------------------------------------------------------------
# The core computation - `decks.deck_support_warnings`
# ---------------------------------------------------------------------------


def test_a_fully_handled_deck_has_no_support_warnings():
    warnings = decks.deck_support_warnings(["Mountain", "Lightning Bolt"])
    assert warnings == []


def test_a_silent_card_is_reported_with_its_dead_clause():
    warnings = decks.deck_support_warnings(["Mountain", "Lightning Bolt", SILENT_CARD_NAME])
    assert [w["card"] for w in warnings] == [SILENT_CARD_NAME]
    (warning,) = warnings
    assert warning["status"] == "silent"
    assert warning["clauses"], "a silent card must name the clause that does nothing"
    assert any("glimmers" in c["text"] for c in warning["clauses"])
    assert all(c["outcome"] and c["reason"] for c in warning["clauses"])
    # A handled clause is not a warning - only the ones the card is flagged for.
    assert all(c["outcome"] != "handled" for c in warning["clauses"])


def test_warnings_are_sorted_silent_first():
    names = [
        "Mountain",
        "Lightning Bolt",
        "Final Showdown",
        "Alhammarret's Archive",
        SILENT_CARD_NAME,
    ]
    warnings = decks.deck_support_warnings(names)
    statuses = [w["status"] for w in warnings]
    assert statuses == ["silent", "unaudited", "notice"]
    assert {w["card"] for w in warnings} == {
        SILENT_CARD_NAME,
        "Alhammarret's Archive",
        "Final Showdown",
    }


def test_a_duplicate_name_is_reported_once():
    warnings = decks.deck_support_warnings(
        [SILENT_CARD_NAME, SILENT_CARD_NAME, SILENT_CARD_NAME, SILENT_CARD_NAME]
    )
    assert len(warnings) == 1


def test_caching_avoids_recomputing_the_same_card(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_card_support = decks.card_support

    def counting_card_support(card: Any) -> Any:
        calls.append(card.name)
        return real_card_support(card)

    monkeypatch.setattr(decks, "card_support", counting_card_support)

    decks.deck_support_warnings([SILENT_CARD_NAME, "Lightning Bolt"])
    assert calls == [SILENT_CARD_NAME, "Lightning Bolt"]

    # A second decklist naming the same two cards, plus one new one, must not
    # recompute either of the first two - `deck_support_warnings`'s own cache
    # is keyed by (name, engine version), independent of which decklist
    # asked. Only "Mountain" is new.
    decks.deck_support_warnings(["Lightning Bolt", SILENT_CARD_NAME, "Mountain"])
    assert calls == [SILENT_CARD_NAME, "Lightning Bolt", "Mountain"]


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
    assert [w["card"] for w in warnings] == [SILENT_CARD_NAME]
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
    assert [w["card"] for w in body["support_warnings"]] == [SILENT_CARD_NAME]


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
        assert [w["card"] for w in body["support_warnings"]["alice"]] == [SILENT_CARD_NAME]
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
        assert [w["card"] for w in by_player["alice"]["support_warnings"]] == [SILENT_CARD_NAME]
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
