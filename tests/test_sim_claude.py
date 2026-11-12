"""Claude-seat sim matches: the pieces they share with the lobby."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
RED = (FIXTURES / "red_burn.txt").read_text()
GREEN = (FIXTURES / "green_stompy.txt").read_text()


def test_boot_decklist_game_registers_no_ui_game():
    from magic_cards_model_api.decks_api import boot_decklist_game
    from magic_cards_model_api.store import store

    game_id, seat_uids, match = boot_decklist_game(
        [("a", RED, True), ("b", GREEN, True)], seed=1, owner_user_id=None
    )
    assert game_id not in store.ui_games()
    assert store.ai_players(game_id) == {"a", "b"}
    assert set(seat_uids) == {"a", "b"}
    assert match.games[-1] is not None
