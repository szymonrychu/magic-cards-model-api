"""The matchup card: the deck's plan and the field's tells, rendered into
turn 0 instead of left in a 58 KB markdown guide for the agent to `Read`.

Every test here runs against `tests/fixtures/guides`, a verbatim slice of the
real corpus (see its README) - the byte budget is the point of the module, and
a budget measured against a hand-written approximation of a boarding plan
would assert nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mtg_agent.driver.guides import Decklist, GuideLibrary, parse_decklist

# Private because nothing outside the module should pick a rung of the trim
# ladder - but the ORDER the rungs are taken in is exactly what
# `test_truncation_shortens_watch_for_before_the_top_three_detail` pins.
from mtg_agent.driver.matchup_card import (
    _TRIMS,
    CARD_HARD_CAP_BYTES,
    CARD_TARGET_BYTES,
    WATCH_FOR_CAP,
    _rendering,
    load_boarding_plan,
    render_matchup_card,
    signature_index,
)

FIXTURE_GUIDES = Path(__file__).resolve().parents[1] / "fixtures" / "guides"
FIXTURE_README = FIXTURE_GUIDES / "README.md"

#: Every boarding plan vendored under `tests/fixtures/guides`. The first
#: three are the largest plans in the corpus, which is the densest INPUT;
#: `living-end` and `affinity` are the two that RENDER largest - 11566 and
#: 11490 bytes against the 12288 hard cap, measured over all thirteen guides
#: with the corpus root at `~/Documents/mtg-decks` (the slice's README
#: carries the whole table, and why a raw figure moves with the root). Both
#: halves matter: the densest input is the worst case for the trim ladder,
#: and the largest rendering is the worst case for the cap. The dense-input
#: three top out at 11357, so a cap test that saw only them was working with
#: 209 bytes of headroom the corpus does not have.
VENDORED = ["dimir-frog", "orzhov-martyr", "prison-tron", "living-end", "affinity"]

#: A budget that genuinely squeezes every plan in the corpus: half the
#: target, two thirds of the cap. No caller in the driver hands the card
#: this - the briefing renders every card at the module's own budget, one
#: whole card per branch even when the classifier could not separate two
#: branches of a family - but `render_matchup_card` takes both numbers as
#: arguments, and the tests that pin what is and is not a trim lever need a
#: budget that actually makes the levers fire. The DEFAULT budget does not:
#: full fidelity fits the default cap for all thirteen guides, which is the
#: whole reason the default is what a briefing uses.
SQUEEZED_BUDGET = (CARD_TARGET_BYTES // 2, CARD_HARD_CAP_BYTES * 2 // 3)


@pytest.fixture
def library() -> GuideLibrary:
    return GuideLibrary(FIXTURE_GUIDES)


def guide_for(library: GuideLibrary, slug: str):
    guide = library.get(slug)
    assert guide is not None, f"{slug} is not in tests/fixtures/guides"
    return guide


def own_decklist(guide) -> Decklist:
    """The 75 the guide ships, parsed the way the driver parses the list the
    server deals: mainboard and sideboard kept apart."""
    return parse_decklist(guide.decklist.read_text())


def without(decklist: Decklist, *names: str) -> Decklist:
    """The same 75 minus `names`, out of BOTH boards - the list of a pilot
    who does not run the card the plan's swap names."""

    def drop(board: dict[str, int]) -> dict[str, int]:
        return {
            name: count
            for name, count in board.items()
            if not any(dropped in name for dropped in names)
        }

    return Decklist(mainboard=drop(decklist.mainboard), sideboard=drop(decklist.sideboard))


def deck_section(text: str) -> str:
    """Section 1 of a rendered card, on its own. Split on the whole heading:
    section 3 numbers its three matchups `#### 1.`, which a shorter needle
    matches too."""
    return text.split("### 1. What your deck is doing")[-1].split("### 2. Tells")[0]


def signature_column(text: str) -> list[str]:
    """The tells table's signature-cards cell, row by row."""
    return [line.split("|")[5].strip() for line in text.splitlines() if line.startswith("| `")]


def rows_in(text: str) -> list[str]:
    """The tells table's rows, as they were actually rendered."""
    return [line for line in text.splitlines() if line.startswith("| `")]


def plan_of(guide) -> dict:
    assert guide.boarding_plan is not None
    return json.loads(guide.boarding_plan.read_text())


def guided_slugs() -> list[str]:
    """Every slug in the slice the renderer can actually be handed: a
    `decklist.txt` and an `analysis/` run, not just an `archetype.json` for
    the tells table to quote signature cards out of."""
    return sorted(
        d.name
        for d in (FIXTURE_GUIDES / "decks").iterdir()
        if (d / "decklist.txt").is_file() and (d / "analysis").is_dir()
    )


def readme_card_bytes() -> dict[str, int]:
    """The README's own figures for what the card renders to, parsed back out
    of it - rows shaped `| `<slug>` | <bytes> |`. The three-column table of
    corpus-root measurements above it does not match."""
    return {
        m["slug"]: int(m["size"])
        for m in re.finditer(
            r"^\| `(?P<slug>[a-z0-9-]+)` \| (?P<size>\d+) \|$",
            FIXTURE_README.read_text(),
            re.MULTILINE,
        )
    }


def path_independent_size(card, guide) -> int:
    """The card's byte size less the two absolute paths it prints.

    The card names its boarding plan in the header and its `matchups/`
    directory in the one-read line, so the RAW byte count is a fact about
    where the corpus happens to sit: it moves by exactly twice the root's
    length, which makes the same guide 58 bytes larger under
    `.claude/worktrees/p2-matchup-card` than under `code`, and smaller again
    under the short `/guides` a seat Job clones to (the README works that one
    out against the corpus root it quotes). That is how the README's figures
    went stale without anyone editing them, so the figure the README quotes
    and this pins is the one that does not move.
    """
    plan = str(guide.boarding_plan)
    assert card.text.count(plan) == 1, "the header stopped naming the plan exactly once"
    assert card.text.count(str(guide.matchups_dir)) == 1, (
        "the one-read line stopped naming the matchups directory exactly once"
    )
    return card.byte_size - 2 * len(plan.encode())


