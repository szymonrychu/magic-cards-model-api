"""What the Priority group offers a player, ported from the old
`ui/app.js`'s `renderActionBar` HTML onto the new `ui/priority.js`'s
`passButtonsFor`.

**The regression this file used to guard.** "Pass priority" and
"Resolve top" both used to sit at the top of the old panel in every game.
Removing both stranded a player: a triggered ability on the stack in their
own precombat main, priority held, no decision pending, and the turn buttons
render only in untap and cleanup - so nothing on the panel could move the
game. The fix brought "Pass priority" back, but only where it was
contextually needed: priority held AND the stack non-empty (CR 117.4 - all
players passing in succession is what resolves the top of the stack).
Passing on an empty stack ended the step instead, which was the picker's
job, so the button stayed hidden there.

**The port.** The picker is gone; five pass verbs replace it, and they are
what `passButtonsFor` now decides whether to offer at all - not a single
"Pass priority" button conditioned on the stack. So the old empty-stack
case flips: with nothing on the picker to cover it any more, an empty stack
now offers all five verbs too (the old rule hid the one button precisely
*because* the picker covered that case; with the picker gone, the verbs are
what covers it). The full-stack case is unchanged in spirit - you still get
offered a way to move the game - just generalized from one button to five.
`test_no_pass_button_on_an_empty_stack_even_though_you_hold_priority` keeps
its old name and board on file for history; its assertion is rewritten to
the new, correct behaviour, with this note standing in for a per-test one.

"Resolve top" stays gone in every state: under CR 117.4 the top of the stack
resolving is a consequence of everyone passing, not an action a player
takes, and `resolveTop()` never existed as a `ui/priority.js` verb to begin
with - `"resolve" not in VERBS` pins that directly, once, rather than three
times against boards that cannot vary the answer.

This drives the shipped `ui/priority.js` under Node via
`tests/ui_node.py`'s `run_module` - no build step, no Python
reimplementation of the logic under test.
"""

from __future__ import annotations

from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node


def board(**overrides: Any) -> dict[str, Any]:
    """A board state shaped like the one `GET /api/games/{id}` returns, with
    only the fields `passButtonsFor` reads."""
    state = {
        "you": "alice",
        "priority_holder": "alice",
        "active_player": "alice",
        "turn_number": 1,
        "step": "precombat_main",
        "stack": [],
        "hands": {"alice": []},
        "battlefield": [],
        "pending_decision": None,
        "winner": None,
        "drawn": None,
    }
    state.update(overrides)
    return state


ABILITY_ON_STACK = [{"id": "ability-1", "name": "Devourer of Destiny", "kind": "ability"}]

BOARDS = {
    # You hold priority and the stack is empty. The old picker covered this
    # case by offering a named destination; the verbs replace the picker, so
    # this now offers all five rather than none.
    "yours-empty-stack": board(),
    # Something is on the stack but it is not your priority, so there is
    # nothing for you to pass.
    "theirs-full-stack": board(
        priority_holder="bob", active_player="bob", stack=list(ABILITY_ON_STACK)
    ),
    # Both halves true - the stranded state the old "Pass priority" button
    # existed for, generalized to the full verb set.
    "yours-full-stack": board(stack=list(ABILITY_ON_STACK)),
}


def test_no_pass_button_on_an_empty_stack_even_though_you_hold_priority() -> None:
    """Under the old picker, passing on an empty stack ended the step, which
    was the picker's job, so the one "Pass priority" button stayed hidden
    here. The picker is gone; the five verbs are what covers "move the game
    forward" now, on an empty stack exactly as on a full one - see the
    module docstring's note on this test's name."""
    buttons = run_module("priority.js", "m.passButtonsFor(s)", s=BOARDS["yours-empty-stack"])
    assert buttons == run_module("priority.js", "m.VERBS"), buttons


def test_no_pass_button_when_the_opponent_holds_priority_over_a_full_stack() -> None:
    """Not your priority, so nothing is offered - full stack or not."""
    buttons = run_module("priority.js", "m.passButtonsFor(s)", s=BOARDS["theirs-full-stack"])
    assert buttons == [], buttons


def test_the_pass_button_is_offered_when_you_hold_priority_over_a_full_stack() -> None:
    """The regression this file exists for: a triggered ability in your own
    precombat main, priority yours, no decision pending. Every verb can move
    the game from here, offered live - there is no state in which this
    returns fewer than the full set."""
    buttons = run_module("priority.js", "m.passButtonsFor(s)", s=BOARDS["yours-full-stack"])
    assert buttons == run_module("priority.js", "m.VERBS"), buttons


def test_resolve_top_is_never_offered() -> None:
    """Priority held or not, stack full or empty - "resolve the top of the
    stack yourself" was never a verb to begin with. Under CR 117.4 the top
    of the stack resolves because everyone passed, so it was never a player
    action. Pinned once, not per board (the three old boards this file
    exercises) - `VERBS` does not vary with `state`, so parametrizing over
    boards that cannot change the answer would only pin the same assertion
    three times."""
    assert "resolve" not in run_module("priority.js", "m.VERBS")


def test_the_rest_of_the_priority_group_is_untouched() -> None:
    """The old picker's markup and its three toggle checkboxes do not exist
    to check any more - `ui/priority.js` renders nothing, that is a later
    panel's job. What survives, board-state independent exactly as before
    (again pinned once rather than per board, for the same reason as above),
    is the data a panel would render from: every verb still has a label, and
    the stops panel still covers both the picker's `who` halves - your own
    side (`mine`, "my main 1" and its kin) and the opponent's (`theirs`,
    "opponent's end" and its kin)."""
    verbs = run_module("priority.js", "m.VERBS")
    labels = run_module("priority.js", "m.VERB_LABELS")
    assert set(verbs) == set(labels), (verbs, labels)

    stops = run_module("priority.js", "m.DEFAULT_STOPS")
    assert stops["mine"], stops
    assert stops["theirs"], stops


def test_the_other_groups_still_render() -> None:
    """Nothing outside the Priority group was disturbed: standing in your
    own declare-attackers step (a different part of the panel, the step's
    own button in the old HTML) does not change what the Priority group
    offers - the pass verbs are unaffected by which step you are in, only by
    whether you hold priority and a decision is pending."""
    buttons = run_module("priority.js", "m.passButtonsFor(s)", s=board(step="declare_attackers"))
    assert buttons == run_module("priority.js", "m.VERBS"), buttons
