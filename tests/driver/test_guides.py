"""The archetype-signature classifier, against the real guides corpus.

The classifier this exercises replaced whole-list similarity scoring. The two
bugs similarity had, and that these tests exist to keep closed:

1. A pasted list was compared against one sample 75 per archetype, so a real
   list of the same deck by a different pilot scored somewhere in the 0.5-0.7
   band and fell on whichever side of a threshold its flex slots landed. Two
   code paths - a bundled list and a pasted one - identified the same 60 cards
   differently.
2. The sideboard was folded into the maindeck before scoring, so fifteen cards
   nobody has cast yet could carry an archetype claim.

`tests/driver/fixtures/mtg-decks/<slug>/` holds a verbatim copy of every
`archetype.json` and `decklist.txt` in the guides corpus
(github.com/szymonrychu/mtg-decks) at the commit named by `MTG_DECKS_SHA`
below - thirteen of each, not a hand-written approximation. The pin is what
makes cross-repo drift a test failure here rather than a surprise in a live
game: when someone edits a signature over there, the table test below is what
notices.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mtg_agent.driver import guides as guides_module
from mtg_agent.driver.guides import (
    CONFIDENT,
    LOW,
    NONE,
    GuideLibrary,
    held_faces,
    holds,
    parse_decklist,
)

#: github.com/szymonrychu/mtg-decks, the commit the fixtures under
#: `fixtures/mtg-decks/` were copied from. Grep for it there to see exactly
#: which signatures this suite is pinned to.
MTG_DECKS_SHA = "9aff048c5e7c3d6a15f8c99593a1dcb4156f4e89"

#: The vendored corpus: `<slug>/archetype.json` + `<slug>/decklist.txt`.
CORPUS = Path(__file__).resolve().parent / "fixtures" / "mtg-decks"

#: This repo's own bundled decklists (`decks/*.txt`, real Scryfall-backed
#: lists the lobby offers in its deck picker) - the same `DECKS_DIR`
#: `api_decks.py` reads.
BUNDLED_DECKS_DIR = Path(__file__).resolve().parents[2] / "decks"

#: Every slug in the vendored corpus, and the `archetype` name each file
#: declares. The names are NOT the slugs - `prison-tron` calls itself
#: "urzatron" and `orzhov-martyr` calls itself "orzhov blink" - and a
#: classifier that conflates the two reports the wrong deck to the agent.
CORPUS_SLUGS = (
    "affinity",
    "bloodchief-combo",
    "boros-aggro",
    "devoted-combo",
    "dimir-frog",
    "eldrazi-ramp",
    "eldrazi-tron",
    "esper-blink",
    "instant-reanimator",
    "living-end",
    "orzhov-martyr",
    "prison-tron",
    "prowess",
)


def _corpus_library(tmp_path: Path, slugs=CORPUS_SLUGS) -> GuideLibrary:
    """A `GuideLibrary` over the vendored corpus, laid out the way the real
    `/guides` clone is: `<root>/decks/<slug>/{archetype.json,decklist.txt}`."""
    root = tmp_path / "guides"
    for slug in slugs:
        deck = root / "decks" / slug
        deck.mkdir(parents=True)
        (deck / "archetype.json").write_text((CORPUS / slug / "archetype.json").read_text())
        (deck / "decklist.txt").write_text((CORPUS / slug / "decklist.txt").read_text())
        (deck / "GAMEPLAN_GUIDE.md").write_text(f"# {slug} - gameplan\n")
    return GuideLibrary(root)


@pytest.fixture
def corpus(tmp_path) -> GuideLibrary:
    return _corpus_library(tmp_path)


def identify_text(library: GuideLibrary, text: str):
    return library.identify(parse_decklist(text))


# ---- the fixtures are the specification, so check they are all here -------


def test_all_thirteen_corpus_archetypes_are_vendored():
    assert sorted(p.name for p in CORPUS.iterdir() if p.is_dir()) == sorted(CORPUS_SLUGS)
    assert len(CORPUS_SLUGS) == 13
    for slug in CORPUS_SLUGS:
        spec = json.loads((CORPUS / slug / "archetype.json").read_text())
        assert spec["signature"], f"{slug} has no signature clauses"
        assert (CORPUS / slug / "decklist.txt").read_text().strip()


# ---- parse_decklist: the mainboard/sideboard split ------------------------


def test_parse_decklist_counts_cards():
    parsed = parse_decklist("4 Fatal Push\n2 Consign to Memory\n")
    assert parsed.mainboard == {"Fatal Push": 4, "Consign to Memory": 2}
    assert parsed.sideboard == {}


def test_parse_decklist_accepts_the_x_suffix():
    assert parse_decklist("4x Fatal Push\n  2 Island  \n").mainboard == {
        "Fatal Push": 4,
        "Island": 2,
    }


def test_parse_decklist_skips_comments_and_set_codes():
    parsed = parse_decklist("# a comment\n// another\n4 Fatal Push (MH3) 12\n")
    assert parsed.mainboard == {"Fatal Push": 4}


def test_parse_decklist_sums_a_card_listed_twice():
    assert parse_decklist("2 Island\n2 Island\n").mainboard == {"Island": 4}


def test_parse_decklist_splits_on_a_sideboard_header():
    parsed = parse_decklist("4 Psychic Frog\n\nSideboard\n2 Nihil Spellbomb\n")
    assert parsed.mainboard == {"Psychic Frog": 4}
    assert parsed.sideboard == {"Nihil Spellbomb": 2}


def test_parse_decklist_splits_on_the_blank_line_convention():
    """`decks/dimir-frog/decklist.txt` in the guides corpus carries no
    `Sideboard` header at all - the blank line IS the separator. Folding its
    fifteen sideboard cards into the maindeck is one of the two bugs this
    module exists to keep closed."""
    parsed = parse_decklist("4 Psychic Frog\n3 Watery Grave\n\n2 Nihil Spellbomb\n")
    assert parsed.mainboard == {"Psychic Frog": 4, "Watery Grave": 3}
    assert parsed.sideboard == {"Nihil Spellbomb": 2}


def test_parse_decklist_ignores_a_blank_line_before_the_first_card():
    assert parse_decklist("# header\n\n4 Psychic Frog\n").sideboard == {}


def test_parse_decklist_with_no_discernible_split_is_all_mainboard():
    parsed = parse_decklist("4 Psychic Frog\n4 Fatal Push\n")
    assert parsed.mainboard == {"Psychic Frog": 4, "Fatal Push": 4}
    assert parsed.sideboard == {}


def test_a_grouped_paste_with_no_header_keeps_the_whole_list_as_mainboard():
    """Finding 3. A list grouped by card type - creatures, then spells, then
    lands, a blank line between each - carries no `Sideboard` header, and the
    module's own docstring names that as the common case for a pasted list.

    The first blank line used to start a sideboard nothing ever switched back
    out of, so two thirds of the deck left the mainboard - and signatures are
    read off the mainboard alone.
    """
    grouped = "4 Psychic Frog\n4 Quantum Riddler\n\n4 Fatal Push\n4 Thoughtseize\n\n20 Island\n"
    parsed = parse_decklist(grouped)
    assert sum(parsed.mainboard.values()) == 36
    assert parsed.sideboard == {}


def test_a_grouped_list_still_finds_its_real_sideboard_boundary():
    """Grouped AND blank-line-separated at once, which is the case the guard
    has to get right rather than merely survive: the boundary taken is the one
    that leaves a legal sideboard behind it, not the first blank line in the
    maindeck."""
    parsed = parse_decklist("20 Island\n\n20 Mountain\n\n20 Forest\n\n15 Dress Down\n")
    assert sum(parsed.mainboard.values()) == 60
    assert parsed.sideboard == {"Dress Down": 15}


def test_a_full_maindeck_before_the_blank_line_is_a_split_however_long_the_rest(tmp_path):
    """The other half of the guard: sixty cards are a maindeck whatever
    follows them, so a blank line after a complete one splits even when what
    comes next is not a legal fifteen."""
    parsed = parse_decklist("60 Island\n\n20 Dress Down\n")
    assert parsed.mainboard == {"Island": 60}
    assert parsed.sideboard == {"Dress Down": 20}


def test_an_explicit_header_beats_a_blank_line_inside_the_mainboard():
    """A list that has a header says where the split is; a blank line in the
    middle of its maindeck is then just whitespace, not a second opinion."""
    parsed = parse_decklist("4 Psychic Frog\n\n4 Fatal Push\n\nSideboard\n2 Dress Down\n")
    assert parsed.mainboard == {"Psychic Frog": 4, "Fatal Push": 4}
    assert parsed.sideboard == {"Dress Down": 2}


def test_a_mainboard_header_switches_back():
    parsed = parse_decklist("Sideboard\n2 Dress Down\nDeck\n4 Psychic Frog\n")
    assert parsed.mainboard == {"Psychic Frog": 4}
    assert parsed.sideboard == {"Dress Down": 2}


def test_the_bundled_lists_all_parse_to_sixty_and_fifteen():
    """Every bundled list is a real tournament 75. If the split silently
    stopped working, this is where 75/0 shows up."""
    for path in sorted(BUNDLED_DECKS_DIR.glob("*.txt")):
        parsed = parse_decklist(path.read_text())
        assert sum(parsed.mainboard.values()) == 60, path.name
        assert sum(parsed.sideboard.values()) == 15, path.name


# ---- DeckGuide: archetype.json and the analysis directory -----------------


def test_a_deck_guide_carries_its_archetype_file_and_boarding_plan(tmp_path):
    root = tmp_path / "guides"
    deck = root / "decks" / "dimir-frog"
    domain = deck / "analysis" / "dimir-frog-modern-2026-08-10" / "domain3-matchup-threat"
    (domain / "matchups").mkdir(parents=True)
    (deck / "decklist.txt").write_text("4 Psychic Frog\n")
    (deck / "archetype.json").write_text((CORPUS / "dimir-frog" / "archetype.json").read_text())
    (deck / "GAMEPLAN_GUIDE.md").write_text("# gameplan\n")
    (deck / "SIDEBOARD_GUIDE.md").write_text("# sideboard\n")
    (deck / "META_CALLS.md").write_text("# meta\n")
    (domain / "boarding-plan.json").write_text("{}")
    (domain / "matchups" / "boros-energy.json").write_text("{}")

    guide = GuideLibrary(root).get("dimir-frog")
    assert guide.archetype.name == "archetype.json"
    assert guide.boarding_plan == domain / "boarding-plan.json"
    assert guide.matchups_dir == domain / "matchups"
    assert guide.archetypes == ["boros-energy"]
    assert guide.signature().name == "dimir frog"


def test_the_analysis_directory_is_chosen_by_date_not_lexicographically(tmp_path):
    """`analysis/<name>-modern-<date>`: sorting the paths as strings picks the
    alphabetically-last NAME, which is only the newest run by accident. Two
    runs whose names differ is all it takes for the old one to win."""
    root = tmp_path / "guides"
    deck = root / "decks" / "dimir-frog"
    (deck).mkdir(parents=True)
    (deck / "decklist.txt").write_text("4 Psychic Frog\n")
    for name in ("zzz-frog-modern-2025-01-01", "aaa-frog-modern-2026-08-10"):
        domain = deck / "analysis" / name / "domain3-matchup-threat"
        (domain / "matchups").mkdir(parents=True)
        (domain / "boarding-plan.json").write_text("{}")

    guide = GuideLibrary(root).get("dimir-frog")
    assert guide.matchups_dir.parents[1].name == "aaa-frog-modern-2026-08-10"
    # The boarding plan is read off the SAME run, by the same key. It has its
    # own glob's worth of temptation to sort lexicographically, and a plan
    # from 2025 rendered next to matchup files from 2026 is a card that
    # describes a field the deck was not analysed against.
    assert guide.boarding_plan.parents[1].name == "aaa-frog-modern-2026-08-10"
    assert guide.boarding_plan.parents[1] == guide.matchups_dir.parents[1]


def test_missing_guide_files_are_none_not_an_error(tmp_path):
    root = tmp_path / "guides"
    deck = root / "decks" / "bare"
    deck.mkdir(parents=True)
    (deck / "decklist.txt").write_text("4 Island\n")
    guide = GuideLibrary(root).get("bare")
    assert guide.gameplan is None
    assert guide.archetype is None
    assert guide.boarding_plan is None
    assert guide.matchups_dir is None
    assert guide.archetypes == []
    assert guide.signature() is None


def test_decks_lists_every_guided_deck(corpus):
    assert sorted(d.slug for d in corpus.decks()) == sorted(CORPUS_SLUGS)


def test_get_of_an_unknown_slug_is_none(corpus):
    assert corpus.get("nope") is None


def test_matchup_path_resolves_a_known_archetype_and_not_an_unknown_one(tmp_path):
    root = tmp_path / "guides"
    deck = root / "decks" / "dimir-frog"
    matchups = deck / "analysis" / "r-2026-01-01" / "domain3-matchup-threat" / "matchups"
    matchups.mkdir(parents=True)
    (deck / "decklist.txt").write_text("4 Psychic Frog\n")
    (matchups / "living-end.json").write_text("{}")
    guide = GuideLibrary(root).get("dimir-frog")
    assert guide.matchup_path("living-end").name == "living-end.json"
    assert guide.matchup_path("nope") is None


def test_a_missing_guides_root_yields_no_decks(tmp_path):
    assert GuideLibrary(tmp_path / "absent").decks() == []


# ---- the classifier: the three outcomes ----------------------------------


def test_similarity_scoring_is_gone():
    """Whole-list similarity is not kept alongside the classifier as a
    fallback: two code paths identifying the same 60 cards differently is the
    bug that was removed, and a fallback reintroduces it."""
    assert not hasattr(guides_module, "_similarity")
    assert not hasattr(guides_module, "MATCH_THRESHOLD")


def test_identify_returns_the_guide_the_confidence_and_the_candidates(corpus):
    """Not a bare `(guide, score)`: a caller has to be able to say "matched
    as X, Y also plausible"."""
    result = identify_text(corpus, (CORPUS / "living-end" / "decklist.txt").read_text())
    assert result.confidence == CONFIDENT
    assert result.guide.slug == "living-end"
    assert result.archetype == "living end"
    assert [c.slug for c in result.candidates] == ["living-end"]
    assert result.candidates[0].matched == result.candidates[0].clauses
    assert result.alternatives == ()


