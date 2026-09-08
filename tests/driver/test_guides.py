import json
from pathlib import Path

import pytest

from mtg_agent.driver.guides import (
    MATCH_THRESHOLD,
    GuideLibrary,
    parse_decklist,
)

DIMIR = """4 Psychic Frog
4 Fatal Push
2 Consign to Memory
4 Flooded Strand
6 Island

Sideboard
2 Nihil Spellbomb
"""

TRON = """4 Urza's Tower
4 Urza's Mine
4 Urza's Power Plant
4 Karn, the Great Creator
1 Wurmcoil Engine
"""


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "guides"
    frog = root / "decks" / "dimir-frog"
    matchups = frog / "analysis" / "run-1" / "domain3-matchup-threat" / "matchups"
    matchups.mkdir(parents=True)
    (frog / "decklist.txt").write_text(DIMIR)
    (frog / "GAMEPLAN_GUIDE.md").write_text("# dimir-frog - gameplan guide\n")
    (frog / "SIDEBOARD_GUIDE.md").write_text("# dimir-frog - sideboard guide\n")
    (frog / "META_CALLS.md").write_text("# meta calls\n")
    (matchups / "boros-energy.json").write_text(json.dumps({"name": "boros energy"}))
    (matchups / "living-end.json").write_text(json.dumps({"name": "living end"}))

    tron = root / "decks" / "prison-tron"
    tron.mkdir(parents=True)
    (tron / "decklist.txt").write_text(TRON)
    (tron / "GAMEPLAN_GUIDE.md").write_text("# prison-tron - gameplan guide\n")
    return GuideLibrary(root)


def test_parse_decklist_counts_cards():
    assert parse_decklist("4 Fatal Push\n2 Consign to Memory\n") == {
        "Fatal Push": 4,
        "Consign to Memory": 2,
    }


def test_parse_decklist_accepts_the_x_suffix_and_blank_lines():
    assert parse_decklist("4x Fatal Push\n\n  2 Island  \n") == {"Fatal Push": 4, "Island": 2}


def test_parse_decklist_folds_the_sideboard_into_one_mapping():
    parsed = parse_decklist(DIMIR)
    assert parsed["Nihil Spellbomb"] == 2
    assert parsed["Psychic Frog"] == 4
    assert "Sideboard" not in parsed


def test_parse_decklist_skips_comments_and_set_codes():
    assert parse_decklist("# a comment\n// another\n4 Fatal Push (MH3) 12\n") == {"Fatal Push": 4}


def test_parse_decklist_sums_a_card_listed_twice():
    assert parse_decklist("2 Island\n2 Island\n") == {"Island": 4}


def test_decks_lists_every_guided_deck(library):
    assert sorted(d.slug for d in library.decks()) == ["dimir-frog", "prison-tron"]


def test_a_deck_guide_carries_every_guide_file_it_has(library):
    guide = library.get("dimir-frog")
    assert guide.gameplan.name == "GAMEPLAN_GUIDE.md"
    assert guide.sideboard.name == "SIDEBOARD_GUIDE.md"
    assert guide.meta_calls.name == "META_CALLS.md"
    assert guide.archetypes == ["boros-energy", "living-end"]


def test_matchup_path_resolves_a_known_archetype_and_not_an_unknown_one(library):
    guide = library.get("dimir-frog")
    assert guide.matchup_path("boros-energy").name == "boros-energy.json"
    assert guide.matchup_path("nope") is None
    assert library.get("prison-tron").matchup_path("boros-energy") is None


def test_missing_guide_files_are_none_not_an_error(library):
    guide = library.get("prison-tron")
    assert guide.gameplan is not None
    assert guide.sideboard is None
    assert guide.matchups_dir is None
    assert guide.archetypes == []


def test_get_of_an_unknown_slug_is_none(library):
    assert library.get("nope") is None


def test_identify_matches_the_deck_it_was_built_from(library):
    guide, score = library.identify(parse_decklist(DIMIR))
    assert guide.slug == "dimir-frog"
    assert score == pytest.approx(1.0)


def test_identify_tolerates_a_few_flex_slots(library):
    played = parse_decklist(DIMIR)
    played["Fatal Push"] = 3
    played["Thoughtseize"] = 1
    guide, score = library.identify(played)
    assert guide.slug == "dimir-frog"
    assert score >= MATCH_THRESHOLD


def test_identify_returns_none_for_a_deck_with_no_guide(library):
    guide, score = library.identify({"Lightning Bolt": 4, "Mountain": 20})
    assert guide is None
    assert score < MATCH_THRESHOLD


def test_identify_picks_the_better_of_two_candidates(library):
    guide, _ = library.identify(parse_decklist(TRON))
    assert guide.slug == "prison-tron"


def test_a_missing_guides_root_yields_no_decks(tmp_path):
    assert GuideLibrary(tmp_path / "absent").decks() == []


# ---- regression: a real bundled decklist against the real guides repo -----
#
# A cluster run reported the driver matching two AI decklists to
# `unknown-deck` (score 0.09 for `izzet_prowess`, 0.11 for `amulet_titan`),
# read as "the matcher matches every bundled deck to unknown-deck". That is
# not what is happening: the guides repo (github.com/szymonrychu/mtg-decks,
# cloned to `/guides` in every seat Job) has guides for exactly four
# archetypes - `dimir-frog`, `living-end`, `orzhov-martyr`, `prison-tron` -
# and `izzet_prowess`/`amulet_titan` are not among them at all. Scoring low
# against guides for entirely different archetypes is the *correct* answer,
# not a matcher defect.
#
# `GUIDE_DECKLISTS` below is each of those four guides' real
# `decks/<slug>/decklist.txt` content, copied verbatim from the guides repo
# at commit `04bd547` (fetched fresh from
# https://github.com/szymonrychu/mtg-decks.git while investigating this) -
# not hand-typed, so the fixture is what the matcher actually sees in
# production, not an approximation of it.

