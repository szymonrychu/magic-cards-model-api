"""sim_card_lookup: card names checked through the Scryfall lookup, without a sandbox."""

from __future__ import annotations

from typing import Any

import pytest

from magic_cards_model_api import decks, scryfall
from magic_cards_model_api.sim import lookup
from magic_cards_model_api.sim.lookup import MAX_LOOKUP_NAMES, card_lookup
from magic_cards_model_api.sim.manager import SimRefused
from tests.test_sim_config import SILENT_CARD_NAME, SILENT_CARD_RAW


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path) -> list[dict[str, str]]:
    """Scryfall's network calls answer from a table: fuzzy "lightnin bolt" finds the Bolt
    fixture, everything else is not found. Exact names come from the bundled printings; the
    cache is a temporary directory, so a fuzzy hit is not written into the fixtures."""
    asked: list[dict[str, str]] = []
    bolt = scryfall.load_cached("Lightning Bolt")
    monkeypatch.setenv("MCM_CACHE_DIR", str(tmp_path))

    def fake_get(path: str, params: dict[str, str]) -> dict[str, Any]:
        asked.append(params)
        if params.get("fuzzy") == "lightnin bolt":
            return bolt
        raise scryfall.CardNotFound(f"no card matching {params!r}")

    monkeypatch.setattr(scryfall, "_get", fake_get)
    return asked


def test_a_card_comes_back_with_its_face_and_support(offline):
    (card,) = card_lookup(["Lightning Bolt"], budget_s=10)["cards"]
    assert card["query"] == "Lightning Bolt" and card["status"] == "found"
    assert card["name"] == "Lightning Bolt"
    assert card["mana_cost"] == "{R}"
    assert card["type_line"] == "Instant"
    assert "3 damage" in card["oracle_text"]
    assert card["engine_support"] == "handled"
    assert "unsupported_clauses" not in card
    assert offline == []  # served from the cache


def test_a_loose_spelling_resolves_through_fuzzy(offline):
    (card,) = card_lookup(["lightnin bolt"], budget_s=10)["cards"]
    assert (card["query"], card["name"]) == ("lightnin bolt", "Lightning Bolt")
    assert {"fuzzy": "lightnin bolt"} in offline


def test_a_fuzzy_match_is_not_reported_as_found(offline):
    (card,) = card_lookup(["lightnin bolt"], budget_s=10)["cards"]
    assert card["status"] == "fuzzy" and card["exact"] is False


def test_an_exact_match_ignores_case_and_punctuation():
    assert lookup._is_exact("lightning  BOLT!", "Lightning Bolt")
    assert not lookup._is_exact("Lightning Blot", "Lightning Bolt")


def test_the_front_face_of_a_double_faced_card_is_exact():
    assert lookup._is_exact("Delver of Secrets", "Delver of Secrets // Insectile Aberration")
    assert not lookup._is_exact("Insectile", "Delver of Secrets // Insectile Aberration")


def test_an_unknown_or_ambiguous_name_is_not_found(offline):
    (card,) = card_lookup(["Bolt"], budget_s=10)["cards"]
    assert card == {"query": "Bolt", "status": "not_found"}


def test_a_creature_shows_power_and_toughness(offline):
    (card,) = card_lookup(["Grizzly Bears"], budget_s=10)["cards"]
    assert card["power_toughness"] == "2/2"


def test_an_unsupported_card_is_flagged(monkeypatch: pytest.MonkeyPatch):
    real = scryfall.fetch_card
    monkeypatch.setattr(
        scryfall, "fetch_card", lambda n: SILENT_CARD_RAW if n == SILENT_CARD_NAME else real(n)
    )
    decks.clear_deck_support_cache()
    try:
        (card,) = card_lookup([SILENT_CARD_NAME], budget_s=10)["cards"]
    finally:
        decks.clear_deck_support_cache()
    assert card["engine_support"] == "silent"
    assert card["unsupported_clauses"]


def test_scryfall_down_is_reported_per_card(monkeypatch: pytest.MonkeyPatch):
    def down(path: str, params: dict[str, str]) -> dict[str, Any]:
        raise scryfall.ScryfallError("Scryfall request failed: boom")

    monkeypatch.setattr(scryfall, "_get", down)
    (card,) = card_lookup(["Not Cached Anywhere"], budget_s=10)["cards"]
    assert card["status"] == "error" and "Scryfall" in card["reason"]


def test_names_are_deduplicated_and_blank_ones_dropped(offline):
    cards = card_lookup(["Shock", " Shock ", "", "Murder"], budget_s=10)["cards"]
    assert [c["query"] for c in cards] == ["Shock", "Murder"]


def test_names_past_the_budget_are_not_checked(offline):
    cards = card_lookup(["Shock", "Murder"], budget_s=0)["cards"]
    assert [c["status"] for c in cards] == ["not_checked", "not_checked"]


@pytest.mark.parametrize("count", [0, MAX_LOOKUP_NAMES + 1])
def test_the_name_count_is_bounded(count):
    with pytest.raises(SimRefused, match="names"):
        card_lookup(["Shock"] * count, budget_s=10)
