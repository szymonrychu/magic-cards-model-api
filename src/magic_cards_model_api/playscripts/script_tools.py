"""The play-script tools, once, for both front ends.

`mcp.run_script`/`mcp.list_scripts` and `mcp-seat script run|list` are the
same two tools with the same argument names, so what they do lives here and
each front end only shapes the answer: MCP narrows the board through
`mcp._tool_result`, the CLI prints the whole state for a human who can scroll.
Like `runtime` itself, nothing here knows which transport called it.

`run` hands `runtime.run_script` the agent's `inputs` with the asserted
`opposing_archetype` among them, and the corpus's declared inputs (`inputs`
beside `scripts`, empty when the corpus carries none), and renders the result
as `script_exit`: `exit`, `script`, `moves_done`, `moves_total`, then only the
typed fields the exit actually set. A run the call's time cap cut off has no
exit yet: `exit` is null and `resume` is the token to hand back.

`listing` judges every registered script against the board as it is now, in
the runner's own order (priority 0, `opposing_archetypes`, `when`, then the
conditions), without running or recording anything, and lists them by
priority, highest first. Each carries its static conflicts - the
`play-conflict`/`next-conflict` warnings `plays check` raises that name it -
and `runtime_conflict`, true when more than one listed script applies now.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from ..seatkit import (
    MAX_WAIT_SECONDS,
    HypotheticalFactsUnavailable,
    Seat,
    decision_is_mine,
    scripts_enabled_here,
    step_position,
)
from ..seatkit import holds_priority as seat_holds_priority
from . import conditions, lint, runtime
from .inputs import BUILTIN_INPUT, input_catalog
from .schema import MAX_NOTE_LENGTH, WILDCARD

logger = logging.getLogger("mcm.script_tools")

#: `script_exit`'s fields, in the order they are written: `runtime.ScriptExit`'s
#: own, with `script_id` spelled `script` and the ledger's `run_id` left out,
#: then `resume`. The first four are always there; the rest only when set.
SCRIPT_EXIT_FIELDS: tuple[str, ...] = (
    *(
        "script" if field.name == "script_id" else field.name
        for field in dataclasses.fields(runtime.ScriptExit)
        if field.name != "run_id"
    ),
    "resume",
)
ALWAYS_WRITTEN = ("exit", "script", "moves_done", "moves_total")

#: Every key `listing` writes, the conflict entries' own included.
LISTING_FIELDS: tuple[str, ...] = (
    "deck_slug",
    "guides_revision",
    "opposing_archetype",
    "scripts",
    "applicable",
    "runtime_conflict",
    "id",
    "name",
    "priority",
    "applies",
    "advice_only",
    "failing_condition",
    "needs_input",
    "invalid_input",
    "refusal",
    "conflicts",
    "code",
    "path",
    "plays",
    "dimensions",
    "message",
)

CUT_OFF = (
    "the call's time cap came before the run ended; pass `resume` back, unchanged, "
    "to carry on with the same run"
)

#: The rest of the built-in `opposing_archetype` enum. `plays check` reads a
#: deck's matchup stems off its newest analysis run; a registered corpus
#: carries no such list, so the enum here is `*`, every stem the corpus itself
#: names, and this one value standing for every stem it does not - the deck
#: has matchups its scripts never mention, and two plays that each exclude a
#: different named stem still both apply against those.
OTHER_ARCHETYPES = "~any-other-matchup"

#: The most an `applicable_hint` answer ever names, highest priority first -
#: a corpus registering many scripts must not blow out every wake/action
#: answer's size.
MAX_HINT_SCRIPTS = 5

#: A hint entry's `name` is truncated to this many characters (S1 review
#: finding 6) - a corpus is registered by the deck, and nothing bounds how
#: long a play's own `name` is before this.
MAX_HINT_NAME_CHARS = 80

#: The whole `scripts` block's own byte budget, on top of `MAX_HINT_SCRIPTS`'
#: cap on entry *count* - a `call` carrying a long `inputs=` dict can still
#: blow a wake/action answer out even at 5 entries, so entries past this
#: budget are dropped rather than counted (S1 review finding 6). Shared by
#: all three of `applicable`, `advice` and `armable` (API-4) - one budget
#: over the flat list of surviving entries, not one per block.
MAX_HINT_BYTES = 4096

#: Where `run`/`listing` stash the archetype this seat last asserted, onto
#: the very corpus dict `Seats.script_corpora` caches - never a key the API
#: itself writes, so it needs no schema entry and nothing else ever reads it
#: but `applicable_hint`, below.
_LAST_ASSERTED_KEY = "_last_asserted_archetype"

#: Where `run` stashes the non-archetype `inputs` a seat last supplied to
#: `run_script`, next to `_LAST_ASSERTED_KEY` on the same corpus dict -
#: `applicable_hint` judges with them too, and names them in the offered
#: `call` when non-empty (S1 review finding 4b).
_LAST_INPUTS_KEY = "_last_supplied_inputs"

#: Where `run` records `[script_id, position, step]` for every script this
#: process has run (any exit) - `applicable_hint` never re-offers one of
#: these at the same position, even when the board still satisfies its
#: conditions (S1 review finding 4c). `position` is `_turn_position`'s
#: reading (`turns_taken`, falling back to `turn_number`), never `turn_number`
#: alone: an extra turn (CR 500.7) reaching the same step must count as a new
#: position, and `turn_number` deliberately does not move for one (S1 review
#: fix 3).
_RECENT_RUNS_KEY = "_recent_runs"

#: Every registered play's normalised/validated `Play` dump, cached on the
#: corpus dict the first time either `listing` or `applicable_hint` needs it
#: - re-validating the same docs on every single wake/action answer is not
#: free, and nothing about a registered corpus changes between two calls that
#: both read it with `fresh=False` (S1 review finding 5).
_PLAYS_CACHE_KEY = "_normalised_plays"

#: `(script_id, position[0], position[1])` -> the `log_seq` of the first
#: `phase: offer` row `applicable_hint` posted for it (API-6, F4) - stashed
#: on the same corpus dict as `_RECENT_RUNS_KEY`, in the same style: a plain
#: dict this process keeps for itself, never a key the API itself writes or
#: reads, and never carried by `fresh=True`'s allow-list either (a refresh
#: drops it, same as `_PLAYS_CACHE_KEY`, and this recomputes exactly when a
#: fresh read would anyway have nothing to dedupe against). A hint that
#: finds a play already offered at this exact position posts no second row
#: - it reuses that first offer's own `log_seq` instead - because without
#: this the hint rides every wake AND every action answer, so one applying
#: play would post a row per answer and the counts feeding the reviewer bars
#: would be meaningless.
_OFFERED_KEY = "_offered_plays"

#: `script_id` -> the `run_id` of the most recent `phase: offer` row this
#: process posted (or reused, F4) for it - stashed on the same corpus dict,
#: in the same per-process style as `_OFFERED_KEY` right above, and refreshed
#: on every `applicable_hint` call that still offers `script_id` (API-10).
#:
#: `run` (below) pops this the moment a **fresh** `run_script` call actually
#: starts that same `script_id`, and hands the popped value on as that run's
#: own `run_id` instead of a freshly minted one - the one piece API-6/API-8
#: never wired: `records.record_script_run_start` mints its own `run_id`
#: (`runtime.py`'s `uuid.uuid4().hex`) independently of whatever `run_id` the
#: offer that invited this run was posted under, so `report.offer_table`'s
#: own "taken"/"passed over" reading - which matches a `script_run_started`
#: row's `run_id` against an offer's - could never see a live run as
#: anything but a `decline` of every offer that ever preceded it. Popped
#: rather than merely read: a run_id is claimed by at most one start, so a
#: second, unrelated start of the same script later (a fresh offer never
#: having intervened) gets its own fresh id rather than a stale reuse of one
#: this process already spent.
_PENDING_OFFER_RUN_ID_KEY = "_pending_offer_run_ids"


def merged_inputs(opposing_archetype: str, inputs: dict[str, Any] | None) -> dict[str, Any] | str:
    """The inputs the runner is handed: `inputs` with `opposing_archetype`
    among them, or why the two disagree. `opposing_archetype` left at `*`
    defers to an `opposing_archetype` in `inputs`."""
    supplied = dict(inputs or {})
    named = supplied.get(BUILTIN_INPUT)
    if BUILTIN_INPUT in supplied and opposing_archetype not in (WILDCARD, named):
        return (
            f"`opposing_archetype` is {opposing_archetype!r} but `inputs` names "
            f"opposing_archetype {named!r}: assert one archetype, in either place"
        )
    supplied.setdefault(BUILTIN_INPUT, opposing_archetype)
    return supplied


def parse_resume(resume: str) -> dict[str, Any] | None | str:
    """The resume token `resume` carries, None for none, or why it is not one."""
    if not resume:
        return None
    try:
        token = json.loads(resume)
    except ValueError as exc:
        return (
            f"`resume` is not JSON: {exc}. Hand it back exactly as the previous "
            "answer gave it to you, or leave it out to start a new run."
        )
    if not isinstance(token, dict):
        return (
            "`resume` must be the object a previous run's `script_exit.resume` "
            "carried, handed back unchanged."
        )
    return token


def run(
    seat: Seat,
    corpora: dict[tuple[str, str], dict[str, Any]],
    *,
    script: str,
    reasoning: str,
    opposing_archetype: str = WILDCARD,
    resume: str = "",
    wait: bool = True,
    max_seconds: float = 0.0,
    inputs: dict[str, Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
    invoked_via: str = "mcp",
) -> dict[str, Any] | str:
    """`{script_exit, state}` for one run, or the refusal text for arguments
    no run can start from. `max_seconds` left at 0 runs up to
    `runtime.RUN_CAP_SECONDS`; a positive value caps the call below that.
    `should_stop` ends the run's waiting early, as its time cap does.
    `invoked_via` ("mcp", "cli" or "armed") is carried onto the start/finish
    rows this run posts - `mcp.run_script` sends "mcp", `mcp-seat script run`
    sends "cli", `playscripts.arming.arm` sends "armed".

    A **fresh** start (never a `resume`, which continues an already-started
    run under its own existing `run_id`) that names a `script` this process
    has an outstanding offer for (`_PENDING_OFFER_RUN_ID_KEY`, API-10) claims
    that offer's own `run_id` for the run it is about to start, rather than
    leaving `runtime.run_script` mint a fresh one - the missing half of
    API-6/API-8's own pairing: without it, `report.offer_table` could never
    read a live run as anything but a decline of the very offer that
    proposed it, since nothing ever tied the two `run_id`s together."""
    supplied = merged_inputs(opposing_archetype, inputs)
    if isinstance(supplied, str):
        return supplied
    _remember_run_inputs(seat, corpora, supplied)
    token = parse_resume(resume)
    if isinstance(token, str):
        return token
    claimed_run_id: str | None = None
    if token is None:
        corpus = runtime._corpus(seat, corpora, fresh=False)
        if not isinstance(corpus, runtime.ScriptExit):
            pending = corpus.get(_PENDING_OFFER_RUN_ID_KEY)
            if isinstance(pending, dict):
                claimed_run_id = pending.pop(script, None)
    result = runtime.run_script(
        seat,
        script_id=script,
        reasoning=reasoning,
        corpora=corpora,
        inputs=supplied,
        declared_inputs=_declared(seat, corpora),
        wait=wait,
        resume=token,
        max_seconds=max_seconds if max_seconds > 0 else runtime.RUN_CAP_SECONDS,
        should_stop=should_stop,
        invoked_via=invoked_via,
        run_id=claimed_run_id,
    )
    exit_ = result.script_exit
    if (
        exit_ is not None
        and exit_.exit is runtime.Exit.REFUSED
        and exit_.refusal_source == "runner"
        and runtime.ADVICE_ONLY_MARKER in (exit_.refusal or "")
    ):
        # API-3 item 2: the same shape `merged_inputs`' own conflict already
        # takes above - a bare refusal string, never the ordinary
        # `{script_exit, state}` envelope, so `mcp.run_script` and
        # `mcp-seat script run` both report it identically (both branch on
        # `isinstance(answer, str)`) rather than one of them running a
        # draft-capturing/`_tool_result`-narrowing path meant for a play that
        # actually reached the board.
        return exit_.refusal
    state = result.state if result.state is not None else seat.fetch_state()
    exit_block = script_exit(result, _registered_moves(seat, corpora))
    _record_run(seat, corpora, script, state, exit_block, advise=seat.advise)
    return {"script_exit": exit_block, "state": state}


def script_exit(result: runtime.RunResult, moves: dict[str, int]) -> dict[str, Any]:
    """`result` as `script_exit`. `moves` is each registered script's move
    count, read for a cut-off run, which has no exit to carry one."""
    if result.script_exit is None:
        token = result.resume or {}
        chain = token.get("chain") or {}
        return {
            "exit": None,
            "script": token.get("script_id"),
            "moves_done": int(chain.get("moves_done") or 0),
            "moves_total": moves.get(str(token.get("script_id")), 0),
            "stop_reason": CUT_OFF,
            "resume": token,
        }
    raw = result.script_exit.to_dict()
    raw["script"] = raw.pop("script_id")
    raw.pop("run_id", None)
    if len(raw.get("scripts_run") or []) < 2:
        raw.pop("scripts_run", None)
    if raw.get("unvalidated"):
        # Named, never echoed: a move's selectors are the script's own text,
        # unbounded, and already in the script the agent wrote.
        raw["unvalidated"] = [
            {"index": entry.get("index"), "kind": entry.get("kind")} for entry in raw["unvalidated"]
        ]
    out = {name: raw.get(name) for name in ALWAYS_WRITTEN}
    for name in SCRIPT_EXIT_FIELDS:
        if name not in out and raw.get(name) not in (None, [], {}):
            out[name] = raw[name]
    return out


def listing(
    seat: Seat,
    corpora: dict[tuple[str, str], dict[str, Any]],
    *,
    opposing_archetype: str = WILDCARD,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any] | str:
    """Every registered script, judged against the board now, highest
    priority first; or why none can be listed."""
    supplied = merged_inputs(opposing_archetype, inputs)
    if isinstance(supplied, str):
        return supplied
    corpus = runtime._corpus(seat, corpora, fresh=False)
    if isinstance(corpus, runtime.ScriptExit):
        return f"cannot list scripts: {corpus.refusal}"
    asserted = supplied[BUILTIN_INPUT]
    corpus[_LAST_ASSERTED_KEY] = asserted
    declared = list(corpus.get("inputs") or [])
    catalog = input_catalog(declared, [asserted] if isinstance(asserted, str) else ())
    docs = [doc for doc in corpus.get("scripts") or [] if isinstance(doc, dict)]
    plays = _plays(corpus, docs)
    wants_contents = any(
        isinstance(play, dict) and conditions.needs_contents(play) for _, play in plays
    )
    state = seat.fetch_state(include_contents=wants_contents)
    facts = _FactsOnce(seat)
    warnings = static_conflicts(docs, declared, str(corpus.get("deck_slug") or ""))
    entries = [
        _judged(
            doc,
            play,
            state,
            seat.player,
            supplied,
            catalog,
            warnings,
            facts=facts,
        )
        for doc, play in plays
    ]
    entries.sort(key=lambda entry: (-_priority(entry), str(entry["id"])))
    # API-3 item 3: an advice-only entry never joins `applicable` (nothing
    # about it is runnable) even when its own conditions genuinely `apply`,
    # and so never counts toward `runtime_conflict` either.
    applicable = [entry["id"] for entry in entries if entry["applies"] and not entry["advice_only"]]
    ambiguous = len(applicable) > 1
    for entry in entries:
        entry["runtime_conflict"] = ambiguous and entry["applies"] and not entry["advice_only"]
    return {
        "deck_slug": corpus.get("deck_slug"),
        "guides_revision": corpus.get("guides_revision"),
        "opposing_archetype": asserted,
        "scripts": entries,
        "applicable": applicable,
        "runtime_conflict": ambiguous,
    }


def static_conflicts(
    docs: list[dict[str, Any]], declared: list[dict[str, Any]], deck_slug: str
) -> list[lint.PlaysWarning]:
    """`plays check`'s conflict warnings over a registered corpus."""
    named: set[str] = set()
    for doc in docs:
        named |= {key for key in doc.get("opposing_archetypes") or [] if isinstance(key, str)}
        for polarity in ("true", "false"):
            for condition in doc.get(polarity) or []:
                if condition.get("kind") == "input" and condition.get("name") == BUILTIN_INPUT:
                    value = condition.get("value")
                    values = value if isinstance(value, list) else [value]
                    named |= {entry for entry in values if isinstance(entry, str)}
    catalog = input_catalog(declared, (named - {WILDCARD}) | {OTHER_ARCHETYPES})
    plays = [
        (doc["id"], f"decks/{deck_slug}/plays/{doc['id']}.json", doc)
        for doc in docs
        if isinstance(doc.get("id"), str)
    ]
    return lint._conflict_warnings(plays, catalog)


