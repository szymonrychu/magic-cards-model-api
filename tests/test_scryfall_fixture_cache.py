"""`tests/fixtures/scryfall` is the offline Scryfall cache, and it is keyed by
`scryfall.slug`, not by the engine pool's `fixture_slug`.

`conftest.py` points `MCM_CACHE_DIR` at that directory so the whole suite runs
without a network, and `scryfall.cache_path` looks a card up there as
`slug(name).json`. The engine's bundled pool (`data/printings`) uses a
*different* rule for the same card - `fixture_slug` gives every non-alphanumeric
character its own underscore, so "Shang-Chi, Master of Kung Fu" is
`shang_chi__master_of_kung_fu` there and `shang_chi_master_of_kung_fu` here.
Printings get copied from the pool into this directory (no network is
permitted), and copying the pool's filename along with the file is the easy
mistake: the file is present, looks right in `ls`, and is a permanent cache
miss. When the engine pool happens to hold the same card the miss is invisible,
because `fetch_card` tries the pool first - so this pins the cache entry
directly rather than through `fetch_card`, which would pass either way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from magic_cards_model_api import scryfall

FIXTURES = Path(__file__).parent / "fixtures" / "scryfall"


def _fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.glob("*.json") if not p.name.startswith("_"))


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.stem)
def test_every_fixture_printing_is_reachable_as_a_cache_entry(path: Path) -> None:
    """Its filename must be `slug()` of a name a caller would really ask for -
    the whole name, or (for a multi-faced card, which decklists spell by its
    front face) that front face alone."""
    printing = json.loads(path.read_text())
    name = printing["name"]
    front = name.split("//")[0].strip()
    accepted = {scryfall.slug(name), scryfall.slug(front)}
    assert path.stem in accepted, (
        f"{path.name} holds {name!r}, which this cache looks up as "
        f"{sorted(accepted)} - the file is a permanent cache miss under its "
        "current name (the engine pool's `fixture_slug` spelling, most likely)"
    )
    cached = scryfall.load_cached(front)
    assert cached is not None and cached["name"] == name


@pytest.mark.parametrize(
    "name",
    [
        "Eldrazi Temple",
        "Cavern of Souls",
        "Arena of Glory",
        "Thought-Knot Seer",
        "Delighted Halfling",
        "Tablet of Discovery",
        "Sunken Citadel",
        "Shang-Chi, Master of Kung Fu",
        "Interdimensional Web Watch",
    ],
)
def test_the_restricted_mana_cards_resolve_offline(name: str) -> None:
    """The printings the restricted-mana work is exercised against, pinned by
    name so a rename or a lost file is a red test rather than a suite that
    quietly reaches for the network."""
    assert scryfall.load_cached(name) is not None, (
        f"{name!r} is not in the offline cache: copy its printing out of the "
        f"engine's data/printings into {FIXTURES} as {scryfall.slug(name)}.json"
    )
