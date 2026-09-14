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

#: Where `run`/`listing` stash the archetype this seat last asserted, onto
#: the very corpus dict `Seats.script_corpora` caches - never a key the API
#: itself writes, so it needs no schema entry and nothing else ever reads it
#: but `applicable_hint`, below.
_LAST_ASSERTED_KEY = "_last_asserted_archetype"

#: The reasoning `applicable_hint`'s `call` template leaves for the seat to
#: fill in - `run_script` itself always requires a real one.
_REASONING_PLACEHOLDER = "<say why>"


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
    `invoked_via` ("mcp" or "cli") is carried onto the start/finish rows this
    run posts - `mcp.run_script` sends "mcp", `mcp-seat script run` sends
    "cli"."""
    supplied = merged_inputs(opposing_archetype, inputs)
    if isinstance(supplied, str):
        return supplied
    _remember_asserted_archetype(seat, corpora, supplied[BUILTIN_INPUT])
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
    plays = [(doc, runtime._normalised(doc)) for doc in docs]
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


def _remember_asserted_archetype(
    seat: Seat, corpora: dict[tuple[str, str], dict[str, Any]], asserted: Any
) -> None:
    """Stash `asserted` onto this seat's cached corpus, for `applicable_hint`
    to read back as "the archetype this seat last asserted" - `run`'s own
    equivalent of `listing`'s inline write of the same key."""
    corpus = runtime._corpus(seat, corpora, fresh=False)
    if not isinstance(corpus, runtime.ScriptExit):
        corpus[_LAST_ASSERTED_KEY] = asserted


def _hint_judged(
    doc: dict[str, Any],
    play: dict[str, Any] | runtime.ScriptExit,
    state: dict[str, Any],
    me: str,
    supplied: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    warnings: list[lint.PlaysWarning],
) -> dict[str, Any]:
    """`_judged`, except a play whose conditions need `include=contents` and
    which `state` was not fetched with (never true for `listing`, which
    fetches contents itself when any play needs them - but always possible
    here, since the hint never fetches at all) is treated as not applying
    rather than raising `conditions.ContentsRequired`. `applicable_hint`'s own
    "no extra board fetch" promise means this is the one path that can meet a
    play `listing` never would."""
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
    """`{"applicable": [...]}` for one seat's wake/action answer - the same
    applicability judgement `listing` makes, over `state` this answer already
    carries rather than a fresh board fetch. `None` when this MCP process has
    scripts switched off (`scripts_enabled_here`), when this seat has no
    registered corpus, or when nothing registered applies right now.

    Each entry is `{id, name, call}`, `call` a ready-to-use `run_script`
    invocation - or, when two or more apply at once (`listing`'s own
    `runtime_conflict`), `{id, name, conflict: true}` with `call` left out,
    since a seat has to pick one itself before either can commit. Capped at
    `MAX_HINT_SCRIPTS`, highest priority first."""
    if not scripts_enabled_here():
        return None
    corpus = runtime._corpus(seat, corpora, fresh=False)
    if isinstance(corpus, runtime.ScriptExit):
        return None
    docs = [doc for doc in corpus.get("scripts") or [] if isinstance(doc, dict)]
    if not docs:
        return None
    asserted = corpus.get(_LAST_ASSERTED_KEY)
    asserted = asserted if isinstance(asserted, str) else WILDCARD
    supplied = merged_inputs(asserted, None)
    if isinstance(supplied, str):
        return None
    declared = list(corpus.get("inputs") or [])
    catalog = input_catalog(declared, [asserted])
    plays = [(doc, runtime._normalised(doc)) for doc in docs]
    warnings = static_conflicts(docs, declared, str(corpus.get("deck_slug") or ""))
    entries = [
        _hint_judged(doc, play, state, seat.player, supplied, catalog, warnings)
        for doc, play in plays
    ]
    entries.sort(key=lambda entry: (-_priority(entry), str(entry["id"])))
    applying = [entry for entry in entries if entry["applies"]]
    if not applying:
        return None
    conflict = len(applying) > 1
    items: list[dict[str, Any]] = []
    for entry in applying[:MAX_HINT_SCRIPTS]:
        item: dict[str, Any] = {"id": entry["id"], "name": entry.get("name")}
        if conflict:
            item["conflict"] = True
        else:
            item["call"] = _call_template(seat, str(entry["id"]), asserted)
        items.append(item)
    return {"applicable": items}


def _call_template(seat: Seat, script_id: str, asserted: str) -> str:
    """A ready-to-use `run_script(...)` invocation string for `script_id` -
    the exact argument names `mcp.run_script` accepts, with `reasoning` left
    for the seat to fill in (every run needs its own, in the seat's own
    words) and `opposing_archetype` set to what this hint judged the script
    against."""
    args = {
        "player_uid": seat.uid,
        "script": script_id,
        "reasoning": _REASONING_PLACEHOLDER,
        "opposing_archetype": asserted,
    }
    rendered = ", ".join(f"{name}={json.dumps(value)}" for name, value in args.items())
    return f"run_script({rendered})"