def _first_move_allowed(play: dict[str, Any], state: dict[str, Any], me: str) -> bool:
    """Whether `play`'s own first move could even be attempted right now -
    CR 608.2d freezes every action but answering the one pending decision
    until it is answered, so a play whose first move is not an `answer` to
    exactly that decision cannot legally run, whoever it is addressed to
    (S2 review finding 4). No pending decision at all: always allowed."""
    pending = state.get("pending_decision")
    if pending is None:
        return True
    moves = play.get("moves") or []
    first = moves[0] if moves else None
    if not isinstance(first, dict) or first.get("kind") != "answer":
        return False
    if not decision_is_mine(pending, state, me):
        return False
    answers = first.get("answers") or []
    return any(
        isinstance(answer, dict) and answer.get("decision") == pending.get("kind")
        for answer in answers
    )


class _FactsOnce:
    """This seat's hypothetical facts (`Seat.hypothetical_facts`) for one
    judgement over one board: fetched at most once, and only when a play
    that needs them (`conditions.needs_hypothetical`) actually reaches
    condition evaluation in `_judged` - `listing`, `applicable_hint` and
    `arming._judge_round` all share this, so a play judged by any of the
    three reads the facts the same way (O7: one fetch however many plays
    need them). `armable`'s pass never evaluates a condition, so it never
    fetches.

    D2: a failed fetch is remembered as `unavailable`, the error's own text,
    so `_judged` marks only the plays that needed the facts not applicable,
    with why - never raises out of the answer, and never stands in a bare
    `{}` (a `count {unlocks: true} == 0` would read that as a real, empty
    answer). Logged once here, the one place the fetch itself is ever made,
    rather than once per play `_judged` goes on to mark not applicable for
    it - `hypothetical_skips` (below) is how `applicable_hint` and
    `arming._judge_round` read which plays that was, to surface it back to
    the caller rather than let it read as an ordinary non-applying script."""

    def __init__(self, seat: Seat) -> None:
        self._seat = seat
        self._fetched = False
        self._cards: dict[str, Any] | None = None
        self._unavailable: str | None = None

    def get(self) -> tuple[dict[str, Any] | None, str | None]:
        if not self._fetched:
            self._fetched = True
            try:
                self._cards = dict(self._seat.hypothetical_facts().get("cards") or {})
            except HypotheticalFactsUnavailable as exc:
                self._unavailable = str(exc)
                logger.warning(
                    "hypothetical facts unavailable for %s: %s", self._seat.uid, self._unavailable
                )
        return self._cards, self._unavailable