def test_nothing_reaching_half_its_clauses_is_none(corpus):
    """A list built from cards no signature names at all."""
    result = corpus.identify(parse_decklist("56 Forest\n4 Grizzly Bears\n"))
    assert result.confidence == NONE
    assert result.guide is None
    assert result.candidates == ()
    assert result.archetype is None


def test_sharing_the_cheap_spells_and_the_mana_is_low_not_none(corpus):
    """Twenty Mountains and forty Lightning Bolts answers two of izzet
    prowess's three clauses - the cheap red spells and a red manabase - and
    not the one that names the threats. `low` is the honest answer: it is a
    red deck the prowess guide has something to say to, and it is not a
    claim that it IS izzet prowess."""
    result = corpus.identify(parse_decklist("20 Mountain\n40 Lightning Bolt\n"))
    assert result.confidence == LOW
    assert result.guide.slug == "prowess"
    assert (result.candidates[0].matched, result.candidates[0].clauses) == (2, 3)


# ---- the table: this repo's bundled lists against the vendored corpus -----
#
# The cross-repo drift check, and the test that would have caught both
# original bugs. Every expectation here was read off the vendored signatures
# and argued against the list, not copied from whatever the code happened to
# print: `jeskai_aggro` really does run the whole boros-aggro one-drop suite
# (4 Ajani, 4 Guide of Souls, 4 Ocelot Pride, 3 Ragavan), `dimir_control`
# really does run 4 Psychic Frog and 3 Quantum Riddler, and `creatures_
# toolbox` really does run 4 Devoted Druid, 4 Tyvar and a Vizier - by the
# corpus's own definitions those ARE those archetypes, whatever the filename
# says.

