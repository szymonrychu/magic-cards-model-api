"""The window in which the pregame is over but CR 103.8's first turn has not
been settled yet - and how a lock-free board read is kept out of it.

`test_priorityless_steps.py` states the contract this exists to hold: *the API
never hands back a state nobody is allowed to move*. CR 103.8's first turn
begins at its untap step, which grants nobody priority (CR 502.4) and which CR
500.3 ends of its own accord, so the last pregame move is always followed by
`engine_api.settle_priorityless_steps` - and the response carries the settled
step rather than `untap`.

That holds for the caller who made the move. It did not hold for anybody
*else* reading the board at that moment. The two statements

    step = game.finish_opening_hand_actions(username)   # pregame over, at untap
    engine_api.settle_priorityless_steps(game)          # on to the upkeep

run under the game's mutation lock, but `GET /api/games/{id}` deliberately
takes no lock at all - `tests/test_action_flow.py`'s
`test_no_deadlock_block_until_wake_never_holds_the_game_lock` pins that down,
because a poll that waited for the lock would deadlock against the very
request meant to change what it sees. An in-memory `Game` is the *same object*
every reader holds, so a poll landing between those two statements was handed
exactly the frozen page the settling exists to prevent: `pregame: null`, step
`untap`, `priority_holder: null`, and no move any client is allowed to make.

Rare, and real: it turned up as an intermittently red
`test_a_human_reaches_turn_one_though_the_ai_seat_never_moves` in a full-suite
run, where the backstop (`ai_pregame.py`) makes the last pregame move on its
own thread while the human's browser polls. A browser hits the same window.

So the mutation announces itself, and a board read waits for it - on this
module's own condition, **never on the game lock**:

- the pregame's last move runs inside `in_flight(game_id)`, from before the
  engine call through the settling;
- `GET /api/games/{id}` calls `wait(game_id)` before it reads.

That keeps the no-lock invariant intact (holding `store.locked` with no
handoff running gates nothing, which is the deadlock test's exact shape) and
leaves every deliberate observation of a priorityless step alone - `POST
/advance` and `PUT /step` are how a harness steps into one on purpose, and
neither opens a handoff.

The wait is bounded rather than indefinite: a handoff is two engine calls
long, so a read that is still waiting after `WAIT_SECONDS` is looking at a
bug somewhere else, and hanging the poll would be worse than handing back
what is there.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

#: The ceiling on how long a board read waits for a handoff to finish. Two
#: engine calls, so this is three orders of magnitude of headroom - long
#: enough that a loaded machine can never trip it, short enough that a poll
#: never appears to hang.
WAIT_SECONDS = 2.0

_condition = threading.Condition()
#: game id -> how many handoffs are open on it. A count, not a flag, so
#: nesting (or two games' worth of the same name) cannot clear it early.
_open: dict[str, int] = {}


@contextmanager
def in_flight(game_id: str) -> Iterator[None]:
    """Mark this game as mid-handoff for the body's duration.

    Wraps the mutation *and* the settling that follows it, so no reader can
    see the half-applied state between them.
    """
    with _condition:
        _open[game_id] = _open.get(game_id, 0) + 1
    try:
        yield
    finally:
        with _condition:
            remaining = _open.get(game_id, 0) - 1
            if remaining > 0:
                _open[game_id] = remaining
            else:
                _open.pop(game_id, None)
            _condition.notify_all()


def wait(game_id: str, timeout: float = WAIT_SECONDS) -> bool:
    """Block until no handoff is open on `game_id`, or `timeout` elapses.

    Returns whether the wait ended with the handoff closed. Returns
    immediately - no lock contention, no condition wait - in the ordinary
    case where nothing is in flight, which is every read but the handful
    that land in the window.
    """
    with _condition:
        return _condition.wait_for(lambda: game_id not in _open, timeout)


def is_in_flight(game_id: str) -> bool:
    """Whether a handoff is open on `game_id` right now. For tests."""
    with _condition:
        return game_id in _open


__all__ = ["WAIT_SECONDS", "in_flight", "is_in_flight", "wait"]