def hypothetical_skips(
    entries: list[dict[str, Any]] | dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """The ids among `entries` (an `applicable_hint`-style list of judged
    entries, or an `arming._judge_round`-style id -> entry dict) that
    `_judged` marked not applicable because the facts fetch `_FactsOnce`
    made for this same round failed (`failing_condition.list ==
    "hypothetical"`), paired with the reason - the HTTP status/reason
    `Seat.hypothetical_facts` raised on (D2). `[]` when nothing was skipped
    for that reason this round."""
    values = entries.values() if isinstance(entries, dict) else entries
    return [
        {"id": str(entry.get("id")), "reason": entry["failing_condition"]["seen"]}
        for entry in values
        if isinstance(entry, dict)
        and (entry.get("failing_condition") or {}).get("list") == "hypothetical"
    ]


def hypothetical_skip_note(skips: list[dict[str, str]]) -> str:
    """One short line for `applicable_hint`'s own `note`: every id
    `hypothetical_skips` found, and the reason - every skip in one round
    shares the same failed fetch, so the same reason for all of them."""
    ids = ", ".join(item["id"] for item in skips)
    return f"skipped for hypothetical facts unavailable ({skips[0]['reason']}): {ids}"


def _judged(
    doc: dict[str, Any],
    play: dict[str, Any] | runtime.ScriptExit,
    state: dict[str, Any],
    me: str,
    supplied: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    warnings: list[lint.PlaysWarning],
    *,
    facts: _FactsOnce | None = None,
    evaluate_conditions: bool = True,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": doc.get("id"),
        "name": doc.get("name"),
        "priority": doc.get("priority"),
        "applies": False,
        "failing_condition": None,
        # API-3 item 3: true for a play with no moves (`schema.Play`'s own
        # docstring calls this "advice-only") - independent of whether its
        # own conditions apply below, since the point of such a play is
        # exactly to say so via its `note`. `listing` itself keeps such an
        # entry out of `applicable`/`runtime_conflict` regardless of
        # `applies`, since there is nothing here a caller could run.
        "advice_only": isinstance(play, dict) and not play.get("moves"),
    }
    asserted = supplied.get(BUILTIN_INPUT)
    if isinstance(play, runtime.ScriptExit):
        entry["refusal"] = play.refusal
    elif play["priority"] == 0:
        entry["failing_condition"] = {"list": "priority", "index": None, "seen": 0}
    elif not runtime._archetype_applies(play, asserted):
        entry["failing_condition"] = {
            "list": "opposing_archetypes",
            "index": None,
            "seen": asserted,
        }
    elif not runtime._in_when(state, play["when"], me):
        entry["failing_condition"] = {
            "list": "when",
            "index": None,
            "seen": runtime._position(state),
        }
    elif not _first_move_allowed(play, state, me):
        # S2 review finding 4: a decision pending anywhere (CR 608.2d) freezes
        # every other action until it is answered - a play whose own first
        # move answers exactly that decision is still the one legal thing to
        # do, but any other play must be judged not-applying here rather than
        # fired and refused by the engine. `_judge_round` (`arming.py`) calls
        # this same function, so an armed script gets the identical gate.
        entry["failing_condition"] = {
            "list": "pending_decision",
            "index": None,
            "seen": (state.get("pending_decision") or {}).get("kind"),
        }
    elif not evaluate_conditions:
        # S2 review finding 7: `armable`'s own criteria (priority, archetype,
        # `when` recurrence, checked above) never need a condition's truth
        # value - only `applicable` does, and that block is always empty
        # when this seat does not hold priority (S1 finding 4a). Called
        # with this off exactly then, so a play whose condition needs
        # `include=contents` never pays that fetch merely to compute
        # something nothing here will read; `applies` stays `False`
        # (unknown, not "checked and failed" - `failing_condition` stays
        # `None` rather than naming a list never evaluated).
        pass
    elif facts is not None and conditions.needs_hypothetical(play) and facts.get()[1] is not None:
        # D2: this play's own selectors need the facts `facts` could not
        # read - marked not applicable, with why, rather than
        # `conditions.evaluate` raising `HypotheticalRequired` uncaught.
        entry["failing_condition"] = {"list": "hypothetical", "index": None, "seen": facts.get()[1]}
    else:
        hypothetical = (
            facts.get()[0] if facts is not None and conditions.needs_hypothetical(play) else None
        )
        result = conditions.evaluate(play, state, me, supplied, catalog, hypothetical=hypothetical)
        if result["invalid_input"] is not None:
            entry["invalid_input"] = result["invalid_input"]
        elif result["applies"]:
            entry["applies"] = True
        else:
            entry["failing_condition"] = {
                "list": result["failing_list"],
                "index": result["failing_index"],
                "seen": result["seen"],
            }
            if result["needs_input"] is not None:
                entry["needs_input"] = result["needs_input"]
    entry["conflicts"] = [warning.to_dict() for warning in warnings if entry["id"] in warning.plays]
    return entry


def _priority(entry: dict[str, Any]) -> int:
    priority = entry.get("priority")
    return priority if isinstance(priority, int) and not isinstance(priority, bool) else 0


def _declared(seat: Seat, corpora: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    corpus = runtime._corpus(seat, corpora, fresh=False)
    return [] if isinstance(corpus, runtime.ScriptExit) else list(corpus.get("inputs") or [])


def _registered_moves(seat: Seat, corpora: dict[tuple[str, str], dict[str, Any]]) -> dict[str, int]:
    corpus = corpora.get((seat.game_id(), seat.uid)) or {}
    return {
        str(doc.get("id")): len(doc.get("moves") or [])
        for doc in corpus.get("scripts") or []
        if isinstance(doc, dict)
    }


def _remember_run_inputs(
    seat: Seat, corpora: dict[tuple[str, str], dict[str, Any]], supplied: dict[str, Any]
) -> None:
    """Stash the archetype and the rest of `supplied` onto this seat's cached
    corpus, for `applicable_hint` to read back as "what this seat last told
    `run_script`" - `run`'s own equivalent of `listing`'s inline write of
    `_LAST_ASSERTED_KEY` (S1 review finding 4b)."""
    corpus = runtime._corpus(seat, corpora, fresh=False)
    if isinstance(corpus, runtime.ScriptExit):
        return
    corpus[_LAST_ASSERTED_KEY] = supplied.get(BUILTIN_INPUT)
    corpus[_LAST_INPUTS_KEY] = {
        name: value for name, value in supplied.items() if name != BUILTIN_INPUT
    }


def _turn_position(state: dict[str, Any]) -> Any:
    """The already-ran filter's own position reading: `turns_taken` when
    `state` carries it, `turn_number` otherwise (a state view built before
    S1 review fix 3, or a caller that trimmed the key). `turns_taken` counts
    every turn including extra ones (CR 500.7); `turn_number` counts rounds
    of the table and does not move for one, so keying on it wrongly
    suppressed a script reached again on an extra turn at the same step."""
    taken = state.get("turns_taken")
    return taken if taken is not None else state.get("turn_number")


def releases_slot(
    moves_done: int | None,
    exit_name: str | None,
    *,
    advise: bool,
    chained: bool,
    already_ran_releases: bool = False,
) -> bool:
    """Whether a run shaped like this touched nothing at all, and so must
    not occupy the once-per-turn-and-step slot both `_record_run` (the
    hint's own in-process cache, below) and `records._prior_script_run`
    (the ledger's) gate on - the two share every common decision here, so
    they cannot drift apart on those the way they once did (S2 arming
    re-review finding 4). `already_ran_releases` is the one decision they
    now deliberately make differently (S2 arming re-review finding 3,
    below) - every other parameter still means exactly the same thing to
    both callers.

    Released - "touched nothing" - when `moves_done == 0` and the exit is
    not `unknown_outcome` (whose move may have landed uncounted) and not a
    *successful* advise proposal (`exit == "advised"` with `advise=True`,
    which always reports zero moves by design - it only ever proposes, and
    still burns the slot so an armed advisor does not repeat the same
    suggestion within one turn and step).

    `moves_done=None` (S2 arming re-review finding 3) is never released,
    regardless of everything else: it means "unknown" - a raw ledger row
    whose own payload omitted the field entirely - not "confirmed zero",
    and treating the two the same let a malformed or incomplete finish row
    free a slot a real one might still be sitting on. `records.
    _prior_script_run` is the only caller that can ever see this: every
    `script_exit` this module's own `_record_run` reads always carries a
    real int (`script_exit`'s own `ALWAYS_WRITTEN` fields).

    `already_ran` - a collision finish reporting that some OTHER run
    already held this slot - is released for the LEDGER
    (`already_ran_releases=True`, `records._prior_script_run`'s own call)
    but never for the HINT's in-process cache (the default `False`,
    `_record_run`'s own call): once the run that row collided with is
    itself released (its own finish also touched nothing), nothing on the
    ledger still blocks a fresh start, and continuing to block on a row
    that only ever recorded "I collided" - never a genuine attempt - blocks
    on a symptom rather than any real work (S2 arming re-review finding 3:
    start A, start B collides -> finish B `already_ran`, finish A with zero
    moves -> a fresh start C must succeed, not collide with B's own
    collision all over again). The hint's cache exists for a narrower job -
    not re-suggesting, to THIS process, a script that already collided at
    this exact position - and keeps locking it regardless of what the
    ledger later does with other rows.

    `chained` (S2 arming re-review finding 3, an earlier round) overrides
    all of that to `False` regardless of `moves_done`: a chained run's own
    trailing `exit_block` carries only the LAST script in the chain's own
    `moves_done` (a chained child's own run starts counting from zero -
    `runtime.py`'s `_next`), so a parent that made a real move before
    handing off to a child that made none reports the combined run as
    zero-move even though real progress happened. `scripts_run` naming
    more than one script id is chaining's own unambiguous signal of that,
    independent of which segment's own count the trailing `moves_done`
    happens to carry."""
    if chained:
        return False
    if moves_done is None:
        return False
    if exit_name == runtime.Exit.ALREADY_RAN.value:
        return already_ran_releases
    return (
        moves_done == 0
        and exit_name is not None
        and exit_name != runtime.Exit.UNKNOWN_OUTCOME.value
        and not (advise and exit_name == runtime.Exit.ADVISED.value)
    )


def _record_run(
    seat: Seat,
    corpora: dict[tuple[str, str], dict[str, Any]],
    script_id: str,
    state: dict[str, Any],
    exit_block: dict[str, Any],
    *,
    advise: bool,
) -> None:
    """Record that this process ran `script_id` at `state`'s
    `(_turn_position(state), step)` - `applicable_hint` never re-offers it
    there again (S1 review finding 4c), UNLESS `releases_slot` says this run
    touched nothing - the same release rule `records._prior_script_run`
    already applies to the ledger's own already-ran slot, mirrored here for
    the hint's own in-process cache (S2 review finding 4, S2 arming
    re-review finding 4). A run blocked by something outside the script's
    own control (a pending decision, most often) takes exactly the released
    shape, so it must not hide the script once whatever blocked it clears -
    and a chained run whose trailing exit under-reports its own moves must
    not be released either (S2 arming re-review finding 3).

    `already_ran_releases=False`, explicitly, unlike the ledger's own call
    (S2 arming re-review finding 3): this cache exists only to stop this one
    process re-suggesting, to itself, a script that just collided at this
    exact position - it keeps that suggestion suppressed regardless of
    whether the ledger later frees the slot up once the run it collided
    with is itself released. See `releases_slot`'s own docstring.

    A chain locks every segment that ran, not only `script_id` - the
    originally-invoked parent (S2 arming re-review finding 4): a chain
    A->B->C that this process ran must not leave B and C still offered as
    `applicable`/`armable` (or still eligible for `arming._judge_round`'s
    own conflict check, which reads this same key) merely because
    `run_script` was only ever asked for A by name. `exit_block['scripts_run']`
    (`runtime.py`'s own `_Run.scripts_run`, threaded onto every chain hop)
    names every segment in order, A first; every id there EXCEPT THE LAST
    made real moves by construction - `_next` only ever chains a completed
    run into its next script once every one of ITS OWN moves has already
    sent, so a segment's mere presence ahead of the trailing one is its own
    proof of that. Only the trailing id's real move count is uncertain, and
    `moves_done` (the whole `exit_block`'s own - always the trailing
    segment's, per `releases_slot`'s own docstring) is exactly that count."""
    moves_done = exit_block.get("moves_done") or 0
    exit_name = exit_block.get("exit")
    scripts_run = list(exit_block.get("scripts_run") or [])
    chained = len(scripts_run) > 1
    if releases_slot(
        moves_done, exit_name, advise=advise, chained=chained, already_ran_releases=False
    ):
        return
    corpus = runtime._corpus(seat, corpora, fresh=False)
    if isinstance(corpus, runtime.ScriptExit):
        return
    ran_ids = [script_id]
    if chained:
        ran_ids += scripts_run[1:-1]
        if moves_done > 0:
            ran_ids.append(scripts_run[-1])
    recent = list(corpus.get(_RECENT_RUNS_KEY) or [])
    position = (_turn_position(state), step_position(state))
    for ran_id in ran_ids:
        entry = [ran_id, position[0], position[1]]
        if entry not in recent:
            recent.append(entry)
    corpus[_RECENT_RUNS_KEY] = recent


def _plays(
    corpus: dict[str, Any], docs: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], dict[str, Any] | runtime.ScriptExit]]:
    """Every doc in `docs`, paired with its normalised `Play` dump - computed
    once per corpus and cached under `_PLAYS_CACHE_KEY` on that same dict, so
    neither `listing` nor `applicable_hint` re-validates the same registered
    scripts on every call (S1 review finding 5). A `fresh=True` read replaces
    the corpus dict wholesale and carries forward only an explicit allow-list
    of seat bookkeeping (`runtime._CARRIED_BOOKKEEPING_KEYS`) - this cache
    key is deliberately not among them, so a refresh always drops it and this
    recomputes exactly when the corpus genuinely changed (S1 review fix 2)."""
    cached = corpus.get(_PLAYS_CACHE_KEY)
    if cached is None:
        cached = [(doc, runtime._normalised(doc)) for doc in docs]
        corpus[_PLAYS_CACHE_KEY] = cached
    return cached


