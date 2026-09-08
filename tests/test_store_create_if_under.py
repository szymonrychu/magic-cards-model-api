"""`GameStore.reserve_slot`/`attach`/`release` - the atomic `MAX_LIVE_GAMES`
cap check, M8's lobby-owned reservation.

`start_game_from_decklists` used to call `store.live_ui_games()` and
`store.create()` as two separately-locked steps: two concurrent requests
could both read the live count just under the cap and both go on to create,
letting the live table grow past the limit `MAX_LIVE_GAMES` exists to
enforce. `create_if_under` (deleted, M8) used to close that window with one
lock around both the count and the create. The reservation closes the same
window differently: `reserve_slot` claims one of `cap` slots - counting both
live ui games and every other outstanding reservation - *before* the
(possibly slow, possibly failing) game creation happens, so no concurrent
caller can ever observe room that has already been spoken for. `attach`
turns a successful creation into a real live game; `release` gives the slot
back when creation failed or never happened - `start_game_from_decklists`
calls it from a `finally`, so a raised exception cannot leak a slot either.

This file keeps its old name (`tests/test_store_create_if_under.py`) per the
2b plan's own T6 file list, even though `create_if_under` itself is gone -
renaming it is not this task's concern.
"""

from __future__ import annotations

import threading
import time

import pytest
from test_decks_api import LEGAL_DECK, LEGAL_DECK_B

from magic_cards_model_api.decks import load_deck_into_game
from magic_cards_model_api.store import GameStore


def _real_game():
    """A real engine `Game`, from a real (if minimal) legal decklist pair -
    exactly what `load_deck_into_game` builds for `POST /api/lobby/games`, no
    hand-built model."""
    match = load_deck_into_game([("you", LEGAL_DECK), ("bot", LEGAL_DECK_B)])
    return match.games[-1]


# ---------------------------------------------------------------------------
# the happy path: reserve, create, attach
# ---------------------------------------------------------------------------


def test_reserve_slot_below_cap_returns_a_token() -> None:
    store = GameStore()

    token = store.reserve_slot(cap=1)

    assert token


def test_reserve_slot_at_cap_refuses() -> None:
    store = GameStore()
    first = store.reserve_slot(cap=1)
    assert first is not None

    second = store.reserve_slot(cap=1)

    assert second is None


def test_attach_makes_the_game_count_as_live() -> None:
    store = GameStore()
    token = store.reserve_slot(cap=1)
    game_id = store.create(_real_game())

    store.attach(token, game_id)

    assert game_id in store.live_ui_games()
    assert game_id in store.ui_games()


def test_a_reservation_counts_against_the_cap_before_it_is_attached() -> None:
    """The whole point of reserve-then-create: the slot is spoken for the
    moment `reserve_slot` returns a token, not once `attach` runs - a second
    caller racing the gap between them must still be refused."""
    store = GameStore()
    store.reserve_slot(cap=1)

    second = store.reserve_slot(cap=1)

    assert second is None


def test_attach_and_reserve_slot_together_never_exceed_the_cap() -> None:
    store = GameStore()
    token = store.reserve_slot(cap=1)
    game_id = store.create(_real_game())
    store.attach(token, game_id)

    # The slot the first reservation held is now a live game, not a freed
    # reservation - a second reservation must still be refused.
    second = store.reserve_slot(cap=1)

    assert second is None


def test_attach_raises_key_error_for_an_unknown_token() -> None:
    store = GameStore()
    game_id = store.create(_real_game())

    with pytest.raises(KeyError):
        store.attach("not-a-real-token", game_id)


def test_attach_raises_key_error_for_an_already_attached_token() -> None:
    store = GameStore()
    token = store.reserve_slot(cap=5)
    game_id = store.create(_real_game())
    store.attach(token, game_id)

    with pytest.raises(KeyError):
        store.attach(token, store.create(_real_game()))


# ---------------------------------------------------------------------------
# a failed create: the reservation is released, and leaks no slot
# ---------------------------------------------------------------------------


def test_release_frees_the_slot_for_a_later_reservation() -> None:
    """The race the plan's M8 exists to close on the failure side: a caller
    reserves, game creation then fails (or raises) before `attach`, and
    `release` - called from that caller's `finally` - must give the slot
    back rather than leaking it forever."""
    store = GameStore()
    token = store.reserve_slot(cap=1)
    assert token is not None

    store.release(token)

    second = store.reserve_slot(cap=1)
    assert second is not None


def test_release_is_a_no_op_for_an_unknown_token() -> None:
    """`start_game_from_decklists` calls `release` unconditionally from a
    `finally` - including on the success path, after `attach` already
    resolved the same token. `release` must never raise there, or a
    perfectly successful game creation would blow up on its way out."""
    store = GameStore()

    store.release("never-reserved")  # must not raise


def test_release_after_attach_does_not_unattach_the_game() -> None:
    """The same unconditional-`finally` shape, on a token that already
    succeeded: `release` must be a no-op, never undoing a completed
    `attach`."""
    store = GameStore()
    token = store.reserve_slot(cap=1)
    game_id = store.create(_real_game())
    store.attach(token, game_id)

    store.release(token)  # must not raise, must not un-attach

    assert game_id in store.live_ui_games()


