import json

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
