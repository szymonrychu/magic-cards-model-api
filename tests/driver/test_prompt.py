from pathlib import Path

import pytest

from mtg_agent.driver.guides import (
    CONFIDENT,
    LOW,
    NONE,
    ArchetypeSignature,
    Candidate,
    DeckGuide,
    Identification,
    SignatureClause,
    parse_decklist,
)
from mtg_agent.driver.prompt import (
    Assignment,
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