# ---- the corpus is what we say it is --------------------------------------


@pytest.mark.parametrize("slug", VENDORED)
def test_the_vendored_guide_carries_a_real_boarding_plan(library, slug):
    guide = guide_for(library, slug)
    assert guide.boarding_plan is not None
    assert guide.boarding_plan.stat().st_size > 80_000, "not the real file"
    assert 15 <= len(plan_of(guide)["matchups"]) <= 19


def test_the_readme_enumerates_every_guided_deck_in_the_slice():
    """A stale README is an undocumented slice. Two decks were vendored with
    full analysis runs without the enumeration growing to name them, so its
    "two `matchups/*.json` per vendored deck" bullet described four of six.
    """
    readme = FIXTURE_README.read_text()
    for slug in guided_slugs():
        assert re.search(rf"decks/(\{{[^}}]*\b{slug}\b[^}}]*\}}|{slug})", readme), (
            f"{slug} is vendored with a full analysis run and the README never names it"
        )


def test_every_guided_deck_vendors_two_matchup_files():
    """What the README's bullet claims, and what makes `matchups_dir` resolve
    for every deck the suite renders."""
    for slug in guided_slugs():
        files = sorted(
            (FIXTURE_GUIDES / "decks" / slug).glob(
                "analysis/*/domain3-matchup-threat/matchups/*.json"
            )
        )
        assert len(files) == 2, f"{slug} vendors {len(files)} matchup files, not two"


def test_the_readme_quotes_the_size_the_card_actually_renders_to(library):
    """Acceptance 7 wants the card's ACTUAL rendered size reported, and the
    README is where it is written down - so the README is a claim the suite
    has to be able to falsify.

    It could not, and the figures drifted ~1.3-1.9 KB low with the ranking
    wrong: they were measured before the trim ladder was reordered in the
    same commit that wrote them, and nothing re-read them afterwards.
    """
    quoted = readme_card_bytes()
    assert quoted, "the README carries no figures for the suite to check"
    assert set(quoted) == set(guided_slugs()), (
        "the README's figures and the guided decks in the slice are different sets"
    )
    for slug, size in sorted(quoted.items()):
        guide = guide_for(library, slug)
        card = render_matchup_card(guide, own_decklist(guide))
        assert card.byte_size <= CARD_HARD_CAP_BYTES
        assert path_independent_size(card, guide) == size, (
            f"{slug} renders {path_independent_size(card, guide)} bytes (paths excluded), "
            f"and the README says {size}"
        )


def test_a_raw_card_size_moves_by_exactly_twice_the_corpus_root(library, tmp_path):
    """The mechanism `path_independent_size` and the README's root-dependence
    note both rest on, measured rather than argued: the same slice reached
    through a second root of a known different length renders a card that
    differs by exactly twice that difference, and the path-excluded figure
    does not move at all.
    """
    slug = "living-end"
    here = guide_for(library, slug)
    from_here = render_matchup_card(here, own_decklist(here))

    deeper = tmp_path / ("d" * 23)
    deeper.symlink_to(FIXTURE_GUIDES, target_is_directory=True)
    there = guide_for(GuideLibrary(deeper), slug)
    from_there = render_matchup_card(there, own_decklist(there))

    grown = len(str(deeper)) - len(str(FIXTURE_GUIDES))
    assert from_there.byte_size - from_here.byte_size == 2 * grown, (
        "a raw card size is supposed to move by exactly twice the root's length"
    )
    assert path_independent_size(from_there, there) == path_independent_size(from_here, here)


def test_the_path_independent_note_quotes_a_delta_that_measures_true():
    """Acceptance 7 binds every byte figure reported for the card, and a
    docstring is a report: `path_independent_size` explained itself with a
    delta of 158 bytes between a `.claude/worktrees/...` checkout and a
    `code/` one, plus a "~250 smaller again under `/guides`". Both were
    arithmetic nobody ran - the card prints the root twice, so the first is
    2 x (33 - 4) = 58 and the README said so all along.

    So the note has to name the two path fragments it is comparing, which
    makes its figure checkable from anywhere the suite runs rather than only
    from the checkout it was written in.
    """
    # One line, so the figure is checkable wherever the docstring happens to wrap.
    note = " ".join((path_independent_size.__doc__ or "").split())
    quoted = re.search(
        r"(?P<bytes>\d+) bytes larger under `(?P<deep>[^`]+)` than under `(?P<shallow>[^`]+)`",
        note,
    )
    assert quoted, (
        "the note must state its delta as `N bytes larger under `<fragment>` than "
        "under `<fragment>`` so the suite can check the figure"
    )
    expected = 2 * (len(quoted["deep"]) - len(quoted["shallow"]))
    assert int(quoted["bytes"]) == expected, (
        f"the note quotes {quoted['bytes']} bytes for "
        f"`{quoted['deep']}` over `{quoted['shallow']}`, and the card prints the "
        f"root twice, so the delta is {expected}"
    )


# ---- the budget -----------------------------------------------------------


@pytest.mark.parametrize("slug", VENDORED)
def test_the_card_renders_for_a_real_guide_under_the_hard_cap(library, slug):
    guide = guide_for(library, slug)
    card = render_matchup_card(guide, own_decklist(guide))
    assert card.text.strip(), "a real guide must render a non-empty card"
    assert card.byte_size == len(card.text.encode())
    assert card.byte_size <= CARD_HARD_CAP_BYTES, (
        f"{slug} rendered {card.byte_size} bytes, over the {CARD_HARD_CAP_BYTES} hard cap"
    )


