"""Decklist parsing, game boot from decklists, and the serve boot layer.

Card data comes only from the recorded Scryfall fixtures that conftest pins
(MCM_CACHE_DIR): Forest, Mountain, Grizzly Bears, Lightning Bolt. No legal
constructed deck can be built from those four cards - CR 100.2a wants sixty -
so the deck-to-game tests load with check_legality=False, the engine's own
escape hatch for a deliberately illegal pile. The serve path itself keeps the
engine's legality check on.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from magic_cards_model.machines import Step
from magic_cards_model.models import Match, Zone

from magic_cards_model_api import serve
from magic_cards_model_api.decks import load_deck_into_game, parse_decklist

#: The real guide decklists this repo ships under `tests/fixtures/guides/`.
GUIDE_DECKS_DIR = Path(__file__).parent / "fixtures" / "guides" / "decks"

# Two 12-card lists over the four recorded fixture cards only.
ALICE = "4 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
BOB = "4 Forest\n4 Grizzly Bears\n2 Mountain\nSideboard\n1 Forest\n1 Grizzly Bears"


def load(seed: int | None) -> Match:
    return load_deck_into_game([("alice", ALICE), ("bob", BOB)], seed=seed, check_legality=False)


def library_names(match: Match, username: str) -> list[str]:
    game = match.games[-1]
    return [state.card.name for state in game.player(username).library]


# -- parse_decklist ---------------------------------------------------------


def test_mainboard_only() -> None:
    main, side = parse_decklist("4 Lightning Bolt\n4 Grizzly Bears")
    assert main == [(4, "Lightning Bolt"), (4, "Grizzly Bears")]
    assert side == []


def test_deck_and_sideboard_headers() -> None:
    main, side = parse_decklist("Deck\n4 Lightning Bolt\n\nSideboard\n1 Mountain")
    assert main == [(4, "Lightning Bolt")]
    assert side == [(1, "Mountain")]


def test_sb_prefixed_lines() -> None:
    main, side = parse_decklist("4 Lightning Bolt\nSB: 1 Mountain\nSB: 1 Forest")
    assert main == [(4, "Lightning Bolt")]
    assert side == [(1, "Mountain"), (1, "Forest")]


def test_mtgo_count_with_x() -> None:
    main, side = parse_decklist("4x Lightning Bolt\n2x Mountain")
    assert main == [(4, "Lightning Bolt"), (2, "Mountain")]
    assert side == []


def test_comments_and_blank_lines() -> None:
    text = "# a burn deck\n\n// sixteen damage exactly\n4 Lightning Bolt\n\n# done"
    assert parse_decklist(text) == ([(4, "Lightning Bolt")], [])


def test_duplicate_lines_sum_into_one_entry() -> None:
    main, _ = parse_decklist("4 Lightning Bolt\n2 Lightning Bolt")
    assert main == [(6, "Lightning Bolt")]


def test_set_bracket_and_collector_number_stripped() -> None:
    # Pinned decision: Arena and MTGO never export a set bracket, but if one
    # is present it is stripped, with the trailing collector number, and the
    # name underneath is used.
    main, side = parse_decklist("4 Lightning Bolt [2X2] 129\n2 Mountain [MH3]")
    assert main == [(4, "Lightning Bolt"), (2, "Mountain")]
    assert side == []


def test_blank_line_closes_the_mainboard_b8_05() -> None:
    """B8-05: dimir-frog's real decklist.txt has no `Sideboard` header at all
    - a blank line is the only boundary, exactly as mtg-decks' own
    `scripts/decklist.py` documents ("the mainboard runs until the first
    blank line or a `Sideboard` header, whichever comes first"). Before this
    fix, a blank line was just skipped and every line after it kept landing
    in the mainboard."""
    main, side = parse_decklist("4 Lightning Bolt\n4 Grizzly Bears\n\n2 Mountain\n1 Forest")
    assert main == [(4, "Lightning Bolt"), (4, "Grizzly Bears")]
    assert side == [(2, "Mountain"), (1, "Forest")]


def test_further_blank_lines_after_the_boundary_are_just_spacing() -> None:
    """mtg-decks: "there is no third section" - once the blank line has
    closed the mainboard, later blank lines are spacing inside the
    sideboard, not a second boundary."""
    main, side = parse_decklist("4 Lightning Bolt\n\n2 Mountain\n\n1 Forest")
    assert main == [(4, "Lightning Bolt")]
    assert side == [(2, "Mountain"), (1, "Forest")]


def test_a_blank_line_before_any_content_is_not_a_boundary() -> None:
    """Leading blank lines (or ones between comments, before any real entry)
    do not prematurely close a mainboard that has not started yet."""
    main, side = parse_decklist("\n\n4 Lightning Bolt\n4 Grizzly Bears")
    assert main == [(4, "Lightning Bolt"), (4, "Grizzly Bears")]
    assert side == []


def test_explicit_sideboard_header_still_works_after_a_blank_line() -> None:
    """The blank-line boundary and the explicit `Sideboard` header agree:
    a file that has both (most of this repo's bundled guide decks do) parses
    exactly the same as one with only the blank line."""
    main, side = parse_decklist("4 Lightning Bolt\n\nSideboard\n1 Mountain")
    assert main == [(4, "Lightning Bolt")]
    assert side == [(1, "Mountain")]


@pytest.mark.parametrize(
    "slug",
    ["dimir-frog", "living-end", "affinity", "boros-aggro", "orzhov-martyr", "prison-tron"],
)
def test_guide_decklists_match_mtg_decks_own_parser_counts(slug: str) -> None:
    """Every real decklist this repo ships under `tests/fixtures/guides/`
    parses to 60 mainboard + 15 sideboard - verified in this session against
    mtg-decks' own `scripts/decklist.py` (`load_decklist(...).mainboard_count`/
    `.sideboard_count`), which is the canonical parser these guide decks are
    written for. `dimir-frog` is the one with no `Sideboard` header at all
    (B8-05's exact repro, a blank line only); the rest also carry the header,
    proving the blank-line fix does not disturb the header-only files."""
    text = (GUIDE_DECKS_DIR / slug / "decklist.txt").read_text()
    main, side = parse_decklist(text)
    assert sum(count for count, _ in main) == 60, (slug, main)
    assert sum(count for count, _ in side) == 15, (slug, side)


def test_garbage_line_raises_with_the_line_quoted() -> None:
    with pytest.raises(ValueError, match="definitely not a card"):
        parse_decklist("4 Lightning Bolt\ndefinitely not a card")


def test_zero_count_raises() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        parse_decklist("0 Lightning Bolt")


# -- load_deck_into_game ----------------------------------------------------


def test_load_deck_into_game_deals_the_engine_start_state() -> None:
    match = load(seed=7)
    game = match.games[-1]
    alice, bob = game.player("alice"), game.player("bob")

    # CR 103.1: seat order - alice's list was given first, so she plays first.
    #
    # The game sits where the engine's start leaves it, which is *before*
    # CR 103.8's first turn: resting in CR 103.5's mulligan step, turn 0, in no
    # phase at all (CR 500.1 makes a phase part of a turn, and no turn has
    # begun). It used to be turn 1 / untap here, which was the observable leak
    # the engine's pregame steps closed - both start-of-game sequences ran
    # while the turn machine already claimed the first turn was under way.
    assert match.first_players == ["alice"]
    assert game.turn is not None
    assert game.turn.turn_number == 0
    assert game.turn.step is Step.MULLIGAN
    assert game.turn.phase is None
    assert game.in_pregame is True
    assert game.turn.active_player == "alice"

    # CR 103.5: opening hands of seven. CR 103.2a: sideboards set aside.
    assert len(alice.hand) == 7
    assert len(bob.hand) == 7
    assert alice.sideboard == []
    assert [state.card.name for state in bob.sideboard] == ["Forest", "Grizzly Bears"]
    assert all(state.zone is Zone.SIDEBOARD for state in bob.sideboard)

    # The library is the mainboard minus the hand drawn, in some shuffled order.
    assert len(alice.library) == 5
    assert len(bob.library) == 3
    dealt = Counter(state.card.name for state in [*alice.library, *alice.hand])
    assert dealt == Counter({"Mountain": 4, "Grizzly Bears": 4, "Lightning Bolt": 4})

    # CR 103.4: starting life totals. CR 108.3: every card owned by its player.
    assert alice.life == 20 and bob.life == 20
    assert all(state.owner == "alice" for state in [*alice.library, *alice.hand])
    assert all(state.owner == "bob" for state in [*bob.library, *bob.hand, *bob.sideboard])


def test_same_seed_reproduces_the_same_library_order() -> None:
    first, second = load(seed=7), load(seed=7)
    for username in ("alice", "bob"):
        assert library_names(first, username) == library_names(second, username)


def test_different_seed_shuffles_differently() -> None:
    # Seeds 7 and 8 were checked to give different orders for these lists at
    # the time this test was written; the pair is deterministic, so the
    # assertion either always holds or always fails - it cannot flake.
    first, second = load(seed=7), load(seed=8)
    for username in ("alice", "bob"):
        assert library_names(first, username) != library_names(second, username)


def test_missing_seed_draws_a_random_one() -> None:
    first, second = load(seed=None), load(seed=None)
    assert isinstance(first.seed, int) and isinstance(second.seed, int)
    assert first.seed != second.seed


# -- the serve boot layer ---------------------------------------------------


def test_parse_args_accepts_two_decks_seed_and_port() -> None:
    args = serve.parse_args(
        ["--deck", "a=x.txt", "--deck", "b=y.txt", "--seed", "7", "--port", "8766"]
    )
    assert args.deck == ["a=x.txt", "b=y.txt"]
    assert args.seed == 7
    assert args.port == 8766


def test_parse_args_rejects_anything_but_two_decks() -> None:
    with pytest.raises(SystemExit):
        serve.parse_args(["--deck", "a=x.txt"])
    with pytest.raises(SystemExit):
        serve.parse_args(["--deck", "a=x.txt", "--deck", "b=y.txt", "--deck", "c=z.txt"])


def test_parse_args_rejects_a_deck_without_a_path() -> None:
    with pytest.raises(SystemExit):
        serve.parse_args(["--deck", "a=x.txt", "--deck", "just-a-name"])


def test_setup_boots_the_game_into_the_store(tmp_path) -> None:
    first = tmp_path / "alice.txt"
    second = tmp_path / "bob.txt"
    first.write_text(ALICE)
    second.write_text(BOB)
    args = serve.parse_args(["--deck", f"p1={first}", "--deck", f"p2={second}", "--seed", "7"])
    boot = serve.setup(args, check_legality=False)
    try:
        assert serve.store.get(boot.game_id) is boot.game
        assert [player.username for player in boot.game.players] == ["p1", "p2"]
        assert boot.match.seed == 7
        assert boot.game.turn is not None and boot.game.turn.active_player == "p1"
        assert all(len(player.hand) == 7 for player in boot.game.players)
        summary = boot.summary()
        assert boot.game_id in summary
        assert "seed: 7" in summary
        assert "p1" in summary and "p2" in summary
    finally:
        serve.store.delete(boot.game_id)