#: `deck file stem -> (confidence, resolved archetype slug or None)`. Every
#: one of the 31 bundled lists appears; `none` means the corpus has no guide
#: whose signature this list satisfies, which for two thirds of them is the
#: correct answer rather than a miss.
BUNDLED_EXPECTATIONS = {
    "45c_aggro": (NONE, None),
    "45c_control": (NONE, None),
    "allosaurus_combo": (NONE, None),
    "amulet_titan": (NONE, None),
    "belcher": (NONE, None),
    # 2/3 of esper-blink and 2/3 of orzhov-martyr, which share the Phelia /
    # Ephemerate / Flickerwisp / Solitude engine and are separated only by
    # the clause this list answers for neither.
    "blink": (LOW, "esper-blink"),
    "boros_aggro": (CONFIDENT, "boros-aggro"),
    # The owner commissioned ONE boros guide. The energy list satisfies all
    # three of its clauses outright - same one-drops, same red half, same
    # manabase - so the one guide covers it.
    "boros_energy": (CONFIDENT, "boros-aggro"),
    "boros_ponza": (NONE, None),
    "broodscale_bloodchief": (CONFIDENT, "bloodchief-combo"),
    "creatures_toolbox": (CONFIDENT, "devoted-combo"),
    "devoted_combo": (CONFIDENT, "devoted-combo"),
    "dimir_control": (CONFIDENT, "dimir-frog"),
    "dimir_frog": (CONFIDENT, "dimir-frog"),
    "eldrazi_ramp": (CONFIDENT, "eldrazi-ramp"),
    # Half of instant-reanimator's signature (the outlets and the esper
    # manabase) without Goryo's Vengeance - low, not a claim.
    "esper_control": (LOW, "instant-reanimator"),
    "esper_goryo": (CONFIDENT, "instant-reanimator"),
    # One of affinity's two clauses: the cheap-artifact count, not the
    # Kappa Cannoneer / Weapons Manufacturing payoffs.
    "grinding_cam": (LOW, "affinity"),
    "grixis_reanimator": (NONE, None),
    "hollow_one": (NONE, None),
    "instant_reanimator": (CONFIDENT, "instant-reanimator"),
    "izzet_prowess": (CONFIDENT, "prowess"),
    "jeskai_aggro": (CONFIDENT, "boros-aggro"),
    "living_end": (CONFIDENT, "living-end"),
    "pinnacle_affinity": (CONFIDENT, "affinity"),
    "ruby_storm": (NONE, None),
    "scepter_chant": (NONE, None),
    "storm": (NONE, None),
    "ur_aggro": (CONFIDENT, "prowess"),
    "urzatron": (CONFIDENT, "prison-tron"),
    "uw_control": (NONE, None),
}