@pytest.mark.parametrize("slug", VENDORED)
def test_the_card_is_a_fraction_of_the_gameplan_guide_it_replaces(library, slug):
    """The whole point: 12-18k tokens of `Read` become ~3k of context."""
    guide = guide_for(library, slug)
    card = render_matchup_card(guide, own_decklist(guide))
    assert card.byte_size * 4 < guide.boarding_plan.stat().st_size


# ---- deck section ---------------------------------------------------------


@pytest.mark.parametrize("slug", VENDORED)
def test_the_deck_section_carries_the_deck_tech_and_the_gaps(library, slug):
    guide = guide_for(library, slug)
    plan = plan_of(guide)
    card = render_matchup_card(guide, own_decklist(guide))
    for tech in plan["deck_tech"]:
        assert tech["name"] in card.text, f"deck_tech row {tech['name']!r} missing"
    for gap in plan["gaps"]:
        assert gap["matchup"] in card.text, f"gap {gap['matchup']!r} missing"


def test_the_deck_section_carries_a_generic_keep_rule(library):
    """The keep rule the agent uses before it knows the matchup - the modal
    keep across the plan's own matchups, not one matchup's keep passed off as
    universal."""
    guide = guide_for(library, "dimir-frog")
    card = render_matchup_card(guide, own_decklist(guide))
    assert "keep" in card.text.lower()
    assert (
        "Two lands with a blue source and either Harbinger of the Seas or two counters."
        in card.text
    )


# ---- tells table ----------------------------------------------------------


@pytest.mark.parametrize("slug", VENDORED)
def test_the_tells_table_has_one_row_per_matchup(library, slug):
    guide = guide_for(library, slug)
    plan = plan_of(guide)
    card = render_matchup_card(guide, own_decklist(guide))
    assert len(card.tells) == len(plan["matchups"])
    rows = [line for line in card.text.splitlines() if line.startswith("| `")]
    assert len(rows) == len(plan["matchups"])
    for tell in card.tells:
        assert f"`{tell.archetype}`" in card.text


@pytest.mark.parametrize("slug", VENDORED)
def test_every_tell_names_its_archetype_canonically(library, slug):
    """Canonical means the slug `matchups/<archetype>.json` and
    `vs-<archetype>.md` both use, so a note written after the game keys the
    same way the table named it."""
    guide = guide_for(library, slug)
    card = render_matchup_card(guide, own_decklist(guide))
    for tell in card.tells:
        assert tell.archetype == tell.archetype.lower()
        assert " " not in tell.archetype
        assert card.archetypes.count(tell.archetype) == 1
    assert "unknown" in card.text, "the unclassifiable key must be named too"


@pytest.mark.parametrize("slug", VENDORED)
def test_the_tells_table_carries_the_signature_cards_of_the_named_archetype(library, slug):
    """The signature cards ARE the tell. Six to nine of the nineteen
    archetypes a plan lists have an `archetype.json` in the corpus; every one
    of those must contribute its own cards, not the piloted deck's."""
    guide = guide_for(library, slug)
    card = render_matchup_card(guide, own_decklist(guide))
    with_signature = [t for t in card.tells if t.signature]
    assert len(with_signature) >= 6
    by_archetype = {t.archetype: t for t in card.tells}
    # living-end's own archetype.json, read for the living-end row
    living = json.loads((FIXTURE_GUIDES / "decks" / "living-end" / "archetype.json").read_text())
    expected = living["signature"][0]["cards"]
    assert set(by_archetype["living-end"].signature) & set(expected)
    for name in by_archetype["living-end"].signature[:2]:
        assert name in card.text


def test_a_signature_is_resolved_through_the_archetype_files_own_name(library):
    """`decks/affinity/archetype.json` calls itself "pinnacle affinity", which
    is the name the boarding plan uses; keying only on the directory slug
    would silently lose it."""
    guide = guide_for(library, "dimir-frog")
    card = render_matchup_card(guide, own_decklist(guide))
    by_archetype = {t.archetype: t for t in card.tells}
    assert by_archetype["pinnacle-affinity"].signature
    assert by_archetype["broodscale-bloodchief"].signature


@pytest.mark.parametrize("slug", VENDORED)
def test_a_tells_row_never_exceeds_the_watch_for_cap(library, slug):
    guide = guide_for(library, slug)
    card = render_matchup_card(guide, own_decklist(guide))
    for tell in card.tells:
        assert len(tell.watch_for) <= WATCH_FOR_CAP
    # A cap assertion is satisfied by the empty string, which is exactly
    # what the renderer used to hand back at the default budget - so the
    # cap is only half the assertion, and the other half is that the field
    # is THERE.
    assert any(tell.watch_for for tell in card.tells)


@pytest.mark.parametrize("slug", VENDORED)
def test_every_tells_row_carries_its_watch_for_at_the_default_budget(library, slug):
    """Acceptance 1 puts that matchup's `play_plan.watch_for` on every row,
    and the default budget has the room for it: the largest rendering in the
    whole corpus is 11566 bytes against a 12288 byte hard cap (11724 as this
    suite measures it, from a fixture root whose longer absolute path the
    card prints twice).

    This is the assertion the suite was missing. The ladder used to stop at
    the first rung under the 10240 byte TARGET, and because the truncation
    notice costs ~350 bytes that full fidelity does not pay, every rung that
    merely SHORTENS the prose renders larger than full fidelity - so the
    first rung that could ever fit was the one that deletes the column
    outright. Eleven of the corpus's thirteen guides rendered with no
    `watch for` column at all and `Tell.watch_for == ""` on every row, with
    ~2 KB of the cap unspent.
    """
    guide = guide_for(library, slug)
    plan = plan_of(guide)
    card = render_matchup_card(guide, own_decklist(guide))
    assert card.byte_size <= CARD_HARD_CAP_BYTES
    assert "| watch for |" in card.text, f"{slug} rendered with no `watch for` column"
    written = {
        str(m["name"]): str((m.get("play_plan") or {}).get("watch_for") or "").strip()
        for m in plan["matchups"]
    }
    written = {name: text for name, text in written.items() if text}
    assert len(written) >= 10, "this plan has almost no watch_for text; assert nothing"
    by_display = {tell.display: tell for tell in card.tells}
    for name, text in written.items():
        rendered = by_display[name].watch_for
        assert rendered, f"{slug}: {name} lost its watch_for entirely"
        assert text.startswith(rendered.removesuffix("...")), (
            f"{slug}: {name}'s watch_for is not the plan's text"
        )
    # Not merely present but unabridged: the cap has room for all of it.
    assert not card.truncated
    assert "truncated" not in card.text.lower()