def _hint_judged(
    doc: dict[str, Any],
    play: dict[str, Any] | runtime.ScriptExit,
    state: dict[str, Any],
    me: str,
    supplied: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    warnings: list[lint.PlaysWarning],
    *,
    facts: _FactsOnce | None = None,
    evaluate_conditions: bool = True,
) -> dict[str, Any]:
    """`_judged`, except a play whose conditions still need `include=contents`
    and which `state` was not fetched with is treated as not applying rather
    than raising `conditions.ContentsRequired` - `applicable_hint` calls this
    only for its second pass, over the one extra `include=contents` fetch it
    pays when a play's priority/archetype/when already matched (S1 review
    finding 1); this is the defensive fallback for the case even that fetch
    does not settle it."""
    try:
        return _judged(
            doc,
            play,
            state,
            me,
            supplied,
            catalog,
            warnings,
            facts=facts,
            evaluate_conditions=evaluate_conditions,
        )
    except conditions.ContentsRequired:
        return {
            "id": doc.get("id"),
            "name": doc.get("name"),
            "priority": doc.get("priority"),
            "applies": False,
            "failing_condition": None,
            "conflicts": [],
        }


def _post_offer(
    seat: Seat,
    corpus: dict[str, Any],
    state: dict[str, Any],
    entry: dict[str, Any],
    play: dict[str, Any],
) -> None:
    """Post one `phase: offer` row for `entry` through `seat`'s existing HTTP
    client (API-6) and fill `entry["offer_seq"]` from the response's own
    `log_seq` - unless this exact `(script_id, _turn_position(state), step)`
    was already offered by this process (`_OFFERED_KEY`, F4), in which case
    no second row is posted and `entry` gets that first offer's own
    `offer_seq` instead.

    Either way also (re)fills `_PENDING_OFFER_RUN_ID_KEY[script_id]` with
    this offer's own `run_id` (API-10) - the first offer's, on a cache hit,
    exactly like `offer_seq` - so a fresh `run` of this same script picks it
    up as that run's own `run_id` and `report.offer_table` can actually
    pair the two up.

    **Failure discipline**, deliberately the same as `mcp._attach_script_hint`
    (`mcp.py:684-690`): whatever this can raise - an HTTP error, a refusal
    (a game that has just ended answers this very route with one, same as
    any other post-game call, and is swallowed here like any other), a
    malformed response missing `log_seq` - must never cost the caller its
    answer. It is caught, logged once, and `entry` is handed back exactly as
    it already was: still in the answer, its own `why`/`call` untouched,
    simply without `offer_seq`. Never dropped."""
    script_id = str(entry.get("id"))
    position = (_turn_position(state), step_position(state))
    turn = position[0] if isinstance(position[0], int) else 0
    step = str(position[1]) if position[1] else "unknown"
    offered = corpus.get(_OFFERED_KEY)
    if not isinstance(offered, dict):
        offered = {}
        corpus[_OFFERED_KEY] = offered
    pending = corpus.get(_PENDING_OFFER_RUN_ID_KEY)
    if not isinstance(pending, dict):
        pending = {}
        corpus[_PENDING_OFFER_RUN_ID_KEY] = pending
    key = (script_id, position[0], position[1])
    cached = offered.get(key)
    if cached is not None:
        cached_log_seq, cached_run_id = cached
        entry["offer_seq"] = cached_log_seq
        pending[script_id] = cached_run_id
        return
    run_id = uuid.uuid4().hex
    try:
        answer = seat.call_json(
            "POST",
            f"/api/games/{seat.game_id()}/script-runs",
            {
                "phase": "offer",
                "run_id": run_id,
                "seat": seat.player,
                "role": "advisor" if seat.advise else "seat",
                "advise": seat.advise,
                "script_id": script_id,
                "priority": play.get("priority") if isinstance(play, dict) else None,
                "turn": turn,
                "step": step,
            },
        )
        log_seq = answer.get("log_seq") if isinstance(answer, dict) else None
        if not isinstance(log_seq, int):
            raise ValueError(f"offer post for {script_id!r} carried no log_seq: {answer!r}")
    except Exception:
        logger.warning(
            "posting the offer of %s for %s raised - entry sent without `offer_seq`",
            script_id,
            seat.uid,
            exc_info=True,
        )
        return
    offered[key] = (log_seq, run_id)
    entry["offer_seq"] = log_seq
    pending[script_id] = run_id


