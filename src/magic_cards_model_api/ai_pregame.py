"""A backstop that keeps a bot seat from hanging the human's pregame.

**The bug this exists for.** A game booted from the play UI rests in
`Step.MULLIGAN` and then `Step.PREGAME_ACTIONS`, and CR 103.5 needs *every*
player's declaration ("first, the starting player declares whether they will
take a mulligan; then each other player in turn order does the same") before
the round resolves, while CR 103.6 hands the opening-hand window to one seat
at a time ("once the mulligan process is complete, the starting player may
take any such actions in any order; then each other player in turn order does
the same"). The opponent in that UI is an AI seat: a headless `claude`
subprocess (`ai_seat.py`) that plays through `seat_cli`.

Which means, before this module, the human's game start was gated on a
language model *choosing* to run two commands. Driven live on this machine,
it did not: the subprocess declared its CR 103.5 keep, then decided the
pregame was "nothing to act on yet", exited 0, and the game sat in
`Step.PREGAME_ACTIONS` with `opening_hand_actor` pointing at a seat whose
process was gone. The human's panel said "waiting on claude" forever and no
button they could press would move it. That is exactly the reported symptom -
"mulligans still not working (nor pregame actions)".

**What this does.** One daemon thread per game with bot seats. It watches for
an obligation owed *by a bot seat* - a CR 103.5 declaration it has not made,
or a CR 103.6 window it holds - and, when nothing is playing that seat any
more, makes the engine's own default move for it: keep the dealt hand
(bottoming the back of the hand, `opening._bottom_answer`'s documented
default), and decline the opening-hand actions. Recorded in the audit log as
`automatic`, `live: false`, so a transcript never claims the bot chose it.

**"When nothing is playing that seat" is measured, not guessed.** The first
version of this module fired on a timer alone: a bot seat that had not moved
within `AI_PREGAME_GRACE_SECONDS` had its move made for it. That is a race
against how long the agent takes to think, and the agent won it only by
accident. The grace period was sized against a measured subprocess that
declared in about 12 seconds; the agent's prompts were then rewritten to make
it plan and investigate before acting, it stopped beating 20 seconds, and the
backstop began declaring first. The agent's own declaration then arrived to a
400 - and in the observed case it happened to be a keep, matching the default,
so nothing looked wrong. **Had it decided to mulligan, that decision would
have been discarded and the seat would have played a hand it had rejected.**

No number fixes that: too short overrules a thinking agent, too long strands a
human behind a dead one, and the right value changes every time the prompts
do. So the timer no longer decides. `ai_seat.registry.activity` does
(`ai_seat.WORKING` / `STARTING` / `GONE`), off what the server already knows
about the process or agent holding the seat - is the subprocess running, does
the driver have a live turn in flight - and only a `GONE` seat is settled for.
A thinking seat is left alone for as long as it is visibly thinking, and a
seat still booting has not been given its chance yet, so no clock runs against
it at all.

Two bounds remain, because "left alone while working" must not mean "for
ever":

- `AI_PREGAME_GRACE_SECONDS` is now how long a seat must be *continuously
  gone* before it is settled for, and how often liveness is re-read. It is no
  longer a guess about how long thinking takes, so it does not need retuning
  when the agent gets slower.
- `AI_PREGAME_MAX_WAIT_SECONDS` is the hard ceiling on how long a human may be
  made to wait on a bot seat's pregame move regardless of how alive it looks -
  the case where the agent is up, playing, and simply never declares.

**And the loser of the race fails loudly.** A live seat's declaration can
still arrive just after the backstop's, and when it does `opening.py` refuses
it with `PregameOverruled` - a 409 whose text says a backstop already moved
for this seat and that any differing decision has been lost - and records a
banner in the audit log. Silently answering 400 is what made the live bug
invisible.

**What it will never do.** Touch a seat that is not in `store.ai_players`.
The set is populated only from the explicit `ai: true` flag on
`POST /api/ui/games`; a human seat is never inferred into it, and this module
takes the set as given rather than guessing from "who has no process". The
human's own mulligan is theirs, however long they take over it.

Liveness is what preserves the bot's agency: it declares for itself whenever
it is alive and willing, and only a seat that has actually gone gets moved
along.

CR 103.5 and CR 103.6 were verified with `knowledge_search(type="rules")` in
the session that wrote this module.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from magic_cards_model.models import Game

from . import ai_seat, opening
from .store import store

#: How long a bot seat must be *continuously gone* - no process, no live turn -
#: before this backstop takes its pregame move for it, and how often that
#: liveness is re-read. Deliberately no longer a guess about how long thinking
#: takes: a seat that is working is never settled for however long this is.
DEFAULT_GRACE_SECONDS = 20.0

#: The hard ceiling on how long a human may be made to wait for one pregame
#: move from a bot seat, however alive that seat looks. It exists for the seat
#: that is genuinely up and playing and simply never declares: liveness alone
#: would wait for it for ever, and a human at a panel would have no way out.
#: Ten minutes - far above a boot (`AGENT_BOOT_WAIT_SECONDS` defaults to 180)
#: plus any amount of thinking, so reaching it means something is wrong rather
#: than slow, and it is recorded as such.
DEFAULT_MAX_WAIT_SECONDS = 600.0

#: How often the obligation is re-read. Only ever touches the game lock.
POLL_SECONDS = 0.25

#: Hard ceiling on one watcher's life, so a game abandoned mid-pregame does
#: not leave a thread polling for the life of the process.
MAX_LIFETIME_SECONDS = 3600.0


#: game_id -> the event that ends that game's watcher thread early.
#:
#: Without this, a watcher only ever stopped by finishing the pregame, by its
#: game disappearing from the store, or by `MAX_LIFETIME_SECONDS` - an hour of
#: a thread polling a game nobody is playing. `lobby.py` sets the event when
#: the lobby abandons a game, so the old game's watcher is gone *before* the
#: new game exists rather than an hour after it.
_watchers: dict[str, threading.Event] = {}
_watchers_lock = threading.Lock()


def stop(game_id: str) -> bool:
    """End this game's watcher thread. `True` if one was running.

    The thread is asked to stop rather than killed - it checks the event once
    per `POLL_SECONDS` and between every step of a pass, so it can never be
    interrupted half way through settling an obligation.
    """
    with _watchers_lock:
        event = _watchers.pop(game_id, None)
    if event is None:
        return False
    event.set()
    return True


def is_watching(game_id: str) -> bool:
    """Whether a watcher thread is registered for this game right now."""
    with _watchers_lock:
        return game_id in _watchers


def _seconds(name: str, fallback: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        return max(0.0, float(raw))
    except ValueError:
        return fallback


def grace_seconds() -> float:
    return _seconds("AI_PREGAME_GRACE_SECONDS", DEFAULT_GRACE_SECONDS)


def max_wait_seconds() -> float:
    return _seconds("AI_PREGAME_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS)


def seat_activity(game_id: str, player: str) -> str:
    """Whether anything is playing `player`'s seat right now.

    One of `ai_seat.WORKING`, `ai_seat.STARTING`, `ai_seat.GONE`. A seat whose
    uid this game does not know is `GONE` - the seat exists in the game and
    nothing holds it - which is also the honest answer for a game whose AI
    seat was flagged but never handed to anything.

    Deliberately not called under the game lock: in wrapper mode it asks the
    driver over HTTP, and holding a game's mutation lock across a network call
    is how this API's pregame used to freeze in the first place.
    """
    for uid, name in store.seat_uids(game_id).items():
        if name == player:
            return ai_seat.registry.activity(uid)
    return ai_seat.GONE


def _why_gone(game_id: str, player: str) -> str:
    for uid, name in store.seat_uids(game_id).items():
        if name == player:
            return ai_seat.registry.why_gone(uid)
    return "this game has no seat credential for that player"


def obligation(game: Game, ai_players: set[str]) -> tuple[str, str] | None:
    """What a bot seat owes the pregame right now, or `None`.

    `("mulligan", name)` for a CR 103.5 declaration still owed, or
    `("opening_hand", name)` for a CR 103.6 window that seat is holding.
    Nothing is ever returned for a seat outside `ai_players`.
    """
    if not game.in_pregame:
        return None
    for name in game.awaiting_mulligan_declarations:
        if name in ai_players:
            return ("mulligan", name)
    actor = game.opening_hand_actor
    if actor is not None and actor in ai_players:
        return ("opening_hand", actor)
    return None


def settle(
    game_id: str, game: Game, owed: tuple[str, str], reason: str | None = None
) -> dict[str, Any]:
    """Take the engine's default move for one owed obligation.

    Keeping is the default for CR 103.5 (`bottom=None` bottoms the back of the
    hand, exactly what `Player.keep_hand(None)` does), and declining is the
    default for CR 103.6 - the same two defaults `Game.settle_pregame` uses,
    applied one seat at a time so the human's own turn is never taken.

    `reason` is the evidence that made this legitimate - which seat was found
    gone and how - and lands in the audit entry beside `automatic: true`, so a
    reader can tell a crashed agent from a missing binary from a seat that was
    up and simply never declared.
    """
    kind, name = owed
    if kind == "mulligan":
        return opening.declare_mulligan(
            game_id, game, name, False, None, automatic=True, reason=reason
        )
    return opening.finish_opening_hand_actions(game_id, game, name, automatic=True, reason=reason)


def settle_now(
    game_id: str,
    ai_players: set[str],
    *,
    expect: tuple[str, str] | None = None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Settle whatever a bot seat owes right now, under the game lock.

    Returns the result, or `None` when nothing is owed. Separate from `watch`
    so a test can drive the decision without waiting on a clock.

    `expect` is the obligation the caller decided about. The game lock is
    dropped between deciding and settling - liveness is read over HTTP and must
    not be read under the lock - so the seat may have declared for itself in
    that window. Re-reading the obligation here and refusing to settle anything
    but `expect` is what keeps that window from costing the seat its own move:
    the check and the write happen under one hold of the lock.
    """
    with store.locked(game_id) as game:
        owed = obligation(game, ai_players)
        if owed is None or (expect is not None and owed != expect):
            return None
        return settle(game_id, game, owed, reason)