@pytest.mark.parametrize("slug", VENDORED)
def test_the_cap_is_spent_before_a_required_field_is_deleted(library, slug):
    """The TARGET is what the card aims at; the CAP is the line a field
    acceptance 1 requires may not be deleted this side of.

    So between the two there is headroom, and the renderer has to spend it:
    a rendering that keeps every required field and is over the target is
    better than one that fits the target by deleting a column off nineteen
    rows. Asserted over a range of caps rather than just the default,
    because the rule is about the relationship between the two numbers, not
    about the corpus happening to fit one of them.
    """
    guide = guide_for(library, slug)
    decklist = own_decklist(guide)
    plan = load_boarding_plan(guide)
    index = signature_index(guide)
    keeps = [trim for trim in _TRIMS if trim.watch_column and trim.top_three]
    assert keeps

    def at(trim) -> int:
        return len(_rendering(guide, plan, decklist, index, trim).text.encode())

    for cap in (CARD_HARD_CAP_BYTES, 12_000, 11 * 1024, 10 * 1024, 8 * 1024):
        card = render_matchup_card(guide, decklist, target_bytes=cap - 2048, hard_cap_bytes=cap)
        assert card.byte_size <= cap
        if any(at(trim) <= cap for trim in keeps):
            assert "| watch for |" in card.text, (
                f"{slug} deleted the `watch for` column at a {cap} byte cap that a "
                "rendering keeping it fits"
            )
            assert "on the play" in card.text, (
                f"{slug} dropped section 3's detail at a {cap} byte cap that a "
                "rendering keeping it fits"
            )


def test_the_column_is_deleted_only_after_both_authorised_levers():
    """The brief names two levers and their order: the tells table's
    `watch_for` TEXT first, the top-three detail second. Deleting the column
    is not one of the two, so it comes after both of them.

    Pinned on the ladder itself because the failure it guards against is an
    ordering one. With the column's deletion ahead of the detail lever, the
    detail lever never fired anywhere on the corpus while the column was
    deleted on eleven of thirteen guides - the plan's first lever inverted
    with a lever the plan does not name.
    """
    shortens_prose = [
        i
        for i, t in enumerate(_TRIMS)
        if t.keeps_required_fields and t.detail == 0 and 0 < t.watch_cap < WATCH_FOR_CAP
    ]
    thins_detail = [i for i, t in enumerate(_TRIMS) if t.keeps_required_fields and t.detail]
    deletes_column = [i for i, t in enumerate(_TRIMS) if not t.watch_column]
    assert shortens_prose, "the brief's first lever must be on the ladder"
    assert thins_detail, "the brief's second lever must be on the ladder"
    assert deletes_column, "the ladder still needs a rung below both levers"
    assert max(shortens_prose) < min(thins_detail), "lever 1 must precede lever 2"
    assert max(thins_detail) < min(deletes_column), (
        "the `watch for` column is deleted before section 3 has thinned - which "
        "inverts the order the brief sets"
    )
    assert _TRIMS[0].full, "the ladder must start at full fidelity"


@pytest.mark.parametrize("slug", VENDORED)
def test_the_detail_lever_fires_while_the_column_is_still_there(library, slug):
    """The second lever has to be REACHABLE, not merely present on the
    ladder. Pressed just past what the table's prose alone can buy, the card
    thins section 3 and still carries the `watch for` column - and says both
    things in its notice.
    """
    guide = guide_for(library, slug)
    decklist = own_decklist(guide)
    plan = load_boarding_plan(guide)
    index = signature_index(guide)

    def at(trim) -> int:
        return len(_rendering(guide, plan, decklist, index, trim).text.encode())

    keeps = [trim for trim in _TRIMS if trim.watch_column and trim.top_three]
    whole = [trim for trim in keeps if trim.detail == 0]
    cap = min(at(trim) for trim in whole) - 1
    assert min(at(trim) for trim in keeps) <= cap, (
        "thinning the detail has to buy something, or the lever is inoperative"
    )
    card = render_matchup_card(guide, decklist, target_bytes=0, hard_cap_bytes=cap)
    assert card.byte_size <= cap
    assert "| watch for |" in card.text
    assert card.truncated
    assert "detail under the three biggest matchups" in card.text
    assert "on the play" in card.text


@pytest.mark.parametrize("slug", VENDORED)
def test_no_tells_row_breaks_the_markdown_table(library, slug):
    guide = guide_for(library, slug)
    card = render_matchup_card(guide, own_decklist(guide))
    rows = [line for line in card.text.splitlines() if line.startswith("| `")]
    widths = {line.count("|") for line in rows}
    assert len(widths) == 1, f"ragged table in {slug}: {widths}"


# ---- top three ------------------------------------------------------------


@pytest.mark.parametrize("slug", VENDORED)
def test_the_top_three_are_the_three_largest_by_meta_share(library, slug):
    guide = guide_for(library, slug)
    plan = plan_of(guide)
    expected = [
        m["name"] for m in sorted(plan["matchups"], key=lambda m: -(m.get("meta_share") or 0.0))[:3]
    ]
    card = render_matchup_card(guide, own_decklist(guide))
    assert [t.display for t in card.top_three] == expected