def _post_offers(
    seat: Seat,
    corpus: dict[str, Any],
    state: dict[str, Any],
    entries: list[dict[str, Any]],
    plays_by_id: dict[str, tuple[dict[str, Any], dict[str, Any] | runtime.ScriptExit]],
) -> None:
    """`_post_offer` for every one of `entries` (API-6) - `applicable_hint`'s
    own kept `applicable` and `advice` items, never `armable` (nothing is
    being offered to run there). Bounded to at most `entries`' own length,
    which `_hint_bounded` has already capped at `MAX_HINT_SCRIPTS` by the
    time this runs - so an answer never pays more than that many extra local
    round trips for this."""
    for entry in entries:
        _post_offer(seat, corpus, state, entry, _play_for(plays_by_id, entry.get("id")))


def applicable_hint(
    seat: Seat,
    corpora: dict[tuple[str, str], dict[str, Any]],
    state: dict[str, Any],
    *,
    cli: bool = False,
) -> dict[str, Any] | None:
    """`{"applicable": [...], "advice": [...], "armable": [...], "arm_call":
    "..."}` for one seat's wake/action answer - the same applicability
    judgement `listing` makes, over `state` this answer already carries
    rather than a fresh board fetch (plus, at most, one extra fetch for a
    contents-needing play whose position already matches - see below).
    `None` when this MCP process has scripts switched off
    (`scripts_enabled_here`), when this seat has no registered corpus, or
    when none of the blocks below has anything to say and no play was
    skipped for unavailable hypothetical facts either.

    **`note`**: a short string, present whenever a play's own selectors
    needed `Seat.hypothetical_facts` and the fetch failed
    (`hypothetical_skips`/D2) - present even when that is the only thing
    this answer has to say, so the caller learns a play went unjudged rather
    than reading a bare missing `scripts` key as "nothing applies".

    **`applicable`** (S1): what this seat holds priority for and could run
    right now (`runtime._drive` requires holding priority before it ever
    sends a script's first move - S1 review finding 4a, so this block is
    empty whenever `state.priority_holder != seat.player`). Judged with the
    archetype and other inputs this seat last supplied to `run_script`
    (`_LAST_ASSERTED_KEY`/`_LAST_INPUTS_KEY` - S1 review finding 4b), and
    never re-offering a script this process has already run at this same
    `(_turn_position(state), step)` (`_RECENT_RUNS_KEY` - finding 4c;
    `_turn_position` reads `turns_taken`, not `turn_number` - fix 3). An
    advice-only play (no moves) never joins this block, whatever its own
    condition says - it has nothing runnable to offer here; see `advice`.

    Each entry is `{id, name, why, call}` (API-4: `why` is the play's own
    `_hint_why` - its `note`, or its `intent` cut to a sentence boundary
    (`_truncated_intent`) when it has no `note`, or its bare `name` when it
    has neither), `call` a ready-to-use `run_script` invocation, `inputs`
    among its arguments only when the remembered inputs are non-empty - or,
    when two or more apply at once (`listing`'s own `runtime_conflict`),
    `{id, name, why, conflict: true}` with `call` left out, since a seat has
    to pick one itself before either can commit. `offer_seq` (API-6) joins
    every kept entry here once its own offer post lands - see below.

    **API-7**: whichever ids `state["armed_applying"]` names (`playscripts.
    arming.arm`'s own judge, spliced onto `state` before this hint is ever
    built) sort first here, ahead of every other applying entry regardless
    of priority, each carrying `armed: true` so a reader can tell why it
    jumped the queue - arming no longer fires anything itself; this is how
    the play it found applying reaches the seat, with its own `call` ready.

    **`advice`** (API-4): one entry per advice-only play (`schema.Play`'s
    own docstring: "exists to explain a decision rather than to act on
    one") that applies right now - judged under the exact same gate
    `applicable` is, above: this seat holding priority, its condition
    genuinely evaluated, and not re-offered at this same
    `(_turn_position(state), step)` (`_RECENT_RUNS_KEY`), so 'hold' advice
    arrives at the moment this seat is actually about to act rather than
    during a wait it is not acting in. Each entry is `{id, why}` only - no
    `call` (there is nothing to run) and no `name` (`why`, the play's own
    `note`/`intent`/`name` via `_hint_why`, is the whole content). Never
    counted toward `applicable`'s own conflict either, the same split
    `listing` already makes (API-3 item 3). `offer_seq` (API-6) joins this
    entry too, exactly as it does an `applicable` one.

    **`armable`** (S2): scripts with `priority > 0`, a matching archetype,
    that do NOT apply yet but whose `when` comes up again before this seat's
    own next turn ends - only offered while the board is in the opponent's
    turn or this seat's own upkeep/draw (`_armable_window`), the two windows
    a human is waiting through rather than acting in. Unlike `applicable`,
    this needs no priority check: it is meant for exactly the waits where
    this seat does not hold priority. Each entry is `{id, name, when}`,
    highest priority first, never re-checking anything `applicable` already
    covers - a script that already applies now is `applicable`, never
    `armable`. `arm_call` is one ready-to-use call arming every surviving
    `armable` id at once: `pass_until(..., arm=[...])` for a seat (advisors
    are refused `pass_until`), `wait_for_priority(..., arm=[...])` for an
    advisor.

    **Offer posting** (API-6, `_post_offer`/`_post_offers`): once
    `_hint_bounded` has decided which entries survive, every surviving
    `applicable` and `advice` entry - never `armable`, which offers nothing
    to run - is posted to the ledger as its own `phase: offer` row, through
    `seat`'s existing HTTP client (the same one `runtime`'s own `start`/
    `finish` rows already go over), so the reviewer can later pair up
    whether a play offered here was actually taken. The entry's own
    `offer_seq` is that row's `log_seq`, once posted.

    Deduped (F4) on `(script_id, _turn_position(state), step)`, an
    in-process cache next to `_RECENT_RUNS_KEY` under `_OFFERED_KEY`: a play
    already offered at this exact position is still rendered here, `why`
    included, but posts no second row and carries the first one's own
    `offer_seq` - this hint rides every wake AND every action answer, so
    without the dedupe one applying play would post a row per answer and
    the counts feeding the reviewer bars would be meaningless. A position
    that has genuinely moved on posts again.

    Failure discipline is deliberately the same `mcp._attach_script_hint`
    already applies around computing the hint itself: nothing an offer post
    can raise - an HTTP error, a refusal (a game that has just ended
    answers this very route with one, and it is swallowed here like any
    other) - ever costs the caller its answer. A failed post is logged once
    and its entry is returned exactly as it otherwise would be, simply
    without `offer_seq` - never dropped.

    Bounded to at most the kept entries - `_hint_bounded` has already
    capped `applicable` plus `advice` plus `armable` together at
    `MAX_HINT_SCRIPTS`, and `armable` posts nothing, so an answer never pays
    more than `MAX_HINT_SCRIPTS` extra local round trips for this, on the
    answer's own path, before it is sent. That is a real latency cost worth
    saying plainly (F5): up to five synchronous HTTP calls to this same
    process's own API, paid inside one wake/action answer, so that the
    accept/reject signal this offer exists to record costs the AGENT
    reading that answer nothing at all.

    An advisor posts its own offers as `role: "advisor"` for the human it
    watches, exactly as it already reports its runs (`runtime._run_fields`).

    All three blocks share `MAX_HINT_NAME_CHARS`'s truncation on `name`
    (`advice` carries no `name` to truncate), and together share
    `MAX_HINT_SCRIPTS`'s cap on entry count and `MAX_HINT_BYTES`'s cap on the
    whole block's own JSON size (`_hint_bounded`, finding 6) - kept in the
    order `applicable`, `advice`, `armable`; see that function's own
    docstring for why. Never `static_conflicts` here - its result is not
    read out of this answer's own items, only `listing`'s (finding 5).

    `cli`: when true, `call`/`arm_call` render as `mcp-seat` command lines
    (`seat_cli.py`'s own transport) instead of the MCP `run_script(...)`/
    `pass_until(...)`/`wait_for_priority(...)` call syntax - the same
    judgement, only the ready-to-use string differs. `mcp.py`'s own call
    sites never pass this, so they keep the MCP-syntax rendering exactly as
    before."""
    if not scripts_enabled_here():
        return None
    corpus = runtime._corpus(seat, corpora, fresh=False)
    if isinstance(corpus, runtime.ScriptExit):
        return None
    docs = [doc for doc in corpus.get("scripts") or [] if isinstance(doc, dict)]
    if not docs:
        return None
    holds_priority = seat_holds_priority(state, seat.player)
    armable_window = _armable_window(state, seat.player)
    if not holds_priority and not armable_window:
        return None
    asserted = corpus.get(_LAST_ASSERTED_KEY)
    asserted = asserted if isinstance(asserted, str) else WILDCARD
    remembered_raw = corpus.get(_LAST_INPUTS_KEY)
    remembered = dict(remembered_raw) if isinstance(remembered_raw, dict) else {}
    supplied = merged_inputs(asserted, remembered)
    if isinstance(supplied, str):
        return None
    declared = list(corpus.get("inputs") or [])
    catalog = input_catalog(declared, [asserted])
    plays = _plays(corpus, docs)
    plays_by_id = {doc["id"]: (doc, play) for doc, play in plays if isinstance(doc.get("id"), str)}
    entries: list[dict[str, Any]] = []
    deferred: list[tuple[dict[str, Any], dict[str, Any] | runtime.ScriptExit]] = []
    facts = _FactsOnce(seat)
    for doc, play in plays:
        try:
            entries.append(
                _judged(
                    doc,
                    play,
                    state,
                    seat.player,
                    supplied,
                    catalog,
                    [],
                    facts=facts,
                    evaluate_conditions=holds_priority,
                )
            )
        except conditions.ContentsRequired:
            deferred.append((doc, play))
    if deferred:
        # S1 review finding 1: a play whose priority/archetype/when already
        # match `state` but whose conditions read the viewer's own library or
        # sideboard contents (`conditions.needs_contents`) is worth exactly
        # one extra `include=contents` fetch - paid once here, for every such
        # play at once, rather than the old blanket "not applicable". Never
        # reached at all when `not holds_priority` (S2 review finding 7):
        # with conditions never evaluated above, `_judged` never raises
        # `ContentsRequired` in the first place - `armable`'s own criteria
        # never needed the fetch this would have paid for.
        contents_state = seat.fetch_state(include_contents=True)
        entries.extend(
            _hint_judged(
                doc,
                play,
                contents_state,
                seat.player,
                supplied,
                catalog,
                [],
                facts=facts,
                evaluate_conditions=holds_priority,
            )
            for doc, play in deferred
        )
    entries.sort(key=lambda entry: (-_priority(entry), str(entry["id"])))

    applicable_items: list[dict[str, Any]] = []
    advice_items: list[dict[str, Any]] = []
    # API-7: `wait_and_arm`/`pass_until`'s own arming judge (`playscripts.
    # arming.arm`) writes the ids it found currently applying onto this very
    # `state` before this hint is ever built (`seatkit._merge_armed`, the
    # `_pass_until_once` armed-judge hook) - never a second wiring path
    # threaded through this function's own arguments. Read once here so
    # every one of them sorts first below, regardless of its own priority,
    # and carries `armed: true` so a reader can tell why.
    armed_ids = {str(script_id) for script_id in (state.get("armed_applying") or []) if script_id}
    if holds_priority:
        recent = {
            (str(run[0]), run[1], run[2])
            for run in corpus.get(_RECENT_RUNS_KEY) or []
            if isinstance(run, list) and len(run) == 3
        }
        position = (_turn_position(state), step_position(state))
        not_recently_run = [
            entry
            for entry in entries
            if entry["applies"] and (str(entry["id"]), *position) not in recent
        ]
        # API-4: an advice-only entry (no moves) never joins `applicable` -
        # the same split `listing` already makes (API-3 item 3) - it has its
        # own `advice` sibling block below, judged under this identical
        # gate (this seat holding priority, right now, over the same
        # `_RECENT_RUNS_KEY`-filtered board).
        applying = [entry for entry in not_recently_run if not entry["advice_only"]]
        if armed_ids:
            # A stable sort: `applying` already carries the priority order
            # `entries.sort` gave it, and moving the armed ones to the front
            # must never reshuffle anyone else relative to one another.
            applying = sorted(applying, key=lambda entry: str(entry["id"]) not in armed_ids)
        # S2 A3 re-review finding 2: `conflict` used to be "more than one
        # play applies at all", which stripped `call` from an armed play the
        # instant any unarmed play also applied - even though the seat
        # already resolved that ambiguity by arming it specifically (Req 3:
        # an armed applying play keeps its `call` ready regardless of what
        # else applies alongside it). An armed entry's own conflict is now
        # judged only against `armed_conflict` - two or more ARMED plays
        # applying at once - which in practice `playscript_arming.arm`
        # already resolves before `armed_ids` ever reaches this function (a
        # real conflict there empties `armed_applying` and reports
        # `armed_conflict` instead), so this is defensive rather than a path
        # normally taken. Every other (unarmed) entry keeps the old
        # behaviour unchanged: `conflict` over the full `applying` count,
        # armed or not - an unarmed alternative alongside an armed play is
        # still not the play the seat chose, so it still carries no `call`.
        armed_applying_count = sum(1 for entry in applying if str(entry["id"]) in armed_ids)
        armed_conflict = armed_applying_count > 1
        conflict = len(applying) > 1
        for entry in applying:
            item: dict[str, Any] = {
                "id": entry["id"],
                "name": _truncated_name(entry.get("name")),
                "why": _hint_why(_play_for(plays_by_id, entry["id"])),
            }
            entry_armed = str(entry["id"]) in armed_ids
            if entry_armed:
                item["armed"] = True
            entry_conflict = armed_conflict if entry_armed else conflict
            if entry_conflict:
                item["conflict"] = True
            else:
                item["call"] = _call_template(seat, str(entry["id"]), asserted, remembered, cli=cli)
            applicable_items.append(item)

        for entry in not_recently_run:
            if not entry["advice_only"]:
                continue
            advice_items.append(
                {"id": entry["id"], "why": _hint_why(_play_for(plays_by_id, entry["id"]))}
            )

    armable_items: list[dict[str, Any]] = []
    occurrences: dict[str, tuple[int, str, str]] = {}
    if armable_window:
        for entry in entries:
            script_id = str(entry["id"])
            if entry["applies"]:
                continue
            found = plays_by_id.get(script_id)
            if found is None:
                continue
            _, play = found
            if not isinstance(play, dict) or play.get("priority", 0) <= 0:
                continue
            if not runtime._archetype_applies(play, asserted):
                continue
            when = play.get("when")
            if not isinstance(when, dict) or not _when_recurs_before_next_own_turn_ends(
                state, when, seat.player
            ):
                continue
            occurrence = _next_occurrence(state, when, seat.player)
            if occurrence is None:
                # `_when_recurs_before_next_own_turn_ends` just confirmed this
                # `when` comes up again before this seat's own next turn
                # ends - `_next_occurrence` computes the same thing in more
                # detail, so the two must agree; defensive only.
                continue
            armable_items.append(
                {"id": entry["id"], "name": _truncated_name(entry.get("name")), "when": when}
            )
            occurrences[script_id] = occurrence

    hyp_skips = hypothetical_skips(entries)
    if not applicable_items and not advice_items and not armable_items and not hyp_skips:
        return None

    kept_applicable, kept_advice, kept_armable = _hint_bounded(
        applicable_items, advice_items, armable_items
    )
    if not kept_applicable and not kept_advice and not kept_armable and not hyp_skips:
        return None
    # API-6: post the offer, once per surviving `applicable`/`advice` entry
    # (never `armable` - nothing there is being offered to run), before this
    # answer is handed back. `_post_offer`'s own failure discipline means
    # this never costs the caller its answer even when every post fails.
    _post_offers(seat, corpus, state, kept_applicable + kept_advice, plays_by_id)
    result: dict[str, Any] = {}
    if kept_applicable:
        result["applicable"] = kept_applicable
    if kept_advice:
        result["advice"] = kept_advice
    if kept_armable:
        result["armable"] = kept_armable
        result["arm_call"] = _arm_call_template(
            seat, kept_armable, occurrences, asserted, remembered, cli=cli
        )
    if hyp_skips:
        result["note"] = hypothetical_skip_note(hyp_skips)
    return result


