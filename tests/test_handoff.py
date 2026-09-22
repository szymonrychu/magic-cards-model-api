"""`handoff.py`'s own gate, away from a game.

The behaviour it exists for is covered where it is reported - `test_mulligans
.py`'s `test_a_board_read_never_lands_inside_the_backstops_last_pregame_move`
drives a real game through the real window. What is here is the gate's own
contract, which that test cannot show: that it costs nothing when nothing is
in flight, that it really does block a waiter while one is, that it is bounded
rather than indefinite, that nesting cannot clear it early, and that it is
per-game.

`stable_read`'s own contract is here too: that a read overtaken *after* it
passed the gate is rebuilt rather than handed back, which is the ordering a
one-shot `wait` cannot hold and which
`test_a_board_read_that_passed_the_gate_first_still_never_sees_the_window`
drives through a real game.
"""

from __future__ import annotations

import threading
import time

from magic_cards_model_api import handoff


def test_a_read_waits_for_nothing_when_no_handoff_is_open() -> None:
    """The ordinary case - every board read but the handful that land in the
    window - must not touch a condition wait at all."""
    started = time.monotonic()
    assert handoff.wait("quiet-game", timeout=5.0) is True
    assert time.monotonic() - started < 0.5


def test_a_read_waits_until_the_handoff_closes() -> None:
    held = threading.Event()
    released = threading.Event()

    def hold() -> None:
        with handoff.in_flight("busy-game"):
            held.set()
            released.wait(5.0)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5.0)
    assert handoff.is_in_flight("busy-game")

    # Still open: the waiter gives up on its own bound rather than reading
    # through the window.
    assert handoff.wait("busy-game", timeout=0.1) is False

    released.set()
    assert handoff.wait("busy-game", timeout=5.0) is True
    holder.join(5.0)
    assert not holder.is_alive()
    assert not handoff.is_in_flight("busy-game")


def test_a_waiter_is_released_the_moment_the_handoff_closes() -> None:
    """Not on a poll interval - the gate notifies, so a read costs the
    mutation's own duration and nothing more."""
    held = threading.Event()

    def hold() -> None:
        with handoff.in_flight("short-game"):
            held.set()
            time.sleep(0.2)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5.0)

    started = time.monotonic()
    assert handoff.wait("short-game", timeout=5.0) is True
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, "the waiter sat past the handoff it was waiting on"
    holder.join(5.0)


def test_nesting_does_not_open_the_gate_early() -> None:
    with handoff.in_flight("nested-game"):
        with handoff.in_flight("nested-game"):
            assert handoff.is_in_flight("nested-game")
        assert handoff.is_in_flight("nested-game"), "the inner exit cleared the outer handoff"
    assert not handoff.is_in_flight("nested-game")


def test_a_handoff_closes_even_when_the_mutation_raises() -> None:
    """`finish_opening_hand_actions` raises `ValueError` for a pass that is
    not this seat's to make. A gate left open by that would stall every later
    read of the game for its whole bound."""
    try:
        with handoff.in_flight("failing-game"):
            raise ValueError("this pregame move was refused")
    except ValueError:
        pass
    assert not handoff.is_in_flight("failing-game")


def test_one_games_handoff_does_not_gate_another() -> None:
    with handoff.in_flight("game-a"):
        started = time.monotonic()
        assert handoff.wait("game-b", timeout=5.0) is True
        assert time.monotonic() - started < 0.5


# -- stable_read: the check after the build, not only before it ---------------


def test_a_build_nothing_races_runs_once_and_is_handed_straight_back() -> None:
    """The ordinary poll: no handoff anywhere near it, so no wait, no rebuild,
    no second pass over `state_view`."""
    runs = []

    def build() -> str:
        runs.append(None)
        return "the board"

    started = time.monotonic()
    assert handoff.stable_read("calm-game", build) == "the board"
    assert len(runs) == 1, "an uncontended read built its view more than once"
    assert time.monotonic() - started < 0.5


def test_a_build_overtaken_by_a_handoff_is_thrown_away_and_rebuilt() -> None:
    """The finding this exists for: the read passes the gate while nothing is
    in flight, and the pregame's last move opens its handoff while the view is
    still being assembled. The half-applied view must not be what comes back.
    """
    seen = []

    def build() -> str:
        if not seen:
            seen.append("half-applied")
            # The mutation overtakes this build, exactly as the backstop's
            # thread does to a poll: opened and closed while we were working.
            with handoff.in_flight("overtaken-game"):
                pass
            return "half-applied"
        seen.append("settled")
        return "settled"

    assert handoff.stable_read("overtaken-game", build) == "settled"
    assert seen == ["half-applied", "settled"], "the overtaken view was not rebuilt"


def test_a_handoff_still_open_when_the_build_ends_is_rebuilt_too() -> None:
    """`_open` alone, unlike the generation counter, does catch this one - but
    only because the handoff is still open at the check. Both halves of the
    mark are load-bearing and both are asserted."""
    holder = []

    def build() -> str:
        if not holder:
            opened = handoff.in_flight("straddled-game")
            opened.__enter__()
            holder.append(opened)
            return "half-applied"
        holder.pop().__exit__(None, None, None)
        return "settled"

    assert handoff.stable_read("straddled-game", build) == "settled"
    assert not handoff.is_in_flight("straddled-game")


def test_a_read_that_cannot_win_the_race_is_still_bounded() -> None:
    """A handoff that never closes must not hang the poll - the same bound
    `wait` carries, for the same reason. What comes back is the last view
    built, which is what the route would have handed back before any of this
    existed."""
    with handoff.in_flight("stuck-game"):
        started = time.monotonic()
        assert handoff.stable_read("stuck-game", lambda: "whatever is there", timeout=0.2) == (
            "whatever is there"
        )
        elapsed = time.monotonic() - started
    assert elapsed < 5.0, "a read against a stuck handoff did not respect its bound"


def test_a_build_that_raises_uncontended_is_not_retried() -> None:
    """A 404 for a game that does not exist is the read's own answer, not a
    race to be retried - it must reach the caller unchanged, and once."""
    runs = []

    def build() -> str:
        runs.append(None)
        raise LookupError("no game gone-game")

    try:
        handoff.stable_read("gone-game", build)
    except LookupError:
        pass
    else:  # pragma: no cover - the raise above is the point
        raise AssertionError("the build's own exception was swallowed")
    assert len(runs) == 1, "an uncontended failure was retried"


def test_a_build_that_raises_because_it_was_overtaken_is_retried() -> None:
    """Half-applied state can make an assembling view raise rather than merely
    come out wrong. That is still a race, and it is still rebuilt."""
    runs = []

    def build() -> str:
        runs.append(None)
        if len(runs) == 1:
            with handoff.in_flight("raising-game"):
                pass
            raise RuntimeError("read a game mid-move")
        return "settled"

    assert handoff.stable_read("raising-game", build) == "settled"
    assert len(runs) == 2


def test_the_generation_counter_only_ever_moves_forward() -> None:
    before = handoff.generation()
    with handoff.in_flight("counted-game"):
        assert handoff.generation() == before + 1
    # Closing does not move it back: a read that noted the count before a
    # handoff that has since ended must still see a different number.
    assert handoff.generation() == before + 1