def test_the_top_three_detail_carries_every_named_field(library):
    guide = guide_for(library, "dimir-frog")
    plan = plan_of(guide)
    biggest = max(plan["matchups"], key=lambda m: m["meta_share"])
    card = render_matchup_card(guide, own_decklist(guide))
    detail = card.text.split("most of the field")[-1]
    assert biggest["stance"] in detail
    assert biggest["play_plan"]["goldfish_for"][:40] in detail
    assert biggest["play_plan"]["keep"][:40] in detail
    assert biggest["opponent_plan"]["posture"] in detail
    assert biggest["survives_opposing_board"][0]["card"] in detail
    assert biggest["stance_rationale"][:40] in detail
    assert "on the play" in detail and "on the draw" in detail


# ---- the swaps are intersected with the real list -------------------------


def test_a_swap_naming_a_card_the_list_lacks_is_marked_absent(library):
    """A boarding plan is written for a 75; the list the server dealt may not
    be that 75. Listing "3 Mystical Dispute" as a swap for a list with no
    Mystical Dispute in it is the kind of confident wrong instruction that
    costs a game. `esper blink` is dimir-frog's largest matchup by
    `meta_share` and boards three of them in."""
    guide = guide_for(library, "dimir-frog")
    trimmed = without(own_decklist(guide), "Mystical Dispute")
    card = render_matchup_card(guide, trimmed)
    assert "Mystical Dispute" in card.text
    assert "Mystical Dispute (NOT IN YOUR LIST)" in card.text
    assert card.missing_cards, "the absent cards must be reported, not only marked"
    assert "Mystical Dispute" in card.missing_cards


def test_the_swaps_are_intersected_against_the_whole_75_not_the_maindeck(library):
    """The classifier reads the MAINBOARD only - a sideboard card has not been
    cast and cannot say what the deck is. A boarding swap is the opposite
    question: every card a plan boards IN comes out of the sideboard by
    definition, so asking the maindeck stamps `(NOT IN YOUR LIST)` on every
    single board-in and turns the whole check into noise.

    dimir-frog is the case verbatim: `esper blink` is its largest matchup by
    `meta_share` and boards in three Mystical Dispute, and the list ships
    them in the sideboard - behind a bare blank line, no header.
    """
    guide = guide_for(library, "dimir-frog")
    dealt = own_decklist(guide)
    assert "Mystical Dispute" not in dealt.mainboard, "the premise"
    assert dealt.sideboard["Mystical Dispute"] == 3

    card = render_matchup_card(guide, dealt)
    assert "Mystical Dispute" in card.text
    assert card.missing_cards == []
    assert "(NOT IN YOUR LIST)" not in card.text

    # And the bug this pins, spelled out: the same list with its sideboard
    # taken away is what asking the maindeck alone amounts to.
    maindeck_only = render_matchup_card(guide, Decklist(mainboard=dealt.mainboard))
    assert "Mystical Dispute" in maindeck_only.missing_cards


def test_a_swap_boarding_out_a_maindeck_card_is_not_marked_either(library):
    """The other half of a swap. `out` names maindeck cards, so a check that
    only looked at the sideboard would be wrong in the mirror image of the
    bug above."""
    guide = guide_for(library, "dimir-frog")
    dealt = own_decklist(guide)
    card = render_matchup_card(guide, dealt)
    out_named = [
        name
        for matchup in load_boarding_plan(guide)["matchups"]
        for side in ("play", "draw")
        for entry in (matchup.get(side) or {}).get("out") or []
        for name in [" ".join(str(entry).split()[1:])]
    ]
    boarded_out_of_the_maindeck = [n for n in out_named if n in dealt.mainboard]
    assert boarded_out_of_the_maindeck, "the premise: the plan boards maindeck cards out"
    for name in boarded_out_of_the_maindeck:
        assert f"{name} (NOT IN YOUR LIST)" not in card.text
    assert card.missing_cards == []


def test_a_swap_naming_a_card_the_list_has_is_not_marked(library):
    guide = guide_for(library, "dimir-frog")
    card = render_matchup_card(guide, own_decklist(guide))
    assert "(NOT IN YOUR LIST)" not in card.text
    assert card.missing_cards == []


def test_a_split_card_the_list_holds_is_not_marked_absent(library):
    """boros-aggro's plan writes the swap as `1 Wear`; the list it is written
    for writes `Wear // Tear`, and the list the server actually deals
    (`decks/boros_aggro.txt`) writes `Wear/Tear`. One card, three spellings.

    It is the only deck in the whole corpus whose swaps name a split card, so
    a raw string intersection makes *every* firing of this feature on real
    data a false positive - a card the seat is holding, reported absent, with
    the footer telling the agent not to go looking for it.
    """
    guide = guide_for(library, "boros-aggro")
    card = render_matchup_card(guide, own_decklist(guide))
    assert "Wear" in card.text, "the swap itself must still be rendered"
    assert card.missing_cards == []
    assert "(NOT IN YOUR LIST)" not in card.text
    assert card.byte_size <= CARD_HARD_CAP_BYTES


@pytest.mark.parametrize("spelling", ["Wear // Tear", "Wear/Tear", "Wear"])
def test_a_swap_matches_a_split_card_however_the_list_spells_it(library, spelling):
    guide = guide_for(library, "boros-aggro")
    stripped = without(own_decklist(guide), "Wear")
    decklist = Decklist(mainboard=stripped.mainboard, sideboard={**stripped.sideboard, spelling: 2})
    card = render_matchup_card(guide, decklist)
    assert card.missing_cards == [], f"{spelling!r} in the list, swap still reported absent"