GUIDE_DECKLISTS = {
    "dimir-frog": """\
2 Consign to Memory
3 Counterspell
2 Drown in the Loch
4 Fatal Push
4 Flooded Strand
3 Force of Negation
1 Gloomlake Verge
2 Island
1 Kaito, Bane of Nightmares
2 Marsh Flats
4 Orcish Bowmasters
1 Otawara, Soaring City
4 Polluted Delta
4 Psychic Frog
3 Quantum Riddler
1 Sheoldred's Edict
2 Sink into Stupor
1 Swamp
2 Spell Snare
3 Subtlety
2 Tamiyo, Inquisitive Student
4 Thoughtseize
2 Undercity Sewers
3 Watery Grave

Sideboard
2 Consign to Memory
1 Dress Down
2 Engineered Explosives
3 Harbinger of the Seas
3 Mystical Dispute
2 Nihil Spellbomb
2 Toxic Deluge
""",
    "living-end": """\
4 Violent Outburst
3 Living End
4 Force of Negation
4 Wistfulness
4 Subtlety
4 Endurance
4 Generous Ent
4 Shardless Agent
4 Street Wraith
2 Oliphaunt
2 Curator of Mysteries
2 Colossal Skyturtle
2 Sink into Stupor
1 Waker of Waves
1 Breeding Pool
1 Commercial District
1 Forest
1 Hedge Maze
1 Island
1 Otawara, Soaring City
3 Misty Rainforest
1 Mountain
2 Scalding Tarn
1 Steam Vents
1 Stomping Ground
1 Thundering Falls
1 Boseiju, Who Endures

Sideboard
2 Anger of the Gods
2 Damping Matrix
3 Dismember
3 Force of Vigor
3 Mystical Dispute
2 Tormod's Crypt
""",
    "orzhov-martyr": """\
4 Serra Ascendant
4 Martyr of Sands
1 Kami of False Hope
1 Thraben Inspector
4 Phelia, Exuberant Shepherd
3 Flickerwisp
2 Enduring Innocence
2 Skyclave Apparition
4 Solitude
4 Starfield Shepherd
4 Overlord of the Balemurk
3 Ephemerate
2 Abiding Grace
2 Emeria, the Sky Ruin
3 Shadowy Backstreet
2 Godless Shrine
4 Marsh Flats
9 Plains
1 Mistveil Plains
1 Flooded Strand

Sideboard
3 White Orchid Phantom
3 Thraben Charm
2 High Noon
3 Wrath of the Skies
2 Sanctifier en-Vec
2 Orim's Chant
""",
    "prison-tron": """\
4 Devourer of Destiny
3 Glaring Fleshraker
3 Thought-Knot Seer
1 Emrakul, the Promised End
4 Karn, the Great Creator
3 Ugin, Eye of the Storms
4 Kozilek's Command
4 Expedition Map
4 Mind Stone
3 Chalice of the Void
2 Dismember
1 All Is Dust
1 Trinisphere
1 Vexing Bauble
4 Eldrazi Temple
4 Ugin's Labyrinth
4 Urza's Mine
4 Urza's Power Plant
4 Urza's Tower
1 Swamp
1 Wastes

Sideboard
2 Disruptor Flute
2 Grafdigger's Cage
2 Torpor Orb
1 Ensnaring Bridge
1 Wurmcoil Engine
1 Trinisphere
1 Walking Ballista
1 Tormod's Crypt
1 The Stone Brain
1 The Filigree Sylex
1 Extinguisher Battleship
1 Cityscape Leveler
""",
}

#: This repo's own bundled decklists (`decks/*.txt`, real Scryfall-backed
#: lists the lobby offers in its deck picker), three parents up from this
#: test file - the same `DECKS_DIR` `api_decks.py` reads.
BUNDLED_DECKS_DIR = Path(__file__).resolve().parents[2] / "decks"


@pytest.fixture
def real_guide_library(tmp_path):
    root = tmp_path / "guides"
    for slug, text in GUIDE_DECKLISTS.items():
        deck_dir = root / "decks" / slug
        deck_dir.mkdir(parents=True)
        (deck_dir / "decklist.txt").write_text(text)
    return GuideLibrary(root)


def test_identify_matches_a_real_bundled_decklist_to_its_real_guide(real_guide_library):
    """`decks/dimir_frog.txt` is a real, independently-sourced decklist (a
    different pilot, a different tournament) for the same archetype as the
    `dimir-frog` guide - not identical to it, which is the point: this is
    the "a real list swaps a few flex slots" case the module docstring
    describes, not a round-trip of the same text."""
    text = (BUNDLED_DECKS_DIR / "dimir_frog.txt").read_text()
    played = parse_decklist(text)
    guide, score = real_guide_library.identify(played)
    assert guide is not None
    assert guide.slug == "dimir-frog"
    assert score >= MATCH_THRESHOLD


def test_identify_correctly_finds_no_guide_for_an_archetype_the_guides_repo_lacks(
    real_guide_library,
):
    """`izzet_prowess` is a real bundled decklist, but the guides repo
    (github.com/szymonrychu/mtg-decks) has no guide for it at all - only the
    four archetypes in `GUIDE_DECKLISTS` above. Scoring below
    `MATCH_THRESHOLD` here is the correct answer, not the matcher failing:
    there is nothing for it to match. This is a regression test for that
    distinction, not a bug fix - see the module comment above."""
    text = (BUNDLED_DECKS_DIR / "izzet_prowess.txt").read_text()
    played = parse_decklist(text)
    guide, score = real_guide_library.identify(played)
    assert guide is None
    assert score < MATCH_THRESHOLD
