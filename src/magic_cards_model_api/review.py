"""The post-game review: a declaration, and what actually happened next.

This is the deliverable the plan-declaration seam exists for. A person who
does not believe an agent can reason through a game of Magic opens one page
and reads, turn by turn, what the agent said it saw, what it said it would
do, what it said the opponent would do and why - and then what the agent and
the opponent actually did. The order is the whole argument: a prediction is
worth nothing unless it demonstrably preceded the outcome, and here it
demonstrably does, because both come out of one append-only ledger ordered
by one monotonic `seq` written by the server, not by anything a client
supplied.

**Nothing here grades anything.** No score, no hit rate, no verdict on
whether a prediction "came true". That is deliberate and it is the point: a
number is something a sceptic argues with (about the metric, the sample, the
weighting), while the raw pairing is not - it either reads as understanding
or it does not, and the reader decides. Auto-grading would also have to
decide what "alice takes the damage rather than chump-blocking" means
against a sequence of records, which is a judgement, dressed as arithmetic.

**No replay, and no state reconstruction.** `audit.record` writes
`serialize.snapshot(game)` into *every* row - life totals, floating mana,
the battlefield with tapped/summoning-sick/power-toughness/damage/counters,
both players' hands, the stack with controllers and targets, graveyards,
exile, combat assignments. Every row is already a self-contained frame, so
this module hands the frames over and builds nothing from scratch.

**Hidden information.** Everything here shows both seats in full, which is
legitimate *after* a game and never during one. That rule is enforced at the
route (`app.py`'s `game_review`: the game must be over or ended, and the
caller must hold the admin key or one of that game's seat uids), not here -
this module is a pure function over entries that has no way to know who is
asking. Nothing may call it on a live game.

**A final state this build cannot decode is not the end of the review.**
When the engine's `CURRENT_SCHEMA_VERSION` moves past what a stored snapshot
was written at, `Game._check_schema_version` refuses it - correctly, and
that refusal is not softened anywhere in this repo. But the refusal is about
*the board*, and almost nothing on this page comes from the board: the
ledger's rows are self-contained frames written when they happened. So a
review of such a game is rebuilt from the ledger, marked `reconstructed`
with the engine's own refusal in `unloadable`, and every field that genuinely
cannot be established without the board is `None` and named in
`unestablished`. It never guesses one. In particular the outcome comes from
the `game_over` row - which the decodable path already carries as its
corroborating second witness, and which here is the only witness left - and
a game with no such row is reported as an outcome nobody recorded, never as
"the game is over".
"""

from __future__ import annotations

from typing import Any

from .audit import GAME_OVER_ACTION, PLAN_ACTION
from .repository import ArchivedGame

#: The keys a paired entry carries inside a declaration. Deliberately not
#: `snapshot`: the frames are large (about 1.5 kB each, measured), they are
#: already in `entries` once, and embedding them a second time would double
#: a long game's review for nothing.
_PAIRED_KEYS = ("seq", "ts", "turn", "step", "phase", "action", "reasoning", "detail")


def ending_of(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The ledger's own record that this game ended (`audit.GAME_OVER_ACTION`,
    written the moment `Game.is_over` first read true after a committed
    command), or `None` if there is none.

    Public because it is also the post-game gate's second witness: with a
    board this build cannot decode there is no `Game.is_over` to ask, and
    this row - recorded when it happened, by the build that could read the
    board - is what establishes that the game is finished. Its absence
    establishes nothing either way, which is why the routes treat it as
    "not finished" and refuse.
    """
    return _last(entries, GAME_OVER_ACTION)


def build_review(archived: ArchivedGame, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The review of one finished (or ended) game.

    `entries` is `audit.entries(game_id)` - the whole ledger, oldest first,
    unredacted. `archived` is `store.archived(game_id)`, which reads past
    the soft delete, so a game the lobby has ended still has a final board
    and a result to head the review with - or, when this build cannot decode
    that board, says so in `archived.unloadable` and this rebuilds the head
    from the ledger instead (see the module docstring).
    """
    game = archived.game
    ending = ending_of(entries)
    head: dict[str, Any]
    if game is not None:
        head = {
            "players": [player.username for player in game.players],
            "is_over": game.is_over,
            # From the final state, with the `game_over` row as the
            # corroborating second witness rather than the source: the state
            # is what the engine actually holds, the row is what the ledger
            # recorded at the moment it happened. They agree; if they ever
            # did not, the state is the truth and the disagreement is the bug.
            "winner": game.winner,
            "drawn": game.drawn,
            "reconstructed": False,
            "unloadable": None,
            "unestablished": [],
        }
    else:
        head = _from_the_ledger(archived, entries, ending)
    return {
        "game_id": archived.game_id,
        "created_at": archived.created_at,
        "ended_at": archived.deleted_at,
        "ended_seq": ending["seq"] if ending is not None else None,
        **head,
        "declarations": _declarations(entries),
        "entries": entries,
    }


