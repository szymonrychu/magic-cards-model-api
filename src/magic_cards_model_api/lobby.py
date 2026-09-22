"""Ending a game is a fresh start, for the game and the seat.

Games the lobby boots live side by side, up to `MAX_LIVE_GAMES`
(`decks_api.max_live_games`) - starting a new one from the lobby never touches
another. **End game** (`DELETE /api/lobby/games/{id}`,
`decks_api.end_lobby_game`) is the only reset the lobby has, and it is
explicit: a human picks a game in the lobby and ends it, rather than starting
a new one silently ending an old one.

`reset_game` covers, per game:

- the watcher thread, asked to stop (`ai_pregame.stop`) so nothing can make a
  pregame move in a game nobody is at,
- every AI seat, released (`ai_seat.registry.release`) - a subprocess is
  terminated, a wrapper dispatch is handed back to the driver, and the
  registry forgets the uid so no stale status can be polled out of it,
- every **buddy** (`repository.Advisor`, B5), released the same way and in
  the same pass: an advisor has a runner behind it exactly like a seat does,
  so leaving one out would leave a pod advising on a game that no longer
  exists, and a status pollable out of a finished game,
- the game itself, deleted from the store, which is what takes the old seat
  uids and the old admin key out of existence.

**What it never touches: the agent's memory.** The point of the continuous
agent is that it carries what it learned between games, and that lives in the
`./memory` volume mounted into the agent and driver containers - not in
anything here. Releasing a seat runs the driver's ordinary end-of-match
`retire`, which ends the agent's *session*; the notes on disk are untouched
and are exactly what the next game starts from.

**And it is written down.** Every reset records `game_reset` in the ended
game's own audit log, naming what it covered, before the game is deleted - so
a transcript ends with the reset rather than trailing off.

**Two routes end a game, and both come through here.** End game is the
lobby's; `DELETE /api/games/{id}` (`app.delete_game`) is the other, and it
deletes *any* game rather than only a lobby-booted one. It used to record the
deletion and drop the game and nothing else, which left every one of the
bullets above undone: the watcher polling, the seat's process alive, the
registry still answering for uids of a game that no longer existed. In
`wrapper` mode that leak is permanent rather than merely untidy - the driver
stays assigned, so it refuses (409) every later dispatch for the life of that
container, because "free again once that game ends" is a promise only this
release keeps. So it delegates here too, and the `action` argument is how the
ended game's own transcript still says which of the two routes ended it.

**A game that ends on its own retires itself, through `retire_game`** - run
from `app._record_game_over` the moment a committed command leaves
`Game.is_over` true (a concession, CR 104.3a; a lethal-damage sweep; a draw).
Retiring stops the pregame watcher and **nothing else**. The AI seat's work is
not finished when the board is: its reflection turn, the Opus review and its
handover all run after game over, on the seat's own uid, so every seat and
advisor uid stays valid and every runner keeps running. The driver releases
the pod itself once the handover is done (`run_handover` -> `retire` ->
`exit_after_match` ends the Job). Releasing at game over, as this used to,
sent the driver down its no-reflection handover and filed a review that
failed (live game 23868a95). The game itself is not deleted either: a
finished game is still there to read, still refuses every action, and still
has a review; an ended one is gone.

**The post-game work is bounded** (`POSTGAME_RELEASE_SECONDS`). Past that
bound after the `game_over` row, `release_overdue` hard-releases whatever is
still running, once. Two things call it: a daemon timer `retire_game` arms,
and a lazy check in `post_game_work`, which the lobby listing asks for every
finished row. An api restart loses the timer; the lazy check and the Job's own
`activeDeadlineSeconds: 14400` (`deploy/seat-job.yaml`) are the backstop for
that. End game (`reset_game`) stays the hard stop at any time.

Nothing here resets a game nobody asked about. `store.register_ui_game` is set
by `POST /api/lobby/games` alone; a game booted by `serve.py --match` or by
`POST /api/games` is somebody else's, and both routes reach this function only
for the one game id their caller named and proved a credential for.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import ai_pregame, ai_seat, audit
from .repository import RECENT_RECORDS_WINDOW, GameUnloadable
from .store import known_players, store

DEFAULT_REASON = "ended from the lobby"
DEFAULT_ACTION = "game_reset"

#: What `retire_game` records itself under, and why. A separate name from
#: `game_reset`/`delete_game` on purpose: those three words all mean "a human
#: took this game away", and this one means "the game itself finished and the
#: runners were let go with it", which is a different fact about a different
#: ledger position. `scripts/eval_report.py` lists it beside them.
RETIRE_ACTION = "game_retired"
RETIRE_REASON = "the game is over"

#: The driver's allowances, mirrored because the api does not ship the
#: driver package: `mtg_agent.driver.handover.HANDOVER_BUDGET_SECONDS` and
#: `mtg_agent.driver.app.RETIRE_REVIEW_JOIN_TIMEOUT`.
#: `tests/test_post_game_lifecycle.py` pins both against the real values.
DRIVER_HANDOVER_BUDGET_SECONDS = 1680.0
DRIVER_RETIRE_REVIEW_JOIN_TIMEOUT = 1330.0

#: What a reflection turn is given before the handover starts. It is one
#: short turn that writes the agent's memory notes, far shorter than the
#: wrapper's 1800s stall bound on an ordinary turn.
DRIVER_REFLECTION_ALLOWANCE_SECONDS = 600.0

POSTGAME_RELEASE_SECONDS = (
    DRIVER_REFLECTION_ALLOWANCE_SECONDS
    + DRIVER_HANDOVER_BUDGET_SECONDS
    + DRIVER_RETIRE_REVIEW_JOIN_TIMEOUT
)
"""How long after the `game_over` row a finished game's runners may keep
doing post-game work before `release_overdue` hard-releases them: the
reflection turn (`DRIVER_REFLECTION_ALLOWANCE_SECONDS`), then the handover
(`DRIVER_HANDOVER_BUDGET_SECONDS`, 1680s), then `retire`'s join on the Opus
review thread (`DRIVER_RETIRE_REVIEW_JOIN_TIMEOUT`, 1330s) - about an hour,
3610s. Past that the driver has overrun every allowance it has, so holding
the row in "recording plays..." any longer only hides a stuck pod."""

#: The wall clock `post_game_work` measures the bound on, against the
#: `game_over` row's own `ts`. A module attribute so a test can move it.
clock: Callable[[], float] = time.time

#: game_id -> the daemon timer `retire_game` armed for `release_overdue`.
_release_timers: dict[str, threading.Timer] = {}
#: Games `release_overdue` already ran for in this process - its "once".
_released_overdue: set[str] = set()
_timers_lock = threading.Lock()


def _release_runners(game_id: str) -> dict[str, Any]:
    """Let go of every live thing this api holds *beside* `game_id`'s board -
    the pregame watcher thread, whatever the registry holds for each seat uid,
    and the same for each buddy - and report what was covered.

    Split out of `reset_game` for `release_overdue`, which needs exactly this
    and must not delete the game. Nothing here reads the board, which is what
    makes it work unchanged on a game this build cannot decode.
    """
    seats = store.seat_uids(game_id)  # uid -> username
    advisors = store.advisor_uids(game_id)  # uid -> repository.Advisor

    watcher_stopped = ai_pregame.stop(game_id)
    # Every uid is released, not only the ones flagged `ai: true`: the flag
    # says what the lobby *meant* a seat to be, and the registry is what
    # actually holds a process or a dispatch. A uid it knows nothing about
    # releases to nothing, cheaply and without touching the driver, so
    # covering the human's seat too costs nothing and leaves no uid behind.
    released = [
        entry | {"player": name}
        for uid, name in seats.items()
        if (entry := ai_seat.registry.release(uid))["mechanism"] is not None
    ]
    # The buddies, in the same pass and through the same registry call. Same
    # reasoning as the seats above: the advisor map is what actually holds
    # the credential, the registry is what holds the process or dispatch
    # behind it, and a uid the registry knows nothing about releases to
    # nothing, cheaply. `role` is stamped on the advisor entries alone, the
    # same convention the Job labels and the dispatch payload follow: a seat
    # entry stays byte-identical to what it always was, and absence means
    # seat.
    released += [
        entry | {"player": advisor.player, "role": ai_seat.ROLE_ADVISOR}
        for uid, advisor in advisors.items()
        if (entry := ai_seat.registry.release(uid))["mechanism"] is not None
    ]
    return {
        "seats_invalidated": len(seats),
        "advisors_invalidated": len(advisors),
        "watcher_stopped": watcher_stopped,
        "ai_seats_released": released,
    }


def _runner_uids(game_id: str) -> list[tuple[str, str, str]]:
    """`(uid, player, role)` for every seat and advisor uid of `game_id`."""
    seats = [(uid, name, ai_seat.ROLE_SEAT) for uid, name in store.seat_uids(game_id).items()]
    advisors = [
        (uid, advisor.player, ai_seat.ROLE_ADVISOR)
        for uid, advisor in store.advisor_uids(game_id).items()
    ]
    return seats + advisors


def _finishing(game_id: str) -> list[str]:
    """`player:mechanism` for each seat or advisor of `game_id` whose runner
    is still finishing its post-game work (`AiSeatRegistry.finishing`)."""
    names = []
    for uid, player, role in _runner_uids(game_id):
        if ai_seat.registry.finishing(uid):
            label = f"{player}:{ai_seat.registry.mechanism(uid)}"
            names.append(label if role == ai_seat.ROLE_SEAT else f"{label} ({role})")
    return names


def retire_game(game_id: str) -> dict[str, Any]:
    """The game ended on its own: stop its pregame watcher, and leave
    everything else - the game, its uids, its runners - exactly where it is.

    Called from `app._record_game_over`, the one place both `_run_command`
    and `_resume` funnel a committed ending through, so this covers every way
    a game reaches CR 104.1. The watcher exists to settle a pregame
    obligation, and a finished game has none left.

    **It releases no runner.** The AI seat's reflection turn, its Opus review
    and its handover are all post-game work done on the seat's own uid; a
    release here is what sent the driver down `handover_without_reflection`
    and failed the review in game 23868a95. Advisors hand over too and get
    the same treatment. The driver ends its own pod when the handover is
    done; `release_overdue`, armed here on a daemon timer, is the bounded
    backstop for one that does not, and End game (`reset_game`) stays the
    hard stop.

    **It does not delete the game.** A finished game stays readable, keeps
    refusing every action with `commands.GameOverError`, and keeps its
    review reachable.

    Recorded as `game_retired` with the payload keys it always carried
    (`scripts/eval_report.py` reads them): nothing was invalidated or
    released, so both counts are 0 and `ai_seats_released` is `[]`, and the
    note names the runners left to finish. Recorded only when there is
    something to say - a watcher stopped or a runner still finishing.
    """
    watcher_stopped = ai_pregame.stop(game_id)
    finishing = _finishing(game_id)
    covered: dict[str, Any] = {
        "seats_invalidated": 0,
        "advisors_invalidated": 0,
        "watcher_stopped": watcher_stopped,
        "ai_seats_released": [],
        "post_game_work": finishing,
    }
    if any(ai_seat.registry.mechanism(uid) for uid, _, _ in _runner_uids(game_id)):
        _arm_release_timer(game_id)
    if not (watcher_stopped or finishing):
        return covered
    watcher = "stopped" if watcher_stopped else "was not running"
    left = ", ".join(finishing) or "none"
    audit.record(
        game_id,
        RETIRE_ACTION,
        reason=RETIRE_REASON,
        note=(
            f"{RETIRE_REASON}: pregame watcher {watcher}; nothing released, "
            f"left to finish post-game work [{left}]"
        ),
        **covered,
    )
    return covered


def _arm_release_timer(game_id: str) -> None:
    timer = threading.Timer(POSTGAME_RELEASE_SECONDS, _release_timer_fired, args=(game_id,))
    timer.daemon = True
    timer.name = f"postgame-release-{game_id}"
    with _timers_lock:
        previous = _release_timers.pop(game_id, None)
        _release_timers[game_id] = timer
    if previous is not None:
        previous.cancel()
    timer.start()


def _release_timer_fired(game_id: str) -> None:
    try:
        release_overdue(game_id)
    finally:
        with _timers_lock:
            _release_timers.pop(game_id, None)


def _cancel_release_timer(game_id: str) -> None:
    with _timers_lock:
        timer = _release_timers.pop(game_id, None)
        _released_overdue.discard(game_id)
    if timer is not None:
        timer.cancel()


def _ended_at(game_id: str) -> float | None:
    """The `game_over` row's `ts` as epoch seconds, or `None` when the
    bounded window holds none (or its stamp does not parse)."""
    for row in reversed(store.recent_records(game_id, RECENT_RECORDS_WINDOW)):
        payload = row["payload"]
        if payload.get("action") == audit.GAME_OVER_ACTION:
            try:
                return datetime.fromisoformat(str(payload.get("ts"))).timestamp()
            except ValueError:
                return None
    return None


def _overdue_reason() -> str:
    return f"post-game work overran {int(POSTGAME_RELEASE_SECONDS)}s"


def _already_released_overdue(game_id: str) -> bool:
    """The "once" of `release_overdue`: this process ran it, or the ledger
    already carries its row (an api restart forgets the first)."""
    with _timers_lock:
        if game_id in _released_overdue:
            return True
    for row in store.recent_records(game_id, RECENT_RECORDS_WINDOW):
        payload = row["payload"]
        if payload.get("action") == RETIRE_ACTION and str(
            (payload.get("detail") or {}).get("reason", "")
        ).startswith("post-game work overran"):
            with _timers_lock:
                _released_overdue.add(game_id)
            return True
    return False


def release_overdue(game_id: str) -> bool:
    """Hard-release every runner of a finished game whose post-game work
    overran `POSTGAME_RELEASE_SECONDS`, through `_release_runners` - once.

    Recorded as `game_retired` with reason "post-game work overran Ns" when
    anything was actually released; a game whose runners all released
    themselves in time has nothing to say here. Always answers `False`,
    which is `post_game_work`'s answer once this has run: nothing is left
    finishing.
    """
    if _already_released_overdue(game_id):
        return False
    with _timers_lock:
        _released_overdue.add(game_id)
    covered = _release_runners(game_id)
    if covered["ai_seats_released"]:
        reason = _overdue_reason()
        audit.record(
            game_id,
            RETIRE_ACTION,
            reason=reason,
            note=_note(covered, reason),
            **covered,
        )
    return False


def post_game_work(game_id: str) -> bool:
    """Is a finished game's AI still doing its post-game work: the game is
    over and some seat or advisor runner is still finishing
    (`AiSeatRegistry.finishing`).

    The lobby's "recording plays..." row reads this (`LobbyGameSummary.
    post_game_work`), which hides Forget until the pod has released itself.
    Bounded: once the `game_over` row is older than
    `POSTGAME_RELEASE_SECONDS`, this runs `release_overdue` and answers
    `False` - the lazy half of the backstop, which still works after an api
    restart lost `retire_game`'s timer.
    """
    try:
        game = store.get(game_id)
    except GameUnloadable:
        return False
    if game is None or not game.is_over:
        return False
    if not any(ai_seat.registry.finishing(uid) for uid, _, _ in _runner_uids(game_id)):
        return False
    ended_at = _ended_at(game_id)
    if ended_at is not None and clock() - ended_at > POSTGAME_RELEASE_SECONDS:
        return release_overdue(game_id)
    return True


def reset_game(
    game_id: str, *, reason: str = DEFAULT_REASON, action: str = DEFAULT_ACTION
) -> dict[str, Any]:
    """Reset one game: stop its watcher, release its AI seats, delete it.

    `action` is the name the reset records itself under, so a transcript
    still says which route ended the game: `game_reset` for End game,
    `delete_game` for `DELETE /api/games/{id}`. Both carry the same payload,
    and `scripts/eval_report.py` already knows both names.

    Returns what was covered, for the audit entry and for the caller. Safe for
    a game that is already gone - it simply reports nothing covered - and
    safe for one this build cannot decode (`repository.GameUnloadable`),
    which is the case that matters most: an undecodable live row counts
    against `MAX_LIVE_GAMES` (`_live_ui_game_ids`), so a reset that refused
    to run on one would leave the lobby with a slot nobody could ever free.
    Everything this function does is readable without the board - the seats
    and the watcher are held beside the game, not in it - and the players it
    names come from `store.known_players` instead, so even the audit entry
    for such a reset still says who was in it.
    """
    unloadable: str | None = None
    try:
        game = store.get(game_id)
    except GameUnloadable as exc:
        game, unloadable = None, exc.reason
    if game is not None:
        players = [player.username for player in game.players]
    elif unloadable is not None:
        players = known_players(game_id)
    else:
        players = []
    _cancel_release_timer(game_id)
    covered: dict[str, Any] = {
        "game_id": game_id,
        "players": players,
        **_release_runners(game_id),
    }
    if game is not None or unloadable is not None:
        # Recorded *before* the delete, so the entry still carries this game's
        # own snapshot: the last thing in its transcript is the state it was
        # abandoned in. An undecodable game gets the entry too, with no
        # snapshot to carry (`audit.record` reads the board through the same
        # `store.get` and tolerates the same refusal) and `unloadable` saying
        # why - a transcript that ends "this build stopped being able to read
        # it, and then it was ended" is the true account of what happened to
        # that game, and the one an operator reading back will need.
        audit.record(
            game_id,
            action,
            reason=reason,
            note=_note(covered, reason),
            seats_invalidated=covered["seats_invalidated"],
            advisors_invalidated=covered["advisors_invalidated"],
            watcher_stopped=covered["watcher_stopped"],
            ai_seats_released=covered["ai_seats_released"],
            **({"unloadable": unloadable} if unloadable is not None else {}),
        )
    covered["deleted"] = store.delete(game_id)
    return covered


def _note(covered: dict[str, Any], reason: str) -> str:
    """The one console line a reset or a retirement gets, saying what it
    covered."""
    seats = ", ".join(
        f"{entry['player']}:{entry['mechanism'] or 'nothing'}"
        for entry in covered["ai_seats_released"]
    )
    advisors = covered.get("advisors_invalidated") or 0
    buddies = f", {advisors} buddy uid(s) invalidated" if advisors else ""
    return (
        f"{reason}: {covered['seats_invalidated']} seat uid(s) invalidated{buddies}, "
        f"pregame watcher {'stopped' if covered['watcher_stopped'] else 'was not running'}, "
        f"ai seats released [{seats or 'none'}]; the agent's memory volume is untouched"
    )


__all__ = [
    "POSTGAME_RELEASE_SECONDS",
    "post_game_work",
    "release_overdue",
    "reset_game",
    "retire_game",
]
