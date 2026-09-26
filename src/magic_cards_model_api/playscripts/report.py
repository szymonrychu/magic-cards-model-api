"""What a seat's scripts have done so far in one game, compact enough to hand
over.

`script_run_table` reads the `script_run` rows `POST .../script-runs` writes
(`records.record_script_run_finish`) and prints one line per script: how
often it ran, how each run ended, how many runs ended with the script still
applying, and who started each run - `invoked_via`, `"mcp"`, `"cli"` or (S2)
`"armed"` (`playscripts.arming.arm`, firing a script on its own between this
seat's own moves). A handover prompt carries it whole, so it is bounded at
`TABLE_LIMIT_BYTES`; past that the lowest-priority rows are cut and a
`TRUNCATION_MARKER` line says how many.

A `"cli"` count in the `invoked_via` column is flagged `(not started by
you)`: a seat's own runs are always `"mcp"` (`mcp.run_script`) or `"armed"`
(fired for this seat, between its own moves), so any `cli` count on its row
is a run the ledger recorded that this seat did not make itself - an
operator staging one from `mcp-seat script run` during development, most
often. `"armed"` never gets the flag: it is this seat's own arming, not an
operator's.

`offer_table` (API-8) reads the `script_run_offered` rows `phase: offer`
writes and prints one line per play a runner offered this seat, whether or
not the seat ever took it. This is the accept/reject signal a reviewer
reads, never a count of runs: it is what tells a human, or API-13's own
review prompt, whether a seat is actually listening to what its scripts
propose. Every bar the review prompt draws from this table counts
`declined` rows only - an offer taken, passed over for a sibling, or never
followed up on at all is never held against a seat the way a genuine
decline is.

D2b adds what the mandatory post-game development step reads:
`hand_played_summary` (the seat's own casts, activations and land plays no
script made), `development_candidates` (every declined or passed-over play
plus every line played by hand twice), `evidence_record` (this game's
per-play offer counts, the file `plays admit` commits to
`decks/<deck>/plays/evidence/<game>.json`) and `evidence_table` (those files
aggregated across games, so a "two distinct games" bar can be met).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..audit import (
    PLAYS_OUTCOME_ACTION,
    SCRIPT_RUN_ACTION,
    SCRIPT_RUN_OFFERED_ACTION,
    SCRIPT_RUN_STARTED_ACTION,
)
from . import decision_dsl

#: The most a table may take, in UTF-8 bytes.
TABLE_LIMIT_BYTES = 4096

#: The start of the last line of a table that was cut.
TRUNCATION_MARKER = "... table cut at 4 KB:"

#: What the runner posts as `reapplicable_after` when the re-check still applies.
REAPPLICABLE = "true"

HEADER = "script | priority | runs | exits | reapplicable_after | invoked_via"

#: `invoked_via` value a row with no runs reporting one falls back to - a
#: ledger row from before S1, when nothing recorded who started a run.
_UNKNOWN_INVOKER = "unknown"


@dataclass
class _ScriptRow:
    label: str
    priority: int | None = None
    runs: int = 0
    exits: dict[str, int] = field(default_factory=dict)
    reapplicable: int = 0
    invoked_via: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        exits = " ".join(f"{name}={count}" for name, count in sorted(self.exits.items()))
        priority = "-" if self.priority is None else str(self.priority)
        invoker = " ".join(f"{name}={count}" for name, count in sorted(self.invoked_via.items()))
        if self.invoked_via.get("cli"):
            invoker = f"{invoker} (not started by you)"
        return (
            f"{self.label} | {priority} | {self.runs} | {exits} | {self.reapplicable} | {invoker}"
        )


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """A row as `store.records` returns it wraps the entry in `payload`; one
    from `audit.entries` is the entry itself."""
    payload = record.get("payload")
    return payload if isinstance(payload, Mapping) else record


def _play_label(script_id: str, advise: bool) -> str:
    """A script/play's own printed name: `(advise)` marks a proposal to a
    human rather than something taken, on both tables."""
    return f"{script_id} (advise)" if advise else script_id


def script_run_table(records: Iterable[Mapping[str, Any]], game_id: str, seat: str) -> str:
    """One line per script `seat` ran in `game_id`, highest priority first.

    Advise runs - moves proposed to a human rather than taken - get a row of
    their own, labelled `(advise)`, so a proposal nobody accepted never reads
    as the seat's own play. `priority` is the latest one a run reported.

    `reapplicable_after` is the runner's one condition re-check at the end of
    a run that resolved a play: `"true"` when the script would apply again,
    `"false"` when not, absent when no play was found. The column counts the
    `"true"` runs, the loss data a future loop decision reads.
    """
    rows: dict[tuple[str, bool], _ScriptRow] = {}
    for record in records:
        payload = _payload(record)
        if payload.get("action") != SCRIPT_RUN_ACTION:
            continue
        detail = payload.get("detail") or {}
        if detail.get("seat") != seat or detail.get("game_id") != game_id:
            continue
        script_id = str(detail.get("script_id"))
        advise = bool(detail.get("advise"))
        row = rows.setdefault((script_id, advise), _ScriptRow(_play_label(script_id, advise)))
        row.runs += 1
        exit_name = str(detail.get("exit") or "unknown")
        row.exits[exit_name] = row.exits.get(exit_name, 0) + 1
        if detail.get("priority") is not None:
            row.priority = int(detail["priority"])
        if detail.get("reapplicable_after") == REAPPLICABLE:
            row.reapplicable += 1
        invoker = str(detail.get("invoked_via") or _UNKNOWN_INVOKER)
        row.invoked_via[invoker] = row.invoked_via.get(invoker, 0) + 1

    if not rows:
        return f"no script runs recorded for {seat} in {game_id}"

    ordered = [
        row.line()
        for _, row in sorted(
            rows.items(),
            key=lambda item: (item[1].priority is None, -(item[1].priority or 0), item[0]),
        )
    ]
    return _bounded_table(HEADER, ordered, "script row(s)")


#: One offer resolved to: the seat's next committing row started exactly the
#: play this offer named.
TAKEN = "taken"

#: F3: the seat's next committing row started a *different* play offered in
#: the same offer set - one of several candidates a conflict hint offered
#: together, of which the seat may take at most one. Never counted as a
#: decline: the seat did exactly what the hint, which withheld every `call`
#: for the conflict and told the seat to pick, asked it to do.
PASSED_OVER = "passed over"

#: The seat's next committing row was something else entirely - neither this
#: play nor a sibling from the same offer set.
DECLINED = "declined"

#: No committing row followed this offer at all (the game ended, the log
#: window cut) - counted as an offer and nothing else, never a decline.
OFFERED = "offered"

OFFER_HEADER = "play | run_id | priority | turn | step | outcome | action | reasoning"

#: Shown in the `reasoning` column of a decline that carries none - a pass
#: declining an offer (`records.require_reasoning`'s `pass_shaped`
#: exemption), or any other committing row that reached here with no
#: `reasoning` of its own. Never a blank column, which would read as "no
#: reasoning was recorded for this row" ambiguously with "not applicable,
#: this was not a decline".
_NO_REASONING = "(no reasoning recorded)"


@dataclass
class _Offer:
    index: int
    seq: Any
    run_id: str
    turn: Any
    step: Any
    detail: Mapping[str, Any]


def _offer_line(offer: _Offer, outcome: str, action: str | None, reasoning: str | None) -> str:
    priority = offer.detail.get("priority")
    priority_display = "-" if priority is None else str(priority)
    label = _play_label(str(offer.detail.get("script_id")), bool(offer.detail.get("advise")))
    action_display = "-" if action is None else action
    reasoning_display = "-" if reasoning is None else reasoning
    return (
        f"{label} | {offer.run_id} | {priority_display} | {offer.turn} | {offer.step} | "
        f"{outcome} | {action_display} | {reasoning_display}"
    )


def _committing_row(
    all_records: list[Mapping[str, Any]], after_index: int, seat: str
) -> Mapping[str, Any] | None:
    """The first row after `after_index` that is this seat's own next
    commitment, or `None` when nothing followed at all.

    Scanning forward from the offer's own position, this is the first row
    whose `detail.player` is `seat` and which is either a `script_run_
    started` row or an action row with `allowed` true. A refused row
    (`allowed: false`) is not a commitment and is skipped, and so is every
    non-action row - `plan_declared`, `report_problem`, `advice`, `game_
    reflection` and the rest - which never carries `allowed` at all and so
    never matches here either.
    """
    for record in all_records[after_index + 1 :]:
        payload = _payload(record)
        detail = payload.get("detail") or {}
        if detail.get("player") != seat:
            continue
        if payload.get("action") == SCRIPT_RUN_STARTED_ACTION or detail.get("allowed") is True:
            return payload
    return None


def offer_table(records: Iterable[Mapping[str, Any]], game_id: str, seat: str) -> str:
    """One line per play offered to `seat` in `game_id` - offered, taken,
    passed over, or declined - highest priority first.

    This is the accept/reject signal a reviewer reads off a seat's script
    offers, never a run count: `script_run_table` already covers how often
    something ran. Every offer gets exactly one row, whether or not the seat
    ever answered it.

    **Outcome, per offer.** `_committing_row` finds the seat's next
    commitment after the offer (see its own docstring for the precise scan).
    From there:

    - **Taken**: that commitment is a `script_run_started` row naming this
      offer's own `run_id`.
    - **Passed over** (F3): that commitment is a `script_run_started` row
      naming a *different* `run_id`, but one offered in the same **offer
      set** - offer rows sharing this seat, `turn`, `step`, and an adjacent
      ledger position (each one's own `seq` immediately following the
      last's; anything recorded in between, by anyone, starts a new set).
      Several plays can ride one answer when a conflict hint offers every
      candidate at once and withholds every `call`, leaving the seat to pick
      exactly one - taking a sibling is not declining the others.
    - **Declined**: the commitment is anything else - a different action
      entirely, a pass, or a `script_run_started` row for a play outside
      this offer set. The table names the action actually taken
      (`payload["action"]`) and its `reasoning`. Reasoning is already
      mandatory on every committing agent action but a pass
      (`records.require_reasoning`), so for everything but a pass the
      rejection rationale is already in the ledger; a pass declining an
      offer carries its action name and no reasoning at all, and the
      `reasoning` column says so (`_NO_REASONING`) rather than reading
      ambiguously blank.
    - **Offered**: no commitment followed at all - the game ended, or the
      log window handed to this function was cut. Counted as an offer and
      nothing else, never a decline: nobody knows what the seat would have
      done.

    An advisor's play (`advise: true` on the offer) is offered and declined
    exactly like any other; it can simply never be *taken*, since only the
    seat itself ever posts `script_run_started` - an advisor's own
    commitment is an `advice` row, which is not a committing row at all.
    """
    lines = _offer_outcomes(records, game_id, seat)
    if not lines:
        return f"no script offers recorded for {seat} in {game_id}"

    ordered = [
        _offer_line(offer, outcome, action, reasoning)
        for offer, outcome, action, reasoning in sorted(
            lines,
            key=lambda item: (
                item[0].detail.get("priority") is None,
                -(item[0].detail.get("priority") or 0),
                item[0].seq,
            ),
        )
    ]
    return _bounded_table(OFFER_HEADER, ordered, "offer row(s)")


#: One offer, paired: the offer itself, its outcome (`TAKEN`, `PASSED_OVER`,
#: `DECLINED` or `OFFERED`), and for a decline the action taken instead and
#: its reasoning.
_Paired = tuple[_Offer, str, str | None, str | None]


def _offer_outcomes(records: Iterable[Mapping[str, Any]], game_id: str, seat: str) -> list[_Paired]:
    """Every offer to `seat` in `game_id`, in ledger order, paired with its
    outcome - the one pairing `offer_table`, `evidence_record` and
    `development_candidates` all read, so the three can never disagree about
    what a seat declined."""
    all_records = list(records)
    offers: list[_Offer] = []
    for index, record in enumerate(all_records):
        payload = _payload(record)
        if payload.get("action") != SCRIPT_RUN_OFFERED_ACTION:
            continue
        detail = payload.get("detail") or {}
        if detail.get("seat") != seat or detail.get("game_id") != game_id:
            continue
        offers.append(
            _Offer(
                index=index,
                seq=record.get("seq"),
                run_id=str(detail.get("run_id")),
                turn=detail.get("turn"),
                step=detail.get("step"),
                detail=detail,
            )
        )

    # Offer sets: consecutive offers (already in ledger order) at the same
    # turn/step whose own `seq` are ledger-adjacent to the previous offer's.
    sets: list[list[_Offer]] = []
    for offer in offers:
        if sets:
            last = sets[-1][-1]
            same_point = last.turn == offer.turn and last.step == offer.step
            adjacent = last.seq is not None and offer.seq is not None and offer.seq == last.seq + 1
            if same_point and adjacent:
                sets[-1].append(offer)
                continue
        sets.append([offer])

    siblings: dict[str, set[str]] = {}
    for offer_set in sets:
        run_ids = {member.run_id for member in offer_set}
        for member in offer_set:
            siblings[member.run_id] = run_ids - {member.run_id}

    paired: list[_Paired] = []
    for offer in offers:
        commitment = _committing_row(all_records, offer.index, seat)
        if commitment is None:
            paired.append((offer, OFFERED, None, None))
            continue
        commitment_detail = commitment.get("detail") or {}
        if commitment.get("action") == SCRIPT_RUN_STARTED_ACTION:
            started_run_id = str(commitment_detail.get("run_id"))
            if started_run_id == offer.run_id:
                paired.append((offer, TAKEN, None, None))
                continue
            if started_run_id in siblings.get(offer.run_id, set()):
                paired.append((offer, PASSED_OVER, None, None))
                continue
        action_name = str(commitment.get("action"))
        reasoning = commitment_detail.get("reasoning")
        reasoning = reasoning.strip() if isinstance(reasoning, str) else reasoning
        reasoning_display = reasoning if reasoning else _NO_REASONING
        paired.append((offer, DECLINED, action_name, reasoning_display))
    return paired


# -- FD7 (1): decision candidates - every expressible decision is owed -----------
# a play or a `plays no-change --decision <seq>`, deduped by generalized DSL.
#
# `development_candidates`, above, stays exactly as it was (`app.
# _followup_payload` still reads it): the old candidate set was only declined
# or passed-over script offers, plus a hand-played line repeated
# `HAND_PLAYED_REPEAT` times. That undercounts on purpose - a single-use
# attack, block, decide or pass was never owed anything. `decision_candidates`
# is the fuller set: every decision `decision_dsl.decision_rows` could
# express at all (`row["dsl"] is not None`), one candidate per distinct
# generalized shape - two decisions whose `dsl` (the exact play
# `decision_rows` would write, card names never among its fields) is
# byte-for-byte identical need only one answer between them, since admitting
# or declining a play answers every decision it would have applied to alike.

#: The prefix a decision candidate's own id is built from - `plays no-change
#: --decision <seq>` posts this as the `no_change` entry's `candidate` (the
#: same free-form field `--play`/`--line` already write there), so
#: `assess` and a filed `plays_outcome` row agree on what closes it. Chosen
#: to collide with neither a play id (`check_name` refuses a `:`) nor a
#: hand-played line (an operator's own words, unlikely to start this way).
DECISION_CANDIDATE_PREFIX = "decision:"


def decision_candidate_id(seq: Any) -> str:
    """The id `plays no-change --decision <seq>` closes: stable for a given
    `seq`, so a candidate grouping several occurrences together is always
    named by its first (oldest, hence stable across a game in progress)."""
    return f"{DECISION_CANDIDATE_PREFIX}{seq}"


@dataclass(frozen=True)
class DecisionCandidate:
    """One expressible decision `seat` faced, generalized: `action` is
    `decision_dsl`'s own action-kind label, `dsl` is the exact generalized
    play every one of `seqs` converts to, and `seqs` is every occurrence,
    oldest first. `id` is what closes it (`decision_candidate_id` of its
    oldest occurrence)."""

    action: str
    dsl: Mapping[str, Any]
    seqs: tuple[int, ...]

    @property
    def id(self) -> str:
        return decision_candidate_id(self.seqs[0])


def decision_candidates(
    records: Iterable[Mapping[str, Any]], seat: str
) -> tuple[DecisionCandidate, ...]:
    """Every expressible decision `seat` faced, in `records` (one game's own
    ledger, unfiltered by `game_id` for the same reason `decision_dsl.
    decision_rows` itself takes none: its caller has already scoped it),
    deduped by generalized DSL - see the section docstring above."""
    all_records = list(records)
    rows = decision_dsl.decision_rows(all_records, seat)
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for decision in rows:
        dsl = decision.get("dsl")
        seq = decision.get("seq")
        if dsl is None or seq is None:
            continue
        key = json.dumps(dsl, sort_keys=True, default=str)
        if key not in grouped:
            order.append(key)
        grouped.setdefault(key, []).append(decision)
    candidates = []
    for key in order:
        group = sorted(grouped[key], key=lambda d: d["seq"])
        candidates.append(
            DecisionCandidate(
                action=group[0]["action"],
                dsl=group[0]["dsl"],
                seqs=tuple(d["seq"] for d in group),
            )
        )
    return tuple(candidates)


@dataclass(frozen=True)
class Assessment:
    """`assess`'s own verdict: every candidate this seat faced, and which of
    them are still `open` - closed by neither an admitted play with a real
    `dsl_change` (any `plays_outcome` row of this seat's own that admitted
    at least one entry whose `dsl_change` is true, the same "development
    happened this game" signal that already closes every
    `development_candidates` line - see `mtg_agent.driver.handover.assess`)
    nor a `plays no-change --decision <seq>` naming one of its own `seqs`.

    Review fix: an `admitted` entry that is only a `plays priority` nudge
    (`dsl_change: False`, nothing in `added`/`updated`) is not a DSL change
    and must not count - `detail.get("admitted")` being a non-empty list is
    not itself the signal; at least one of its entries must actually carry
    `dsl_change: true`, exactly the distinction `handover.assess`'s own
    `dsl_change` accumulator already draws."""

    candidates: tuple[DecisionCandidate, ...]
    open: tuple[DecisionCandidate, ...]

    @property
    def satisfied(self) -> bool:
        return not self.open


def assess(records: Iterable[Mapping[str, Any]], seat: str) -> Assessment:
    """Every expressible decision `seat` faced, closed or open - see
    `Assessment`'s own docstring for exactly what closes one."""
    all_records = list(records)
    candidates = decision_candidates(all_records, seat)
    closed_ids: set[str] = set()
    any_admitted = False
    for record in all_records:
        payload = _payload(record)
        if payload.get("action") != PLAYS_OUTCOME_ACTION:
            continue
        detail = payload.get("detail") or {}
        if detail.get("player") != seat:
            continue
        for admitted_entry in detail.get("admitted") or ():
            if isinstance(admitted_entry, Mapping) and admitted_entry.get("dsl_change"):
                any_admitted = True
                break
        for entry in detail.get("no_change") or ():
            if not isinstance(entry, Mapping):
                continue
            candidate_id = entry.get("candidate")
            if isinstance(candidate_id, str):
                closed_ids.add(candidate_id)
    open_candidates = tuple(
        candidate
        for candidate in candidates
        if not any_admitted
        and not any(decision_candidate_id(seq) in closed_ids for seq in candidate.seqs)
    )
    return Assessment(candidates=candidates, open=open_candidates)


# -- D2b: the post-game development tables ---------------------------------------

#: The move rows a seat can make by hand that a play could make for it, and
#: the verb each one is printed with: a cast, an activation, a land play.
HAND_PLAYED_KINDS = {"cast": "cast", "activate": "activate", "play_land": "play land"}

#: How many times one hand-played line must repeat in a game before it is a
#: development candidate: a line played twice is a line a script could play.
HAND_PLAYED_REPEAT = 2

HAND_PLAYED_HEADER = "line | times | candidate"

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _script_spans(all_records: list[Mapping[str, Any]], game_id: str, seat: str) -> list[range]:
    """The ledger spans `seat`'s own script runs covered in `game_id`: from a
    run's `script_run_started` row (or its finish's `seq_start`, whichever is
    earlier) to its `script_run` finish row (or `seq_end`, whichever is
    later). A run with no finish yet covers nothing but its own start row -
    a run that crashed must not swallow every later hand play."""
    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    for record in all_records:
        payload = _payload(record)
        action = payload.get("action")
        if action not in (SCRIPT_RUN_STARTED_ACTION, SCRIPT_RUN_ACTION):
            continue
        detail = payload.get("detail") or {}
        if detail.get("seat") != seat or detail.get("game_id") != game_id:
            continue
        seq = record.get("seq")
        if not isinstance(seq, int):
            continue
        run_id = str(detail.get("run_id"))
        if action == SCRIPT_RUN_STARTED_ACTION:
            starts[run_id] = min(seq, starts.get(run_id, seq))
            continue
        low = detail.get("seq_start")
        high = detail.get("seq_end")
        starts[run_id] = min(
            [seq, starts.get(run_id, seq), *([low] if isinstance(low, int) else [])]
        )
        ends[run_id] = max([seq, *([high] if isinstance(high, int) else [])])
    return [range(starts[run_id], ends[run_id] + 1) for run_id in ends]


def _card_names(all_records: list[Mapping[str, Any]]) -> dict[str, str]:
    """Card id -> name, from every ledger row that names both (`add_card`
    and its kin). A seat may cast by name or by id; this is how a line cast
    by id still groups with the same card cast by name."""
    names: dict[str, str] = {}
    for record in all_records:
        detail = _payload(record).get("detail") or {}
        card_id, card = detail.get("card_id"), detail.get("card")
        if isinstance(card_id, str) and isinstance(card, str) and not _UUID.match(card):
            names[card_id] = card
    return names


def _card_label(reference: Any, names: Mapping[str, str]) -> str:
    """The card a move names, by name where the ledger says it; an id no row
    names prints as `card <id>`."""
    text = str(reference or "").strip()
    if not text:
        return "card ?"
    if text in names:
        return names[text]
    if _UUID.match(text):
        return f"card {text}"
    return text


def _hand_played_counts(
    records: Iterable[Mapping[str, Any]], game_id: str, seat: str
) -> dict[str, int]:
    """How often `seat` played each line by hand: its own allowed casts,
    activations and land plays that no script run of its own covered."""
    all_records = list(records)
    spans = _script_spans(all_records, game_id, seat)
    names = _card_names(all_records)
    counts: dict[str, int] = {}
    for record in all_records:
        payload = _payload(record)
        kind = HAND_PLAYED_KINDS.get(str(payload.get("action")))
        if kind is None:
            continue
        detail = payload.get("detail") or {}
        if detail.get("player") != seat or detail.get("allowed") is not True:
            continue
        seq = record.get("seq")
        if isinstance(seq, int) and any(seq in span for span in spans):
            continue
        if payload.get("action") == "activate":
            label = f"{kind} {_card_label(detail.get('card_id') or detail.get('card'), names)}"
            if detail.get("ability_index") is not None:
                label = f"{label} ability {detail['ability_index']}"
        else:
            label = f"{kind} {_card_label(detail.get('card') or detail.get('card_id'), names)}"
        counts[label] = counts.get(label, 0) + 1
    return counts


def hand_played_summary(records: Iterable[Mapping[str, Any]], game_id: str, seat: str) -> str:
    """One line per line `seat` played by hand in `game_id` - its own casts,
    activations and land plays that were allowed and that no script run of
    its own made - grouped by kind and card, most repeated first.

    A line repeated `HAND_PLAYED_REPEAT` or more times is flagged
    `candidate`: a line the seat keeps playing by hand is a line a play
    could make for it, and the handover must either write that play or say
    why not (`mcp-seat plays no-change --line`). The `line` column is the
    exact text that command takes. Bounded like every table here
    (`_bounded_table`)."""
    counts = _hand_played_counts(records, game_id, seat)
    if not counts:
        return f"no hand-played casts, activations or land plays recorded for {seat} in {game_id}"
    ordered = [
        f"{line} | {times} | {'candidate' if times >= HAND_PLAYED_REPEAT else '-'}"
        for line, times in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return _bounded_table(HAND_PLAYED_HEADER, ordered, "hand-played line(s)")


@dataclass(frozen=True)
class DevelopmentCandidates:
    """What a handover must answer, one way or the other: every play this
    seat declined or passed over, and every line it played by hand
    `HAND_PLAYED_REPEAT` or more times. Each needs a conditions or
    advice-only change landed, or a `plays no-change` naming why not."""

    plays: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.plays and not self.lines


def development_candidates(
    records: Iterable[Mapping[str, Any]], game_id: str, seat: str
) -> DevelopmentCandidates:
    """`DevelopmentCandidates` for `seat` in `game_id`: the declined or
    passed-over plays in the offer pairing `offer_table` prints, in ledger
    order, and the `candidate` lines of `hand_played_summary`, most repeated
    first."""
    all_records = list(records)
    plays: list[str] = []
    for offer, outcome, _, _ in _offer_outcomes(all_records, game_id, seat):
        play_id = str(offer.detail.get("script_id"))
        if outcome in (DECLINED, PASSED_OVER) and play_id not in plays:
            plays.append(play_id)
    counts = _hand_played_counts(all_records, game_id, seat)
    lines = [
        line
        for line, times in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if times >= HAND_PLAYED_REPEAT
    ]
    return DevelopmentCandidates(plays=tuple(plays), lines=tuple(lines))


#: The four counts one play carries in an evidence file, in print order.
EVIDENCE_COUNTS = ("offered", "taken", "passed_over", "declined")

_OUTCOME_COUNT = {TAKEN: "taken", PASSED_OVER: "passed_over", DECLINED: "declined"}


def evidence_record(
    records: Iterable[Mapping[str, Any]],
    game_id: str,
    seat: str,
    deck: str,
    opponent_archetype: str | None,
) -> dict[str, Any]:
    """This game's evidence file for `deck`: `{game_id, deck,
    opponent_archetype, plays: {<play_id>: {offered, taken, passed_over,
    declined}}}` - the schema `plays admit` commits to
    `decks/<deck>/plays/evidence/<game_id>.json`.

    Counted off the same offer pairing `offer_table` prints
    (`_offer_outcomes`). `offered` is every offer of the play, whatever came
    of it; `taken`, `passed_over` and `declined` count the offers that came
    to each - so an offer nobody answered is counted as offered and nothing
    else, exactly as the table treats it."""
    plays: dict[str, dict[str, int]] = {}
    for offer, outcome, _, _ in _offer_outcomes(records, game_id, seat):
        play = plays.setdefault(
            str(offer.detail.get("script_id")), dict.fromkeys(EVIDENCE_COUNTS, 0)
        )
        play["offered"] += 1
        if outcome in _OUTCOME_COUNT:
            play[_OUTCOME_COUNT[outcome]] += 1
    return {
        "game_id": game_id,
        "deck": deck,
        "opponent_archetype": opponent_archetype,
        "plays": plays,
    }


#: How many of the newest evidence files `evidence_table` reads by default.
EVIDENCE_GAMES = 20

EVIDENCE_HEADER = "play | games | offered | taken | passed_over | declined | declined_games"


def evidence_dir(root: Path | str, deck: str) -> Path:
    """Where `plays admit` commits `deck`'s per-game evidence files."""
    return Path(root) / "decks" / deck / "plays" / "evidence"


def evidence_table(root: Path | str, deck: str, games: int = EVIDENCE_GAMES) -> str:
    """One line per play across the newest `games` evidence files of `deck`
    under `root` (a guides checkout): the games it was offered in, its
    summed counts, and `declined_games` - how many distinct games declined
    it at least once, which is what `WRITE_BARS`' "1 decline in each of 2
    distinct games" bar reads. Most declined first.

    Newest is by file modification time, then name: the schema carries no
    timestamp, and a file is written once, when its game is admitted. A file
    that does not parse, or is not the schema's shape, is skipped and the
    table says how many were. Bounded like every table here."""
    folder = evidence_dir(root, deck)
    try:
        files = [path for path in folder.glob("*.json") if path.is_file()]
    except OSError:
        files = []
    files.sort(key=lambda path: (_mtime(path), path.name), reverse=True)
    totals: dict[str, dict[str, int]] = {}
    skipped = 0
    read = 0
    for path in files[: max(0, games)]:
        try:
            plays = json.loads(path.read_text()).get("plays")
        except (OSError, ValueError, AttributeError):
            skipped += 1
            continue
        if not isinstance(plays, Mapping):
            skipped += 1
            continue
        read += 1
        for play_id, counts in plays.items():
            if not isinstance(counts, Mapping):
                continue
            row = totals.setdefault(
                str(play_id), {"games": 0, **dict.fromkeys(EVIDENCE_COUNTS, 0), "declined_games": 0}
            )
            row["games"] += 1
            for name in EVIDENCE_COUNTS:
                value = counts.get(name)
                row[name] += value if isinstance(value, int) and value > 0 else 0
            declined = counts.get("declined")
            if isinstance(declined, int) and declined > 0:
                row["declined_games"] += 1
    note = f"{skipped} unreadable evidence file(s) skipped" if skipped else None
    if not totals:
        absence = (
            f"no cross-game evidence recorded for {deck} yet "
            f"(decks/{deck}/plays/evidence/ holds no game)"
        )
        return f"{absence}; {note}" if note else absence
    ordered = [
        " | ".join(
            [
                play_id,
                str(row["games"]),
                *(str(row[name]) for name in EVIDENCE_COUNTS),
                str(row["declined_games"]),
            ]
        )
        for play_id, row in sorted(totals.items(), key=lambda item: (-item[1]["declined"], item[0]))
    ]
    header = EVIDENCE_HEADER if note is None else f"{note}\n{EVIDENCE_HEADER}"
    return _bounded_table(header, ordered, "play row(s)")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _bounded_table(header: str, lines: list[str], noun: str) -> str:
    """`lines`, prefixed with `header`, cut to `TABLE_LIMIT_BYTES` from the
    bottom - every caller sorts highest priority first, so the lowest-
    priority rows are the ones dropped - with a `TRUNCATION_MARKER` line
    naming how many of `noun` were left out."""
    table = "\n".join([header, *lines])
    if _size(table) <= TABLE_LIMIT_BYTES:
        return table

    kept = [header]
    for shown, line in enumerate(lines):
        marker = _marker(len(lines) - shown - 1, noun)
        if _size("\n".join([*kept, line, marker])) > TABLE_LIMIT_BYTES:
            return "\n".join([*kept, _marker(len(lines) - shown, noun)])
        kept.append(line)
    raise AssertionError("a table over the limit always cuts at least one row")


def _marker(omitted: int, noun: str) -> str:
    return f"{TRUNCATION_MARKER} {omitted} more {noun} not shown"


def _size(text: str) -> int:
    return len(text.encode("utf-8"))
