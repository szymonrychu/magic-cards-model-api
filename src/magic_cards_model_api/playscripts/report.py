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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..audit import SCRIPT_RUN_ACTION, SCRIPT_RUN_OFFERED_ACTION, SCRIPT_RUN_STARTED_ACTION

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

    if not offers:
        return f"no script offers recorded for {seat} in {game_id}"

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

    lines: list[tuple[Any, _Offer, str]] = []
    for offer in offers:
        commitment = _committing_row(all_records, offer.index, seat)
        if commitment is None:
            lines.append((offer, OFFERED, None, None))
            continue
        commitment_detail = commitment.get("detail") or {}
        if commitment.get("action") == SCRIPT_RUN_STARTED_ACTION:
            started_run_id = str(commitment_detail.get("run_id"))
            if started_run_id == offer.run_id:
                lines.append((offer, TAKEN, None, None))
                continue
            if started_run_id in siblings.get(offer.run_id, set()):
                lines.append((offer, PASSED_OVER, None, None))
                continue
        action_name = str(commitment.get("action"))
        reasoning = commitment_detail.get("reasoning")
        reasoning = reasoning.strip() if isinstance(reasoning, str) else reasoning
        reasoning_display = reasoning if reasoning else _NO_REASONING
        lines.append((offer, DECLINED, action_name, reasoning_display))

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
