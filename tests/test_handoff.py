"""`handoff.py`'s own gate, away from a game.

The behaviour it exists for is covered where it is reported - `test_mulligans
.py`'s `test_a_board_read_never_lands_inside_the_backstops_last_pregame_move`
drives a real game through the real window. What is here is the gate's own
contract, which that test cannot show: that it costs nothing when nothing is
in flight, that it really does block a waiter while one is, that it is bounded
rather than indefinite, that nesting cannot clear it early, and that it is
per-game.
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
