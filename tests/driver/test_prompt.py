import json
from dataclasses import replace
from pathlib import Path

import pytest

from mtg_agent.driver.guides import (
    CONFIDENT,
    LOW,
    NONE,
    ArchetypeSignature,
    Candidate,
    DeckGuide,
    Decklist,
    GuideLibrary,
    Identification,
    SignatureClause,
    parse_decklist,
    slugify,
)
from mtg_agent.driver.matchup_card import CARD_HARD_CAP_BYTES
from mtg_agent.driver.prompt import (
    HANDOVER_OPENING,
    Assignment,
    advisor_turn_zero,
    handover_nudge,
    handover_turn,
    memory_index,
    plan_path,
    reflection_turn,
    render_current_game,
    turn_zero,
)


@pytest.fixture
def guide(tmp_path) -> DeckGuide:
    root = tmp_path / "guides" / "decks" / "dimir-frog"
    matchups = root / "analysis" / "r1" / "domain3-matchup-threat" / "matchups"
    matchups.mkdir(parents=True)
    (root / "decklist.txt").write_text("4 Psychic Frog\n")
    (root / "GAMEPLAN_GUIDE.md").write_text("# gameplan\n")
    (root / "SIDEBOARD_GUIDE.md").write_text("# sideboard\n")
    (matchups / "boros-energy.json").write_text("{}")
    (matchups / "living-end.json").write_text("{}")
    return DeckGuide(
        slug="dimir-frog",
        root=root,
        decklist=root / "decklist.txt",
        gameplan=root / "GAMEPLAN_GUIDE.md",
        sideboard=root / "SIDEBOARD_GUIDE.md",
        matchups_dir=matchups,
    )


DECKLIST = "4 Psychic Frog\n4 Fatal Push\n\nSideboard\n2 Dress Down\n"


def candidate(guide: DeckGuide, name: str, clauses: int, matched: int) -> Candidate:
    """A `Candidate` with `clauses` clauses of which `matched` are satisfied -
    the shape `GuideLibrary.identify` builds, without needing a real corpus
    on disk to build it from."""
    all_clauses = tuple(SignatureClause((f"Card {i}",), 4, f"clause {i}") for i in range(clauses))
    return Candidate(guide, ArchetypeSignature(name, all_clauses), all_clauses[:matched])


def identified(guide: DeckGuide | None, confidence: str = CONFIDENT, **kwargs) -> Identification:
    if guide is None and confidence == NONE:
        return Identification(NONE)
    candidates = kwargs.pop("candidates", None)
    if candidates is None:
        clauses = 3
        matched = clauses if confidence == CONFIDENT else 2
        candidates = (candidate(guide, "dimir frog", clauses, matched),)
    return Identification(
        confidence,
        guide,
        tuple(candidates),
        kwargs.pop("archetype", candidates[0].archetype if candidates else None),
    )


def assignment(guide: DeckGuide | None, confidence: str = CONFIDENT, **kwargs) -> Assignment:
    identification = kwargs.pop("identification", None)
    if identification is None:
        identification = identified(guide, NONE if guide is None else confidence, **kwargs)
    return Assignment(
        game_id="g-1",
        uid="secret-uid",
        player="bob",
        opponent="alice",
        api_base="http://game:8000",
        decklist=parse_decklist(DECKLIST),
        identification=identification,
    )


def test_current_game_states_the_seat_and_the_deck(tmp_path, guide):
    text = render_current_game(assignment(guide), tmp_path / "memory")
    assert "g-1" in text
    assert "secret-uid" in text
    assert "bob" in text
    assert "dimir-frog" in text
    assert "4 Psychic Frog" in text


def test_current_game_lists_the_guide_paths_not_their_contents(tmp_path, guide):
    text = render_current_game(assignment(guide), tmp_path / "memory")
    assert str(guide.gameplan) in text
    assert str(guide.sideboard) in text
    assert "# gameplan" not in text, "guides are ~500 lines; hand over paths"


def test_current_game_lists_the_known_opponent_archetypes(tmp_path, guide):
    text = render_current_game(assignment(guide), tmp_path / "memory")
    assert "boros-energy" in text
    assert "living-end" in text


def test_current_game_never_names_the_opponents_deck(tmp_path, guide):
    """Fair play: the agent deduces the matchup from cards it sees."""
    text = render_current_game(assignment(guide), tmp_path / "memory")
    lowered = text.lower()
    assert "opponent's deck" not in lowered
    assert "opponent's decklist" not in lowered
    assert "alice" in text, "the opponent's seat NAME is public at a real table"


# ---- one briefing per classification outcome -----------------------------
#
# Three outcomes, three different things to say. Getting these wrong is not
# cosmetic: a low-confidence match presented as a confident one is an agent
# playing a guide written for somebody else's deck and never questioning it.


def test_a_confident_match_states_the_deck_without_hedging(tmp_path, guide):
    text = render_current_game(assignment(guide, CONFIDENT), tmp_path / "memory")
    assert "You are piloting **dimir-frog**" in text
    assert "dimir frog" in text, "the archetype's own name for itself, not just the slug"
    assert "low-confidence" not in text
    assert "plausible" not in text


def test_a_low_confidence_match_says_so_and_names_the_other_candidate(tmp_path, guide):
    """`low` hands over the guide AND says which other archetype is
    plausible, so the agent knows the guide is a hypothesis."""
    other = DeckGuide(slug="instant-reanimator", root=tmp_path, decklist=tmp_path / "d.txt")
    text = render_current_game(
        assignment(
            guide,
            LOW,
            candidates=(
                candidate(guide, "dimir frog", clauses=4, matched=2),
                candidate(other, "instant reanimator", clauses=3, matched=2),
            ),
        ),
        tmp_path / "memory",
    )
    assert "probably piloting **dimir-frog**" in text
    assert "low-confidence" in text
    assert "2 of its 4 signature clauses" in text
    assert "Also plausible: `instant-reanimator`" in text
    assert str(guide.gameplan) in text, "low still hands over the guide"