def test_a_swap_naming_a_split_card_no_face_of_which_is_in_the_list_is_marked(library):
    """The normalisation must not swallow the real case it exists to protect:
    a list holding neither face still gets the warning."""
    guide = guide_for(library, "boros-aggro")
    card = render_matchup_card(guide, without(own_decklist(guide), "Wear"))
    assert card.missing_cards == ["Wear"]
    assert "Wear (NOT IN YOUR LIST)" in card.text


def test_a_swap_naming_the_back_face_matches_the_card_the_list_holds(tmp_path):
    """A plan is written by hand and may name either half. The seat holding
    `Wear // Tear` can cast Tear, so a swap that says `1 Tear` names a card
    it has."""
    root = tmp_path / "guides" / "decks" / "brewed"
    d3 = root / "analysis" / "r1" / "domain3-matchup-threat"
    d3.mkdir(parents=True)
    (root / "decklist.txt").write_text("2 Wear // Tear\n")
    (d3 / "boarding-plan.json").write_text(
        json.dumps(
            {
                "matchups": [
                    {
                        "name": "boros energy",
                        "meta_share": 5.0,
                        "stance": "race",
                        "play_plan": {"keep": "two lands", "posture": "aggro"},
                        "play": {"in": ["1 Tear"], "out": ["1 Wear"]},
                        "draw": {"in": ["1 Wear/Tear"], "out": ["1 Ancient Grudge"]},
                    }
                ],
                "deck_tech": [],
                "gaps": [],
            }
        )
    )
    guide = GuideLibrary(tmp_path / "guides").get("brewed")
    card = render_matchup_card(guide, parse_decklist(guide.decklist.read_text()))
    assert card.missing_cards == ["Ancient Grudge"]
    assert "Tear (NOT IN YOUR LIST)" not in card.text


# ---- truncation -----------------------------------------------------------


def test_a_card_squeezed_under_a_small_budget_says_it_was_truncated(library):
    guide = guide_for(library, "orzhov-martyr")
    card = render_matchup_card(guide, own_decklist(guide), target_bytes=4000, hard_cap_bytes=5000)
    assert card.truncated
    assert "truncated" in card.text.lower()
    assert card.byte_size <= 5000
    # never silently dropped: the tells are all still there, just shorter
    assert len(card.tells) == len(plan_of(guide)["matchups"])


def test_a_card_that_fits_does_not_claim_truncation(library):
    """Full fidelity for these plans is 10-11.6 KB - over the 10 KB target
    and under the 12 KB hard cap, which is the whole reason the ladder
    spends the cap rather than deleting a required field to reach the
    target. Given room, no card claims a truncation it did not perform."""
    guide = guide_for(library, "dimir-frog")
    card = render_matchup_card(
        guide, own_decklist(guide), target_bytes=64_000, hard_cap_bytes=64_000
    )
    assert not card.truncated
    assert "truncated" not in card.text.lower()
    assert card.byte_size > CARD_TARGET_BYTES, "otherwise this asserts nothing"


@pytest.mark.parametrize("slug", VENDORED)
def test_truncation_shortens_watch_for_before_the_top_three_detail(library, slug):
    """The stated trim order: the tells table's prose gives first, and the
    top-three detail is not touched until the table has given everything it
    is going to give.

    Pressed to exactly what squeezing the table to the ladder's floor
    delivers, the renderer must deliver it out of the table and leave
    section 3 byte-for-byte what full fidelity renders.

    Past that the invariant is the one the brief states rather than a
    particular rung: section 3 is thinned only when NO rendering that leaves
    it whole fits the budget. Asserting it that way rather than by arithmetic
    on rung sizes matters, because the notice a truncated rendering carries
    costs ~300 bytes of its own.

    The pressure comes from the CAP, not the target: full fidelity gets the
    whole cap, so a card only starts down the ladder once the cap itself
    cannot hold the whole thing.
    """
    guide = guide_for(library, slug)
    decklist = own_decklist(guide)
    plan = load_boarding_plan(guide)
    index = signature_index(guide)
    full = render_matchup_card(guide, decklist, target_bytes=64_000, hard_cap_bytes=64_000)
    assert not full.truncated

    def at(trim) -> int:
        return len(_rendering(guide, plan, decklist, index, trim).text.encode())

    #: Every rung that leaves section 3 byte-for-byte whole AND keeps every
    #: field the brief requires - the watch ladder. The rung that deletes
    #: the `watch for` column leaves section 3 whole too, but it is not a
    #: rendering this invariant is allowed to prefer: it buys section 3 by
    #: deleting a field off nineteen rows.
    whole = [trim for trim in _TRIMS if trim.keeps_required_fields and trim.detail == 0]
    floor_size = at(whole[-1])
    cap = min(floor_size, full.byte_size - 1)
    squeezed = render_matchup_card(guide, decklist, target_bytes=0, hard_cap_bytes=cap)
    assert squeezed.truncated
    assert squeezed.byte_size <= cap
    section_3 = squeezed.text.split("### 3.")[-1]
    watch_squeezed = sum(len(t.watch_for) for t in squeezed.tells)
    watch_full = sum(len(t.watch_for) for t in full.tells)
    if floor_size < full.byte_size:
        assert watch_squeezed < watch_full, "the table had slack and did not give it"
        assert section_3 == full.text.split("### 3.")[-1]
    else:
        assert section_3 != full.text.split("### 3.")[-1], "something had to give"
        assert all(at(trim) > cap for trim in whole), (
            "section 3 gave while a rendering that leaves it whole still fit"
        )
        assert watch_squeezed <= watch_full
    assert "on the play" in squeezed.text