#: The two windows an `armable` entry is ever offered in - the opponent's
#: turn, or this seat's own upkeep/draw. `applicable` is what a human acts on
#: right now; `armable` is what is worth arming for the wait ahead of it.
_ARMABLE_OWN_STEPS = ("upkeep", "draw")


def _armable_window(state: dict[str, Any], me: str) -> bool:
    active = state.get("active_player")
    if active is None:
        return False
    if active != me:
        return True
    return state.get("step") in _ARMABLE_OWN_STEPS


def _step_order_key(current_step: Any) -> int:
    """Where `current_step` sits for comparing against `runtime.
    AUTOPASS_STEPS` order - `untap` (or any other step this list does not
    name, defensively) sorts before every one of them, since the turn has
    not reached any of them yet. `cleanup` is the one exception: CR 514 puts
    it after every step this list names, so it has to sort AFTER all of
    them, never before (S2 arming re-review finding 2) - treating it as
    "before everything" made a `when` read from the opponent's own cleanup
    come back with a step from earlier THIS turn as if it were still ahead,
    when the turn is already over and nothing in it is."""
    if current_step == "cleanup":
        return len(runtime.AUTOPASS_STEPS)
    if current_step not in runtime.AUTOPASS_STEPS:
        return -1
    return runtime.AUTOPASS_STEPS.index(current_step)


def _has_step_strictly_after(steps: list[str], current_step: Any) -> bool:
    """Whether any of `steps` is still strictly ahead of `current_step` this
    same turn - never `current_step` itself (S2 arming re-review finding 2):
    naming the step a run is already sitting in as its own destination means
    the NEXT occurrence of it, never this one (`start_autopass`'s own
    starting-occurrence exemption), so a `when` that only ever "recurs" on
    the step already reached has nothing left this turn either."""
    here = _step_order_key(current_step)
    return any(
        runtime.AUTOPASS_STEPS.index(step) > here
        for step in steps
        if step in runtime.AUTOPASS_STEPS
    )


