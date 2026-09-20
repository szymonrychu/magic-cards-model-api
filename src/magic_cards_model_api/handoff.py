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

So the mutation announces itself, and a board read is *validated against*
that announcement - on this module's own condition, **never on the game
lock**:

- the pregame's last move runs inside `in_flight(game_id)`, from before the
  engine call through the settling, and opening one bumps a generation
  counter;
- `GET /api/games/{id}` builds its whole answer inside `stable_read`, which
  waits out any handoff that is already open, notes the generation, builds
  the view, and only hands it back once it has checked that no handoff was
  opened while it was building. When one was, the view is thrown away and
  built again.

**Checking only before the build is what a seqlock exists to fix, and it is
why `stable_read` is not a bare `wait`.** A board read is not
instantaneous: `game_or_404` -> `resolve_viewer` -> `state_view` all run
against the live `Game` the store hands back *by reference*. A read that
passed a one-shot gate microseconds before the handoff opened is overtaken
by it and assembles its answer out of the half-applied state - the same
frozen page, reached by the other ordering, and no amount of gating *before*
the build can see it coming. The re-check afterwards can: a view that a
handoff was open across is discarded and rebuilt, so what is handed back is
always a view of one side of the move or the other.

The counter is global rather than per game, so it cannot accumulate an entry
per game id that ever ended a pregame. The cost is that another game's
handoff can make a read rebuild once - a rebuild, never a wait, since `wait`
stays strictly per game - and a handoff is rare enough (one per game, at the
end of its pregame) that this is cheaper than the bookkeeping to avoid it.

That keeps the no-lock invariant intact (holding `store.locked` with no
handoff running gates nothing, which is the deadlock test's exact shape) and
leaves every deliberate observation of a priorityless step alone - `POST
/advance` and `PUT /step` are how a harness steps into one on purpose, and
neither opens a handoff.

Both the wait and the retrying are bounded rather than indefinite: a handoff
is two engine calls long, so a read still contending after `WAIT_SECONDS` is
looking at a bug somewhere else, and hanging the poll would be worse than
handing back what is there.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
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
#: How many handoffs have ever been opened, on any game. A read notes this
#: before it builds its view and checks it again afterwards, which is what
#: catches a handoff that opened *and closed* while the view was being built
#: - `_open` alone reads the same either side of that. Global rather than
#: per game so it cannot grow an entry per game id; see the module
#: docstring.
_generation = 0


def _mark(game_id: str) -> tuple[int, bool]:
    """This game's `(generation, mid-handoff)` right now. Caller must not
    hold `_condition`."""
    with _condition:
        return (_generation, game_id in _open)


@contextmanager
def in_flight(game_id: str) -> Iterator[None]:
    """Mark this game as mid-handoff for the body's duration.

    Wraps the mutation *and* the settling that follows it, so no reader can
    see the half-applied state between them.
    """
    global _generation
    with _condition:
        _open[game_id] = _open.get(game_id, 0) + 1
        _generation += 1
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


def generation() -> int:
    """How many handoffs have been opened on any game so far. For tests."""
    with _condition:
        return _generation


def stable_read[T](game_id: str, build: Callable[[], T], timeout: float = WAIT_SECONDS) -> T:
    """Run `build` and return its result, with no handoff on `game_id` open
    at any point while it ran.

    The seqlock this module's docstring describes, and the whole reason a
    bare `wait` before the read is not enough: `build` is not instantaneous
    and reads the live `Game` by reference, so it is checked *after* it has
    run as well as before it starts. A `build` that a handoff opened across
    is discarded and run again - it must therefore be free of side effects,
    which is exactly what assembling a board view is.

    Bounded by `timeout`, like `wait`, and for the same reason: past it this
    hands back the last view it built rather than spinning or hanging a
    poll. `build` raising is not retried once nothing raced it - that
    exception is the read's own answer (a 404 for an unknown game, a 409 for
    one this build cannot decode) and must reach the caller unchanged.
    """
    deadline = time.monotonic() + timeout
    while True:
        wait(game_id, max(0.0, deadline - time.monotonic()))
        before = _mark(game_id)
        try:
            value = build()
        except Exception:
            if _mark(game_id) == before and not before[1]:
                raise
            if time.monotonic() >= deadline:
                raise
            continue
        if _mark(game_id) == before and not before[1]:
            return value
        if time.monotonic() >= deadline:
            return value


__all__ = ["WAIT_SECONDS", "generation", "in_flight", "is_in_flight", "stable_read", "wait"]
