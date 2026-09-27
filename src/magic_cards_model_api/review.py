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

**DR2: the DSL-shaped decision record.** `decisions`/`play_stats` (per seat)
are built here from three things the caller hands in rather than looked up:
`frame_views` (every relevant `seq` decoded and redaction-ready, one exact
board each - see `build_review`'s own docstring for why this module still
makes no store call of its own to get them) and `corpora` (each seat's
registered play-script corpus). `playscripts.decision_dsl.decision_rows` and
`playscripts.stats.game_play_stats` are DR1's; this module is only their
caller, over its own already-filtered `entries` and the frames the route
batched for it.

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

from collections.abc import Mapping
from typing import Any

from . import visibility
from .audit import (
    ADVICE_ACTION,
    ADVICE_APPROVED_ACTION,
    AI_REVIEW_FAILED_ACTION,
    GAME_OVER_ACTION,
    PLAN_ACTION,
    PLAYS_OUTCOME_ACTION,
    SCRIPT_RUN_ACTION,
    SCRIPT_RUN_ACTIONS,
)
from .playscripts import decision_dsl, stats
from .records import SCRIPT_STAGED_ACTION
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


def build_review(
    archived: ArchivedGame,
    entries: list[dict[str, Any]],
    *,
    exact_frames: list[int] | None = None,
    viewer: str | None = None,
    frame_views: Mapping[int, Mapping[str, Any]] | None = None,
    corpora: Mapping[str, Mapping[str, Any] | None] | None = None,
    with_boards: bool = True,
) -> dict[str, Any]:
    """The review of one finished (or ended) game.

    `entries` is `audit.entries(game_id)` - the whole ledger, oldest first,
    unredacted. `archived` is `store.archived(game_id)`, which reads past
    the soft delete, so a game the lobby has ended still has a final board
    and a result to head the review with - or, when this build cannot decode
    that board, says so in `archived.unloadable` and this rebuilds the head
    from the ledger instead (see the module docstring).

    `exact_frames`/`viewer` are R2's own two additions, computed by the
    caller (`app.game_review`) and simply carried into the payload here -
    this module reads no store, so whether a `seq` has an exact state
    recorded, and who the presented credential belongs to, are both handed
    in rather than looked up. `exact_frames` defaults to `[]` (every review
    built before R2 - and every direct call in `tests/test_review.py`, which
    stays green unchanged - has none); `viewer` defaults to `None`.

    `frame_views`/`corpora` are DR2's: `frame_views` is `seq -> serialize.
    frame_view(...)`'s own dict shape, for every `seq` this review wants a
    decision's board from - which for `app.game_review` is `exact_frames`,
    read back with **one** `store.record_states_for_seqs(game_id,
    exact_frames)` call (never one `store.record_state` per `seq` - see that
    method's own docstring for why a batch exists at all) - and `corpora` is
    `seat -> store.scripts(game_id, uid)["scripts"] or None`, that seat's
    own registered play id -> play doc mapping (never the wrapper `scripts()`
    itself returns - `deck_slug`/`guides_revision`/`registered_at` play no
    part in `play_stats`). Both default to `{}`/`None` (every review
    built before DR2 has neither), in which case every seat's `decisions`
    carries `board`/`board_by_perspective: None` throughout and `play_stats`
    counts a corpus of zero - this module still makes no store call of its
    own to get either.

    `with_boards=False` (OM1) builds every decision's `board`/
    `board_by_perspective` as `None` - for the ops coverage sweep, which
    reads `dsl_coverage`/`play_stats` alone; `frame_views` still feed each
    row's conversion exactly as they otherwise would.
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
    decisions, play_stats = _decisions_and_play_stats(
        archived.game_id,
        entries,
        players=head["players"],
        viewer=viewer,
        frame_views=frame_views or {},
        corpora=corpora or {},
        with_boards=with_boards,
    )
    return {
        "game_id": archived.game_id,
        "created_at": archived.created_at,
        "ended_at": archived.deleted_at,
        "ended_seq": ending["seq"] if ending is not None else None,
        **head,
        "declarations": _declarations(entries),
        "advice": _advice(entries),
        "ai_review_failures": _ai_review_failures(entries),
        "entries": entries,
        "exact_frames": list(exact_frames) if exact_frames else [],
        "viewer": viewer,
        "decisions": decisions,
        "play_stats": play_stats,
        "dsl_coverage": dsl_coverage(decisions),
        "adhoc_scripts": adhoc_scripts_by_seat(entries, players=head["players"], viewer=viewer),
    }


# -- DR2: decisions / play_stats / board_by_perspective --------------------


def _entries_with_frame_views(
    entries: list[dict[str, Any]], frame_views: Mapping[int, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """`entries`, each row that has a `frame_views` entry carrying it under
    its own `state_view` key - the attachment `decision_dsl.decision_rows`
    asks its caller to make (see that module's own docstring). Built as new
    dicts, never mutating `entries` in place: those are the exact objects
    this function's own caller also returns verbatim as `entries` in the
    payload, and `state_view` embedding a whole board a second time onto
    every row is exactly what `_PAIRED_KEYS` above already declines to do
    for a declaration's pairing, for the same reason."""
    if not frame_views:
        return entries
    out = []
    for entry in entries:
        state_view = frame_views.get(entry["seq"])
        out.append({**entry, "state_view": state_view} if state_view is not None else entry)
    return out


def _board_by_perspective(
    state_view: Mapping[str, Any], players: list[str]
) -> dict[str, dict[str, Any]]:
    """One decision's board, once per player in `players`, each redacted to
    what that player could actually see at that exact moment
    (`visibility.redact_view` - the engine's own visibility rules, never
    re-derived here) and then reduced the same way `board` is
    (`decision_dsl.compact_board`, imported, never edited - DR1's own).

    Three fields ride on top of what `compact_board` already carries, all
    read off the *redacted* view so none can ever name a card this player
    was not entitled to: `hand`, that player's own hand card names (a real
    list - `redact_view` never empties a viewer's own hand, only every other
    seat's); `known_opponent_hand`, whatever of another player's hand this
    player was specifically shown or has looked at by now
    (`known_cards[player]["hand"]` - CR 701.20a/e's reveal/look, the same
    record `visibility._redact_known_cards` already scopes to this player
    alone); and `known_library`, this player's own live knowledge of any
    library's top cards, in the order they sit (`known_cards[player][
    "library"]`, already ordered by `index_from_top` - CR 701.20e again, a
    scry or a look at the top of a library being the canonical case,
    `Game.look`). Unlike `known_opponent_hand`, `known_library` is not
    limited to another player's cards: `serialize._known_cards_view` keeps a
    player's own library knowledge too (their hand is dropped there because
    they already see it whole; their library order is not, since nothing
    else on the board carries it). All three are left off the board
    entirely when empty, matching `compact_board`'s own "only the facts a
    board actually has" style. Library *contents*, as opposed to a specific
    known card, are never in any of these fields for anyone, own seat
    included - `compact_board` never reads `libraries` to begin with."""
    boards: dict[str, dict[str, Any]] = {}
    for player in players:
        redacted = visibility.redact_view(state_view, player)
        board = decision_dsl.compact_board(redacted)
        hand = redacted.get("hands", {}).get(player) or ()
        hand_names = sorted(
            card.get("name") for card in hand if isinstance(card, Mapping) and card.get("name")
        )
        if hand_names:
            board["hand"] = hand_names
        known = (redacted.get("known_cards") or {}).get(player) or {}
        known_names = sorted(
            card.get("name")
            for card in known.get("hand", ())
            if isinstance(card, Mapping) and card.get("name")
        )
        if known_names:
            board["known_opponent_hand"] = known_names
        known_library_names = [
            record["card"].get("name")
            for record in known.get("library", ())
            if isinstance(record, Mapping)
            and isinstance(record.get("card"), Mapping)
            and record["card"].get("name")
        ]
        if known_library_names:
            board["known_library"] = known_library_names
        boards[player] = board
    return boards


#: The keys of `game_play_stats`' own answer that mean something under
#: `play_stats[seat]["reviewer"]` (FD7 gap 4): what the reviewer's own
#: `plays_outcome` rows admitted, classified exactly as `admit._landing`
#: classifies any handover's. `offered`/`used`/`denied`/`passed_over` and
#: `plays_total`/`plays_move_bearing`/`plays_advice` are dropped from that
#: sub-object - they are the seat's own script-offer and corpus counts, not
#: something a reviewer's own admits have a second, competing version of.
_REVIEWER_STATS_FIELDS = ("added", "updated", "reprioritized", "added_ids", "updated_ids")

#: FD7 gap 4: the role a `plays_outcome` row's own `detail["role"]` carries
#: when it was filed by the post-game reviewer's own driver rather than by
#: the seat (`"seat"`) or its advisor (`"advisor"`) - `gitwork.REVIEWER_ROLE`
#: restated here rather than imported, since this module owns no dependency
#: on `playscripts.gitwork` otherwise and the string is part of the ledger's
#: own vocabulary (`PlaysOutcomeIn.role`), not that module's internal detail.
REVIEWER_ROLE = "reviewer"


def _decisions_and_play_stats(
    game_id: str,
    entries: list[dict[str, Any]],
    *,
    players: list[str],
    viewer: str | None,
    frame_views: Mapping[int, Mapping[str, Any]],
    corpora: Mapping[str, Mapping[str, Any] | None],
    with_boards: bool = True,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """`decisions`/`play_stats`, one entry per seat in `players` that
    `viewer` may see - `None` (the admin key, by the time this module ever
    sees it - see `app._review_access`) sees every seat, a seat name sees
    only its own, the identical rule `visibility.hides_script_run` already
    applies to that seat's own script-run rows.

    **FD7 gap 4.** Every `plays_outcome` row for a seat already counted in
    its own `added`/`updated`/`reprioritized` totals, whatever `role` filed
    it - that was already true before this method changed, since the totals
    never read `role` at all. What is new is `play_stats[seat]["reviewer"]`:
    the SAME classification, over only the rows this seat's own reviewer
    filed (`detail["role"] == REVIEWER_ROLE`), so a reader can see how much
    of a seat's own totals the reviewer contributed without having to
    subtract anything by hand. `plays_total`/`plays_move_bearing`/
    `plays_advice` (`corpora.get(seat)`) are handed to `game_play_stats`
    exactly once per seat, from whatever `corpora` this call itself was
    given - the caller's own read of the corpus, made when THIS review was
    built, not a value carried over from an earlier one.

    **Review fix.** That is refreshed on every call, but `corpora` itself
    (`app._review_corpora` -> `store.scripts`) is registered once, at
    dispatch, and never again (`mtg_agent.driver.app`'s own docstring) - so
    a play THIS game's own handover admitted new is not in it, and calling
    that "refreshed from the corpus at review time" was wrong: the corpus
    this module is HANDED is stale, only the reading of it is fresh.
    `plays_total` alone is corrected for this below, by adding every play
    id an admitted entry's own `added` names that `corpus` does not already
    have - the one fact provable without the play's own content, which this
    module has no way to read. `plays_move_bearing`/`plays_advice` still
    come from `corpus` alone: splitting a brand-new play on whether it
    carries a move needs its actual document, which only `admit.py`'s own
    checkout has."""
    if not players:
        return {}, {}
    entries_with_state = _entries_with_frame_views(entries, frame_views)
    admitted_by_seat: dict[str, list[dict[str, Any]]] = {player: [] for player in players}
    reviewer_admitted_by_seat: dict[str, list[dict[str, Any]]] = {player: [] for player in players}
    for entry in entries:
        if entry["action"] != PLAYS_OUTCOME_ACTION:
            continue
        detail = entry.get("detail") or {}
        player = detail.get("player")
        if player not in admitted_by_seat:
            continue
        batch = detail.get("admitted") or []
        admitted_by_seat[player].append(batch)
        if detail.get("role") == REVIEWER_ROLE:
            reviewer_admitted_by_seat[player].append(batch)
    decisions: dict[str, list[dict[str, Any]]] = {}
    play_stats: dict[str, dict[str, Any]] = {}
    for seat in players:
        if viewer is not None and viewer != seat:
            continue
        rows = decision_dsl.decision_rows(entries_with_state, seat, with_boards=with_boards)
        for row in rows:
            state_view = frame_views.get(row["seq"]) if with_boards else None
            row["board_by_perspective"] = (
                _board_by_perspective(state_view, players) if state_view is not None else None
            )
        decisions[seat] = rows
        admitted = [entry for batch in admitted_by_seat[seat] for entry in batch]
        corpus = corpora.get(seat) or {}
        seat_stats = stats.game_play_stats(entries, game_id, seat, corpus, admitted)
        # Review fix (gap 4): `corpus` is `app._review_corpora`'s read of
        # `store.scripts` - registered once, at dispatch
        # (`mtg_agent.driver.app`'s own docstring: "once, at dispatch.
        # Nothing registers mid-game"), so a play THIS game's own handover
        # admitted new never entered it and `plays_total` undercounts by
        # exactly that many. `admit._landing`'s own `added` classification
        # (a brand-new play file, whatever content it carries) is read
        # straight off the one ledger row that fact was ever recorded in,
        # never reimplemented - the same discipline `stats.py`'s own
        # docstring states for `added`/`updated`/`reprioritized`. A play id
        # `added` names that the dispatch-time corpus ALREADY had (an edge
        # case: a file readded under the same id after being deleted and
        # re-created within one game) is not double-counted, since only ids
        # outside `corpus` are added.
        new_ids = {
            play_id
            for entry in admitted
            for play_id in entry.get("added") or ()
            if play_id not in corpus
        }
        if new_ids:
            seat_stats["plays_total"] += len(new_ids)
        reviewer_admitted = [entry for batch in reviewer_admitted_by_seat[seat] for entry in batch]
        reviewer_stats = stats.game_play_stats(
            entries, game_id, seat, corpora.get(seat) or {}, reviewer_admitted
        )
        seat_stats["reviewer"] = {key: reviewer_stats[key] for key in _REVIEWER_STATS_FIELDS}
        play_stats[seat] = seat_stats
    return decisions, play_stats


def dsl_coverage(decisions: Mapping[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """`{kind: {count, unexpressible, labels}}` across every seat's own
    `decisions` (DR2's per-seat list `_decisions_and_play_stats` already
    built - `action`/`unexpressible` are that same function's own fields,
    read here rather than recomputed).

    `count` is how many decisions of that kind this game had, across every
    seat `decisions` carries; `unexpressible` is how many of them could not
    become a play at all (`row["unexpressible"] is not None`); `labels` is
    the distinct non-null `unexpressible` reasons seen for that kind,
    sorted, so a kind's own gap is named without walking `decisions` a
    second time. A game with no decisions at all is `{}`, never a missing
    key on some caller's own expected shape.

    Public (FD7 gap 4) so FD5's ops endpoint can call it once per game and
    fold the answers together into one coverage report across every game -
    this function itself is scoped to one game's own `decisions`."""
    coverage: dict[str, dict[str, Any]] = {}
    for rows in decisions.values():
        for row in rows:
            kind = row.get("action")
            if kind is None:
                continue
            entry = coverage.setdefault(kind, {"count": 0, "unexpressible": 0, "labels": set()})
            entry["count"] += 1
            label = row.get("unexpressible")
            if label is not None:
                entry["unexpressible"] += 1
                entry["labels"].add(label)
    return {kind: {**entry, "labels": sorted(entry["labels"])} for kind, entry in coverage.items()}


def adhoc_scripts_by_seat(
    entries: list[dict[str, Any]], *, players: list[str], viewer: str | None
) -> dict[str, list[dict[str, Any]]]:
    """FD10 (a): every ad-hoc script one of `players` staged this game
    (`records.stage_adhoc_script`'s own `script_staged` row), each with the
    runs the ledger recorded of it and how each ended - so the post-game
    review can generalize and admit a script a seat actually used, exactly
    as it already does for a hand-played line or a declined offer.

    One entry per seat `viewer` may see (`None`, the admin key, sees every
    seat), oldest staged first; each is `{id, staged_at, turn, step, play,
    reasoning, warnings, runs}`, `runs` a list of `{run_id, exit,
    moves_done}` for every `script_run` finish row naming that same id.
    `entries` is `audit.entries(game_id)` - the whole ledger, unredacted,
    same convention as every other function here."""
    out: dict[str, list[dict[str, Any]]] = {}
    for seat in players:
        if viewer is not None and viewer != seat:
            continue
        staged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for entry in entries:
            if entry.get("action") != SCRIPT_STAGED_ACTION:
                continue
            detail = entry.get("detail") or {}
            if detail.get("seat") != seat:
                continue
            script_id = detail.get("id")
            if not isinstance(script_id, str):
                continue
            if script_id not in staged:
                order.append(script_id)
            staged[script_id] = {
                "id": script_id,
                "staged_at": entry.get("ts"),
                "turn": entry.get("turn"),
                "step": entry.get("step"),
                "play": detail.get("play"),
                "reasoning": entry.get("reasoning"),
                "warnings": list(detail.get("warnings") or []),
                "runs": [],
            }
        for entry in entries:
            if entry.get("action") != SCRIPT_RUN_ACTION:
                continue
            detail = entry.get("detail") or {}
            if detail.get("seat") != seat:
                continue
            script_id = detail.get("script_id")
            if script_id in staged:
                staged[script_id]["runs"].append(
                    {
                        "run_id": detail.get("run_id"),
                        "exit": detail.get("exit"),
                        "moves_done": detail.get("moves_done"),
                    }
                )
        out[seat] = [staged[script_id] for script_id in order]
    return out


def _ai_review_failures(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every `ai_review_failed` row, flattened to the fields a reader needs
    (T35).

    The rows are already in `entries`, like everything else - this list
    exists so the question an operator actually asks ("there is no AI review
    on this page; was one ever attempted, and what went wrong?") is answered
    by reading one key rather than by walking a whole ledger looking for an
    action they would have to know the name of first. `invoked` is the field
    that makes it an answer rather than a shrug: it separates a reviewer that
    ran and failed from one that was never asked."""
    return [
        {
            "seq": entry["seq"],
            "ts": entry.get("ts"),
            "player": (entry.get("detail") or {}).get("player"),
            "stage": (entry.get("detail") or {}).get("stage"),
            "reason": (entry.get("detail") or {}).get("reason"),
            "invoked": (entry.get("detail") or {}).get("invoked"),
        }
        for entry in entries
        if entry.get("action") == AI_REVIEW_FAILED_ACTION
    ]


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
        # `detail["turn_number"]` is the turn this declaration is ABOUT
        # (`app.declare_plan` writes it for every row, `for_turn: "current"`
        # included, where it is simply the same value `entry["turn"]` already
        # is) - the row's own top-level `entry["turn"]` is only ever the turn
        # it was WRITTEN in, which for `for_turn: "next"` is a different
        # turn, and in a 3+ player game can even be a different *player's*
        # turn under the same `turn_number` (a live-game repro, 2026-09-14).
        # A row written before this field existed carries no `turn_number`
        # key at all - `entry["turn"]` is the fallback for those.
        target_turn = detail.get("turn_number")
        declared.append(
            {
                "seq": entry["seq"],
                "ts": entry["ts"],
                "turn": target_turn if target_turn is not None else entry["turn"],
                "turns_taken": detail.get("turns_taken"),
                "step": entry["step"],
                "phase": entry["phase"],
                "player": player,
                # F3: `board_read`/`board_read_auto` are mutually exclusive -
                # a row carries whichever one it was actually written with,
                # honestly, rather than the review quietly presenting an
                # auto-generated board as if it were the seat's own prose (or
                # vice versa). Same for `my_role_carried`/`reused_from_seq`:
                # both are markers next to the value they describe, never a
                # substitute for it - `my_role`/`plan`/`prediction` still
                # carry the actual content whether it was typed fresh,
                # carried forward, or reused.
                "board_read": detail.get("board_read"),
                "board_read_auto": detail.get("board_read_auto"),
                "my_role": detail.get("my_role"),
                "my_role_carried": detail.get("my_role_carried", False),
                "plan": detail.get("plan"),
                "prediction": detail.get("prediction"),
                "reused_from_seq": detail.get("reused_from_seq"),
                "still_because": detail.get("still_because"),
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
