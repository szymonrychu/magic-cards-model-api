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
or a CR 103.6 window it holds - and if the seat has not taken it within
`AI_PREGAME_GRACE_SECONDS`, makes the engine's own default move for it:
keep the dealt hand (bottoming the back of the hand, `opening._bottom_answer`'s
documented default), and decline the opening-hand actions. Recorded in the
audit log as `automatic`, `live: false`, so a transcript never claims the bot
chose it.

**What it will never do.** Touch a seat that is not in `store.ai_players`.
The set is populated only from the explicit `ai: true` flag on
`POST /api/ui/games`; a human seat is never inferred into it, and this module
takes the set as given rather than guessing from "who has no process". The
human's own mulligan is theirs, however long they take over it.

The grace period is what preserves the bot's agency: it declares for itself
whenever it is alive and willing, and only a seat that has gone quiet gets
moved along.

CR 103.5 and CR 103.6 were verified with `knowledge_search(type="rules")` in
the session that wrote this module.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from magic_cards_model.models import Game

from . import opening
from .store import store

#: How long a bot seat is given to take its own pregame move before this
#: backstop takes the default one for it. Long enough that a live agent
#: normally wins the race (the measured subprocess declared in ~12 s), short
#: enough that a dead one does not strand a human at a dead panel.
DEFAULT_GRACE_SECONDS = 20.0

#: How often the obligation is re-read. Only ever touches the game lock.
POLL_SECONDS = 0.25

#: Hard ceiling on one watcher's life, so a game abandoned mid-pregame does
#: not leave a thread polling for the life of the process.
MAX_LIFETIME_SECONDS = 3600.0


def grace_seconds() -> float:
    raw = os.environ.get("AI_PREGAME_GRACE_SECONDS")
    if not raw:
        return DEFAULT_GRACE_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_GRACE_SECONDS


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


def settle(game_id: str, game: Game, owed: tuple[str, str]) -> dict[str, Any]:
    """Take the engine's default move for one owed obligation.

    Keeping is the default for CR 103.5 (`bottom=None` bottoms the back of the
    hand, exactly what `Player.keep_hand(None)` does), and declining is the
    default for CR 103.6 - the same two defaults `Game.settle_pregame` uses,
    applied one seat at a time so the human's own turn is never taken.
    """
    kind, name = owed
    if kind == "mulligan":
        return opening.declare_mulligan(game_id, game, name, False, None, automatic=True)
    return opening.finish_opening_hand_actions(game_id, game, name, automatic=True)


def settle_now(game_id: str, ai_players: set[str]) -> dict[str, Any] | None:
    """Settle whatever a bot seat owes right now, under the game lock.

    Returns the result, or `None` when nothing is owed. Separate from `watch`
    so a test can drive the decision without waiting on a clock.
    """
    with store.locked(game_id) as game:
        owed = obligation(game, ai_players)
        if owed is None:
            return None
        return settle(game_id, game, owed)


def watch(
    game_id: str,
    ai_players: set[str],
    *,
    grace: float | None = None,
    poll: float = POLL_SECONDS,
    max_lifetime: float = MAX_LIFETIME_SECONDS,
) -> None:
    """Block until this game's pregame is over, settling for a bot seat that
    lets its grace period lapse. Blocking on purpose: `start` runs it on a
    thread, and a test runs it inline with a tiny grace."""
    if not ai_players:
        return
    if grace is None:
        grace = grace_seconds()
    started = time.monotonic()
    pending: tuple[str, str] | None = None
    deadline = 0.0
    while time.monotonic() - started < max_lifetime:
        try:
            with store.locked(game_id) as game:
                if not game.in_pregame:
                    return
                owed = obligation(game, ai_players)
                now = time.monotonic()
                if owed is None:
                    pending, deadline = None, 0.0
                elif owed != pending:
                    # A new obligation: start its clock, and give the seat
                    # itself the first chance at it.
                    pending, deadline = owed, now + grace
                elif now >= deadline:
                    settle(game_id, game, owed)
                    pending, deadline = None, 0.0
                    continue
        except KeyError:  # the game was deleted out from under us
            return
        time.sleep(poll)


def start(game_id: str, ai_players: list[str]) -> None:
    """Register `ai_players` as this game's bot seats and watch their pregame.

    A no-op for a game with no bot seats, which is every game booted by any
    path but the play UI's `ai: true`.
    """
    if not ai_players:
        return
    store.register_ai_players(game_id, ai_players)
    threading.Thread(
        target=watch, args=(game_id, set(ai_players)), daemon=True, name=f"ai-pregame-{game_id}"
    ).start()


__all__ = ["grace_seconds", "obligation", "settle", "settle_now", "start", "watch"]