def watch(
    game_id: str,
    ai_players: set[str],
    *,
    grace: float | None = None,
    max_wait: float | None = None,
    poll: float = POLL_SECONDS,
    max_lifetime: float = MAX_LIFETIME_SECONDS,
    stop_event: threading.Event | None = None,
) -> None:
    """Block until this game's pregame is over, settling for a bot seat that
    has gone. Blocking on purpose: `start` runs it on a thread, and a test runs
    it inline with a tiny grace.

    The shape of one pass: read what is owed under the game lock and let go of
    it; if the grace window on that obligation has elapsed, ask - without the
    lock - whether anything is still playing that seat; settle only for a seat
    that is `GONE`, or for any seat at all once `max_wait` has run out.

    `stop_event`, set by `stop()`, ends the loop between passes - the lobby
    abandoning this game must not leave a thread that can still take a seat's
    pregame move in a game the human has walked away from.
    """
    if not ai_players:
        return
    if grace is None:
        grace = grace_seconds()
    if max_wait is None:
        max_wait = max_wait_seconds()
    started = time.monotonic()
    pending: tuple[str, str] | None = None
    look_again = 0.0
    give_up = 0.0
    while time.monotonic() - started < max_lifetime:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            with store.locked(game_id) as game:
                if not game.in_pregame:
                    return
                owed = obligation(game, ai_players)
        except KeyError:  # the game was deleted out from under us
            return
        now = time.monotonic()
        if owed is None:
            pending, look_again, give_up = None, 0.0, 0.0
        elif owed != pending:
            # A new obligation: start its clocks, and give the seat itself the
            # first chance at it.
            pending, look_again, give_up = owed, now + grace, now + max_wait
        elif now >= look_again:
            activity = seat_activity(game_id, owed[1])
            if activity == ai_seat.GONE:
                settle_now(
                    game_id,
                    ai_players,
                    expect=owed,
                    reason=f"nothing is playing this seat: {_why_gone(game_id, owed[1])}",
                )
                pending = None
            elif now >= give_up:
                settle_now(
                    game_id,
                    ai_players,
                    expect=owed,
                    reason=(
                        f"the seat is {activity} but has not made this pregame move in "
                        f"{max_wait:.0f}s (AI_PREGAME_MAX_WAIT_SECONDS) - a human cannot "
                        "be made to wait on it any longer"
                    ),
                )
                pending = None
            else:
                # Visibly still working. Leave its move to it and look again
                # after another grace period.
                look_again = now + grace
        if stop_event is not None:
            if stop_event.wait(poll):
                return
        else:
            time.sleep(poll)


def start(game_id: str, ai_players: list[str]) -> None:
    """Register `ai_players` as this game's bot seats and watch their pregame.

    A no-op for a game with no bot seats, which is every game booted by any
    path but the play UI's `ai: true`.
    """
    if not ai_players:
        return
    store.register_ai_players(game_id, ai_players)
    event = threading.Event()
    with _watchers_lock:
        _watchers[game_id] = event

    def run() -> None:
        try:
            watch(game_id, set(ai_players), stop_event=event)
        finally:
            # Deregister only if this thread's own event is still the one
            # registered: a `stop()` that already popped it, or a later
            # watcher for the same id, must not be forgotten by this one.
            with _watchers_lock:
                if _watchers.get(game_id) is event:
                    del _watchers[game_id]

    threading.Thread(target=run, daemon=True, name=f"ai-pregame-{game_id}").start()


__all__ = [
    "grace_seconds",
    "is_watching",
    "max_wait_seconds",
    "obligation",
    "seat_activity",
    "settle",
    "settle_now",
    "start",
    "stop",
    "watch",
]