def test_a_family_match_names_the_family_and_hands_over_both_branches(tmp_path):
    """The tron case: neither branch chosen, so neither branch's guide is
    presented as THE guide - both are offered under their own heading."""
    guides = []
    for slug in ("eldrazi-tron", "prison-tron"):
        root = tmp_path / slug
        root.mkdir()
        (root / "GAMEPLAN_GUIDE.md").write_text("# gameplan\n")
        guides.append(
            DeckGuide(
                slug=slug,
                root=root,
                decklist=root / "decklist.txt",
                gameplan=root / "GAMEPLAN_GUIDE.md",
            )
        )
    text = render_current_game(
        assignment(
            None,
            LOW,
            identification=Identification(
                LOW,
                None,
                tuple(candidate(g, g.slug, clauses=3, matched=2) for g in guides),
                "tron",
            ),
        ),
        tmp_path / "memory",
    )
    assert "some kind of **tron**" in text
    assert "`eldrazi-tron`" in text
    assert "`prison-tron`" in text
    for offered in guides:
        assert str(offered.gameplan) in text


def test_the_briefing_keeps_the_maindeck_and_the_sideboard_apart(tmp_path, guide):
    """The agent boards between games, so it has to be able to tell which
    fifteen cards are not in its library."""
    text = render_current_game(assignment(guide), tmp_path / "memory")
    assert "Maindeck:" in text
    assert "4 Psychic Frog" in text
    assert "Sideboard" in text
    assert "2 Dress Down" in text
    assert text.index("4 Psychic Frog") < text.index("2 Dress Down")


def test_current_game_says_so_when_there_is_no_guide(tmp_path):
    text = render_current_game(assignment(None), tmp_path / "memory")
    assert "no guide" in text.lower()
    assert "4 Psychic Frog" in text


def test_memory_index_finds_this_decks_notes_only(tmp_path):
    memory = tmp_path / "memory"
    (memory / "dimir-frog" / "games").mkdir(parents=True)
    (memory / "dimir-frog" / "vs-boros-energy.md").write_text("read")
    (memory / "dimir-frog" / "games" / "g-0.md").write_text("game")
    (memory / "living-end").mkdir()
    (memory / "living-end" / "vs-tron.md").write_text("other deck")
    notes = memory_index(memory, "dimir-frog")
    assert all(isinstance(note, Path) for note in notes)
    found = [p.name for p in notes]
    assert "vs-boros-energy.md" in found
    assert "g-0.md" in found
    assert "vs-tron.md" not in found


def test_memory_index_of_an_absent_directory_is_empty(tmp_path):
    assert memory_index(tmp_path / "nothing", "dimir-frog") == []


def test_current_game_lists_past_notes_when_they_exist(tmp_path, guide):
    memory = tmp_path / "memory"
    (memory / "dimir-frog").mkdir(parents=True)
    (memory / "dimir-frog" / "vs-boros-energy.md").write_text("they curve out")
    text = render_current_game(assignment(guide), memory)
    assert "vs-boros-energy.md" in text