@pytest.mark.parametrize("slug", VENDORED)
def test_the_deck_section_is_not_a_truncation_lever(library, slug):
    """Two levers are authorised, and the notice names both: the tells
    table's `watch for` first, the top-three detail second. Section 1 is not
    one of them - it renders byte-for-byte the same at every rung - because
    a card whose notice says section 3 gave while section 1 quietly gave too
    is a card that lies about what it dropped.
    """
    guide = guide_for(library, slug)
    decklist = own_decklist(guide)
    target, cap = SQUEEZED_BUDGET
    full = render_matchup_card(guide, decklist, target_bytes=64_000, hard_cap_bytes=64_000)
    squeezed = render_matchup_card(guide, decklist, target_bytes=target, hard_cap_bytes=cap)
    assert squeezed.truncated, "this budget does not fit these plans; that is the premise"
    assert deck_section(squeezed.text) == deck_section(full.text)


@pytest.mark.parametrize("slug", VENDORED)
def test_the_signature_cards_are_not_a_truncation_lever_either(library, slug):
    """The other half of the same rule. `watch for` is the tells table's only
    trim column - the brief says so by name - so a squeezed card still names
    every signature card a full one does. The signature cards ARE the tell;
    dropping the fourth one off nineteen rows to buy 300 bytes, without
    saying so, takes away the thing section 2 exists for.
    """
    guide = guide_for(library, slug)
    decklist = own_decklist(guide)
    target, cap = SQUEEZED_BUDGET
    full = render_matchup_card(guide, decklist, target_bytes=64_000, hard_cap_bytes=64_000)
    squeezed = render_matchup_card(guide, decklist, target_bytes=target, hard_cap_bytes=cap)
    assert squeezed.truncated
    assert signature_column(squeezed.text) == signature_column(full.text)


def test_an_absurd_budget_still_produces_something_under_it(library):
    guide = guide_for(library, "orzhov-martyr")
    card = render_matchup_card(guide, own_decklist(guide), target_bytes=800, hard_cap_bytes=1200)
    assert card.byte_size <= 1200
    assert card.truncated
    assert "truncated" in card.text.lower()


# ---- a squeezed budget is still an honest one -----------------------------
#
# `SQUEEZED_BUDGET`, defined at the top of this file, is half the target with
# two thirds of the cap. No driver caller hands the card that today - a
# briefing renders one whole card per guide at the module's own budget - but
# the two numbers are arguments, and what the card does when they are tight
# is the whole subject of the trim ladder. The history is worth keeping:
# `render_current_game` once divided both numbers between a family's branches
# and every real corpus plan came out of it maimed, first by the byte cut
# (tells rows, the brew prose, the matchup-file read and all of section 3,
# with the notice still claiming nothing was dropped) and then, once the
# ladder had rungs for it, by losing the `watch for` column off every row.
# The renderer's behaviour under a tight budget is pinned here; that a
# briefing does not impose one is pinned in `test_prompt.py`.


@pytest.mark.parametrize("slug", VENDORED)
def test_a_squeezed_budget_still_gives_every_archetype_a_row(library, slug):
    """The tells table is the section a squeezed card least gets to lose: the
    rows are the thing the agent identifies the field with."""
    guide = guide_for(library, slug)
    target, cap = SQUEEZED_BUDGET
    card = render_matchup_card(guide, own_decklist(guide), target_bytes=target, hard_cap_bytes=cap)
    assert card.byte_size <= cap
    assert len(rows_in(card.text)) == len(plan_of(guide)["matchups"])
    assert card.omitted == [], "no archetype should have to lose its row at this budget"
    assert "cut here" not in card.text, "the byte cut is not a trim lever"


@pytest.mark.parametrize("slug", VENDORED)
def test_a_squeezed_budget_keeps_the_brew_case_and_the_one_small_read(library, slug):
    """Acceptance 4 and 5 are prose, and prose is exactly what a byte cut at
    the end of the card takes away: both live at the foot of section 2."""
    guide = guide_for(library, slug)
    target, cap = SQUEEZED_BUDGET
    card = render_matchup_card(guide, own_decklist(guide), target_bytes=target, hard_cap_bytes=cap)
    lowered = card.text.lower()
    assert "none of the rows" in lowered or "matches none" in lowered
    assert "no matchup file" in lowered
    assert "`unknown`" in card.text
    assert str(guide.matchups_dir) in card.text
    assert "### 1." in card.text and "### 2." in card.text and "### 3." in card.text


@pytest.mark.parametrize("slug", VENDORED)
def test_a_dropped_section_is_named_rather_than_denied(library, slug):
    """The notice is the card's only account of what it did to itself, so it
    has to match: a card that dropped a column and a section must not go on
    claiming that nothing was dropped."""
    guide = guide_for(library, slug)
    target, cap = SQUEEZED_BUDGET
    card = render_matchup_card(guide, own_decklist(guide), target_bytes=target, hard_cap_bytes=cap)
    assert card.truncated
    assert "Nothing was dropped" not in card.text
    notice = card.text.split("\n\n")[0]
    assert "watch for" in notice, "the column it dropped goes unnamed"
    assert "section 3" in notice, "the section it dropped goes unnamed"
    section_3 = card.text.split("### 3.")[-1]
    for tell in card.top_three:
        assert f"`{tell.archetype}`" in section_3, "the three biggest are not even named"
    assert "matchup file" in section_3.lower(), "nothing says where the detail went"


@pytest.mark.parametrize("slug", VENDORED)
@pytest.mark.parametrize("cap", [12 * 1024, 10 * 1024, 8 * 1024, 7 * 1024, 6 * 1024, 5 * 1024])
def test_every_budget_is_honest_about_what_it_dropped(library, slug, cap):
    """One invariant over the whole range of budgets a caller can ask for:
    the card fits, every archetype the plan covers is still NAMED in it, and
    the notice's account of what gave is true."""
    guide = guide_for(library, slug)
    card = render_matchup_card(
        guide, own_decklist(guide), target_bytes=cap - 1024, hard_cap_bytes=cap
    )
    assert card.byte_size <= cap
    rows = rows_in(card.text)
    if "cut here" not in card.text:
        for tell in card.tells:
            assert f"`{tell.archetype}`" in card.text, f"{tell.archetype} vanished at {cap} bytes"
        assert len(rows) == len(card.tells) - len(card.omitted)
    if "Nothing was dropped" in card.text:
        assert not card.omitted
        assert len(rows) == len(card.tells)
        assert "on the play" in card.text, "section 3 went and the notice denied it"
        assert "cut here" not in card.text


