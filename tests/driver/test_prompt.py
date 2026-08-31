from pathlib import Path

import pytest

from mtg_agent.driver.guides import DeckGuide
from mtg_agent.driver.prompt import (
    Assignment,
    memory_index,
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


def assignment(guide: DeckGuide | None, score: float = 1.0) -> Assignment:
    return Assignment(
        game_id="g-1",
        uid="secret-uid",
        player="bob",
        opponent="alice",
        api_base="http://game:8000",
        decklist={"Psychic Frog": 4, "Fatal Push": 4},
        guide=guide,
        match_score=score,
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


def test_current_game_says_so_when_there_is_no_guide(tmp_path):
    text = render_current_game(assignment(None, score=0.2), tmp_path / "memory")
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
    for tool in ("wait_for_priority", "my_state", "cast", "play_land", "decide", "pass_priority"):
        assert tool in text
    assert "seat_cli" not in text


def test_turn_zero_carries_the_uid_and_the_hard_won_rules_of_engagement(tmp_path, guide):
    text = turn_zero(assignment(guide), tmp_path / "current-game.md")
    assert "secret-uid" in text
    assert "winner" in text
    assert "never state" in text.lower()


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
    text = reflection_turn(assignment(None, 0.1), memory, outcome="alice won")
    assert str(memory / "unknown-deck" / "games" / "g-1.md") in text