# ---------------------------------------------------------------------------
# a stale reservation from a process that died: it expires, it does not leak
# ---------------------------------------------------------------------------


def test_a_reservation_older_than_the_ttl_stops_counting_against_the_cap() -> None:
    """A process that reserved a slot and then died before calling `attach`
    or `release` - a hard crash, not an exception `finally` can catch - can
    never call either again. Nothing in this deployment's single-process,
    `replicas: 1` shape (M9) makes such a reservation durable across a
    restart of *this* process (a restart wipes `GameStore` outright, taking
    every reservation with it) - but a reservation that outlives its own
    request *without* the process dying (a caller that never reaches its
    `finally`, a bug) must still not hold a slot hostage forever. The TTL is
    that backstop: a reservation older than `RESERVATION_TTL_SECONDS` is
    treated as abandoned and no longer counts, the same "fail closed, and
    record it" this task's brief asks for - `reserve_slot` sweeps expired
    reservations on every call, so nothing needs its own timer thread."""
    store = GameStore()
    store.RESERVATION_TTL_SECONDS = 0.05
    token = store.reserve_slot(cap=1)
    assert token is not None

    time.sleep(0.1)

    second = store.reserve_slot(cap=1)
    assert second is not None


def test_attaching_an_expired_reservation_raises_key_error() -> None:
    """Once a reservation has expired it is indistinguishable from one that
    was never made - the slot it held may already have gone to someone
    else, so a late `attach` must never silently succeed and double-count."""
    store = GameStore()
    store.RESERVATION_TTL_SECONDS = 0.05
    token = store.reserve_slot(cap=1)
    game_id = store.create(_real_game())

    time.sleep(0.1)

    with pytest.raises(KeyError):
        store.attach(token, game_id)


# ---------------------------------------------------------------------------
# concurrency: N threads race, exactly `cap` reservations succeed
# ---------------------------------------------------------------------------


def test_reserve_slot_is_atomic_under_concurrent_callers() -> None:
    """N threads racing a cap of `cap`: exactly `cap` must win, never more
    (the `MAX_LIVE_GAMES` overshoot this method exists to close) and never
    fewer (a correct implementation does not under-admit either)."""
    store = GameStore()
    cap = 3
    attempts = 8
    results: list[str | None] = []
    lock = threading.Lock()
    ready = threading.Barrier(attempts)

    def attempt() -> None:
        ready.wait(timeout=5)
        token = store.reserve_slot(cap=cap)
        with lock:
            results.append(token)

    threads = [threading.Thread(target=attempt) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    succeeded = [r for r in results if r is not None]
    assert len(succeeded) == cap
    assert len(set(succeeded)) == cap  # every winner got a distinct token


def test_a_failed_create_leaves_no_leaked_slot_under_concurrency() -> None:
    """The failure-path race, under concurrency, in two waves so releasing a
    slot mid-wave (which legitimately lets a *concurrent* racer reuse it
    right away - not a leak) cannot be mistaken for one.

    Wave 1: exactly `cap` threads race `reserve_slot`, all `cap` win. Every
    *odd* winner then simulates a failed create (`release` instead of
    `attach` - exactly `start_game_from_decklists`'s `finally`), concurrently
    with the even winners' `attach`. Once every thread has finished, exactly
    the attached half must be live - nothing leaked, nothing double-counted.

    Wave 2, only after wave 1 fully joins: `cap` more threads race
    `reserve_slot` again. Exactly as many must win as wave 1 released - the
    freed slots came back, and not one more than that."""
    store = GameStore()
    cap = 4
    tokens: list[str | None] = []
    lock = threading.Lock()
    ready = threading.Barrier(cap)

    def wave_one(index: int) -> None:
        ready.wait(timeout=5)
        token = store.reserve_slot(cap=cap)
        with lock:
            tokens.append(token)
        assert token is not None  # exactly `cap` threads for `cap` slots
        if index % 2 == 0:
            game_id = store.create(_real_game())
            store.attach(token, game_id)
        else:
            store.release(token)  # simulated failed create

    threads = [threading.Thread(target=wave_one, args=(i,)) for i in range(cap)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    attached = sum(1 for i in range(cap) if i % 2 == 0)
    released = cap - attached
    assert len(store.live_ui_games()) == attached

    second_tokens: list[str | None] = []
    second_lock = threading.Lock()
    second_ready = threading.Barrier(cap)

    def wave_two() -> None:
        second_ready.wait(timeout=5)
        token = store.reserve_slot(cap=cap)
        with second_lock:
            second_tokens.append(token)

    second_threads = [threading.Thread(target=wave_two) for _ in range(cap)]
    for t in second_threads:
        t.start()
    for t in second_threads:
        t.join(timeout=5)

    succeeded_again = [t for t in second_tokens if t is not None]
    assert len(succeeded_again) == released  # exactly the freed slots came back
