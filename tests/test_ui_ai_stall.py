"""The play UI has to say when its opponent has stopped playing.

This runs `aiStallNotice` out of the real `ui/store.js` under Node, the same
black-box way `tests/test_ui_autopilot.py` runs the auto-pass stop
conditions - no Python restatement of the JS anywhere here.

The freeze it exists for, observed live in the compose stack: the human set
auto-pass to run until their own precombat main, the AI seat took priority in
the untap step and never gave it back (its claude had hit
`API Error: 401 API key is invalid` and stopped), and the page showed nothing
at all. Auto-pass had correctly stopped - it only ever passes while the human
holds priority - so the page was not broken, it just had no way to say
"your opponent is not coming back" and no button that could move the game on.
"""

from __future__ import annotations

import json
from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node


def notice(state: dict, status: dict | None, seconds: float) -> Any:
    return run_module(
        "store.js",
        f"m.aiStallNotice({json.dumps(state)}, 'claude', {json.dumps(status)}, {seconds})",
    )


def board(**overrides: Any) -> dict:
    """The live stuck board, as `GET /api/games/{id}` reported it."""
    return {
        "you": "you",
        "winner": None,
        "drawn": False,
        "turn_number": 1,
        "step": "untap",
        "priority_holder": "claude",
        "stack": [],
    } | overrides


def test_the_seat_holding_priority_and_saying_nothing_is_reported():
    message = notice(board(), {"state": "dispatched", "last_error": None}, 60)
    assert message is not None
    assert "claude" in message
    assert "60s" in message


def test_a_seat_that_has_only_just_taken_priority_is_left_alone():
    """An agent thinking is not an agent that has died, and accusing one that
    is mid-turn would be worse than saying nothing."""
    assert notice(board(), {"state": "dispatched", "last_error": None}, 5) is None


def test_the_grace_period_is_long_enough_for_a_real_turn():
    assert run_module("store.js", "m.AI_STALL_SECONDS") >= 30


def test_an_error_the_server_already_knows_beats_the_timer():
    """No point making the human wait out a countdown for something the
    driver reported at second one."""
    message = notice(
        board(),
        {"state": "error", "last_error": "turn turn-1 failed: API Error: 401"},
        1,
    )
    assert message is not None
    assert "401" in message


def test_an_unreachable_agent_is_reported_as_such():
    message = notice(board(), {"state": "unreachable", "last_error": None}, 1)
    assert message is not None
    assert "cannot be reached" in message


def test_nothing_is_said_while_the_human_holds_priority():
    """This is the human's own think time, however long they take over it."""
    assert notice(board(priority_holder="you"), {"state": "dispatched"}, 600) is None


def test_nothing_is_said_once_the_game_is_over():
    assert notice(board(winner="you"), {"state": "dispatched"}, 600) is None
    assert notice(board(drawn=True), {"state": "dispatched"}, 600) is None


def test_a_game_with_no_ai_seat_is_never_accused():
    state = json.dumps(board())
    assert run_module("store.js", f"m.aiStallNotice({state}, null, null, 600)") is None


def test_a_missing_status_still_reports_a_silent_seat():
    """The status poll is best-effort - it swallows its own errors - so the
    notice must not depend on having one."""
    assert notice(board(), None, 90) is not None


# ---- activity: the false "215s without acting" accusation -----------------
#
# The board can sit unchanged for a couple of minutes while the agent is
# genuinely mid-turn - reading cards, thinking, working through a plan - and
# the same status payload that says "priority hasn't moved" also carries
# `activity`, published by the wrapper's transcript watcher. The stall notice
# must read it before accusing anyone.


def test_live_recent_activity_suppresses_the_stall_notice():
    """The exact bug report: the board unchanged 215s, but the activity pill
    is showing 'making its play' - freshly observed. Not a stall."""
    status = {
        "state": "dispatched",
        "last_error": None,
        "activity": {"state": "acting", "seconds": 2, "seq": 9},
    }
    assert notice(board(), status, 215) is None