def _from_the_ledger(
    archived: ArchivedGame,
    entries: list[dict[str, Any]],
    ending: dict[str, Any] | None,
) -> dict[str, Any]:
    """The head of a review whose final board this build refuses to decode:
    every field it can establish from the ledger and the game's own plain
    columns, and an honest `None` (named in `unestablished`) for every field
    it cannot.

    `players` comes from the newest frame that has life totals, which is the
    engine's own player order as it stood when that row was written, and
    falls back to the seat columns (`ArchivedGame.seat_uids`, the same
    columns `store.known_players` reads for every listing route that has to
    name an undecodable game's players). The outcome comes from the
    `game_over` row and from nowhere else - never from a life total, which is
    exactly the inference that row was added to stop a reader having to make.
    """
    detail = (ending or {}).get("detail") or {}
    unestablished = [] if ending is not None else ["is_over", "winner", "drawn"]
    return {
        "players": _players_in(entries) or sorted(set(archived.seat_uids.values())),
        "is_over": True if ending is not None else None,
        "winner": detail.get("winner") if ending is not None else None,
        "drawn": bool(detail.get("drawn")) if ending is not None else None,
        "reconstructed": True,
        "unloadable": archived.unloadable,
        "unestablished": unestablished,
    }


def _players_in(entries: list[dict[str, Any]]) -> list[str]:
    """The seats named by the newest frame that names any, in that frame's
    own order - `serialize.snapshot` writes `life` keyed by username, walking
    `game.players`, so this is the engine's own seating order and not a
    re-sorted approximation of it."""
    for entry in reversed(entries):
        life = ((entry.get("snapshot") or {}).get("life")) or {}
        if life:
            return list(life)
    return []


def _declarations(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every `plan_declared` row, paired with what happened before the next
    one.

    The window for a declaration runs from its own `seq` to the next
    declaration's, exclusive - by *any* seat, so two agents alternating
    declarations get windows that never overlap and never double-count a
    row. Inside that window the rows are split by who made them: `did` is
    the declaring seat's own moves (with its own per-action `reasoning`
    beside them, which is "what it actually did and why"), `opponent_did` is
    everybody else's ("what the opponent actually did").

    Rows made by nobody - `game_over`, a lobby reset, a harness poke with no
    `player` - belong to neither seat and are in neither list; they are in
    `entries`, where a reader can still see them in order.
    """
    marks = [index for index, entry in enumerate(entries) if entry["action"] == PLAN_ACTION]
    declared: list[dict[str, Any]] = []
    for position, index in enumerate(marks):
        entry = entries[index]
        detail = entry.get("detail") or {}
        player = detail.get("player")
        end = marks[position + 1] if position + 1 < len(marks) else len(entries)
        window = entries[index + 1 : end]
        declared.append(
            {
                "seq": entry["seq"],
                "ts": entry["ts"],
                "turn": entry["turn"],
                "turns_taken": detail.get("turns_taken"),
                "step": entry["step"],
                "phase": entry["phase"],
                "player": player,
                "board_read": detail.get("board_read"),
                "my_role": detail.get("my_role"),
                "plan": detail.get("plan"),
                "prediction": detail.get("prediction"),
                "did": [_paired(row) for row in window if _actor(row) == player],
                "opponent_did": [
                    _paired(row)
                    for row in window
                    if _actor(row) is not None and _actor(row) != player
                ],
            }
        )
    return declared


def _actor(entry: dict[str, Any]) -> str | None:
    """Whose row this is, or `None` for one nobody made.

    `detail["player"]` is what the acting route recorded, which for most
    routes is the request's own `player` field. That is fine here and only
    here: this is a post-game read where both seats are shown in full, so
    there is no hidden information riding on getting the attribution exactly
    right - unlike `visibility.redact_log_entry`, which must never trust it
    (see `records.LegalActionsBefore`).
    """
    return (entry.get("detail") or {}).get("player")


def _paired(entry: dict[str, Any]) -> dict[str, Any]:
    """`player` is lifted out of `detail` and up onto the entry: which seat a
    row belongs to is the one thing a reader of the pairing looks at first,
    and having to reach into `detail` for it makes the two lists harder to
    read than they need to be. It stays in `detail` too, unchanged."""
    paired = {key: entry.get(key) for key in _PAIRED_KEYS}
    paired["player"] = _actor(entry)
    return paired


def _last(entries: list[dict[str, Any]], action: str) -> dict[str, Any] | None:
    for entry in reversed(entries):
        if entry["action"] == action:
            return entry
    return None