def test_the_table_covers_every_bundled_deck():
    assert sorted(BUNDLED_EXPECTATIONS) == sorted(p.stem for p in BUNDLED_DECKS_DIR.glob("*.txt"))


@pytest.mark.parametrize(
    ("stem", "expected"),
    sorted((stem, expected) for stem, expected in BUNDLED_EXPECTATIONS.items()),
)
def test_a_bundled_decklist_classifies_as_the_table_says(corpus, stem, expected):
    confidence, slug = expected
    result = identify_text(corpus, (BUNDLED_DECKS_DIR / f"{stem}.txt").read_text())
    assert result.confidence == confidence, [
        (c.slug, c.matched, c.clauses) for c in result.candidates
    ]
    assert (result.guide.slug if result.guide else None) == slug


# ---- mutual exclusion ----------------------------------------------------


@pytest.mark.parametrize("slug", CORPUS_SLUGS)
def test_a_guides_own_decklist_identifies_as_itself_and_only_itself(corpus, slug):
    """Mutual exclusion, at the corpus's own thirteen lists: each is a
    confident match for its own signature, and no other archetype claims it
    confidently. Thirteen lists against thirteen signatures is the whole
    matrix - if a signature is loose enough to claim a neighbour, it is this
    test that says so."""
    result = identify_text(corpus, (CORPUS / slug / "decklist.txt").read_text())
    assert result.confidence == CONFIDENT, [
        (c.slug, c.matched, c.clauses) for c in result.candidates
    ]
    assert result.guide.slug == slug