def test_stale_activity_does_not_suppress_the_stall_notice():
    """The last thing the wrapper ever saw the agent do was minutes ago -
    that is not evidence of a live agent, it is a leftover. The human still
    deserves to be told the seat has gone quiet."""
    status = {
        "state": "dispatched",
        "last_error": None,
        "activity": {"state": "acting", "seconds": 1000, "seq": 9},
    }
    message = notice(board(), status, 215)
    assert message is not None
    assert "claude" in message


def test_an_error_beats_live_activity():
    """The server already knows this seat is broken - a transcript line from
    before it broke does not get to override that."""
    status = {
        "state": "error",
        "last_error": "turn turn-1 failed: API Error: 401",
        "activity": {"state": "acting", "seconds": 1, "seq": 9},
    }
    message = notice(board(), status, 60)
    assert message is not None
    assert "401" in message


def test_unreachable_beats_live_activity():
    status = {
        "state": "unreachable",
        "last_error": None,
        "activity": {"state": "acting", "seconds": 1, "seq": 9},
    }
    message = notice(board(), status, 60)
    assert message is not None
    assert "cannot be reached" in message


def test_no_activity_at_all_is_unaffected():
    """An older agent build, the plain `subprocess` mechanism, or a seat that
    never published anything: no `activity` key at all. The stall notice
    must behave exactly as it always has - it has nothing to suppress it
    with."""
    status = {"state": "dispatched", "last_error": None}
    message = notice(board(), status, 60)
    assert message is not None
    assert "60s" in message


# -- the pregame, where nobody holds priority ---------------------------------
#
# CR 103.5's mulligan step and CR 103.6's opening-hand step grant no player
# priority, so `priority_holder` is null and every check this module makes
# fell straight through: a game held on a dead AI seat's mulligan showed the
# human nothing whatsoever. The server now says so itself (`pregame.stalled`,
# `ai_pregame.stall`), and this passes that notice through verbatim - it
# already names the seat, the move, why nothing answered it, and the ways
# out, and paraphrasing it here would be a second copy to keep in step.


def pregame_board(stalled: list[dict], **overrides: Any) -> dict:
    return board(
        priority_holder=None,
        turn_number=0,
        step="mulligan",
        pregame={
            "step": "mulligan",
            "awaiting_mulligan": ["claude"],
            "stalled": stalled,
        },
        **overrides,
    )


HELD = {
    "player": "claude",
    "kind": "mulligan",
    "reason": "the seat's claude subprocess exited 0",
    "text": "claude's CR 103.5 mulligan declaration is owed, and nothing is playing that seat.",
}


def test_a_pregame_move_the_server_refused_to_make_is_reported():
    message = notice(pregame_board([HELD]), {"state": "stopped", "last_error": None}, 0)
    assert message == HELD["text"], "the server's own notice was not what the human was shown"


def test_the_pregame_notice_does_not_wait_for_a_stall_timer():
    # Zero seconds unchanged, and a seat the server has not called broken:
    # the ordinary timer below would say nothing for another 45 seconds, and
    # the game is already held.
    assert notice(pregame_board([HELD]), {"state": "dispatched", "last_error": None}, 0) is not None


def test_a_pregame_held_on_the_other_seat_is_not_blamed_on_this_one():
    held = HELD | {"player": "you", "text": "you owe a declaration."}
    assert notice(pregame_board([held]), {"state": "stopped", "last_error": None}, 0) is None


def test_an_ordinary_pregame_says_nothing():
    assert notice(pregame_board([]), {"state": "dispatched", "last_error": None}, 600) is None


def test_a_server_too_old_to_report_stalls_is_not_a_crash():
    stale = pregame_board([])
    del stale["pregame"]["stalled"]
    assert notice(stale, {"state": "dispatched", "last_error": None}, 600) is None
