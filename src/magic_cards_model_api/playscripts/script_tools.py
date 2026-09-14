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
from collections.abc import Callable
from typing import Any

from ..seatkit import Seat, scripts_enabled_here
from . import conditions, lint, runtime
from .inputs import BUILTIN_INPUT, input_catalog
from .schema import WILDCARD

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
#: budget are dropped rather than counted (S1 review finding 6).
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
    sends "cli", `playscripts.arming.arm` sends "armed"."""
    supplied = merged_inputs(opposing_archetype, inputs)
    if isinstance(supplied, str):
        return supplied
    _remember_run_inputs(seat, corpora, supplied)
    token = parse_resume(resume)
    if isinstance(token, str):
        return token
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
    )
    state = result.state if result.state is not None else seat.fetch_state()
    _record_run(seat, corpora, script, state)
    return {"script_exit": script_exit(result, _registered_moves(seat, corpora)), "state": state}


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
    warnings = static_conflicts(docs, declared, str(corpus.get("deck_slug") or ""))
    entries = [
        _judged(doc, play, state, seat.player, supplied, catalog, warnings) for doc, play in plays
    ]
    entries.sort(key=lambda entry: (-_priority(entry), str(entry["id"])))
    applicable = [entry["id"] for entry in entries if entry["applies"]]
    ambiguous = len(applicable) > 1
    for entry in entries:
        entry["runtime_conflict"] = ambiguous and entry["applies"]
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


def _judged(
    doc: dict[str, Any],
    play: dict[str, Any] | runtime.ScriptExit,
    state: dict[str, Any],
    me: str,
    supplied: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    warnings: list[lint.PlaysWarning],
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": doc.get("id"),
        "name": doc.get("name"),
        "priority": doc.get("priority"),
        "applies": False,
        "failing_condition": None,
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
    else:
        result = conditions.evaluate(play, state, me, supplied, catalog)
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


def _record_run(
    seat: Seat,
    corpora: dict[tuple[str, str], dict[str, Any]],
    script_id: str,
    state: dict[str, Any],
) -> None:
    """Record that this process ran `script_id` at `state`'s
    `(_turn_position(state), step)` - `applicable_hint` never re-offers it
    there again, however it exited (S1 review finding 4c)."""
    corpus = runtime._corpus(seat, corpora, fresh=False)
    if isinstance(corpus, runtime.ScriptExit):
        return
    recent = list(corpus.get(_RECENT_RUNS_KEY) or [])
    entry = [script_id, _turn_position(state), state.get("step")]
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
) -> dict[str, Any]:
    """`_judged`, except a play whose conditions still need `include=contents`
    and which `state` was not fetched with is treated as not applying rather
    than raising `conditions.ContentsRequired` - `applicable_hint` calls this
    only for its second pass, over the one extra `include=contents` fetch it
    pays when a play's priority/archetype/when already matched (S1 review
    finding 1); this is the defensive fallback for the case even that fetch
    does not settle it."""
    try:
        return _judged(doc, play, state, me, supplied, catalog, warnings)
    except conditions.ContentsRequired:
        return {
            "id": doc.get("id"),
            "name": doc.get("name"),
            "priority": doc.get("priority"),
            "applies": False,
            "failing_condition": None,
            "conflicts": [],
        }


def applicable_hint(
    seat: Seat, corpora: dict[tuple[str, str], dict[str, Any]], state: dict[str, Any]
) -> dict[str, Any] | None:
    """`{"applicable": [...], "armable": [...], "arm_call": "..."}` for one
    seat's wake/action answer - the same applicability judgement `listing`
    makes, over `state` this answer already carries rather than a fresh board
    fetch (plus, at most, one extra fetch for a contents-needing play whose
    position already matches - see below). `None` when this MCP process has
    scripts switched off (`scripts_enabled_here`), when this seat has no
    registered corpus, or when neither block below has anything to say.

    **`applicable`** (S1): what this seat holds priority for and could run
    right now (`runtime._drive` requires holding priority before it ever
    sends a script's first move - S1 review finding 4a, so this block is
    empty whenever `state.priority_holder != seat.player`). Judged with the
    archetype and other inputs this seat last supplied to `run_script`
    (`_LAST_ASSERTED_KEY`/`_LAST_INPUTS_KEY` - S1 review finding 4b), and
    never re-offering a script this process has already run at this same
    `(_turn_position(state), step)` (`_RECENT_RUNS_KEY` - finding 4c;
    `_turn_position` reads `turns_taken`, not `turn_number` - fix 3).

    Each entry is `{id, name, call}`, `call` a ready-to-use `run_script`
    invocation, `inputs` among its arguments only when the remembered inputs
    are non-empty - or, when two or more apply at once (`listing`'s own
    `runtime_conflict`), `{id, name, conflict: true}` with `call` left out,
    since a seat has to pick one itself before either can commit.

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

    Both blocks share `MAX_HINT_NAME_CHARS`'s truncation on `name`, and
    together share `MAX_HINT_SCRIPTS`'s cap on entry count and
    `MAX_HINT_BYTES`'s cap on the whole block's own JSON size - `applicable`
    entries always kept first (finding 6). Never `static_conflicts` here -
    its result is not read out of this answer's own items, only `listing`'s
    (finding 5)."""
    if not scripts_enabled_here():
        return None
    corpus = runtime._corpus(seat, corpora, fresh=False)
    if isinstance(corpus, runtime.ScriptExit):
        return None
    docs = [doc for doc in corpus.get("scripts") or [] if isinstance(doc, dict)]
    if not docs:
        return None
    holds_priority = state.get("priority_holder") == seat.player
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
    for doc, play in plays:
        try:
            entries.append(_judged(doc, play, state, seat.player, supplied, catalog, []))
        except conditions.ContentsRequired:
            deferred.append((doc, play))
    if deferred:
        # S1 review finding 1: a play whose priority/archetype/when already
        # match `state` but whose conditions read the viewer's own library or
        # sideboard contents (`conditions.needs_contents`) is worth exactly
        # one extra `include=contents` fetch - paid once here, for every such
        # play at once, rather than the old blanket "not applicable".
        contents_state = seat.fetch_state(include_contents=True)
        entries.extend(
            _hint_judged(doc, play, contents_state, seat.player, supplied, catalog, [])
            for doc, play in deferred
        )
    entries.sort(key=lambda entry: (-_priority(entry), str(entry["id"])))

    applicable_items: list[dict[str, Any]] = []
    if holds_priority:
        recent = {
            (str(run[0]), run[1], run[2])
            for run in corpus.get(_RECENT_RUNS_KEY) or []
            if isinstance(run, list) and len(run) == 3
        }
        position = (_turn_position(state), state.get("step"))
        applying = [
            entry
            for entry in entries
            if entry["applies"] and (str(entry["id"]), *position) not in recent
        ]
        conflict = len(applying) > 1
        for entry in applying:
            item: dict[str, Any] = {"id": entry["id"], "name": _truncated_name(entry.get("name"))}
            if conflict:
                item["conflict"] = True
            else:
                item["call"] = _call_template(seat, str(entry["id"]), asserted, remembered)
            applicable_items.append(item)

    armable_items: list[dict[str, Any]] = []
    armable_ids: list[str] = []
    earliest_steps: list[str] = []
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
            armable_items.append(
                {"id": entry["id"], "name": _truncated_name(entry.get("name")), "when": when}
            )
            armable_ids.append(script_id)
            earliest_steps.append(_earliest_step(when["steps"]))

    if not applicable_items and not armable_items:
        return None

    kept_applicable, kept_armable = _hint_bounded(applicable_items, armable_items)
    if not kept_applicable and not kept_armable:
        return None
    result: dict[str, Any] = {}
    if kept_applicable:
        result["applicable"] = kept_applicable
    if kept_armable:
        kept_ids = [item["id"] for item in kept_armable]
        kept_steps = [
            step
            for item, step in zip(armable_items, earliest_steps, strict=True)
            if item["id"] in kept_ids
        ]
        result["armable"] = kept_armable
        result["arm_call"] = _arm_call_template(
            seat, kept_ids, asserted, kept_steps, kept_armable[0]["when"], remembered
        )
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


def _has_step_at_or_after(steps: list[str], current_step: Any) -> bool:
    if current_step not in runtime.AUTOPASS_STEPS:
        # untap/cleanup (or an unrecognised step): every named step is still
        # ahead of it in turn order.
        return True
    here = runtime.AUTOPASS_STEPS.index(current_step)
    return any(
        runtime.AUTOPASS_STEPS.index(step) >= here
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
        if whose in ("theirs", "either") and _has_step_at_or_after(steps, state.get("step")):
            return True
        return whose in ("mine", "either")
    # Already in this seat's own upkeep/draw: the window is the rest of THIS
    # turn. `theirs` alone can never come up before this own turn ends - that
    # would cross into the opponent's next turn.
    if whose not in ("mine", "either"):
        return False
    return _has_step_at_or_after(steps, state.get("step"))


def _earliest_step(steps: list[str]) -> str:
    ordered = [step for step in steps if step in runtime.AUTOPASS_STEPS]
    if not ordered:
        return str(steps[0]) if steps else ""
    return min(ordered, key=runtime.AUTOPASS_STEPS.index)


def _hint_bounded(
    applicable_items: list[dict[str, Any]], armable_items: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`applicable_items` and `armable_items`, combined under one shared
    `MAX_HINT_SCRIPTS` entry cap and `MAX_HINT_BYTES` byte budget -
    `applicable` entries kept first, `armable` filling whatever is left."""
    combined = [("applicable", item) for item in applicable_items]
    combined += [("armable", item) for item in armable_items]
    combined = combined[:MAX_HINT_SCRIPTS]
    kept: list[tuple[str, dict[str, Any]]] = []
    for kind, item in combined:
        candidate = [*kept, (kind, item)]
        # Measured the same way `_byte_bounded` always has - the flat list
        # of surviving items' own JSON size, not the two-key object they end
        # up wrapped in: `applicable` and `armable` share one budget over
        # what they themselves cost, not a second helping for the wrapper.
        if len(json.dumps([entry for _, entry in candidate]).encode("utf-8")) > MAX_HINT_BYTES:
            break
        kept = candidate
    kept_applicable = [item for kind, item in kept if kind == "applicable"]
    kept_armable = [item for kind, item in kept if kind == "armable"]
    return kept_applicable, kept_armable