def test_no_signature_confidently_claims_another_archetypes_bundled_list(corpus):
    """The same claim from the bundled side: where the table above expects a
    confident match, exactly one archetype may match fully. A second full
    matcher is a signature that is too loose, and would silently pick one of
    the two by tie-break."""
    for stem, (confidence, slug) in BUNDLED_EXPECTATIONS.items():
        result = identify_text(corpus, (BUNDLED_DECKS_DIR / f"{stem}.txt").read_text())
        if confidence != CONFIDENT:
            continue
        full = [c.slug for c in result.candidates if c.matched == c.clauses]
        assert full == [slug], f"{stem} is claimed by more than one signature: {full}"


# ---- low: two plausible candidates, and the tie-breaks -------------------


def test_two_candidates_at_equal_specificity_are_low_with_both_reported(corpus):
    """`decks/blink.txt` answers two of esper-blink's three clauses and two
    of orzhov-martyr's three - the blink engine and the payoff bodies - and
    neither deck's separating clause. Equal specificity, so the honest answer
    is low with both named, not a coin flip presented as a match."""
    result = identify_text(corpus, (BUNDLED_DECKS_DIR / "blink.txt").read_text())
    assert result.confidence == LOW
    assert sorted(c.slug for c in result.candidates) == ["esper-blink", "orzhov-martyr"]
    assert result.guide is not None
    assert [c.slug for c in result.alternatives] != []
    assert {c.archetype for c in result.candidates} == {"esper blink", "orzhov blink"}


def test_exactly_one_candidate_over_half_is_low_not_confident(corpus):
    """`decks/grinding_cam.txt` satisfies one of affinity's two clauses: the
    zero-and-one-mana artifact count, not the Kappa Cannoneer payoffs. Half
    is low, and low still hands over the guide."""
    result = identify_text(corpus, (BUNDLED_DECKS_DIR / "grinding_cam.txt").read_text())
    assert result.confidence == LOW
    assert result.guide.slug == "affinity"
    assert result.candidates[0].matched * 2 >= result.candidates[0].clauses
    assert result.candidates[0].matched < result.candidates[0].clauses


def test_more_signature_clauses_wins_the_tie_break(tmp_path):
    """A two-clause sub-archetype beats a one-clause parent when both match
    fully: the more specific claim is the one that was harder to satisfy."""
    root = tmp_path / "guides"
    for slug, spec in (
        (
            "broad",
            {
                "archetype": "broad",
                "signature": [{"cards": ["Urza's Tower"], "min": 4, "why": "the shared clause"}],
            },
        ),
        (
            "narrow",
            {
                "archetype": "narrow",
                "signature": [
                    {"cards": ["Urza's Tower"], "min": 4, "why": "the shared clause"},
                    {"cards": ["Mind Stone"], "min": 3, "why": "and this branch's own"},
                ],
            },
        ),
    ):
        deck = root / "decks" / slug
        deck.mkdir(parents=True)
        (deck / "decklist.txt").write_text("4 Urza's Tower\n")
        (deck / "archetype.json").write_text(json.dumps(spec))

    result = GuideLibrary(root).identify(parse_decklist("4 Urza's Tower\n4 Mind Stone\n"))
    assert result.confidence == CONFIDENT
    assert result.guide.slug == "narrow"
    assert [c.slug for c in result.candidates] == ["narrow", "broad"]


def test_a_declared_exclude_clause_breaks_a_tie_that_clause_count_does_not(tmp_path):
    """Same clause count, one of them carrying an exclude clause: the file
    that went to the trouble of saying what it is NOT is the more precise
    claim, so it wins rather than the pair falling to `low`."""
    root = tmp_path / "guides"
    clause = {"cards": ["Urza's Tower"], "min": 4, "why": "shared"}
    for slug, spec in (
        ("plain", {"archetype": "plain", "signature": [clause]}),
        (
            "precise",
            {
                "archetype": "precise",
                "signature": [clause],
                "exclude": [{"cards": ["Psychic Frog"], "min": 4, "why": "not the frog deck"}],
            },
        ),
    ):
        deck = root / "decks" / slug
        deck.mkdir(parents=True)
        (deck / "decklist.txt").write_text("4 Urza's Tower\n")
        (deck / "archetype.json").write_text(json.dumps(spec))

    result = GuideLibrary(root).identify(parse_decklist("4 Urza's Tower\n"))
    assert result.confidence == CONFIDENT
    assert result.guide.slug == "precise"