def test_turn_zero_orders_reading_before_acting(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert str(tmp_path / "current-game.md") in text
    lowered = text.lower()
    assert lowered.index("read") < lowered.index("wait_for_priority")


def test_turn_zero_names_the_mcp_tools_not_seat_cli(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    for tool in (
        "wait_for_priority",
        "battlefield_state",
        "cast",
        "play_land",
        "decide",
        "pass_until",
        "declare_plan",
    ):
        assert tool in text
    assert "seat_cli" not in text


def test_turn_zero_carries_the_uid_and_the_hard_won_rules_of_engagement(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert "secret-uid" in text
    assert "winner" in text
    assert "never state" in text.lower()


def test_turn_zero_says_that_declining_to_act_can_be_the_play(tmp_path, guide):
    """The seat used to dump its hand and pass; passing with a purpose is a play."""
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    lowered = text.lower()
    assert "not playing is a play" in lowered
    assert "117.3d" in text
    assert "beatdown" in lowered
    assert "passing with purpose is not stalling" in lowered


def test_current_game_names_the_running_plan_file(tmp_path, guide):
    memory = tmp_path / "memory"
    text = render_current_game(assignment(guide), memory)
    assert str(plan_path(memory, assignment(guide))) in text
    assert str(memory / "dimir-frog" / "games" / "g-1.md") in text


def test_turn_zero_tells_it_to_write_the_plan_down(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert "plan file" in text.lower()


def test_turn_zero_tells_it_to_declare_a_plan_each_own_turn(tmp_path, guide):
    """The per-turn plan file rewrite is gone; `declare_plan` replaces it,
    called once at the top of each of the agent's own turns."""
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    lowered = text.lower()
    assert "declare_plan" in text
    assert "top of every one of your own turns" in lowered
    # the three facts that make the prediction evidence rather than
    # commentary, per the review this whole seam exists for:
    assert "before your opponent" in lowered
    assert "append-only ledger" in lowered
    assert "next to what your opponent actually did" in lowered
    assert "not scored" in lowered or "no grade" in lowered
    assert "honest wrong read" in lowered


def test_turn_zero_no_longer_asks_for_a_per_turn_plan_rewrite(tmp_path, guide):
    """`declare_plan` is what pays for removing this - a `Write` round trip
    of a whole file body, every turn, is gone. Leaving both in place would
    have made the new tool call pure overhead rather than a replacement."""
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    lowered = text.lower()
    assert "update it as the game moves" not in lowered
    assert "rewrite lines rather than appending" not in lowered
    assert "keep a plan, in writing" not in lowered


def test_turn_zero_tells_it_to_deduce_the_matchup(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert "deduce" in text.lower() or "identify" in text.lower()
    assert "matchups" in text


def test_reflection_turn_names_the_files_to_write(tmp_path, guide):
    memory = tmp_path / "memory"
    text = reflection_turn(assignment(guide), memory, outcome="bob won")
    assert str(memory / "dimir-frog" / "games" / "g-1.md") in text
    assert "vs-<archetype>.md" in text
    assert str(memory / "MEMORY.md") in text
    assert "bob won" in text


def test_reflection_turn_for_an_unguided_deck_still_writes_notes(tmp_path):
    memory = tmp_path / "memory"
    text = reflection_turn(assignment(None), memory, outcome="alice won")
    assert str(memory / "unknown-deck" / "games" / "g-1.md") in text


def test_turn_zero_inlines_the_briefing_when_given_one(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md", briefing="# This game\nBODY")
    assert "BODY" in text


def test_turn_zero_without_a_briefing_still_points_at_the_file(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert str(tmp_path / "current-game.md") in text
    assert "BODY" not in text


# ---- P2: the brief is the card, not a 58 KB read --------------------------
#
# An agent seat used to be handed its guide PATHS and told to read them,
# which meant a 42-73 KB markdown `Read` (12-18k tokens) before its first
# action - while the structured plan those guides are generated from sat
# unread beside them. The briefing now inlines that plan as a matchup card,
# and inlines the agent's own standing notes with it. The paths stay for a
# deliberate look-up of one specific line.

FIXTURE_GUIDES = Path(__file__).resolve().parents[1] / "fixtures" / "guides"


@pytest.fixture
def real_guide() -> DeckGuide:
    """A real guide out of the vendored corpus - boarding plan, archetype
    files and all. See `tests/fixtures/guides/README.md`."""
    guide = GuideLibrary(FIXTURE_GUIDES).get("dimir-frog")
    assert guide is not None and guide.boarding_plan is not None
    return guide


def test_current_game_inlines_the_matchup_card(tmp_path, real_guide):
    text = render_current_game(assignment(real_guide), tmp_path / "memory")
    assert "Matchup card" in text
    # section 1, the deck's own plan
    assert "Psychic Frog into Quantum Riddler" in text
    # section 2, the tells table with another archetype's signature cards
    assert "signature cards" in text
    assert "`living-end`" in text
    # section 3, the three biggest matchups, swaps included
    assert "on the play" in text


def test_current_game_still_lists_the_guide_paths_for_a_deliberate_read(tmp_path, real_guide):
    """The card is the brief; the guide is still there for one specific line
    the card does not carry."""
    text = render_current_game(assignment(real_guide), tmp_path / "memory")
    assert str(real_guide.boarding_plan) in text
    assert str(real_guide.matchups_dir) in text


def test_current_game_names_the_one_small_read_for_the_deduced_matchup(tmp_path, real_guide):
    text = render_current_game(assignment(real_guide), tmp_path / "memory")
    assert "<archetype>.json" in text
    assert "unknown" in text


def test_current_game_for_a_guide_with_no_boarding_plan_says_so(tmp_path, guide):
    """The fixture `guide` has matchup files but no boarding plan - a guided
    deck that predates the analysis pipeline. It must not raise, and it must
    not pretend to a plan it has not got."""
    text = render_current_game(assignment(guide), tmp_path / "memory")
    assert "no boarding plan" in text.lower()
    assert "4 Psychic Frog" in text


def test_current_game_inlines_the_standing_notes_rather_than_their_paths(tmp_path, real_guide):
    memory = tmp_path / "memory"
    (memory / "dimir-frog").mkdir(parents=True)
    (memory / "dimir-frog" / "vs-living-end.md").write_text(
        "Their cascade trigger is the only window; hold Consign to Memory for it."
    )
    text = render_current_game(assignment(real_guide), memory)
    assert "vs-living-end.md" in text
    assert "hold Consign to Memory for it" in text


def test_current_game_puts_the_deduced_matchups_note_first(tmp_path, real_guide):
    memory = tmp_path / "memory"
    (memory / "dimir-frog").mkdir(parents=True)
    (memory / "dimir-frog" / "vs-amulet-titan.md").write_text("AMULET NOTE")
    (memory / "dimir-frog" / "vs-living-end.md").write_text("LIVING END NOTE")
    text = render_current_game(assignment(real_guide), memory, opponent_archetype="living end")
    assert text.index("LIVING END NOTE") < text.index("AMULET NOTE")


def test_current_game_caps_what_it_inlines_from_the_notes(tmp_path, real_guide):
    """A note the agent has been revising for twenty games can be any size.
    Inlining all of it would undo the saving the card just made."""
    memory = tmp_path / "memory"
    (memory / "dimir-frog").mkdir(parents=True)
    (memory / "dimir-frog" / "vs-living-end.md").write_text("x" * 40_000)
    text = render_current_game(assignment(real_guide), memory)
    assert len(text.encode()) < 20_000
    assert "vs-living-end.md" in text


def test_current_game_still_lists_the_game_notes_as_paths(tmp_path, real_guide):
    """Only the `vs-<archetype>.md` standing reads are inlined. A past game's
    write-up is a path: there can be dozens of them."""
    memory = tmp_path / "memory"
    (memory / "dimir-frog" / "games").mkdir(parents=True)
    (memory / "dimir-frog" / "games" / "g-0.md").write_text("PAST GAME BODY")
    text = render_current_game(assignment(real_guide), memory)
    assert "g-0.md" in text
    assert "PAST GAME BODY" not in text


def test_turn_zero_no_longer_requires_a_gameplan_guide_read(tmp_path, real_guide):
    """The instruction this replaces was "Read your deck's gameplan guide" -
    a 58 KB `Read` before the first action, every game."""
    briefing = render_current_game(assignment(real_guide), tmp_path / "memory")
    text = turn_zero(assignment(real_guide), tmp_path / "current-game.md", briefing=briefing)
    assert "Read your deck's gameplan guide" not in text
    # prose wraps, so compare against the text with its line breaks folded
    flat = " ".join(text.lower().split())
    assert "matchup card" in flat
    assert "do not read the gameplan guide to get started" in flat
    assert "42-73 kb" in flat


def test_turn_zero_still_orders_the_brief_before_the_first_action(tmp_path, real_guide):
    briefing = render_current_game(assignment(real_guide), tmp_path / "memory")
    text = turn_zero(assignment(real_guide), tmp_path / "current-game.md", briefing=briefing)
    lowered = text.lower()
    assert lowered.index("before you take a single action") < lowered.index("wait_for_priority")
    assert "matchup card" in lowered


def test_turn_zero_points_at_the_one_small_read_once_the_opponent_shows_cards(tmp_path, real_guide):
    briefing = render_current_game(assignment(real_guide), tmp_path / "memory")
    text = turn_zero(assignment(real_guide), tmp_path / "current-game.md", briefing=briefing)
    assert "<archetype>.json" in text


def test_advisor_turn_zero_no_longer_requires_a_gameplan_guide_read(tmp_path, real_guide):
    """Mirrors `test_turn_zero_no_longer_requires_a_gameplan_guide_read` for the
    buddy: the advisor seat is the most inference-hungry one in the system (it
    thinks at every decision point its human holds), and it was still being
    sent to read a 42-73 KB gameplan guide that the briefing already inlines
    as a matchup card."""
    briefing = render_current_game(assignment(real_guide), tmp_path / "memory")
    text = advisor_turn_zero(
        assignment(real_guide), tmp_path / "current-game.md", briefing=briefing
    )
    assert "Read their deck's gameplan guide" not in text
    flat = " ".join(text.lower().split())
    assert "matchup card" in flat
    assert "do not read the gameplan guide to get started" in flat


def test_advisor_turn_zero_brief_carries_the_inlined_matchup_card(tmp_path, real_guide):
    """The buddy's brief must carry the same inlined card the opponent seat
    gets - it is advising on the human's own deck, so the card is rendered
    from that deck's boarding plan, same as the seat's."""
    briefing = render_current_game(assignment(real_guide), tmp_path / "memory")
    text = advisor_turn_zero(
        assignment(real_guide), tmp_path / "current-game.md", briefing=briefing
    )
    assert "Matchup card" in text
    assert "Psychic Frog into Quantum Riddler" in text


def test_reflection_turn_keys_the_note_on_a_canonical_archetype_or_unknown(tmp_path, guide):
    """The tells table, the note the reflection turn writes and any later
    pass over those notes have to key identically, or the pass finds
    nothing."""
    text = reflection_turn(assignment(guide), tmp_path / "memory", outcome="bob won")
    assert "vs-<archetype>.md" in text
    assert "tells table" in text
    assert "`unknown`" in text


# ---- P1 and P2 meet here -------------------------------------------------
#
# The classifier says WHICH deck the agent is holding and how sure it is; the
# matchup card says what that deck does and what to look for across the
# field. A briefing carrying one and not the other is half a brief, and the
# two were built on separate branches, so this is the seam worth pinning.


def _cards_in(text: str) -> list[str]:
    """Every inlined matchup card in a briefing, as its own string.

    A card's own headings are `###`/`####`; the only `##` after one is the
    guide-paths heading that follows it.
    """
    return [chunk.split("\n## ")[0] for chunk in text.split("## Matchup card - ")[1:]]


def test_the_briefing_carries_the_classifier_claim_and_the_card(tmp_path, real_guide):
    text = render_current_game(assignment(real_guide, CONFIDENT), tmp_path / "memory")
    assert "You are piloting **dimir-frog**" in text
    assert "dimir frog" in text, "the archetype's own name for itself"
    assert "## Matchup card - dimir-frog" in text
    assert "matchup card below" in text, "the claim has to point at the card, not at a read"
    assert text.index("You are piloting") < text.index("## Matchup card"), (
        "which deck it is comes before what that deck does"
    )


def test_a_low_confidence_briefing_hedges_the_claim_and_still_inlines_the_card(
    tmp_path, real_guide
):
    """The card is rendered from the guide the classifier is only PROBABLY
    right about, so the hedge and the runner-up have to travel with it - a
    card presented without them reads as settled fact."""
    other = DeckGuide(slug="instant-reanimator", root=tmp_path, decklist=tmp_path / "d.txt")
    text = render_current_game(
        assignment(
            real_guide,
            LOW,
            candidates=(
                candidate(real_guide, "dimir frog", clauses=4, matched=2),
                candidate(other, "instant reanimator", clauses=3, matched=2),
            ),
        ),
        tmp_path / "memory",
    )
    assert "probably piloting **dimir-frog**" in text
    assert "low-confidence" in text
    assert "Also plausible: `instant-reanimator`" in text
    assert "## Matchup card - dimir-frog" in text
    assert len(_cards_in(text)) == 1
    assert "matchup card below" in text, "the hedge has to point at the card, not at a read"


def family_briefing(tmp_path: Path, slugs: tuple[str, ...]) -> tuple[list[DeckGuide], list[str]]:
    """A briefing for the family case, and the cards inlined in it.

    `LOW` with no branch chosen is what `GuideLibrary.identify` returns for a
    list holding only the clauses a family's branches share - the tron case
    it was built for, and a routine one now that pilots bring their own
    lists. Every leading branch's guide comes back in `Identification.guides`
    and `render_current_game` renders a card for each.
    """
    library = GuideLibrary(FIXTURE_GUIDES)
    branches = [library.get(slug) for slug in slugs]
    assert all(b is not None and b.boarding_plan is not None for b in branches)
    text = render_current_game(
        assignment(
            None,
            LOW,
            identification=Identification(
                LOW,
                None,
                tuple(candidate(b, b.slug, clauses=3, matched=2) for b in branches),
                "tron",
            ),
        ),
        tmp_path / "memory",
    )
    return branches, _cards_in(text)


def watch_for_column(card: str) -> dict[str, str]:
    """The tells table's `watch for` cell, keyed on the row's archetype - the
    last column, when the card renders one at all."""
    rows = [line.split("|") for line in card.splitlines() if line.startswith("| `")]
    return {row[1].strip().strip("`"): row[6].strip() for row in rows}


@pytest.mark.parametrize(
    "slugs",
    [
        ("prison-tron", "orzhov-martyr"),
        ("prison-tron", "orzhov-martyr", "dimir-frog"),
    ],
)
def test_a_family_briefing_keeps_the_watch_for_on_every_row_of_every_branch(tmp_path, slugs):
    """Acceptance 1 puts that matchup's `play_plan.watch_for` on every one of
    the fifteen to nineteen tells rows, and a briefing with two branches in it
    is still a briefing: the requirement is a property of the CARD, not of
    how many cards the classifier happened to be undecided between.

    This is the assertion the suite was missing, and the defect it was
    missing: the family path handed each branch `CARD_TARGET_BYTES // len(
    offered)` (5120) and a shared cap, which is below every rung that keeps
    the column, so every card in a family briefing rendered with no `watch
    for` column at all and `Tell.watch_for == ""` on every row - 34 rows
    between the two branches of the real corpus tron case - while each of
    those plans renders whole, column and all, under its own 12288 byte cap.
    The briefing-wide budget that forced it is one the brief never set.
    """
    branches, cards = family_briefing(tmp_path, slugs)
    assert len(cards) == len(branches), "one card per branch, neither dropped"
    for branch, card in zip(branches, cards, strict=True):
        assert len(card.encode()) <= CARD_HARD_CAP_BYTES, (
            f"{branch.slug} rendered over the per-card hard cap"
        )
        assert "| watch for |" in card, f"{branch.slug} rendered with no `watch for` column"
        matchups = json.loads(branch.boarding_plan.read_text())["matchups"]
        written = {
            slugify(str(m["name"])): " ".join(
                str((m.get("play_plan") or {}).get("watch_for") or "").split()
            )
            for m in matchups
        }
        written = {archetype: prose for archetype, prose in written.items() if prose}
        assert len(written) >= 10, "this plan has almost no watch_for text; assert nothing"
        cells = watch_for_column(card)
        assert len(cells) == len(matchups), f"{branch.slug}: a tells row went missing"
        for archetype, prose in written.items():
            cell = cells[archetype]
            assert cell and cell != "-", (
                f"{branch.slug}: {archetype} rendered an empty `watch for` cell"
            )
            assert prose.startswith(cell.removesuffix("...")), (
                f"{branch.slug}: {archetype}'s `watch for` is not the plan's text"
            )
        assert "**Truncated**" not in card, (
            f"{branch.slug} fits its own budget whole; nothing had to give"
        )


def test_a_family_briefing_inlines_an_honest_card_per_branch(tmp_path):
    """`LOW` with no branch chosen hands over every leading branch's guide,
    so it gets every branch's card, and each of those is a whole card.

    The budget is per card, and it stays per card however many branches the
    classifier could not separate. Dividing it between them - half the
    target, a shared cap - was a briefing-wide budget the brief never set,
    and what it bought came out of the fields the brief requires: first the
    byte cut (tells rows, the brew case, the matchup-file read and all of
    section 3, with each card's own notice still saying nothing had been
    dropped), and then, once the cut was fixed, the `watch for` column off
    every row of both cards. Two whole cards cost ~23 KB against the ~15 KB
    that bought, and they are still under a tenth of the two boarding plans
    and a fraction of the two gameplan guides they replace.
    """
    branches, cards = family_briefing(tmp_path, ("prison-tron", "orzhov-martyr"))
    text = render_current_game(
        assignment(
            None,
            LOW,
            identification=Identification(
                LOW,
                None,
                tuple(candidate(b, b.slug, clauses=3, matched=2) for b in branches),
                "tron",
            ),
        ),
        tmp_path / "memory",
    )
    assert "some kind of **tron**" in text
    assert "matchup card below for each branch" in text
    assert len(cards) == 2, "one card per branch, neither dropped"
    assert cards[0].startswith("prison-tron")
    assert cards[1].startswith("orzhov-martyr")
    assert all(len(c.encode()) <= CARD_HARD_CAP_BYTES for c in cards)
    assert sum(len(c.encode()) for c in cards) <= len(branches) * CARD_HARD_CAP_BYTES
    assert sum(len(c.encode()) for c in cards) * 4 < sum(
        b.boarding_plan.stat().st_size for b in branches
    ), "a card per branch still has to be a fraction of the plans it replaces"
    for branch, card in zip(branches, cards, strict=True):
        assert str(branch.gameplan or branch.boarding_plan) in text
        plan = json.loads(branch.boarding_plan.read_text())
        rows = [line for line in card.splitlines() if line.startswith("| `")]
        assert len(rows) == len(plan["matchups"]), "a branch's card lost tells rows"
        assert "no matchup file" in card.lower(), "the brew case went with them"
        assert str(branch.matchups_dir) in card, "and so did the one small read"
        assert "cut here" not in card, "the byte cut is not a trim lever"
        assert "**Truncated**" not in card, (
            "a branch's card fits its own budget whole; it has nothing to notice"
        )
        assert "### 1." in card and "### 2." in card and "### 3." in card
        assert "on the play" in card, "section 3 kept its detail, swaps and all"


def test_an_unguided_briefing_still_names_the_note_key(tmp_path):
    """The unclassifiable own-deck case. `NONE` has no guide, so no plan and
    no tells - and the agent still has to file what it learns under a key the
    next game's briefing looks up, which is `unknown`."""
    text = render_current_game(assignment(None), tmp_path / "memory")
    assert "no guide" in text.lower()
    assert "`unknown`" in text
    assert "4 Psychic Frog" in text


def test_the_briefing_intersects_the_swaps_against_the_whole_75(tmp_path):
    """End to end, through the briefing rather than the renderer: the seat is
    handed the 75 the server dealt, the classifier reads the maindeck of it,
    and the card's swaps are checked against both boards. Passing the
    maindeck alone down to the card would stamp `(NOT IN YOUR LIST)` on
    every card the plan boards in."""
    guide = GuideLibrary(FIXTURE_GUIDES).get("dimir-frog")
    dealt = parse_decklist(guide.decklist.read_text())
    assert "Mystical Dispute" not in dealt.mainboard and dealt.sideboard["Mystical Dispute"] == 3
    seat = Assignment(
        game_id="g-1",
        uid="secret-uid",
        player="bob",
        opponent="alice",
        api_base="http://game:8000",
        decklist=dealt,
        identification=identified(guide, CONFIDENT),
    )
    text = render_current_game(seat, tmp_path / "memory")
    assert "Mystical Dispute" in text
    assert "(NOT IN YOUR LIST)" not in text


def test_the_briefing_keeps_the_boards_apart_for_the_classifier_claim(tmp_path, real_guide):
    """Both halves of the same list, read two different ways in one
    briefing: the claim is about the maindeck, the swaps are about the 75."""
    dealt = Decklist(mainboard={"Psychic Frog": 4}, sideboard={"Dress Down": 2})
    seat = Assignment(
        game_id="g-1",
        uid="secret-uid",
        player="bob",
        opponent="alice",
        api_base="http://game:8000",
        decklist=dealt,
        identification=identified(real_guide, CONFIDENT),
    )
    text = render_current_game(seat, tmp_path / "memory")
    assert "Maindeck:" in text
    assert text.index("4 Psychic Frog") < text.index("2 Dress Down")
    assert "maindeck" in text.lower()


# ---- play scripts: the doctrine every agent is told ----------------------
#
# A6 gave a seat `run_script`/`list_scripts` and `mcp-seat script run|list`.
# A tool nobody is told how to reach for is a tool nobody uses, so the doctrine
# is pinned word for word in both turn-zero prompts and both CLI briefs.

SCRIPT_DOCTRINE = (
    "Delegate repetitive actions to scripts.",
    "Develop new and improve existing scripts through subagents, along with guides.",
    "Aim for deterministic scripts.",
    "When plays conflict, resolve it by extending conditions, never by priority alone.",
    "Pass inputs, such as the opposing archetype or your preferences, explicitly.",
)

LOOP_SCRIPT_DOCTRINE = (
    "`run_script` counts as both your wait and your one committing call; "
    "read `script_exit` and do exactly one thing."
)

# S1: a script applicability hint (`scripts.applicable`) now rides every
# wait/wake and action answer, so a seat no longer has to call `list_scripts`
# to find out a script applies - the measured problem being that no seat ever
# did, and cracked every fetch land by hand instead (`play_land` then
# `activate`). This doctrine is pinned word for word in both MCP prompts.
#: The sentence this whole doctrine replaces, everywhere it used to appear -
#: pinned only as the string `test_the_old_never_run_sentence_is_gone_everywhere`
#: asserts is now GONE, never as something any prompt or brief still carries.
OLD_NEVER_RUN_SENTENCE = (
    "Scripts never run on their own: a run you did not start yourself was started by someone "
    "else, such as an operator, and is not something to wait for or expect."
)

#: 2026-09-14 S2 A8: the replacement, word for word, everywhere an MCP prompt
#: (seat or buddy) said the old sentence above - arming is now a second,
#: equally real way to start a run, and the old wording never admitted that.
NEW_NEVER_RUN_SENTENCE_MCP = (
    "Scripts only run when you start them with `run_script` or arm them on a "
    "wait; a run you neither started nor armed was started by someone else."
)

HINT_DOCTRINE = (
    "When an answer carries `scripts.applicable`, that entry's `run_script` call is this "
    "turn's committing call - not the hand-played moves.",
    "After `play_land` of a fetch land, read that same answer's `scripts.applicable` before "
    "you `activate` it by hand.",
)

LOOP_HINT_DOCTRINE = (
    "When the answer you are holding carries `scripts.applicable`, that entry's `call` "
    "already is this turn's committing call - run it directly, with no `list_scripts` call first."
)

#: The universal half of the same caution, for the two CLI briefs
#: (`ai_seat_brief.md`, `ai_buddy_brief.md`) - no CLI hint field exists (S1
#: is MCP-only), but the false belief this closes came from a CLI: root cause
#: #3, staged `mcp-seat script run` calls during development that made a seat
#: write "scripts auto-fire" into its own memory.
#:
#: 2026-09-14 S2 A8: replaced, same as `NEW_NEVER_RUN_SENTENCE_MCP` above -
#: `--arm` (S2) is now a second real way a run starts from the CLI too, and
#: the old wording never admitted that. Per brief: a buddy is refused
#: `pass-until` outright, so its own wording only ever names `wait`.
CLI_HINT_CAUTION = {
    "ai_seat_brief.md": (
        "Scripts only run when you start them with `script run` or arm them with `--arm` on "
        "`wait`/`pass-until`; a run you neither started nor armed was started by someone else."
    ),
    "ai_buddy_brief.md": (
        "Scripts only run when you start them with `script run` or arm them with `--arm` on "
        "`wait`; a run you neither started nor armed was started by someone else."
    ),
}

BRIEFS = Path(__file__).resolve().parents[2] / "src" / "magic_cards_model_api" / "docs"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    """`heading` through the line before the next `## ` heading."""
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + len(heading) + 1)
    return text[start : end if end != -1 else len(text)]


@pytest.mark.parametrize("sentence", SCRIPT_DOCTRINE)
def test_seat_prompt_scripts_section_carries_the_doctrine(tmp_path, guide, sentence):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert sentence in _flat(_section(text, "## Scripts"))


@pytest.mark.parametrize("sentence", SCRIPT_DOCTRINE)
def test_buddy_prompt_carries_the_script_doctrine(tmp_path, guide, sentence):
    text = advisor_turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert sentence in _flat(_section(text, "## Scripts"))


def test_seat_prompt_loop_counts_run_script_as_wait_and_commit(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert LOOP_SCRIPT_DOCTRINE in _flat(_section(text, "## The loop"))


def test_seat_prompt_scripts_section_says_how_to_pick_a_script(tmp_path, guide):
    """Pick with `list_scripts`, and say which archetype you read rather than
    leaving the `*` default to stand in for a read you have made."""
    section = _flat(_section(turn_zero(assignment(guide), tmp_path / "x.md"), "## Scripts"))
    assert "list_scripts(" in section
    assert "run_script(" in section
    assert "opposing_archetype" in section
    assert "inputs" in section


def test_buddy_prompt_no_longer_checks_list_scripts_on_wake(tmp_path, guide):
    """S1: the hint replaces the old "check `list_scripts` on wake"
    instruction - a buddy proposes straight off `scripts.applicable` now."""
    section = _flat(_section(advisor_turn_zero(assignment(guide), tmp_path / "x.md"), "## Scripts"))
    assert "check `list_scripts` on wake" not in section
    assert "run_script(" in section


@pytest.mark.parametrize("sentence", HINT_DOCTRINE)
def test_seat_prompt_scripts_section_carries_the_hint_doctrine(tmp_path, guide, sentence):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert sentence in _flat(_section(text, "## Scripts"))


@pytest.mark.parametrize("sentence", HINT_DOCTRINE)
def test_buddy_prompt_scripts_section_carries_the_hint_doctrine(tmp_path, guide, sentence):
    text = advisor_turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert sentence in _flat(_section(text, "## Scripts"))


def test_seat_prompt_loop_names_run_script_among_the_committing_calls(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    section = _flat(_section(text, "## The loop"))
    assert "`run_script`" in section
    assert LOOP_HINT_DOCTRINE in section


@pytest.mark.parametrize("brief", ["ai_seat_brief.md", "ai_buddy_brief.md"])
def test_briefs_scripts_section_says_scripts_never_auto_run(brief):
    section = _flat(_section((BRIEFS / brief).read_text(), "## Scripts"))
    assert CLI_HINT_CAUTION[brief] in section, f"{brief} is missing the never-auto-run caution"


# ---- S2 A8: plan ahead and arming, taught wherever a seat or buddy reads ---
#
# S1 gave a seat `scripts.applicable`; S2 gave every seat and buddy
# `declare_plan(for_turn=...)`, `arm`/`arm_reasoning` on `wait_for_priority`/
# `pass_until`, and the hint's own `armable`/`arm_call`; S2 A6 gave a buddy
# `wait_for_priority(wake_to_draft=True)` and `stage_advice`. None of those
# reached prompt.py or either CLI brief before this task - not one call, not
# one field name.


def test_the_old_never_run_sentence_is_gone_from_the_seat_prompt(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert OLD_NEVER_RUN_SENTENCE not in text


def test_the_old_never_run_sentence_is_gone_from_the_buddy_prompt(tmp_path, guide):
    text = advisor_turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert OLD_NEVER_RUN_SENTENCE not in text


@pytest.mark.parametrize("brief", ["ai_seat_brief.md", "ai_buddy_brief.md"])
def test_the_old_never_run_sentence_is_gone_from_the_briefs(brief):
    assert OLD_NEVER_RUN_SENTENCE not in (BRIEFS / brief).read_text()


def test_the_new_never_run_sentence_is_in_the_seat_prompt(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert NEW_NEVER_RUN_SENTENCE_MCP in _flat(_section(text, "## Scripts"))


def test_the_new_never_run_sentence_is_in_the_buddy_prompt(tmp_path, guide):
    text = advisor_turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert NEW_NEVER_RUN_SENTENCE_MCP in _flat(_section(text, "## Scripts"))


def test_seat_prompt_names_plan_ahead_for_next_turn(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    flat = _flat(text)
    assert 'for_turn="next"' in flat
    assert "opponent's turn" in flat


def test_seat_loop_names_arming_and_the_hints_own_fields(tmp_path, guide):
    """`arm`, `armable` and `arm_call` are the fields the S2 hint carries -
    named where the loop teaches a seat to copy the hint rather than compose
    the arm arguments itself (per the coordinator's note: pin only the tool
    name and the `arm` argument, never the hint's own full templated call)."""
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    section = _flat(_section(text, "## The loop"))
    assert "arm=[...]" in section
    assert "`armable`" in section
    assert "`arm_call`" in section
    assert "copy its `arm_call` verbatim" in section


def test_seat_loop_says_arming_does_not_contradict_nothing_in_between(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    section = _flat(_section(text, "## The loop"))
    assert "does not contradict" in section
    assert '"nothing in between"' in section.lower()


def test_seat_loop_names_the_fetch_land_arming_case(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    section = _flat(_section(text, "## The loop")).lower()
    assert "arm the fetch play before your turn begins" in section


def test_seat_loop_says_to_resume_a_wait_for_own_priority_to_fire_armed(tmp_path, guide):
    """The concurrent arming fix this task was warned about: a `pass_until`
    waiting for this seat's own priority to fire something armed hands back a
    note, not a silent "reached". Pinned generically - only the verb and
    `resume`, never a hardcoded field name for the note itself, since that
    shape is still moving on `s2-arming`."""
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    section = _flat(_section(text, "## The loop")).lower()
    assert "waiting for your own priority to fire" in section
    assert "resume" in section
    assert "never start a fresh run there" in section


def test_plan_section_says_upkeep_and_draw_passes_with_an_empty_stack_need_no_plan(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    section = _flat(_section(text, "## Declare your plan before you act")).lower()
    assert "upkeep or draw step with an empty stack needs none" in section


def test_plan_section_still_allows_a_current_turn_plan_if_not_planned_ahead(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    section = _flat(_section(text, "## Declare your plan before you act"))
    assert '"current"' in section
    assert "if you did not plan ahead for it" in section


def test_buddy_prompt_names_wake_to_draft_and_stage_advice(tmp_path, guide):
    text = advisor_turn_zero(assignment(guide), tmp_path / "current-game.md")
    flat = _flat(text)
    assert "wake_to_draft=True" in flat
    assert "stage_advice(player_uid, step, whose_turn)" in flat


def test_buddy_prompt_names_arming_on_wait_for_priority(tmp_path, guide):
    text = advisor_turn_zero(assignment(guide), tmp_path / "current-game.md")
    flat = _flat(text)
    assert "arm=[...]" in flat
    assert "`armable`" in flat
    assert "`arm_call`" in flat


def test_buddy_prompt_names_every_draft_status(tmp_path, guide):
    text = advisor_turn_zero(assignment(guide), tmp_path / "current-game.md")
    flat = _flat(text)
    for status in ('"held"', '"posted"', '"stale"', '"superseded_by_script"'):
        assert status in flat, f"buddy prompt never names draft status {status!r}"


def test_buddy_prompt_keeps_the_silence_rules(tmp_path, guide):
    """The draft-ahead addition must not crowd out the existing silence
    doctrine - no proposals in upkeep, draw or an empty-stack end step."""
    text = advisor_turn_zero(assignment(guide), tmp_path / "current-game.md")
    flat = _flat(text).lower()
    assert "propose nothing there" in flat
    assert "untap step, the upkeep step, the draw step or the end step" in flat


def test_buddy_prompt_keeps_the_rationale_cap(tmp_path, guide):
    text = advisor_turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert "240 characters" in text
    assert "reasoning_too_long" in text


def test_seat_prompt_never_teaches_buddy_only_drafting_tools(tmp_path, guide):
    """`stage_advice` and `wake_to_draft` are refused outright for a seat
    (`seatkit.STAGE_ADVICE_ADVISOR_ONLY`/`WAKE_TO_DRAFT_ADVISOR_ONLY`) - the
    seat prompt must never send a seat reaching for either."""
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert "stage_advice" not in text
    assert "wake_to_draft" not in text


@pytest.mark.parametrize("brief", ["ai_seat_brief.md", "ai_buddy_brief.md"])
def test_briefs_name_repeatable_arm_and_arm_reasoning(brief):
    text = (BRIEFS / brief).read_text()
    flat = _flat(text)
    assert "--arm" in flat
    assert "--arm-reasoning" in flat


def test_seat_brief_names_plan_for_turn_next():
    flat = _flat((BRIEFS / "ai_seat_brief.md").read_text())
    assert "plan --for-turn next" in flat or "--for-turn next" in flat


def test_seat_brief_committing_command_list_includes_script_run():
    section = _flat(_section((BRIEFS / "ai_seat_brief.md").read_text(), "## The loop"))
    assert "`script run`" in section or "script run" in section


def test_buddy_brief_names_stage_advice_and_wake_to_draft_flag():
    flat = _flat((BRIEFS / "ai_buddy_brief.md").read_text())
    assert "stage-advice" in flat
    assert "--wake-to-draft" in flat


def test_buddy_brief_names_every_draft_status():
    flat = _flat((BRIEFS / "ai_buddy_brief.md").read_text())
    for status in ('"held"', '"posted"', '"stale"', '"superseded_by_script"'):
        assert status in flat, f"buddy brief never names draft status {status!r}"


def test_seat_brief_never_teaches_buddy_only_drafting_commands():
    text = (BRIEFS / "ai_seat_brief.md").read_text()
    assert "stage-advice" not in text
    assert "--wake-to-draft" not in text


HANDOVER_PINS = (
    "Delegate repetitive actions to scripts.",
    "Develop new and improve existing scripts through subagents, along with guides.",
    "Update the priority of each script that ran with `mcp-seat plays priority` from this "
    "game's evidence.",
    "`mcp-seat plays priority <play-id> --delta N --deck dimir-frog --game-id g-1`",
    "Land scripts with `mcp-seat plays admit --deck dimir-frog --game-id g-1`",
    "propose guides with `mcp-seat guides propose --deck dimir-frog --game-id g-1`",
    "mtg-decks has no CI budget, so its ci and automerge workflows never run",
    "the owner reviews and merges it by hand",
    "Do not wait for that pull request",
    "it is not live until the owner merges it",
    "Edit only `matchups[].play_plan` and `matchups[].notes`",
    "`decks/dimir-frog/NOTES.md`",
    # Game 8a55cab7: both seats proposed on one branch, so one PR mixed decks.
    "on its own `seat/<game-id>-<deck>-<role>-guides` branch",
    # Game 8a55cab7: the turn ended with a script subagent still running.
    "Wait for every subagent and background task you started to report back before you "
    "end the turn",
)

#: Game 16a9cf8f: a seat lowered two fetch plays by 20 that never had a window
#: that game, ran out its 510 s budget developing, and ran `git stash` in its
#: own /guides clone. These rules go wherever a seat or buddy is told to hand
#: over: the handover turn and both briefs.
HANDOVER_RULES = (
    "Change a script's priority only for scripts that actually ran this game, with recorded "
    "exits, as shown in the run table.",
    "Never lower a script for `not_applicable`, or for having had no applicable window.",
    "Change a script's priority by at most 5 per game.",
    "Finish the handover inside the time budget: do priority updates and admits first, then "
    "optional script or guide development through subagents.",
    "Never `git stash` in /guides: commit through the provided commands or discard.",
)

#: The subset the play-script spec carries where it describes `priority`.
PRIORITY_RULES = HANDOVER_RULES[:3]

SPEC = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-09-13-play-script-format-design.md"
)

RUN_TABLE = "script | priority | runs | exits | reapplicable_after\nfetch-x | 50 | 2 | done=2 | 1"


def _handover(guide, role: str) -> str:
    return handover_turn(
        replace(assignment(guide), role=role),
        "released",
        RUN_TABLE,
        "play-conflict: fetch-x, fetch-y (whose_turn, steps)",
    )


@pytest.mark.parametrize("role", ["seat", "advisor"])
@pytest.mark.parametrize("pin", HANDOVER_PINS)
def test_handover_prose_pins_for_seat_and_buddy(guide, role, pin):
    assert pin in _flat(_handover(guide, role))


@pytest.mark.parametrize("role", ["seat", "advisor"])
def test_handover_embeds_the_outcome_the_run_table_and_the_conflicts(guide, role):
    text = _handover(guide, role)
    assert text.startswith(HANDOVER_OPENING)
    assert "released" in text
    assert RUN_TABLE in _section(text, "## Script runs this game")
    assert "play-conflict: fetch-x, fetch-y (whose_turn, steps)" in _section(
        text, "## Conflict warnings"
    )


@pytest.mark.parametrize("role", ["seat", "advisor"])
@pytest.mark.parametrize("rule", HANDOVER_RULES)
def test_handover_turn_carries_the_handover_rules(guide, role, rule):
    assert rule in _flat(_handover(guide, role))


def test_handover_turn_names_its_time_budget(guide):
    """28 minutes: `HANDOVER_BUDGET_SECONDS`, not a number the prose invents."""
    assert "28 minutes" in _flat(_handover(guide, "seat"))


def test_handover_turn_orders_priority_and_admit_before_development(guide):
    text = _flat(_section(_handover(guide, "seat"), "## What to do"))
    assert text.index("mcp-seat plays priority") < text.index("subagents: one per new script")
    assert text.index("mcp-seat plays admit") < text.index("subagents: one per new script")


@pytest.mark.parametrize("brief", ["ai_seat_brief.md", "ai_buddy_brief.md"])
@pytest.mark.parametrize("rule", HANDOVER_RULES)
def test_briefs_carry_the_handover_rules(brief, rule):
    section = _flat(
        _section((BRIEFS / brief).read_text(), "## Play scripts and guides, for handover")
    )
    assert rule in section, f"{brief} is missing {rule!r}"


@pytest.mark.parametrize("brief", ["ai_seat_brief.md", "ai_buddy_brief.md"])
def test_briefs_name_the_priority_limit_refusal_and_the_game_id(brief):
    section = _flat(
        _section((BRIEFS / brief).read_text(), "## Play scripts and guides, for handover")
    )
    assert "`priority_limit`" in section
    assert "mcp-seat plays priority <play-id> --delta N --deck SLUG --game-id GAME" in section


@pytest.mark.parametrize("brief", ["ai_seat_brief.md", "ai_buddy_brief.md"])
def test_briefs_name_the_per_seat_guides_branch_and_the_subagent_wait(brief):
    section = _flat(
        _section((BRIEFS / brief).read_text(), "## Play scripts and guides, for handover")
    )
    assert "`seat/GAME-SLUG-ROLE-guides`" in section
    assert "Wait for every subagent and background task you started to report back" in section


def test_the_play_script_spec_names_the_per_seat_guides_branch():
    assert "`seat/<game-id>-<deck>-<role>-guides`" in _flat(SPEC.read_text())


@pytest.mark.parametrize("rule", PRIORITY_RULES)
def test_the_play_script_spec_carries_the_priority_rules(rule):
    assert rule in _flat(SPEC.read_text())


def test_the_seat_and_the_buddy_get_the_identical_handover(guide):
    assert _handover(guide, "seat") == _handover(guide, "advisor")


def test_the_nudge_says_push_now(guide):
    text = handover_nudge(assignment(guide))
    assert "push now" in text.lower()
    assert text.startswith("23 minutes into the handover")
    assert "mcp-seat plays admit --deck dimir-frog --game-id g-1" in text


@pytest.mark.parametrize("brief", ["ai_seat_brief.md", "ai_buddy_brief.md"])
def test_briefs_describe_the_script_cli_and_carry_the_doctrine(brief):
    text = (BRIEFS / brief).read_text()
    section = _flat(_section(text, "## Scripts"))
    for sentence in SCRIPT_DOCTRINE:
        assert sentence in section, f"{brief} is missing {sentence!r}"
    for needle in ("script run", "script list", "--input NAME=VALUE", "`inputs`"):
        assert needle in section, f"{brief}'s Scripts section never names {needle!r}"
