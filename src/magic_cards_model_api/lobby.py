"""Ending a game from the lobby is a fresh start, for the game and the seat.

Games the lobby boots live side by side, up to `MAX_LIVE_GAMES`
(`decks_api.max_live_games`) - starting a new one from the lobby never touches
another. **End game** (`DELETE /api/ui/games/{id}`, `decks_api.end_lobby_game`)
is the only reset there is, and it is explicit: a human picks a game in the
lobby and ends it, rather than starting a new one silently ending an old one.

`reset_game` covers, per game:

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

**And it is written down.** Every reset records `game_reset` in the ended
game's own audit log, naming what it covered, before the game is deleted - so
a transcript ends with the reset rather than trailing off.

Only games the lobby booted are ever reset this way. `store.register_ui_game`
is set by `POST /api/ui/games` alone; a game booted by `serve.py --match` or by
`POST /api/games` is somebody else's - `decks_api.end_lobby_game` still finds
it through the store like any other game, but nothing in this module or in the
lobby UI ever calls it on a game the lobby did not boot.
"""

from __future__ import annotations

from typing import Any

from . import ai_pregame, ai_seat, audit
from .repository import GameUnloadable
from .store import known_players, store

DEFAULT_REASON = "ended from the lobby"


def reset_game(game_id: str, *, reason: str = DEFAULT_REASON) -> dict[str, Any]:
    """Reset one game: stop its watcher, release its AI seats, delete it.

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
            "game_reset",
            reason=reason,
            note=_note(covered, reason),
            seats_invalidated=len(seats),
            watcher_stopped=watcher_stopped,
            ai_seats_released=released,
            **({"unloadable": unloadable} if unloadable is not None else {}),
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


__all__ = ["reset_game"]
