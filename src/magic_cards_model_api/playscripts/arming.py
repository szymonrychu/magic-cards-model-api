"""S2 A2 / API-7: arming - let an agent hand over an ordered list of scripts
to judge on its own, rather than check `list_scripts`/a script's own `when`
by hand every time the human plays.

The owner's own original framing was "if the situation still matches a
script's conditions, it fires automatically" - API-7 replaces that with a
narrower, load-bearing one: arming never fires anything. `arm()` only judges
every armed id in `arm` against the board right now, the same way the hint
judges them (`_judge_round`, below - a plain fetch first, then one extra
`include=contents` fetch, paid once per round for however many judged
scripts need it), and reports which of them apply. The seat itself still
sends the one committing call that actually runs a play - `pass_until`'s own
armed-judge hook (`seatkit._pass_until_once`) stops the wait the instant this
reports an applying play, and `applicable_hint` (`script_tools.py`) marks
that play first in `scripts.applicable` with a ready `call`, so the seat
reads it off the very answer that stopped, exactly as it would read any other
applicable play. Nothing here remembers an armed list once `arm()` returns -
a caller that wants the same scripts judged on its next wait says so again.

Two or more armed scripts applying at the same instant is a conflict, exactly
as before: arming is meant to save a human a decision, not make one for them,
so `applying` comes back empty and the conflicting ids are named back under
`armed_conflict` instead - the seat picks.

`armed_facts_unavailable: [{"id": ..., "reason": ...}, ...]` is added
whenever this round's own `_judge_round` left one or more of `arm`'s own
scripts un-judged because the hypothetical-facts fetch their selectors
needed failed (`script_tools.hypothetical_skips`, D2) - before this, that
failure only ever showed up as the script silently never applying,
indistinguishable from its condition genuinely not holding.

`armed_unregistered: [{"id": ..., "reason": ...}]` names an armed id this
seat has no script for at all - a typo, or a script never registered -
`_judge_round`'s own computed `refusal` for exactly that id, surfaced rather
than silently read the same as "does not apply yet".
"""

from __future__ import annotations

from typing import Any

from ..seatkit import Seat
from . import conditions, script_tools
from .inputs import BUILTIN_INPUT, input_catalog
from .runtime import REASONING_PLACEHOLDER, ScriptExit, _corpus

EMPTY_ARM_REASONING = (
    "arm needs your own arm_reasoning: every armed script is judged against the board with "
    "it before it is ever reported as applying, and an armed batch without one is refused"
)
PLACEHOLDER_ARM_REASONING = (
    f"arm needs your own arm_reasoning, not the call template's placeholder text: replace "
    f"{REASONING_PLACEHOLDER!r} with why you are arming this"
)


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
) -> dict[str, Any] | str | None:
    """Judge every armed id in `arm` against the board right now - never
    runs anything (API-7: the owner dropped autofire entirely). `None` when
    `arm` is empty (nothing to do); a refusal string when `arm` is
    non-empty but `arm_reasoning` is blank or is still the call template's
    own placeholder - refused before any fetch, the same as `run_script`'s
    own empty-reasoning refusal.

    Otherwise `{"applying": [...], "state": {...}}`. `applying` names the
    armed ids (from `arm`, deduplicated) that apply against the board right
    now - always at most one: two or more applying at the same instant is a
    conflict (`armed_conflict`, below), and `applying` is then empty, since
    arming exists to save a seat a decision, never to make one for it.

    `armed_conflict: [ids]` when two or more armed ids apply at once.

    `armed_unregistered: [{"id": ..., "reason": ...}]` for an armed id this
    seat has no script for - see the module docstring.

    `armed_facts_unavailable: [{"id": ..., "reason": ...}, ...]` for an
    armed script whose own selectors needed `Seat.hypothetical_facts` and
    the fetch failed - see the module docstring.

    `state` is this judge's own fresh read of the board, with
    `library_contents`/`sideboard_contents` stripped: a judge fetch is paid
    only to evaluate a hidden-zone condition, never to reveal one (S2
    review finding 7, still true now that judging is all this function ever
    does)."""
    if not arm:
        return None
    problem = validate_arm_request(arm, arm_reasoning, opposing_archetype, inputs)
    if problem is not None:
        return problem
    supplied = script_tools.merged_inputs(opposing_archetype, inputs)
    if isinstance(supplied, str):
        return supplied

    ids = list(dict.fromkeys(arm))
    judged, state = _judge_round(seat, corpora, ids, supplied)

    applying = [script_id for script_id in ids if judged.get(script_id, {}).get("applies")]
    out: dict[str, Any] = {"applying": []}
    if len(applying) > 1:
        out["armed_conflict"] = applying
    else:
        out["applying"] = applying

    unregistered = [
        {"id": script_id, "reason": judged[script_id]["refusal"]}
        for script_id in ids
        if judged.get(script_id, {}).get("refusal") is not None
    ]
    if unregistered:
        out["armed_unregistered"] = unregistered

    facts_unavailable = script_tools.hypothetical_skips(judged)
    if facts_unavailable:
        out["armed_facts_unavailable"] = facts_unavailable

    # S2 review finding 7, still true with nothing here ever running
    # anything: a judge's own `include=contents` fetch is paid only to
    # evaluate a hidden-zone condition, and must never leak that zone into
    # the answer merely because judging happened to read it.
    out["state"] = {
        key: value
        for key, value in state.items()
        if key not in ("library_contents", "sideboard_contents")
    }
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
        position = (script_tools._turn_position(state), script_tools.step_position(state))
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
    "PLACEHOLDER_ARM_REASONING",
    "arm",
    "validate_arm_request",
]
