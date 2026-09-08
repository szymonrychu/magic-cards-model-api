"""What the AI seat's pill says while the agent is playing.

It used to say "AI (claude): thinking" for the whole match, whatever the
agent was doing. `aiSeatActivityLabel` turns the coarse state the wrapper
observed in the transcript (`mtg_agent/wrapper/activity.py`, carried through
the driver onto the seat status the UI already polls) into the words on the
pill - and returns `null` for everything it cannot honestly label, which is
the signal to fall back to the plain state word the pill has always shown.

This runs the real shipped `ui/store.js` under Node, the same black-box way
`test_ui_ai_loading.py` and `test_ui_ai_stall.py` run their own functions -
no Python restatement of the JS anywhere here.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from ui_node import requires_node, run_module

pytestmark = requires_node

#: A live seat, mid-match: the two states the mechanisms report while the
#: agent actually has the game (`k8s_seats.py`'s `thinking`, and the
#: `wrapper` mechanism's `dispatched`).
PLAYING = "thinking"


def label(state: dict[str, Any] | None, ai_status: dict[str, Any] | None) -> Any:
    return run_module(
        "store.js",
        f"m.aiSeatActivityLabel({json.dumps(state)}, {json.dumps(ai_status)})",
    )


def seat(activity: Any, **overrides: Any) -> dict[str, Any]:
    return {
        "uid": "ai-uid",
        "player": "claude",
        "mechanism": "kubernetes",
        "state": PLAYING,
        "activity": activity,
        **overrides,
    }


def live(state: str, seconds: float = 1.0) -> dict[str, Any]:
    return {"state": state, "seconds": seconds, "seq": 3}


PLAYING_GAME = {"winner": None, "drawn": False, "priority_holder": "claude"}


# ---- every state, one at a time --------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("waiting", "waiting for priority"),
        ("reading_board", "reading the board"),
        ("reading_cards", "reading cards"),
        ("reading_rules", "checking the rules"),
        ("deciding", "deciding"),
        ("acting", "making its play"),
        ("thinking", "thinking"),
        ("working", "working"),
    ],
)
def test_each_observed_state_has_its_own_words(state, expected):
    assert label(PLAYING_GAME, seat(live(state))) == expected


def test_the_wrapper_mechanism_s_own_live_state_is_labelled_too():
    """`ai_seat.py`'s `wrapper` mechanism never rewrites its `dispatched`
    state while the agent is healthy - so a seat that only ever says
    `dispatched` still has to get its activity shown."""
    status = seat(live("reading_cards"), mechanism="wrapper", state="dispatched")
    assert label(PLAYING_GAME, status) == "reading cards"


# ---- everything that has to fall back --------------------------------------


def test_a_state_this_ui_has_no_words_for_falls_back():
    """A wrapper newer than this page, reporting a state added after it. The
    pill shows its plain state word rather than a raw key from the wire."""
    assert label(PLAYING_GAME, seat(live("sideboarding"))) is None


def test_no_activity_on_the_payload_falls_back():
    status = seat(None)
    del status["activity"]
    assert label(PLAYING_GAME, status) is None


def test_a_null_activity_falls_back():
    assert label(PLAYING_GAME, seat(None)) is None


def test_an_activity_that_is_not_an_object_falls_back():
    assert label(PLAYING_GAME, seat("reading_cards")) is None


def test_no_seat_status_at_all_falls_back():
    assert label(PLAYING_GAME, None) is None


def test_an_unreachable_wrapper_falls_back_to_the_seat_s_own_word():
    """`state: "unreachable"` is what the game server says when it cannot
    reach the driver. Whatever activity was last carried is now unknowable,
    and a failure must never be dressed up as "reading cards"."""
    assert label(PLAYING_GAME, seat(live("reading_cards"), state="unreachable")) is None


@pytest.mark.parametrize("state", ["error", "gone", "stopped", "idle", "pending", "starting"])
def test_a_seat_that_is_not_playing_keeps_its_own_word(state):
    assert label(PLAYING_GAME, seat(live("acting"), state=state)) is None


def test_a_seat_with_no_agent_running_keeps_its_fallback_panel():
    status = seat(live("acting"), mechanism="fallback", state="not_started")
    assert label(PLAYING_GAME, status) is None


def test_the_game_being_over_ends_the_label():
    """Nothing is being played any more, so nothing is being done about it -
    a pill still saying "deciding" after a concede is a lie."""
    over = {"winner": "claude", "drawn": False}
    assert label(over, seat(live("deciding"))) is None
    drawn = {"winner": None, "drawn": True}
    assert label(drawn, seat(live("deciding"))) is None


def test_no_game_state_yet_still_labels():
    """The seat panel renders before the first board poll lands."""
    assert label(None, seat(live("thinking"))) == "thinking"


# ---- staleness: observed, never invented -----------------------------------


def test_a_quiet_agent_keeps_the_last_thing_it_was_actually_doing():
    """Twenty seconds of silence is not evidence of anything new. The honest
    label is still the last observed one - no timer gets to invent a state."""
    assert label(PLAYING_GAME, seat(live("thinking", seconds=20))) == "thinking"


def test_a_state_old_enough_to_be_worthless_falls_back():
    """An observation this page can no longer stand behind is retired - it
    goes back to the plain state word rather than presenting a ten-minute-old
    reading as what the agent is doing now."""
    stale = run_module("store.js", "m.AI_ACTIVITY_STALE_SECONDS")
    assert label(PLAYING_GAME, seat(live("reading_cards", seconds=stale))) is None
    assert label(PLAYING_GAME, seat(live("reading_cards", seconds=stale + 600))) is None
    assert label(PLAYING_GAME, seat(live("reading_cards", seconds=stale - 1))) == "reading cards"


def test_the_stale_limit_leaves_room_for_a_real_wait_for_priority():
    """Measured over 65 minutes of real seat transcripts, a legitimate
    `wait_for_priority` block holds for a p90 of 110s with nothing written to
    the transcript meanwhile. A limit near the 45s stall banner would retire
    that live, correct label mid-wait - on that data it blanked 49% of polls
    - and replace it with the flat "thinking" this feature exists to remove.
    """
    stall = run_module("store.js", "m.AI_STALL_SECONDS")
    stale = run_module("store.js", "m.AI_ACTIVITY_STALE_SECONDS")
    assert stale > 2 * 110, "a p90 wait must not be retired as stale"
    assert stale > stall, "the pill outlives the stall banner rather than echoing it"
    assert label(PLAYING_GAME, seat(live("waiting", seconds=110))) == "waiting for priority"


def test_an_age_the_wrapper_did_not_report_is_not_treated_as_stale():
    assert label(PLAYING_GAME, seat({"state": "acting", "seq": 1})) == "making its play"


def test_a_nonsense_age_is_not_treated_as_stale():
    assert label(PLAYING_GAME, seat({"state": "acting", "seconds": "soon"})) == "making its play"


# ---- the leak, pinned on this side too -------------------------------------


def test_only_the_state_word_can_ever_reach_the_label():
    """Requirement: the human opponent reads this pill mid-game. Even handed
    a payload carrying the agent's plan, the label comes from a fixed table
    keyed by the state alone - nothing off the wire is ever rendered."""
    poisoned = {
        "state": "reading_cards",
        "seconds": 1,
        "card": "Lightning Bolt",
        "text": "he is on Frog, hold up the counterspell",
        "tool": "mcp__spellslinger__resolve_cards",
        "args": {"names": ["Ragavan, Nimble Pilferer"]},
    }
    assert label(PLAYING_GAME, seat(poisoned)) == "reading cards"


def test_every_label_this_ui_can_show_is_a_fixed_phrase():
    """The complete set, enumerated from the module itself: if a future edit
    ever interpolated something off the wire into one of these, it would
    have to change this list to say so."""
    labels = run_module("store.js", "Object.values(m.AI_ACTIVITY_LABELS).sort()")
    assert labels == sorted(
        [
            "waiting for priority",
            "reading the board",
            "reading cards",
            "checking the rules",
            "deciding",
            "making its play",
            "thinking",
            "working",
        ]
    )