def _arm_call_template(
    seat: Seat,
    armable_ids: list[str],
    asserted: str,
    earliest_steps: list[str],
    top_when: dict[str, Any],
    remembered: dict[str, Any] | None = None,
) -> str:
    """A ready-to-use call arming every one of `armable_ids` at once -
    `pass_until(...)` for a seat, `wait_for_priority(...)` for an advisor
    (advisors are refused `pass_until`). `top_when` is the highest-priority
    kept `armable` entry's own `when` - `pass_until`'s destination is one
    `step`/`whose_turn` pair, so a batch of several armed ids is targeted at
    the top one's own window, in its own vocabulary
    (`runtime.WHOSE_TURN_FOR_AUTOPASS`: `when`'s `theirs`/`either` are
    `pass_until`'s `opponent`/`any`) - never a hardcoded `"mine"`, which sent
    a `theirs` script's own template straight past the opponent's window and
    into this seat's own next turn, never matching (S2 review finding 1).
    `remembered` is this seat's own non-archetype inputs last supplied to
    `run_script` (S1 finding 4b) - named here too, exactly as `_call_template`
    already names them for `applicable`, so an armed script whose condition
    reads one of them does not come back `needs_input` merely because the
    template left it out."""
    args: dict[str, Any] = {"player_uid": seat.uid}
    if not seat.advise:
        step = min(earliest_steps, key=runtime.AUTOPASS_STEPS.index) if earliest_steps else ""
        args["step"] = step
        args["whose_turn"] = runtime.WHOSE_TURN_FOR_AUTOPASS.get(top_when.get("whose_turn"), "mine")
    args["arm"] = armable_ids
    args["arm_reasoning"] = runtime.REASONING_PLACEHOLDER
    args["opposing_archetype"] = asserted
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
    seat: Seat, script_id: str, asserted: str, inputs: dict[str, Any] | None = None
) -> str:
    """A ready-to-use `run_script(...)` invocation string for `script_id` -
    the exact argument names `mcp.run_script` accepts, with `reasoning` left
    for the seat to fill in (every run needs its own, in the seat's own
    words), `opposing_archetype` set to what this hint judged the script
    against, and `inputs` named only when the seat's remembered inputs are
    non-empty (S1 review finding 4b)."""
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
