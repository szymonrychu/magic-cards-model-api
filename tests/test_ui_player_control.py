"""The play UI while one seat controls another's turn (CR 723).

Live game 0cf08288: the AI seat cast Emrakul, the Promised End and made the
human's decisions for their whole turn 7. The human's page showed their own
priority, offered them a pass the engine then refused ("claude is making
you's decisions this turn (CR 723.5)"), and said nothing about why. These run
`ui/store.js` and `ui/priority.js` under Node, like `test_ui_ai_stall.py`.
"""

from __future__ import annotations

import json
from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node


def board(you: str, **overrides: Any) -> dict:
    """The human's own view of their controlled upkeep."""
    return {
        "you": you,
        "winner": None,
        "drawn": False,
        "turn_number": 7,
        "step": "upkeep",
        "active_player": "you",
        "priority_holder": "you",
        "pending_decision": None,
        "stack": [],
        "player_control": {"you": "claude"},
        "players": [
            {"username": "you", "decisions_made_by": "claude"},
            {"username": "claude", "decisions_made_by": None},
        ],
    } | overrides


def notice(state: dict) -> Any:
    return run_module("store.js", f"m.playerControlNotice({json.dumps(state)})")


def test_the_controlled_player_is_told_who_is_deciding_for_them():
    message = notice(board("you"))
    assert message is not None
    assert "claude" in message
    assert "CR 723" in message


def test_the_controller_is_told_whose_decisions_it_is_making():
    message = notice(board("claude"))
    assert message is not None
    assert "you" in message


def test_nothing_is_said_when_nobody_is_controlled():
    state = board(
        "you",
        player_control={},
        players=[
            {"username": "you", "decisions_made_by": None},
            {"username": "claude", "decisions_made_by": None},
        ],
    )
    assert notice(state) is None


def test_the_controlled_player_is_offered_no_pass():
    """CR 723.5: the pass is not theirs to make, and the engine refuses it."""
    buttons = run_module("priority.js", f"m.passButtonsFor({json.dumps(board('you'))})")
    assert buttons == []


def test_the_ai_stall_banner_watches_the_controller_of_the_holder():
    """The AI seat is the one deciding, so the human's page must still notice
    when it goes quiet on the human's own priority."""
    status = {"state": "dispatched", "last_error": None}
    message = run_module(
        "store.js",
        f"m.aiStallNotice({json.dumps(board('you'))}, 'claude', {json.dumps(status)}, 120)",
    )
    assert message is not None and "claude" in message