def test_an_exclude_clause_knocks_an_archetype_out_entirely(corpus):
    """`instant-reanimator` runs the same 4 Psychic Frog, Thoughtseize and
    fetch manabase as `dimir-frog`, and dimir-frog's own file says so: four
    copies across Goryo's Vengeance and Atraxa is the reanimation package,
    not a splash. The list satisfies every dimir-frog clause and is still
    not dimir-frog."""
    result = identify_text(corpus, (CORPUS / "instant-reanimator" / "decklist.txt").read_text())
    assert result.guide.slug == "instant-reanimator"
    assert "dimir-frog" not in [c.slug for c in result.candidates]


def test_a_shared_clause_alone_resolves_to_the_parent(corpus):
    """Nine Tron lands plus the Temple/Labyrinth mana and neither branch's
    package: `prison-tron` and `eldrazi-tron` both answer two of three
    clauses and both declare `parent: tron`. Naming a branch would hand the
    pilot the wrong branch's guide, so the family is the answer - and both
    candidates are reported."""
    result = corpus.identify(
        parse_decklist(
            "4 Urza's Tower\n4 Urza's Mine\n4 Urza's Power Plant\n"
            "4 Eldrazi Temple\n4 Ugin's Labyrinth\n20 Wastes\n"
        )
    )
    assert result.confidence == LOW
    assert result.archetype == "tron"
    assert result.guide is None
    assert sorted(c.slug for c in result.candidates) == ["eldrazi-tron", "prison-tron"]
    assert sorted(g.slug for g in result.guides) == ["eldrazi-tron", "prison-tron"]


# ---- the sideboard cannot carry a signature ------------------------------


def test_a_signature_cannot_be_satisfied_out_of_the_sideboard(corpus):
    """The second original bug, directly. Fifteen sideboard cards that would
    answer two of dimir-frog's four clauses if folded in answer none of them
    where it counts: nobody has cast a sideboard card."""
    board = "4 Psychic Frog\n3 Quantum Riddler\n4 Fatal Push\n4 Thoughtseize\n"
    parsed = parse_decklist(f"60 Forest\n\nSideboard\n{board}")
    assert sum(parsed.sideboard.values()) == 15
    assert corpus.identify(parsed).confidence == NONE
    # ... and in the maindeck those same fifteen cards DO reach half of
    # dimir-frog's four clauses, which is what makes this a real guard
    # rather than a tautology about a list nothing would have matched.
    folded = parse_decklist(f"60 Forest\n{board}")
    assert folded.sideboard == {}
    assert corpus.identify(folded).confidence == LOW


# ---- a user's own paste, grouped by card type -----------------------------


#: Anything a list uses to name a board. Stripped to build the paste a user
#: actually produces when they group a list by card type and never type the
#: word "Sideboard" at all.
_BOARD_HEADER = re.compile(
    r"^\s*(sideboard|side board|sb:|maindeck|mainboard|main deck|deck)\b", re.I
)


def grouped_paste(text: str) -> str:
    """The same 75, pasted the way a user groups one: every board header
    gone, and a blank line inside the maindeck where the grouping falls."""
    rows = [line for line in text.splitlines() if line.strip() and not _BOARD_HEADER.match(line)]
    return "\n".join([*rows[:8], "", *rows[8:]]) + "\n"


@pytest.mark.parametrize("slug", ["dimir-frog", "living-end", "prowess"])
def test_a_real_list_regrouped_without_its_header_still_identifies(corpus, slug):
    """Finding 3 at the level that made it user-facing.

    The whole reason for the signature classifier was "I want users to bring
    their own decklists for the opponents", and a list grouped by card type
    with no `Sideboard` header is a completely ordinary way to paste one.
    These three real corpus lists went from `confident` to `low` when pasted
    that way, with 21, 31 and 27 of their 75 cards left in the mainboard and
    the rest filed as a sideboard that cannot make a claim about anything.
    """
    text = (CORPUS / slug / "decklist.txt").read_text()
    assert identify_text(corpus, text).confidence == CONFIDENT

    pasted = parse_decklist(grouped_paste(text))
    assert sum(pasted.mainboard.values()) == 75, "the paste has no sideboard marker to read"
    assert pasted.sideboard == {}
    result = corpus.identify(pasted)
    assert result.confidence == CONFIDENT
    assert result.guide.slug == slug


# ---- a broken archetype.json in a third repo must not take a game down ----