def _when_recurs_before_next_own_turn_ends(
    state: dict[str, Any], when: dict[str, Any], me: str
) -> bool:
    """Whether `when` comes up again before this seat's own next turn ends,
    from `state`'s current position - called only from within
    `_armable_window`'s own two cases, below."""
    whose = when.get("whose_turn")
    steps = when.get("steps") or []
    active = state.get("active_player")
    if active != me:
        # Somewhere in the opponent's turn (or a step naming no active
        # player - defensive): `when` recurs before this seat's own next
        # turn ends when either it can still happen later this same
        # (opponent's) turn - only for `theirs`/`either`, and only if one of
        # its steps has not already gone by - or it can happen at all during
        # this seat's own following turn, which `mine`/`either` always does.
        if whose in ("theirs", "either") and _has_step_strictly_after(steps, step_position(state)):
            return True
        return whose in ("mine", "either")
    # Already in this seat's own upkeep/draw: the window is the rest of THIS
    # turn. `theirs` alone can never come up before this own turn ends - that
    # would cross into the opponent's next turn.
    if whose not in ("mine", "either"):
        return False
    return _has_step_strictly_after(steps, step_position(state))


def _earliest_step(steps: list[str]) -> str:
    ordered = [step for step in steps if step in runtime.AUTOPASS_STEPS]
    if not ordered:
        return str(steps[0]) if steps else ""
    return min(ordered, key=runtime.AUTOPASS_STEPS.index)


def _next_step_strictly_after(steps: list[str], current_step: Any) -> str | None:
    """The earliest of `steps` still strictly ahead of `current_step`, in
    `runtime.AUTOPASS_STEPS` turn order - `None` when every one of them has
    already gone by this occurrence, `current_step` itself included (S2
    arming re-review finding 2): a `pass_until` call templated from this
    would be told to stop at `current_step`, which `start_autopass`'s own
    starting-occurrence exemption reads as the NEXT occurrence of it, never
    this one - so a step this run is already sitting in is never a valid
    "still ahead this turn" answer, the same as one already behind it.
    `current_step` outside `AUTOPASS_STEPS` is ordered by `_step_order_key`
    - `untap` (or anything unrecognised) before everything, `cleanup` after
    everything."""
    ordered = [step for step in steps if step in runtime.AUTOPASS_STEPS]
    if not ordered:
        return None
    here = _step_order_key(current_step)
    candidates = [step for step in ordered if runtime.AUTOPASS_STEPS.index(step) > here]
    return min(candidates, key=runtime.AUTOPASS_STEPS.index) if candidates else None


def _next_occurrence(
    state: dict[str, Any], when: dict[str, Any], me: str
) -> tuple[int, str, str] | None:
    """The nearest actual occurrence of `when`, from `state`'s current board
    position, as `(phase, step, whose_turn)` - `step`/`whose_turn` already in
    `pass_until`'s own vocabulary (`runtime.WHOSE_TURN_FOR_AUTOPASS`), `phase`
    0 for a step still ahead in the turn already under way (whoever's turn
    that is), 1 for one that only comes up in this seat's own following turn.

    Two entries' occurrences are directly comparable: every phase-0 window
    shares the one turn now in progress, so their `runtime.AUTOPASS_STEPS`
    index alone orders them; every phase-1 window shares the same following
    own turn, likewise; and phase 0 is always sooner than phase 1 by
    construction. `_arm_call_template` uses exactly that to find the single
    nearest window among several kept `armable` entries (S2 arming re-review
    finding 1) - the old template instead took the earliest step named
    anywhere among them, in bare turn-order, regardless of whether the board
    had already passed it *this* occurrence (a `theirs` `when` naming
    `[upkeep, end_step]`, read from the opponent's own main phase - upkeep
    already behind it this turn - used to template a wait for the opponent's
    NEXT upkeep, skipping this very turn's end step).

    `None` only when `when` does not actually recur before this seat's own
    next turn ends - every caller has already confirmed that with
    `_when_recurs_before_next_own_turn_ends`, so this mirrors its own two
    cases in more detail rather than duplicating its own gating."""
    whose = when.get("whose_turn") or "mine"
    steps = when.get("steps") or []
    target = runtime.WHOSE_TURN_FOR_AUTOPASS.get(whose, "mine")
    active = state.get("active_player")
    if active != me:
        # Somewhere in the opponent's turn: a theirs/either step still ahead
        # of the current one this same turn is phase 0 and comes first;
        # otherwise (mine alone, or theirs/either with every one of its
        # steps already behind it this turn) the next occurrence is this
        # seat's own following turn, phase 1, at the earliest of `steps` in
        # turn order (that turn has not started yet, so nothing is "already
        # behind" in it).
        if whose in ("theirs", "either"):
            step = _next_step_strictly_after(steps, step_position(state))
            if step is not None:
                return 0, step, target
        if whose in ("mine", "either"):
            return 1, _earliest_step(steps), target
        return None
    # Already in this seat's own upkeep/draw: only a step still ahead this
    # same turn ever qualifies (theirs alone never does - see
    # `_when_recurs_before_next_own_turn_ends`), so this is always phase 0.
    if whose not in ("mine", "either"):
        return None
    step = _next_step_strictly_after(steps, step_position(state))
    return (0, step, target) if step is not None else None


