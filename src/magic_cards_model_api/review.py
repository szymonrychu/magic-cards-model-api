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

from .audit import (
    ADVICE_ACTION,
    ADVICE_APPROVED_ACTION,
    GAME_OVER_ACTION,
    PLAN_ACTION,
    SCRIPT_RUN_ACTIONS,
)
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
        "advice": _advice(entries),
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
                "did": [_paired(row) for row in window if _is_move(row) and _actor(row) == player],
                "opponent_did": [
                    _paired(row)
                    for row in window
                    if _is_move(row) and _actor(row) is not None and _actor(row) != player
                ],
            }
        )
    return declared


def _is_move(entry: dict[str, Any]) -> bool:
    """Whether `entry` can be something a seat did: every row but a
    play-script run report (`audit.SCRIPT_RUN_ACTIONS`). A run report names
    its seat as `player`, but it records that a runner ran, not a move - the
    moves it made are rows of their own."""
    return entry["action"] not in SCRIPT_RUN_ACTIONS


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


def _advice(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every `ADVICE_ACTION` row - the mtg-buddy advisor's own proposal to a
    human seat - paired with whether it was approved and what the seat did
    about it.

    A **separate stream from `_declarations`, on purpose**: a declaration's
    window runs from one `plan_declared` row to the next *by any seat*, and
    an advice row - one per decision point, potentially several a turn - would
    slice that window into fragments it was never meant to have. Marking
    unaffected: this function reads `ADVICE_ACTION` only, `_declarations`
    reads `PLAN_ACTION` only, and neither list's marks depend on the other
    existing. An advice row still appears inside a declaration's own
    `did`/`opponent_did`, exactly like any other row by that seat - it is
    simply never a *window boundary* the way `plan_declared` is.

    **Deliberately not scored** - see the module docstring's stand on
    `_declarations`, which is the same stand here: whether a piece of advice
    was *good* is a judgement, not an outcome, and it stays off this page.
    What is reported, plainly, is one of four things: the proposal was
    approved and the seat's next real move matched it; it was approved,
    attempted, and the engine refused it - `did` stays `None` (a refusal is a
    failed attempt, not "what happened", so it is never handed back as the
    outcome), but the refusal itself, and the engine's own verbatim reason,
    is not lost: it is carried in `refused`, which is the single most
    instructive line this page can show (this engine's whole reason for
    existing is that it says out loud why a refused action is illegal); or it
    was never approved, in which case the seat either did something else
    (`did` is that row) or did nothing at all before the next piece of advice
    (`did` is `None`, `refused` is empty).

    `refused` is every row by this seat in the same window that the engine
    refused (`detail["allowed"] is False`) - ordinarily zero or one, but
    never assumed to be at most one, since nothing stops a seat retrying a
    refused attempt more than once before either succeeding or giving up.
    `did` is unaffected by this - it is still the first *non*-refused row, so
    a window with a refusal followed by a real move still reports both: the
    refusal in `refused`, and the move in `did`.

    `reasoning` is read off the entry's own top-level field first, falling
    back to `detail["reasoning"]` - the write side may land it either way
    (every action route's own `reasoning` argument to `audit.record` lands
    top-level; this defends against a payload that instead folded it into
    `detail`, since both spellings carry the same meaning here).
    """
    approvals = [row for row in entries if row["action"] == ADVICE_APPROVED_ACTION]
    result: list[dict[str, Any]] = []
    for index, row in enumerate(entries):
        if row["action"] != ADVICE_ACTION:
            continue
        detail = row.get("detail") or {}
        player = detail.get("player")
        seq = row["seq"]
        approved_row = next(
            (a for a in approvals if (a.get("detail") or {}).get("advice_seq") == seq), None
        )
        boundary = len(entries)
        for later_index in range(index + 1, len(entries)):
            later = entries[later_index]
            if (
                later["action"] == ADVICE_ACTION
                and (later.get("detail") or {}).get("player") == player
            ):
                boundary = later_index
                break
        did_row = _first_real_action(entries, index + 1, boundary, player)
        refused_rows = _refused_in_window(entries, index + 1, boundary, player)
        result.append(
            {
                "seq": seq,
                "ts": row["ts"],
                "turn": row["turn"],
                "step": row["step"],
                "phase": row["phase"],
                "player": player,
                "summary": detail.get("summary"),
                "reasoning": row.get("reasoning") or detail.get("reasoning"),
                "path": detail.get("path"),
                "body": detail.get("body"),
                "based_on_seq": detail.get("based_on_seq"),
                "validated": detail.get("validated"),
                "approved": approved_row is not None,
                "approved_seq": approved_row["seq"] if approved_row is not None else None,
                "did": _paired(did_row) if did_row is not None else None,
                "followed": _advice_followed(detail.get("path"), detail.get("body"), did_row),
                "refused": [_paired(refused_row) for refused_row in refused_rows],
            }
        )
    return result


def _first_real_action(
    entries: list[dict[str, Any]], start: int, end: int, player: str | None
) -> dict[str, Any] | None:
    """The first row in `entries[start:end]` that is a real move by `player`:
    not an `advice`/`advice_approved` marker or a script-run report (none of
    them mutates the game), and
    never a row the engine refused - a refusal is a failed attempt, not a
    thing that happened, and is deliberately never handed back as `did` (see
    `_advice`, and `_refused_in_window` for where a refusal *is* reported)."""
    for candidate in entries[start:end]:
        if candidate["action"] in (ADVICE_ACTION, ADVICE_APPROVED_ACTION):
            continue
        if not _is_move(candidate):
            continue
        if _actor(candidate) != player:
            continue
        if (candidate.get("detail") or {}).get("allowed") is False:
            continue
        return candidate
    return None


def _refused_in_window(
    entries: list[dict[str, Any]], start: int, end: int, player: str | None
) -> list[dict[str, Any]]:
    """Every row in `entries[start:end]` that is `player`'s own attempt and
    that the engine refused - the evidence `_first_real_action` deliberately
    excludes from `did`. Carries the engine's verbatim `reason` (inside each
    row's own `detail`, exactly as `GET .../log` and the console line already
    show it) rather than dropping it, since "you approved this and the
    engine refused it because X" is the single most instructive line a
    retrospective can contain."""
    return [
        candidate
        for candidate in entries[start:end]
        if candidate["action"] not in (ADVICE_ACTION, ADVICE_APPROVED_ACTION)
        and _is_move(candidate)
        and _actor(candidate) == player
        and (candidate.get("detail") or {}).get("allowed") is False
    ]


def _kind_of_path(path: str | None) -> str | None:
    """The ledger's own action name for a proposal's `path` - the route's
    last segment, hyphens turned to underscores (`/actions/play-land` ->
    `play_land`), which is exactly how every action route's own path relates
    to the name it audits under."""
    if not path:
        return None
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    return tail.replace("-", "_") or None


def _advice_followed(
    path: str | None, body: dict[str, Any] | None, did_row: dict[str, Any] | None
) -> bool:
    """Whether `did_row` actually matches the proposal: the same action kind
    for `path`, and the same card where `body` names one by id or name (a
    proposal that names no card - a pass, most often - has nothing further to
    check, so the kind match alone is enough)."""
    if did_row is None:
        return False
    kind = _kind_of_path(path)
    if kind is None or did_row.get("action") != kind:
        return False
    body = body or {}
    proposed_card = body.get("card_id", body.get("card"))
    if proposed_card is None:
        return True
    did_detail = did_row.get("detail") or {}
    did_card = did_detail.get("card_id", did_detail.get("card"))
    return did_card is not None and str(did_card) == str(proposed_card)


def _last(entries: list[dict[str, Any]], action: str) -> dict[str, Any] | None:
    for entry in reversed(entries):
        if entry["action"] == action:
            return entry
    return None
