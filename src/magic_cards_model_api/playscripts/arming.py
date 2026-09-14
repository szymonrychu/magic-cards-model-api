"""S2 A2: arming - let an agent hand over an ordered list of scripts to fire
on its own, rather than run one by hand every time the human plays.

The owner's own framing: "wait until could require picking scripts to run (in
priority order). if the situation still matches scripts conditions, those
will fire automatically." Arming is preferred, never mandatory - a seat that
never arms anything plays exactly as it always did - and its lifetime is one
call: nothing here remembers an armed list once `arm()` returns, so a seat
that wants the same scripts armed on its next wait says so again.

`arm()` judges every armed id against the board with the same logic
`script_tools._judged`/the hint uses (`_judge_round`, below), fetching
`include=contents` once per round when a judged script needs it - never more,
since a script whose `when` does not match yet never reaches the condition
check that could need it. Two or more armed scripts applying at the same
instant is a conflict: arming is meant to save a human a decision, not make
one for them, so nothing fires and the conflicting ids are named back.
Otherwise the one script that applies fires through
`script_tools.run(wait=False, invoked_via="armed")` - `wait=False` because
firing is only ever "now, if it already applies", never "pass priority to
reach a later `when`" (that is what the seat's own wait/pass_until is for;
see `seatkit.py`'s wiring).

Every exit `script_tools.run` can hand back is accounted for:

- `chain_complete` fired; the batch re-judges the rest of `arm` on the new
  board and keeps going.
- `not_applicable`/`already_ran` did not fire (a race between the judge
  snapshot and the run's own fresh read, most often); dropped, batch
  continues with what is left.
- `needs_input`/`invalid_input` did not fire either, but is worth surfacing
  prominently - it is spliced to the *front* of `armed`, not appended, and
  the batch still continues with what is left.
- `needs_plan` stops the batch: a human has to `declare_plan` first.
- `refused`, `unforeseen`, `interrupted`, `ambiguous`, `stale`,
  `unknown_outcome`, or `advised` (an advisor seat) all stop the batch and
  hand back the board exactly where the run left it - none of them is
  something arming should paper over by trying again.
- `exit: null` (the run's own time cap) stops the batch too, carrying the
  `resume` token in that entry - arming itself has no cross-call memory to
  resume it with, so a caller who wants to finish it hands the token to
  `run_script` directly.

At most `MAX_ARMED_RUNS` scripts actually run in one `arm()` call.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..seatkit import Seat
from . import conditions, script_tools
from .inputs import BUILTIN_INPUT, input_catalog
from .runtime import REASONING_PLACEHOLDER, RUN_CAP_SECONDS, Exit, ScriptExit, _corpus

#: The most `arm()` actually runs in one call - the same ceiling
#: `runtime.MAX_SCRIPTS_PER_RUN` puts on one script's own chain, applied here
#: to how many *separate* armed scripts one call may fire.
MAX_ARMED_RUNS = 3

#: The least a run needs left of the batch's own deadline before `arm()` will
#: even start it (S2 arming re-review finding 5). `script_tools.run` posts
#: its own `start` row before it ever checks the time cap it was handed - a
#: run started with only a few milliseconds left posts that row, then exits
#: null (`resume`) the moment it next checks, with no matching `finish`. A
#: bare start with no finish yet still blocks the script for the rest of the
#: step (`records._prior_script_run`'s own docstring), so a near-zero budget
#: silently burns the slot instead of firing anything. Chosen with headroom
#: over a single ordinary HTTP round trip or two (a fetch plus one move), not
#: measured against any one script's own worst case - a script that
#: genuinely needs longer than this just resumes on the caller's next call,
#: the same as any other time-capped run.
MIN_ARMED_RUN_SECONDS = 3.0

#: `arm()`'s own note when the batch stops here rather than starting a run
#: below `MIN_ARMED_RUN_SECONDS` - the remaining armable ids were never
#: attempted, so a caller that wants them fired calls `arm()` again.
INSUFFICIENT_RUN_BUDGET_NOTE = (
    "stopped the batch here: fewer than MIN_ARMED_RUN_SECONDS remained of this call's own "
    "budget once the board was judged, and starting a run that close to the deadline risks "
    "posting a start row with no matching finish, which blocks the script for the rest of the "
    "step. The remaining armable ids were left unfired - call arm again for them."
)

EMPTY_ARM_REASONING = (
    "arm needs your own arm_reasoning: every armed script is judged against the board with "
    "it before it is ever allowed to fire, and an armed batch without one is refused"
)
PLACEHOLDER_ARM_REASONING = (
    f"arm needs your own arm_reasoning, not the call template's placeholder text: replace "
    f"{REASONING_PLACEHOLDER!r} with why you are arming this"
)

#: Exits that count as "this armed script fired" and let the batch move on to
#: re-judging the rest of `arm` without stopping.
_CONTINUES = frozenset(
    {Exit.CHAIN_COMPLETE.value, Exit.NOT_APPLICABLE.value, Exit.ALREADY_RAN.value}
)
#: Exits worth surfacing first in the answer rather than in firing order -
#: still let the batch continue, same as `_CONTINUES`.
_SURFACE_FIRST = frozenset({Exit.NEEDS_INPUT.value, Exit.INVALID_INPUT.value})


def validate_arm_request(
    arm: list[str],
    arm_reasoning: str,
    opposing_archetype: str = "*",
    inputs: dict[str, Any] | None = None,
) -> str | None:
    """The same up-front checks `arm()` itself makes before any fetch - a
    blank/placeholder `arm_reasoning`, or `opposing_archetype` disagreeing
    with `inputs` - factored out so a caller that wraps a wait or a pass
    around arming (`seatkit.wait_and_arm`/`pass_until`) can refuse an
    invalid arm request before that wait or pass ever runs, not only once
    `arm()` is finally reached afterward (S2 review finding 7). `None` for
    an empty `arm` (nothing to validate) or a request that passes."""
    if not arm:
        return None
    if not arm_reasoning or not arm_reasoning.strip():
        return EMPTY_ARM_REASONING
    if arm_reasoning.strip() == REASONING_PLACEHOLDER:
        return PLACEHOLDER_ARM_REASONING
    supplied = script_tools.merged_inputs(opposing_archetype, inputs)
    if isinstance(supplied, str):
        return supplied
    return None


def arm(
    seat: Seat,
    corpora: dict[tuple[str, str], dict[str, Any]],
    *,
    arm: list[str],
    arm_reasoning: str,
    opposing_archetype: str = "*",
    inputs: dict[str, Any] | None = None,
    max_seconds: float = 0.0,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any] | str | None:
    """Fire the armed scripts in `arm` that already apply, judged the same
    way the hint judges them. `None` when `arm` is empty (nothing to do); a
    refusal string when `arm` is non-empty but `arm_reasoning` is blank or is
    still the call template's own placeholder - refused before any fetch, the
    same as `run_script`'s own empty-reasoning refusal.

    Otherwise `{"armed": [...], "state": {...}}`, `armed_conflict: [ids]`
    added when two or more armed scripts applied at once (nothing fired that
    round). Each `armed` entry is a `script_exit`-shaped dict: `script`,
    `exit`, `moves_done`, `moves_total`, plus whatever else that exit sets.

    `armed_facts_unavailable: [{"id": ..., "reason": ...}, ...]` is added
    whenever a `_judge_round` this call made left one or more of `arm`'s own
    scripts not applicable because the hypothetical-facts fetch their
    selectors needed failed (`script_tools.hypothetical_skips`, D2) - `id`
    the script, `reason` the HTTP status/reason `Seat.hypothetical_facts`
    raised on. Before this, that failure only ever showed up as the script
    silently never firing, indistinguishable from its condition genuinely
    not holding; deduplicated across rounds within this one call, and logged
    once per round by `_FactsOnce` itself, never here."""
    if not arm:
        return None
    problem = validate_arm_request(arm, arm_reasoning, opposing_archetype, inputs)
    if problem is not None:
        return problem
    supplied = script_tools.merged_inputs(opposing_archetype, inputs)
    if isinstance(supplied, str):
        return supplied

    remaining = list(dict.fromkeys(arm))
    armed_out: list[dict[str, Any]] = []
    conflict: list[str] | None = None
    state: dict[str, Any] | None = None
    # S2 review finding 7: whether `state` is currently a run's own fresh
    # read (legitimate to carry whatever it revealed, contents included, as
    # part of what it actually did) or merely `_judge_round`'s own judging
    # fetch (paid only to evaluate a hidden-zone condition, never to reveal
    # one) - the latter must never become this call's own answer.
    state_from_run = False
    runs = 0
    # S2 review finding 3: one deadline for the whole batch, set once here -
    # each run used to get the *whole* `max_seconds`/`RUN_CAP_SECONDS` again,
    # so up to `MAX_ARMED_RUNS` runs could together take that many times the
    # cap. Every run below gets only what is left of this deadline.
    cap = max_seconds if max_seconds > 0 else RUN_CAP_SECONDS
    deadline = time.monotonic() + cap
    note: str | None = None
    facts_unavailable: list[dict[str, str]] = []
    facts_unavailable_ids: set[str] = set()

    while remaining and runs < MAX_ARMED_RUNS:
        if deadline - time.monotonic() <= 0:
            break
        judged, state = _judge_round(seat, corpora, remaining, supplied)
        state_from_run = False
        for skip in script_tools.hypothetical_skips(judged):
            if skip["id"] not in facts_unavailable_ids:
                facts_unavailable_ids.add(skip["id"])
                facts_unavailable.append(skip)
        applying = [
            script_id for script_id in remaining if judged.get(script_id, {}).get("applies")
        ]
        if len(applying) > 1:
            conflict = applying
            break
        if applying:
            script_id = applying[0]
        else:
            needing = [
                script_id
                for script_id in remaining
                if judged.get(script_id, {}).get("needs_input") is not None
                or judged.get(script_id, {}).get("invalid_input") is not None
            ]
            if not needing:
                break
            script_id = needing[0]

        # S2 arming re-review finding 5: read fresh here, AFTER `_judge_round`'s
        # own fetch - not once up front, which could hand a run whatever
        # near-zero time `_judge_round` left of the deadline. `script_tools.run`
        # posts its own `start` row before it ever checks the cap it is given,
        # so a run started this close to the deadline posts that row and then
        # exits null with no matching `finish` - which blocks the script for
        # the rest of the step (`records._prior_script_run`'s own docstring).
        # Below the minimum, the batch stops here rather than risk that.
        run_budget = deadline - time.monotonic()
        if run_budget < MIN_ARMED_RUN_SECONDS:
            note = INSUFFICIENT_RUN_BUDGET_NOTE
            break

        result = script_tools.run(
            seat,
            corpora,
            script=script_id,
            reasoning=arm_reasoning,
            opposing_archetype=opposing_archetype,
            inputs=inputs,
            wait=False,
            max_seconds=run_budget,
            should_stop=should_stop,
            invoked_via="armed",
        )
        runs += 1
        remaining.remove(script_id)
        if isinstance(result, str):
            # `merged_inputs` already validated above; this defends anyway
            # against a run-time disagreement (e.g. a caller mutating
            # `inputs` between judge and fire is not possible here, but a
            # future caller of `script_tools.run` might refuse for a reason
            # this function has not foreseen).
            armed_out.append({"script": script_id, "exit": Exit.REFUSED.value, "refusal": result})
            break

        exit_block = result["script_exit"]
        state = result["state"]
        state_from_run = True
        exit_name = exit_block.get("exit")
        if exit_name in _SURFACE_FIRST:
            armed_out.insert(0, exit_block)
        else:
            armed_out.append(exit_block)
        if exit_name in _CONTINUES or exit_name in _SURFACE_FIRST:
            continue
        break

    out: dict[str, Any] = {"armed": armed_out}
    if conflict is not None:
        out["armed_conflict"] = conflict
    if note is not None:
        out["note"] = note
    if facts_unavailable:
        out["armed_facts_unavailable"] = facts_unavailable
    if state is not None:
        if not state_from_run:
            state = {
                key: value
                for key, value in state.items()
                if key not in ("library_contents", "sideboard_contents")
            }
        out["state"] = state
    return out


def _judge_round(
    seat: Seat,
    corpora: dict[tuple[str, str], dict[str, Any]],
    ids: list[str],
    supplied: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Every id in `ids`, judged against the board right now - the same
    `_judged`/`_hint_judged` two-pass logic `applicable_hint` uses: a plain
    fetch first, then one extra `include=contents` fetch, paid once for
    however many of `ids` need it, for a script whose priority/archetype/
    `when` already match but whose conditions read this seat's own hidden
    zones."""
    corpus = _corpus(seat, corpora, fresh=False)
    if isinstance(corpus, ScriptExit):
        state = seat.fetch_state()
        return {}, state
    declared = list(corpus.get("inputs") or [])
    asserted = supplied.get(BUILTIN_INPUT)
    catalog = input_catalog(declared, [asserted] if isinstance(asserted, str) else ())
    docs = [doc for doc in corpus.get("scripts") or [] if isinstance(doc, dict)]
    plays = {
        doc["id"]: (doc, play)
        for doc, play in script_tools._plays(corpus, docs)
        if isinstance(doc.get("id"), str)
    }
    state = seat.fetch_state()
    facts = script_tools._FactsOnce(seat)
    judged: dict[str, dict[str, Any]] = {}
    deferred: list[str] = []
    for script_id in ids:
        entry = plays.get(script_id)
        if entry is None:
            judged[script_id] = {
                "id": script_id,
                "applies": False,
                "failing_condition": None,
                "refusal": f"no script {script_id!r} is registered for this seat",
            }
            continue
        doc, play = entry
        try:
            judged[script_id] = script_tools._judged(
                doc, play, state, seat.player, supplied, catalog, [], facts=facts
            )
        except conditions.ContentsRequired:
            deferred.append(script_id)
    if deferred:
        contents_state = seat.fetch_state(include_contents=True)
        for script_id in deferred:
            doc, play = plays[script_id]
            judged[script_id] = script_tools._hint_judged(
                doc, play, contents_state, seat.player, supplied, catalog, [], facts=facts
            )
        state = contents_state
    # S2 review finding 5: an id this process already ran at this exact
    # `(turns_taken, step)` position must not count toward the conflict
    # check either - `applicable_hint` already never re-offers one of these
    # (S1 finding 4c), and a script still condition-true after firing once
    # (two untapped Islands still satisfy "at least one untapped" after
    # tapping one) could otherwise tie with a genuinely new applier and
    # report a conflict that was never really there.
    recent = {
        (str(run[0]), run[1], run[2])
        for run in corpus.get(script_tools._RECENT_RUNS_KEY) or []
        if isinstance(run, list) and len(run) == 3
    }
    if recent:
        position = (script_tools._turn_position(state), state.get("step"))
        for script_id, entry in judged.items():
            if entry.get("applies") and (script_id, *position) in recent:
                entry["applies"] = False
                entry["failing_condition"] = {
                    "list": "already_run_this_step",
                    "index": None,
                    "seen": position,
                }
    return judged, state


__all__ = [
    "EMPTY_ARM_REASONING",
    "INSUFFICIENT_RUN_BUDGET_NOTE",
    "MAX_ARMED_RUNS",
    "MIN_ARMED_RUN_SECONDS",
    "PLACEHOLDER_ARM_REASONING",
    "arm",
    "validate_arm_request",
]