@pytest.mark.parametrize(
    "content",
    [
        "not json at all {{{",
        "[]",
        '{"schema_version": 1}',
        '{"archetype": "x", "signature": []}',
        '{"archetype": "x", "signature": "four Psychic Frog"}',
        '{"archetype": "x", "signature": [{"cards": [], "min": 4}]}',
        '{"archetype": "x", "signature": [{"cards": ["Psychic Frog"]}]}',
        '{"archetype": "x", "signature": [{"cards": ["Psychic Frog"], "min": "four"}]}',
        # The shapes that used to RAISE rather than degrade, each one
        # reproduced before it was closed. `cards` was checked element by
        # element and never checked for being iterable at all, so a scalar
        # or a mapping there came out of `json.loads` and straight into a
        # `for` loop: `TypeError: 'int' object is not iterable`, out of
        # `load_signature`, whose own docstring says it never raises.
        '{"archetype": "x", "signature": [{"cards": 5, "min": 1}]}',
        '{"archetype": "x", "signature": [{"cards": {"Psychic Frog": 4}, "min": 1}]}',
        '{"archetype": "x", "signature": [{"cards": [5], "min": 1}]}',
        '{"archetype": "x", "signature": {"cards": ["Psychic Frog"], "min": 4}}',
        '{"archetype": "x", "signature": 5}',
        # ... and the same hole in the `exclude` branch, which reads through
        # the identical helper.
        '{"archetype": "x", "signature": [{"cards": ["Psychic Frog"], "min": 4}],'
        ' "exclude": [{"cards": 5, "min": 1}]}',
        '{"archetype": "x", "signature": [{"cards": ["Psychic Frog"], "min": 4}],'
        ' "exclude": "Ragavan, Nimble Pilferer"}',
        '{"archetype": "x", "signature": [{"cards": ["Psychic Frog"], "min": 4}], "exclude": null}',
        # An `exclude` row with no `min` at all: the file said "a list
        # holding Psychic Frog is not me" and half-said how many.
        '{"archetype": "x", "signature": [{"cards": ["Psychic Frog"], "min": 4}],'
        ' "exclude": [{"cards": ["Psychic Frog"]}]}',
    ],
)
def test_a_malformed_archetype_file_degrades_to_none_without_raising(tmp_path, content):
    root = tmp_path / "guides"
    deck = root / "decks" / "broken"
    deck.mkdir(parents=True)
    (deck / "decklist.txt").write_text("4 Psychic Frog\n")
    (deck / "archetype.json").write_text(content)

    library = GuideLibrary(root)
    assert library.get("broken").signature() is None
    assert library.identify(parse_decklist("4 Psychic Frog\n")).confidence == NONE


def test_a_missing_archetype_file_degrades_to_none_without_raising(tmp_path):
    root = tmp_path / "guides"
    deck = root / "decks" / "unsigned"
    deck.mkdir(parents=True)
    (deck / "decklist.txt").write_text("4 Psychic Frog\n")
    (deck / "GAMEPLAN_GUIDE.md").write_text("# gameplan\n")

    library = GuideLibrary(root)
    assert library.get("unsigned").archetype is None
    assert library.identify(parse_decklist("4 Psychic Frog\n")).confidence == NONE


def test_one_broken_file_does_not_hide_the_decks_that_are_fine(tmp_path):
    """A malformed file in a third repo degrades THAT deck to unclassifiable,
    not the whole library: the other twelve guides still match."""
    library = _corpus_library(tmp_path)
    (library.root / "decks" / "living-end" / "archetype.json").write_text("{ broken")
    result = identify_text(library, (CORPUS / "prowess" / "decklist.txt").read_text())
    assert result.confidence == CONFIDENT
    assert result.guide.slug == "prowess"
    assert (
        identify_text(library, (CORPUS / "living-end" / "decklist.txt").read_text()).confidence
        == NONE
    )


@pytest.mark.parametrize(
    "content",
    [
        "{ broken",
        '{"archetype": "zz", "signature": [{"cards": 5, "min": 1}]}',
        '{"archetype": "zz", "signature": 5}',
        '{"archetype": "zz", "signature": [{"cards": ["Psychic Frog"], "min": 4}],'
        ' "exclude": [{"cards": 5, "min": 1}]}',
    ],
)
def test_one_broken_file_does_not_stop_identify_answering_for_the_rest(tmp_path, content):
    """The blast radius, which is what made this HIGH rather than cosmetic.

    `identify` asks EVERY archetype in the corpus about the same mainboard,
    and `driver/app.py`'s `/dispatch` calls it unguarded - so a single
    malformed file anywhere in a third repo used to raise out of the loop and
    500 the dispatch for BOTH seats and EVERY deck. Turn 0 never rendered and
    neither agent ever played, over one file that had nothing to do with
    either list.
    """
    library = _corpus_library(tmp_path)
    broken = library.root / "decks" / "zz-broken"
    broken.mkdir(parents=True)
    (broken / "decklist.txt").write_text("4 Psychic Frog\n")
    (broken / "archetype.json").write_text(content)

    for slug in CORPUS_SLUGS:
        result = identify_text(library, (CORPUS / slug / "decklist.txt").read_text())
        assert result.confidence == CONFIDENT, slug
        assert result.guide.slug == slug
    assert library.get("zz-broken").signature() is None


# ---- degradation must never make a match EASIER ---------------------------
#
# The non-obvious half of the hardening, and the one worth a test each.
# `_clauses` used to drop an unusable clause and leave the rest of the file
# standing, while `Candidate.clauses` counts only the survivors - so a
# half-broken signature was a SHORTER signature, and a shorter signature is a
# cheaper one to satisfy in full. Malformation bought a confident answer.


#: A one-card list, and a two-clause archetype the second clause of which it
#: cannot answer. Enough to measure "how much of the signature did the list
#: actually answer" going the wrong way.
RAGAVAN = "4 Ragavan, Nimble Pilferer\n"
TWO_CLAUSES = [
    {"cards": ["Ragavan, Nimble Pilferer"], "min": 4, "why": "the one-drop"},
    {"cards": ["Slickshot Show-Off"], "min": 3, "why": "the payoff"},
]