def _hint_bounded(
    applicable_items: list[dict[str, Any]],
    advice_items: list[dict[str, Any]],
    armable_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """`applicable_items`, `advice_items` and `armable_items` (API-4),
    combined under one shared `MAX_HINT_SCRIPTS` entry cap and
    `MAX_HINT_BYTES` byte budget - the same two caps `applicable`/`armable`
    already shared, now stretched over all three blocks together rather than
    given a third helping.

    Kept in this order: `applicable` first, then `advice`, then `armable` -
    deliberately, not alphabetically and not by priority across blocks.
    `applicable` is what this seat could run right now, so an advice-only
    play - nothing here is ever runnable - must never displace a runnable
    one out of the cap; that ordering is exactly what would turn the hint
    into telling this seat what to think about instead of what it can do.
    `advice` still outranks `armable`, though: an advice-only play that
    applies is judged under the very same gate `applicable` is (this seat
    holding priority, right now, over the same `_RECENT_RUNS_KEY`-filtered
    board), so it is live for the moment already reached - while `armable`
    is only ever worth arming for a wait still ahead of this seat, not the
    moment it is already in."""
    combined = [("applicable", item) for item in applicable_items]
    combined += [("advice", item) for item in advice_items]
    combined += [("armable", item) for item in armable_items]
    combined = combined[:MAX_HINT_SCRIPTS]
    kept: list[tuple[str, dict[str, Any]]] = []
    for kind, item in combined:
        candidate = [*kept, (kind, item)]
        # Measured the same way `_byte_bounded` always has - the flat list
        # of surviving items' own JSON size, not the three-key object they
        # end up wrapped in: all three blocks share one budget over what
        # they themselves cost, not a second helping for the wrapper.
        if len(json.dumps([entry for _, entry in candidate]).encode("utf-8")) > MAX_HINT_BYTES:
            break
        kept = candidate
    kept_applicable = [item for kind, item in kept if kind == "applicable"]
    kept_advice = [item for kind, item in kept if kind == "advice"]
    kept_armable = [item for kind, item in kept if kind == "armable"]
    return kept_applicable, kept_advice, kept_armable


def _cli_quoted(text: str) -> str:
    """`text` wrapped in double quotes for a `mcp-seat` command line - the
    reasoning placeholder itself carries no quote of its own to escape, and
    this is a hint string for an LLM to read and imitate, not something
    literally `subprocess`-executed, so nothing more elaborate than a plain
    wrap is worth doing here."""
    return f'"{text}"'


def _cli_input_value(value: Any) -> str:
    """One `--input NAME=VALUE` value, in `parse_inputs`'s own vocabulary
    (`seat_cli.py`'s `--input` help text): a string reads back as text
    unchanged, everything else is rendered as the JSON `parse_inputs` would
    read it back from."""
    return value if isinstance(value, str) else json.dumps(value)


def _cli_input_flags(inputs: dict[str, Any] | None) -> list[str]:
    """`--input NAME=VALUE` for each of `inputs`, one flag per input,
    repeated the way `seat_cli.py`'s `--input` is documented to repeat -
    empty when `inputs` is empty or `None`."""
    flags: list[str] = []
    for name, value in (inputs or {}).items():
        flags += ["--input", f"{name}={_cli_input_value(value)}"]
    return flags


def _arm_call_template(
    seat: Seat,
    kept_armable: list[dict[str, Any]],
    occurrences: dict[str, tuple[int, str, str]],
    asserted: str,
    remembered: dict[str, Any] | None = None,
    *,
    cli: bool = False,
) -> str:
    """A ready-to-use call arming the subset of `kept_armable` that shares
    the single nearest occurrence among them - `pass_until(...)` for a seat,
    `wait_for_priority(...)` for an advisor (advisors are refused
    `pass_until`).

    `kept_armable` is already priority-ordered (`applicable_hint` sorts
    `entries` highest priority first before building it, and `_hint_bounded`
    preserves that order); `occurrences` is each one's own `_next_occurrence`
    reading. `pass_until`'s own destination is one `step`/`whose_turn` pair,
    so a batch of several armed ids built as one call has to be templated
    from ONE window - the old version instead took `step` as the earliest
    step named anywhere among the kept entries and `whose_turn` from the
    top-priority entry alone, a combination that routinely matched neither
    script's own `when` (S2 arming re-review finding 1). The fix: rank the
    kept entries by their own nearest occurrence (`_next_occurrence`'s
    `(phase, step)` - ties broken by priority, since `kept_armable` is
    already in that order), take the single nearest one as the window, and
    arm every entry whose own next occurrence shares that same `(phase,
    step)` - never the stricter `(step, whose_turn)` triple `occurrences`
    itself carries (S2 arming re-review finding 2): `whose_turn` there is
    each entry's own `when.whose_turn` mapped through `runtime.
    WHOSE_TURN_FOR_AUTOPASS` (`theirs`->`opponent`, `mine`->`mine`,
    `either`->`any`), so a `theirs` and an `either` entry landing on the
    exact same step of the exact same occurrence - the opponent's one
    upcoming end step, say - compared `"opponent" != "any"` and only the
    first ever made it into `arm`, leaving the second armed for nothing this
    window even though its own `when` matches the board `pass_until` was
    about to reach. `(phase, step)` alone is exactly the occurrence itself:
    within one call, every phase-0 entry already shares the one turn/side
    now in progress (`_next_occurrence` only ever returns phase 0 for
    `theirs`/`either` during the opponent's turn, or `mine`/`either` during
    this seat's own upkeep/draw - never both at once), so nothing outside
    `window`'s own occurrence can share its `(phase, step)` by accident. The
    rest stay listed under `armable` without being in this call; a later
    `arm()` call, once the board has moved past this window, judges them
    against their own next one instead.

    `remembered` is this seat's own non-archetype inputs last supplied to
    `run_script` (S1 finding 4b) - named here too, exactly as `_call_template`
    already names them for `applicable`, so an armed script whose condition
    reads one of them does not come back `needs_input` merely because the
    template left it out.

    `cli`: render the equivalent `mcp-seat pass-until`/`mcp-seat wait`
    command line instead of the MCP `pass_until(...)`/`wait_for_priority(...)`
    call syntax - same window, same armed ids, same arguments, only the
    surface differs."""

    def rank(item: dict[str, Any]) -> tuple[int, int]:
        phase, step, _ = occurrences[str(item["id"])]
        index = runtime.AUTOPASS_STEPS.index(step) if step in runtime.AUTOPASS_STEPS else -1
        return phase, index

    ranked = sorted(kept_armable, key=rank)
    top_rank = rank(ranked[0])
    window = occurrences[str(ranked[0]["id"])][1:]
    armed_ids = [str(item["id"]) for item in kept_armable if rank(item) == top_rank]
    if cli:
        if seat.advise:
            parts = ["mcp-seat", "wait"]
        else:
            step, whose_turn = window
            parts = ["mcp-seat", "pass-until", step, "--whose-turn", whose_turn]
        for script_id in armed_ids:
            parts += ["--arm", script_id]
        parts += ["--arm-reasoning", _cli_quoted(runtime.REASONING_PLACEHOLDER)]
        parts += ["--opposing-archetype", asserted]
        if not seat.advise:
            # Same reasoning as the MCP branch's own `max_seconds` below -
            # `pass-until` needs a bounded wait to have any real chance of
            # reaching this seat's own priority at the destination before
            # giving up.
            parts += ["--max-seconds", str(MAX_WAIT_SECONDS)]
        parts += _cli_input_flags(remembered)
        return " ".join(parts)
    args: dict[str, Any] = {"player_uid": seat.uid}
    if not seat.advise:
        args["step"], args["whose_turn"] = window
    args["arm"] = armed_ids
    args["arm_reasoning"] = runtime.REASONING_PLACEHOLDER
    args["opposing_archetype"] = asserted
    if not seat.advise:
        # S2 arming re-review finding 1: a `theirs`/`either` destination is
        # usually still the active opponent's own first priority in it (CR
        # 117.3a), so `pass_until` needs its own bounded wait
        # (`max_seconds`) to have any real chance of reaching THIS seat's
        # own priority there before giving up - left at the tool's own
        # default (`0.0`), `_wait_for_own_priority_at_destination` never
        # even runs once and this call would give straight back up on the
        # very first "reached" it sees. `seatkit.MAX_WAIT_SECONDS` is the
        # same ceiling every other bounded wait in this codebase is already
        # capped to. Never for an advisor's `wait_for_priority`: that is a
        # single check-and-return, not a `pass_until`-style multi-call run,
        # so it has no matching wait to bound.
        args["max_seconds"] = MAX_WAIT_SECONDS
    if remembered:
        args["inputs"] = remembered
    rendered = ", ".join(f"{name}={json.dumps(value)}" for name, value in args.items())
    verb = "wait_for_priority" if seat.advise else "pass_until"
    return f"{verb}({rendered})"


def _truncated_name(name: Any) -> Any:
    """`name`, cut to `MAX_HINT_NAME_CHARS` - a registered corpus is the
    deck's own, and nothing else bounds how long a play's `name` is before
    this (S1 review finding 6)."""
    if isinstance(name, str) and len(name) > MAX_HINT_NAME_CHARS:
        return name[: MAX_HINT_NAME_CHARS - 3] + "..."
    return name


#: The sentence-ending punctuation `_truncated_intent` cuts on - `.`, `!`,
#: `?` - and the marker it appends when no such cut is possible and it falls
#: back to a bare word boundary instead, so a reader never mistakes the cut
#: for the play's own words trailing off there.
_SENTENCE_ENDINGS = ".!?"
_TRUNCATION_MARKER = "..."


def _truncated_intent(intent: str, limit: int = MAX_NOTE_LENGTH) -> str:
    """`intent` cut to at most `limit` characters, for a hint's own `why`
    (API-4/F13) when the play carries no `note` of its own - `intent`
    carries no length bound of its own (`schema.Note`'s own docstring, next
    to `schema.NonBlank`), unlike `note`.

    Returned whole when it already fits. Otherwise cut at the LAST
    sentence-ending punctuation (`_SENTENCE_ENDINGS`) at or before `limit`:
    `intent` is prose written for a human to read, and a whole sentence
    reads better than an arbitrary slice. When no sentence ends that early -
    the very first sentence already runs past `limit`, or `intent` carries
    no sentence punctuation anywhere - cut at the last word boundary at or
    before `limit` instead and mark it with `_TRUNCATION_MARKER`, never
    mid-word and never a bare slice with nothing to say the words kept on
    going past it."""
    if len(intent) <= limit:
        return intent
    window = intent[:limit]
    sentence_end = max((window.rfind(mark) for mark in _SENTENCE_ENDINGS), default=-1)
    if sentence_end != -1:
        return intent[: sentence_end + 1]
    space = window.rfind(" ")
    cut = window[:space] if space > 0 else window
    return f"{cut.rstrip()} {_TRUNCATION_MARKER}"


def _play_for(
    plays_by_id: dict[str, tuple[dict[str, Any], dict[str, Any] | runtime.ScriptExit]],
    script_id: Any,
) -> dict[str, Any]:
    """The normalised `Play` dump for `script_id` among `applicable_hint`'s
    own `plays_by_id` - `{}` when it is not registered under that id, or
    when normalising it refused (a `runtime.ScriptExit`, never reached by an
    entry that got as far as `applies=True` in practice, but `_hint_why`
    reads only `.get(...)`, so an empty dict is exactly as safe here as the
    real thing would be)."""
    found = plays_by_id.get(str(script_id))
    if found is None:
        return {}
    _, play = found
    return play if isinstance(play, dict) else {}


def _hint_why(play: dict[str, Any]) -> Any:
    """A hint entry's own `why` (API-4): `play`'s `note` - written for
    exactly this reader - when it has one; otherwise `play`'s `intent`, cut
    to a sentence boundary within `MAX_NOTE_LENGTH` characters
    (`_truncated_intent`, since `intent` carries no length bound of its
    own); and when `play` has neither, its bare `name` (F13)."""
    note = play.get("note")
    if isinstance(note, str) and note:
        return note
    intent = play.get("intent")
    if isinstance(intent, str) and intent:
        return _truncated_intent(intent)
    return play.get("name")


def _byte_bounded(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`items`, in order, up to `MAX_HINT_BYTES` of JSON - an entry that
    would push the running total past the budget is dropped, along with
    every entry after it (S1 review finding 6)."""
    kept: list[dict[str, Any]] = []
    for item in items:
        candidate = [*kept, item]
        if len(json.dumps(candidate).encode("utf-8")) > MAX_HINT_BYTES:
            break
        kept = candidate
    return kept


def _call_template(
    seat: Seat,
    script_id: str,
    asserted: str,
    inputs: dict[str, Any] | None = None,
    *,
    cli: bool = False,
) -> str:
    """A ready-to-use `run_script(...)` invocation string for `script_id` -
    the exact argument names `mcp.run_script` accepts, with `reasoning` left
    for the seat to fill in (every run needs its own, in the seat's own
    words), `opposing_archetype` set to what this hint judged the script
    against, and `inputs` named only when the seat's remembered inputs are
    non-empty (S1 review finding 4b).

    `cli`: render the equivalent `mcp-seat script run` command line instead -
    same script id, same reasoning placeholder, same archetype, same
    inputs, only the surface differs."""
    if cli:
        parts = ["mcp-seat", "script", "run", script_id]
        parts += ["--reasoning", _cli_quoted(runtime.REASONING_PLACEHOLDER)]
        parts += ["--opposing-archetype", asserted]
        parts += _cli_input_flags(inputs)
        return " ".join(parts)
    args: dict[str, Any] = {
        "player_uid": seat.uid,
        "script": script_id,
        "reasoning": runtime.REASONING_PLACEHOLDER,
        "opposing_archetype": asserted,
    }
    if inputs:
        args["inputs"] = inputs
    rendered = ", ".join(f"{name}={json.dumps(value)}" for name, value in args.items())
    return f"run_script({rendered})"