def test_rows_that_do_not_fit_are_named_rather_than_vanishing(library):
    """Below every trim lever there is one thing left to give: a row. It is
    given from the bottom of the field up, and the archetypes that lost
    theirs are named where the rows were."""
    guide = guide_for(library, "orzhov-martyr")
    decklist = own_decklist(guide)
    # The last rung of the ladder, rendered directly: a budget of 0 no
    # longer gets it, because full fidelity gets the whole cap and 64 KB of
    # cap holds it comfortably.
    smallest = _rendering(
        guide,
        load_boarding_plan(guide),
        decklist,
        signature_index(guide),
        _TRIMS[-1],
    )
    cap = len(smallest.text.encode()) - 600
    card = render_matchup_card(guide, decklist, target_bytes=0, hard_cap_bytes=cap)
    assert card.byte_size <= cap
    assert card.omitted, "something has to give, and at this budget it is rows"
    assert "cut here" not in card.text, "rows give before the byte cut does"
    assert len(rows_in(card.text)) == len(card.tells) - len(card.omitted)
    for tell in card.omitted:
        assert f"`{tell.archetype}`" in card.text
    assert min(t.meta_share for t in card.tells if t not in card.omitted) >= max(
        t.meta_share for t in card.omitted
    ), "the rows given are the smallest slices of the field"
    assert "Nothing was dropped" not in card.text


def test_the_byte_cut_of_last_resort_says_what_is_missing(library):
    """A budget no rendering can serve still gets cut, and still says so -
    but it must not carry the notice of a card that only shortened prose."""
    guide = guide_for(library, "orzhov-martyr")
    card = render_matchup_card(guide, own_decklist(guide), target_bytes=800, hard_cap_bytes=1200)
    assert card.byte_size <= 1200
    assert card.truncated
    assert "Nothing was dropped" not in card.text
    assert "cut" in card.text.lower()


# ---- degrading ------------------------------------------------------------


def test_a_guide_with_no_boarding_plan_degrades_without_raising(tmp_path):
    root = tmp_path / "guides" / "decks" / "brewed"
    root.mkdir(parents=True)
    (root / "decklist.txt").write_text("4 Lightning Bolt\n20 Mountain\n")
    guide = GuideLibrary(tmp_path / "guides").get("brewed")
    assert guide is not None and guide.boarding_plan is None
    card = render_matchup_card(guide, parse_decklist(guide.decklist.read_text()))
    assert card.tells == []
    assert card.top_three == []
    assert not card.truncated
    assert "no boarding plan" in card.text.lower()
    assert "unknown" in card.text


def test_a_malformed_boarding_plan_degrades_without_raising(tmp_path):
    root = tmp_path / "guides" / "decks" / "brewed"
    d3 = root / "analysis" / "r1" / "domain3-matchup-threat"
    d3.mkdir(parents=True)
    (root / "decklist.txt").write_text("4 Lightning Bolt\n")
    (d3 / "boarding-plan.json").write_text("{ not json at all")
    guide = GuideLibrary(tmp_path / "guides").get("brewed")
    card = render_matchup_card(guide, parse_decklist(guide.decklist.read_text()))
    assert card.tells == []
    assert "no boarding plan" in card.text.lower()


def test_a_plan_with_no_matchups_still_renders_the_deck_section(tmp_path):
    root = tmp_path / "guides" / "decks" / "brewed"
    d3 = root / "analysis" / "r1" / "domain3-matchup-threat"
    d3.mkdir(parents=True)
    (root / "decklist.txt").write_text("4 Lightning Bolt\n")
    (d3 / "boarding-plan.json").write_text(
        json.dumps(
            {
                "matchups": [],
                "deck_tech": [{"kind": "loop", "name": "Bolt them", "cards": [], "lines": []}],
                "gaps": [],
            }
        )
    )
    guide = GuideLibrary(tmp_path / "guides").get("brewed")
    card = render_matchup_card(guide, parse_decklist(guide.decklist.read_text()))
    assert "Bolt them" in card.text
    assert card.tells == []


@pytest.mark.parametrize("slug", VENDORED)
def test_the_card_says_what_to_do_when_nothing_in_the_table_matches(library, slug):
    """A human opponent may bring a brew no guide covers. The honest answer
    is the deck section, and a note keyed on `unknown`."""
    guide = guide_for(library, slug)
    card = render_matchup_card(guide, own_decklist(guide))
    lowered = card.text.lower()
    assert "none of the rows" in lowered or "matches none" in lowered
    assert "no matchup file" in lowered
    assert "`unknown`" in card.text


@pytest.mark.parametrize("slug", VENDORED)
def test_the_card_names_the_one_small_read_once_the_opponent_shows_cards(library, slug):
    guide = guide_for(library, slug)
    card = render_matchup_card(guide, own_decklist(guide))
    assert str(guide.matchups_dir) in card.text
    assert "<archetype>.json" in card.text


@pytest.mark.parametrize("slug", VENDORED)
def test_the_card_never_names_the_opponents_actual_deck(library, slug):
    """The owner keeps the opponent's archetype hidden: the card is about the
    agent's own deck plus what to look for, never a known opponent."""
    guide = guide_for(library, slug)
    card = render_matchup_card(guide, own_decklist(guide))
    lowered = card.text.lower()
    assert "your opponent is playing" not in lowered
    assert "you are facing" not in lowered
