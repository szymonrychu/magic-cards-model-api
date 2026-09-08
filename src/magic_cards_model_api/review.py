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


def build_review(archived: ArchivedGame, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The review of one finished (or ended) game.

    `entries` is `audit.entries(game_id)` - the whole ledger, oldest first,
    unredacted. `archived` is `store.archived(game_id)`, which reads past
    the soft delete, so a game the lobby has ended still has a final board
    and a result to head the review with.
    """
    game = archived.game
    ending = _last(entries, GAME_OVER_ACTION)
    return {
        "game_id": archived.game_id,
        "players": [player.username for player in game.players],
        "created_at": archived.created_at,
        "ended_at": archived.deleted_at,
        "is_over": game.is_over,
        # From the final state, with the `game_over` row as the corroborating
        # second witness rather than the source: the state is what the engine
        # actually holds, the row is what the ledger recorded at the moment it
        # happened. They agree; if they ever did not, the state is the truth
        # and the disagreement is the bug.
        "winner": game.winner,
        "drawn": game.drawn,
        "ended_seq": ending["seq"] if ending is not None else None,
        "declarations": _declarations(entries),
        "entries": entries,
    }


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
