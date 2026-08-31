"""The engine's scenario-test card pool as a read-only first-tier card source.

Why this file exists: with only `cache_dir()` in front of Scryfall, validating
a decklist nobody had loaded before cost one HTTP round trip per distinct card
- 35 seconds for `decks/grixis_reanimator.txt` against an empty cache, which
in the browser is a "validating" spinner that never seems to finish. The pool
next door already holds those printings, so what these tests pin is that it is
actually being read: a cold cache that stays *empty* is the whole proof.

No mocks anywhere: the real `tests/fixtures/cards/*.json` from the engine
checkout, the real `decks/*.txt`, and a real (empty) cache directory.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from magic_cards_model.models import Card

from magic_cards_model_api import scryfall
from magic_cards_model_api.decks import parse_decklist
from magic_cards_model_api.decks_api import validate_decklist_text

DECKS_DIR = Path(__file__).resolve().parents[1] / "decks"

#: The three decks a first-run user could not start: newest cards, so nothing
#: in the shipped cache, so every name went to the network.
SLOW_DECKS = ["grixis_reanimator", "dimir_frog", "esper_goryo"]


@pytest.fixture()
def pool() -> Path:
    directory = scryfall.fixture_pool_dir()
    if directory is None:
        pytest.skip("engine checkout with tests/fixtures/cards is not present")
    return directory


@pytest.fixture()
def cold_cache(tmp_path, monkeypatch) -> Path:
    """An empty cache directory, in place of the one `conftest` shares."""
    cache = tmp_path / "cold-cache"
    cache.mkdir()
    monkeypatch.setenv("MCM_CACHE_DIR", str(cache))
    return cache


def test_fixture_slug_follows_the_engines_own_rule():
    """The pool's filenames come from the engine's `scripts/add_card.py`
    `slugify`, which is not this module's `slug`: it keeps only the front
    face, gives every non-alphanumeric character its own underscore, and
    counts an accented letter as alphanumeric. Get any of the three wrong and
    every lookup misses silently, which is exactly how the six "missing"
    cards in this pool were first miscounted."""
    assert scryfall.fixture_slug("Otawara, Soaring City") == "otawara__soaring_city"
    assert scryfall.fixture_slug("Lórien Revealed") == "lórien_revealed"
    assert scryfall.fixture_slug("Wear // Tear") == "wear"
    assert scryfall.fixture_slug("Wear/Tear") == "wear"
    assert (
        scryfall.fixture_slug("Tamiyo, Inquisitive Student // Tamiyo, Seasoned Scholar")
        == "tamiyo__inquisitive_student"
    )
    # `slug` stays what it was - the cache's filenames must not move.
    assert scryfall.slug("Otawara, Soaring City") == "otawara_soaring_city"


def test_pool_serves_a_whole_scryfall_printing(pool, cold_cache):
    """A pooled card must answer everything the network answer does: the
    engine `Card`, the image links `serialize.py` reads, and the Modern
    legality `decks_api` reports as a warning."""
    raw = scryfall.fetch_card("Otawara, Soaring City")

    assert raw["name"] == "Otawara, Soaring City"
    assert Card.model_validate(raw).name == "Otawara, Soaring City"
    assert scryfall.image_uris(raw)["normal"].startswith("https://")
    assert scryfall.modern_status(raw) == "legal"
    assert list(cold_cache.iterdir()) == [], "a pooled card must not be written to the cache"


def test_pool_is_never_written_to(pool, cold_cache):
    """Read-only means read-only: the engine repo is a different repo with
    its own agents in it, and a fetch here must not change what its scenario
    tests see."""
    before = {path.name: path.stat().st_mtime_ns for path in pool.glob("*.json")}
    for name in ["Murktide Regent", "Psychic Frog", "Counterspell"]:
        scryfall.fetch_card(name)
    after = {path.name: path.stat().st_mtime_ns for path in pool.glob("*.json")}
    assert after == before


def test_double_faced_and_split_names_both_resolve(pool):
    """The pool files a multi-face card under its front face alone. Requests
    arrive in all three shapes - Scryfall's "A // B", MTGO's "A/B", and the
    front face on its own - and all three must find it."""
    full = "Tamiyo, Inquisitive Student // Tamiyo, Seasoned Scholar"
    for requested in [full, "Tamiyo, Inquisitive Student"]:
        assert (scryfall.load_pooled(requested) or {}).get("name") == full
    for requested in ["Wear // Tear", "Wear/Tear", "Wear"]:
        assert (scryfall.load_pooled(requested) or {}).get("name") == "Wear // Tear"


def test_a_pool_file_holding_a_different_card_is_refused(pool, tmp_path, monkeypatch):
    """The front-face filing rule means a slug can collide with an unrelated
    card's name. The reader compares names before believing the file, so a
    collision falls through to Scryfall instead of quietly serving the wrong
    printing. Staged with a real printing under a real slug - the card data is
    the engine's own `wear.json`, only the filename is chosen."""
    fake_pool = tmp_path / "pool"
    fake_pool.mkdir()
    (fake_pool / "lightning_bolt.json").write_text((pool / "wear.json").read_text())
    monkeypatch.setenv("MCM_FIXTURE_CARDS_DIR", str(fake_pool))

    assert scryfall.load_pooled("Lightning Bolt") is None
    # And the card that *is* in there is not found either, because it is not
    # filed under its own slug - a miss, never a wrong answer.
    assert scryfall.load_pooled("Wear // Tear") is None