def _one_deck_library(tmp_path: Path, name: str, spec: dict) -> GuideLibrary:
    """A library holding exactly one guide, whose `archetype.json` is `spec`.
    `name` keeps two of them apart inside one test's `tmp_path`."""
    root = tmp_path / name
    deck = root / "decks" / "e"
    deck.mkdir(parents=True)
    (deck / "decklist.txt").write_text(RAGAVAN)
    (deck / "archetype.json").write_text(json.dumps(spec))
    return GuideLibrary(root)


def test_a_malformed_clause_does_not_make_the_signature_easier_to_satisfy(tmp_path):
    """Measured, same list and same archetype: intact, `4 Ragavan` answered
    1 of 2 clauses and got `low`; with the second clause malformed it
    answered 1 of 1 and got `confident`. Breaking the file promoted the
    answer.

    So a signature with any unusable row loses the whole signature, and the
    deck degrades to unclassifiable - which is what `load_signature`'s
    docstring already promised ("a malformed or half-written one must cost
    that deck its signature").
    """
    intact = _one_deck_library(tmp_path, "intact", {"archetype": "e", "signature": TWO_CLAUSES})
    assert identify_text(intact, RAGAVAN).confidence == LOW

    broken = _one_deck_library(
        tmp_path,
        "broken",
        {"archetype": "e", "signature": [TWO_CLAUSES[0], {"cards": [], "min": 3}]},
    )
    assert broken.get("e").signature() is None
    assert identify_text(broken, RAGAVAN).confidence == NONE


def test_a_malformed_exclude_clause_does_not_un_disown_a_list(tmp_path):
    """The same defect on the `exclude` side, and the worse half of it.

    An `exclude` clause is the file saying "a list holding these is playing
    the other deck, not me". Dropping the row deletes the disavowal, so the
    archetype comes back into contention and - with its one signature clause
    answered - is handed over CONFIDENTLY. Measured: `none` (correctly
    disowned) became `confident` with the wrong guide attached.
    """
    disowned = _one_deck_library(
        tmp_path,
        "disowned",
        {
            "archetype": "e",
            "signature": [TWO_CLAUSES[0]],
            "exclude": [{"cards": ["Ragavan, Nimble Pilferer"], "min": 1}],
        },
    )
    assert identify_text(disowned, RAGAVAN).confidence == NONE

    broken = _one_deck_library(
        tmp_path,
        "broken",
        {
            "archetype": "e",
            "signature": [TWO_CLAUSES[0]],
            "exclude": [{"cards": ["Ragavan, Nimble Pilferer"]}],
        },
    )
    result = identify_text(broken, RAGAVAN)
    assert result.confidence == NONE
    assert result.guide is None


def test_an_empty_cards_list_never_vacuously_matches(tmp_path):
    """Already safe before the hardening and it must stay safe: a clause
    naming no cards sums to 0, which never reaches a `min` of 1 or more, and
    the clause is unusable anyway - so it costs the file its signature rather
    than matching every list ever pasted."""
    library = _one_deck_library(
        tmp_path, "empty", {"archetype": "e", "signature": [{"cards": [], "min": 1}]}
    )
    assert library.get("e").signature() is None
    assert identify_text(library, RAGAVAN).confidence == NONE


# ---- a card the list holds, whichever way either side spells it -----------
#
# The corpus spells one card three ways: Scryfall and the guides' own
# decklists write "Wear // Tear", the bundled decks the server deals write
# "Wear/Tear", and a hand-written boarding plan writes the swap as "1 Wear".
# `matchup_card` intersects the second against the third, so the rule that
# says those are one card lives here, next to the decklist parser.


def test_a_split_card_is_held_under_every_spelling_of_it():
    for spelling in ("Wear // Tear", "Wear/Tear", "Wear", "wear // tear"):
        held = held_faces({spelling: 2})
        assert holds(held, "Wear"), spelling
        assert holds(held, "Wear // Tear"), spelling
        assert holds(held, "Wear/Tear"), spelling


def test_either_face_of_a_split_the_list_holds_counts_as_held():
    """Looser than `scryfall._pool_names_match`'s "whole name or front face",
    and deliberately so: a seat holding Wear // Tear can cast Tear, and the
    expensive mistake here is telling the agent not to board a card it has."""
    held = held_faces({"Wear // Tear": 2})
    assert holds(held, "Tear")


def test_a_card_no_face_of_which_is_in_the_list_is_not_held():
    held = held_faces({"Wear // Tear": 2, "Lightning Bolt": 4})
    assert not holds(held, "Mystical Dispute")
    assert not holds(held, "Bolt")
    assert not holds(held, "")


def test_holding_is_read_off_a_parsed_decklist():
    held = held_faces(parse_decklist("2 Wear // Tear\n4 Fatal Push (MH3) 89\n"))
    assert holds(held, "Wear")
    assert holds(held, "Fatal Push")
