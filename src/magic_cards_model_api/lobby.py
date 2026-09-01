"""Starting a game from the lobby is a fresh start, for the game and the seat.

Before this module, `POST /api/ui/games` only ever *added*. Everything the
previous lobby game had went on existing: its `Game` in the store, its seat
uids (still openable, still able to act on that game), its pregame watcher
thread (`ai_pregame.py`, alive for up to `MAX_LIFETIME_SECONDS`), its AI-seat
record in `ai_seat.registry`, and - the one a human actually felt - the
driver's assignment, which made every dispatch for the *new* game answer 409
"this agent is already playing game <the old one>". A second game started from
the lobby therefore had no opponent at all.

So the lobby resets before it boots. `reset_previous_games` covers, per game:

- the watcher thread, asked to stop (`ai_pregame.stop`) so nothing can make a
  pregame move in a game nobody is at,
- every AI seat, released (`ai_seat.registry.release`) - a subprocess is
  terminated, a wrapper dispatch is handed back to the driver, and the
  registry forgets the uid so no stale status can be polled out of it,
- the game itself, deleted from the store, which is what takes the old seat
  uids and the old admin key out of existence.

**What it never touches: the agent's memory.** The point of the continuous
agent is that it carries what it learned between games, and that lives in the
`./memory` volume mounted into the agent and driver containers - not in
anything here. Releasing a seat runs the driver's ordinary end-of-match
`retire`, which ends the agent's *session*; the notes on disk are untouched
and are exactly what the next game starts from.

**And it is written down.** Every reset records `game_reset` in the abandoned
game's own audit log, naming what it covered, before the game is deleted - so
a transcript ends with the reset rather than trailing off - and
`reset_previous_games` in the new game's log, naming the games it replaced. A
transcript never silently spans two games.

Only games the lobby booted are ever reset. `store.register_ui_game` is set by
`POST /api/ui/games` alone; a game booted by `serve.py --match` or by
`POST /api/games` is somebody else's, and the browser starting a game must
never delete it.
"""

from __future__ import annotations

from typing import Any

from . import ai_pregame, ai_seat, audit
from .store import store

DEFAULT_REASON = "a new game was started from the lobby"


def reset_game(game_id: str, *, reason: str = DEFAULT_REASON) -> dict[str, Any]:
    """Reset one game: stop its watcher, release its AI seats, delete it.

    Returns what was covered, for the audit entry and for the caller. Safe for
    a game that is already gone - it simply reports nothing covered.
    """
    game = store.get(game_id)
    players = [player.username for player in game.players] if game is not None else []
    seats = store.seat_uids(game_id)  # uid -> username

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
    covered: dict[str, Any] = {
        "game_id": game_id,
        "players": players,
        "seats_invalidated": len(seats),
        "watcher_stopped": watcher_stopped,
        "ai_seats_released": released,
    }
    if game is not None:
        # Recorded *before* the delete, so the entry still carries this game's
        # own snapshot: the last thing in its transcript is the state it was
        # abandoned in.
        audit.record(
            game_id,
            "game_reset",
            reason=reason,
            note=_note(covered, reason),
            seats_invalidated=len(seats),
            watcher_stopped=watcher_stopped,
            ai_seats_released=released,
        )
    covered["deleted"] = store.delete(game_id)
    return covered


def _note(covered: dict[str, Any], reason: str) -> str:
    """The one console line a reset gets, saying what it covered."""
    seats = ", ".join(
        f"{entry['player']}:{entry['mechanism'] or 'nothing'}"
        for entry in covered["ai_seats_released"]
    )
    return (
        f"{reason}: {covered['seats_invalidated']} seat uid(s) invalidated, "
        f"pregame watcher {'stopped' if covered['watcher_stopped'] else 'was not running'}, "
        f"ai seats released [{seats or 'none'}]; the agent's memory volume is untouched"
    )


def reset_previous_games(*, reason: str = DEFAULT_REASON) -> list[dict[str, Any]]:
    """Reset every game the lobby booted before this one, oldest first."""
    return [reset_game(game_id, reason=reason) for game_id in store.ui_games()]


def record_reset(
    game_id: str, covered: list[dict[str, Any]], *, reason: str = DEFAULT_REASON
) -> None:
    """Note in the *new* game's log which games it replaced, and what the
    reset covered. Recorded even when nothing was reset: "this game replaced
    nothing" is worth as much to a reader as the list, and its absence would
    be indistinguishable from a reset that never ran."""
    audit.record(
        game_id,
        "reset_previous_games",
        reason=reason,
        note=(
            "replaced " + ", ".join(entry["game_id"] for entry in covered)
            if covered
            else "no previous lobby game to reset"
        ),
        games=[entry["game_id"] for entry in covered],
        covered=covered,
    )


__all__ = ["record_reset", "reset_game", "reset_previous_games"]