def test_missing_or_disabled_pool_degrades_quietly(tmp_path, monkeypatch):
    """The pool is an optimisation, not a dependency: a deployment without
    the engine checkout beside it must still work, just slowly."""
    monkeypatch.setenv("MCM_FIXTURE_CARDS_DIR", str(tmp_path / "nowhere"))
    assert scryfall.fixture_pool_dir() is None
    assert scryfall.load_pooled("Otawara, Soaring City") is None

    monkeypatch.setenv("MCM_FIXTURE_CARDS_DIR", "")
    assert scryfall.fixture_pool_dir() is None
    assert scryfall.load_pooled("Otawara, Soaring City") is None


def test_a_bad_pool_file_is_ignored_rather_than_raising(pool, tmp_path, monkeypatch):
    fake_pool = tmp_path / "pool"
    fake_pool.mkdir()
    (fake_pool / "lightning_bolt.json").write_text("{ not json")
    (fake_pool / "counterspell.json").write_text(json.dumps([1, 2, 3]))
    monkeypatch.setenv("MCM_FIXTURE_CARDS_DIR", str(fake_pool))

    assert scryfall.load_pooled("Lightning Bolt") is None
    assert scryfall.load_pooled("Counterspell") is None


@pytest.mark.parametrize("deck", SLOW_DECKS)
def test_the_slow_decks_validate_off_the_pool_alone(deck, pool, cold_cache):
    """The regression this whole change exists for. Every distinct card in
    these three lists is in the pool, so a cold cache must end the validation
    still empty - an empty cache directory is a proof that not one network
    request was made. If a deck later gains a card the pool lacks, this fails
    loudly here rather than as a 35-second spinner in someone's browser."""
    text = (DECKS_DIR / f"{deck}.txt").read_text()

    start = time.monotonic()
    result = validate_decklist_text(text)
    elapsed = time.monotonic() - start

    assert result["valid"], result["errors"]
    assert result["mainboard_size"] == 60
    assert result["sideboard_size"] == 15
    assert list(cold_cache.iterdir()) == [], "these decks must resolve entirely from the pool"
    assert elapsed < 5, f"{deck} took {elapsed:.1f}s off a pool that should be disk-speed"


def test_every_bundled_deck_is_almost_entirely_pooled(pool):
    """A sweep of `decks/*.txt`, so a deck added later that would be slow is
    visible. `Fire Magic` is the one printing the pool does not have; it is
    listed rather than tolerated silently."""
    unpooled = {
        name
        for path in sorted(DECKS_DIR.glob("*.txt"))
        for count_and_name in [parse_decklist(path.read_text())]
        for name in {n for _, n in [*count_and_name[0], *count_and_name[1]]}
        if scryfall.load_pooled(name) is None
    }
    assert unpooled == {"Fire Magic"}
