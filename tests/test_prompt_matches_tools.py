"""T3's closing half: `src/mtg_agent/driver/prompt.py` is prose the agent
obeys as instructions, and it names MCP tools and their parameters -
`wait_for_priority(player_uid)`, `` `stop_on_stack=False` ``, `cast(...,
hold_priority=True)`` - with nothing before this test tying that prose to
`contract/tools.json` (`scripts/export_tools.py`'s static read of the real
`@server.tool()` registrations in `src/magic_cards_model_api/mcp.py`). Before
this test, renaming a tool or a parameter told the agent something false and
nothing failed.

Three syntactic shapes carry a tool or parameter reference in this file, and
this test extracts exactly those, nothing looser - a bare-word scan over
every backtick span is not used because prose backticks plenty that is not a
tool surface at all (`wake_reason` and `finished` are fields on a tool's
*answer*, not a parameter of the call; `spellslinger` names a wholly
different MCP server; `None`, `mtg`, `briefing` are not the tool surface
either) - false positives there would make this test noise no one could
trust, which is its own way of nothing failing:

1. A call example, backticked or in an indented code block -
   `wait_for_priority(player_uid)`, `cast(player_uid=..., card=...,
   hold_priority=True)` - names a tool by the text before `(` and every
   `keyword=` inside the parens by name.
2. A backticked `name=value` fragment on its own - `` `step="upkeep"` ``,
   `` `stop_on_stack=False` `` - names a parameter the same way, with no
   enclosing call.
3. A markdown bullet enumerating tools by name, one clause each - "-
   `wait_for_priority(player_uid)` checks...", "- `cast`, `play_land`, ...
   act." - every backtick token in a `## How you act` bullet that is not
   already covered by (1) or (2) and is not on `NON_TOOL_VOCABULARY` is
   asserted to be either a real tool name or a real parameter name of *some*
   tool (prose does not always repeat which tool a shared parameter such as
   `hold_priority` belongs to at the point it names it).

A call example's own *name* is checked too, and separately, because for a
while it was not: (1) reads every `name(...)` in the whole module, so most of
what it finds is this file's Python, and the test that consumes it skips every
name absent from the contract - which is exactly what a drifted name looks
like. Renaming `battlefield_state` in `contract/tools.json` therefore produced
zero failures here, on the one tool this file's whole doctrine is about, and a
fabricated `board_snapshot(player_uid, deep=True)` in `## How you act` failed
nothing either. `extract_call_examples` is the narrowed extraction whose names
*can* be asserted (see `_CALL_EXAMPLE_RE`), and
`test_every_call_example_names_a_tool_that_exists` asserts them.

Every extraction here is also asserted non-empty by the test that reads it. A
prose guard that silently matches nothing is worse than no guard at all,
because a green run gets read as evidence that the prose was checked.

Expect this to fail the first time a tool or a parameter it names actually
drifts from `contract/tools.json` - fix the prose, not this parsing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPT_SOURCE = REPO_ROOT / "src" / "mtg_agent" / "driver" / "prompt.py"
CONTRACT = REPO_ROOT / "contract" / "tools.json"

#: Backtick vocabulary in prompt.py that is not this MCP server's tool
#: surface at all, and so is not checked against contract/tools.json.
NON_TOOL_VOCABULARY = {
    # fields on a tool's *answer*, not a parameter of any call
    "wake_reason",
    "finished",
    "stopped_because",
    "winner",
    # the log tail every state-bearing answer now carries, and its own two
    # fields - all three are answer shape, none of them a tool or a parameter
    "recent_log",
    "omitted",
    "seq",
    # the shape an action tool's answer now has (`mcp._action_answer`) - the
    # refusal-or-acceptance it leads with, the narrowed board block, and that
    # block's own honesty about what it left out. All three are answer shape;
    # none is a tool or a parameter.
    "outcome",
    "board",
    "pending_decision",
    "cast_options",
    # wave 1's own additions to that answer, and the keys the two-tool loop
    # points a seat at instead of a second call: the board summary that
    # replaces `battlefield_state`, a target slot's honesty about its own
    # cap and where the rest are, the paste-ready answer to a pending
    # decision and the parameter it names, and the payment plan an action
    # reports. All answer shape; none is a tool or a parameter.
    "battlefield_digest",
    # the legal-action list itself: it rides `pass_until`/`pass` and every
    # action's answer, and is the one thing `wait_for_priority`'s state view
    # does not carry - answer shape either way, never a tool or a parameter
    "available_actions",
    "truncated",
    "more_with",
    "answer_with",
    "answer_field",
    "pays_with",
    # the fail-closed notice list every action/pass tool can now carry
    # (commands._attach_notices) and two of its own per-entry fields - answer
    # shape, none of them a tool or a parameter
    "notices",
    "mode_number",
    "source_name",
    # the MCP 2026-07-28 result-shape name, not a tool or a parameter
    "input_required",
    # local Python names in this module, no MCP meaning
    "briefing",
    "current_game_path",
    "None",
    # the mtg MCP server's own name, not one of its tools
    "mtg",
    # a different MCP server (spellslinger) and its own tools
    "spellslinger",
    "knowledge_search",
    "resolve_cards",
}

_IDENT = r"[a-zA-Z_][a-zA-Z0-9_]*"
_CALL_RE = re.compile(rf"\b({_IDENT})\(([^()]*)\)")
_KWARG_RE = re.compile(rf"\b({_IDENT})\s*=")
_INLINE_KWARG_RE = re.compile(rf"`({_IDENT})=")
_BACKTICK_IDENT_RE = re.compile(rf"`({_IDENT})`")
#: A call example, narrowed enough that its *name* can be asserted against
#: the contract rather than skipped. `_CALL_RE` cannot be: it runs over the
#: whole module, so two thirds of what it finds is this file's own Python
#: (`guide_paths.rglob(...)`, `len(...)`, `_clause_count(text)`), which is why
#: `test_every_call_example_names_a_real_tool_and_real_parameters` skips every
#: name absent from the contract - and skipping exactly the names that drifted
#: is the one thing a drift guard must not do. A tool-call example in prose is
#: never an attribute access and never a private name, so both are excluded
#: here; `_looks_like_a_call_example` does the rest.
_CALL_EXAMPLE_RE = re.compile(r"(?<![.\w])([a-zA-Z][a-zA-Z0-9_]*)\(([^()]*)\)")
#: A backticked span, and the whole tokens inside one - `-` and `_` are part
#: of a token, so containment is exact. A loose `verb in text` let `pass` be
#: satisfied by `pass_until` and by the English "Do NOT **pass**
#: `mana_sources`" two bullets down, and `cast` by `casting`; a verb the
#: prose actually teaches is always backticked.
_BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")
_VERB_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def _load_contract() -> dict[str, set[str]]:
    """tool name -> its parameter names, from contract/tools.json."""
    data = json.loads(CONTRACT.read_text())
    return {tool["name"]: {p["name"] for p in tool["parameters"]} for tool in data["tools"]}


def _how_you_act_section(source: str) -> str:
    start = source.index("## How you act")
    next_heading = source.index("\n## ", start + len("## How you act"))
    return source[start:next_heading]


def extract_tool_calls(source: str) -> list[tuple[str, list[str]]]:
    """Every `name(kwarg=..., ...)` call example - (tool name, kwarg names)."""
    calls = []
    for match in _CALL_RE.finditer(source):
        name, args = match.group(1), match.group(2)
        kwargs = _KWARG_RE.findall(args)
        calls.append((name, kwargs))
    return calls


def extract_inline_kwargs(source: str) -> list[str]:
    """Every standalone `` `name=value` `` parameter mention."""
    return _INLINE_KWARG_RE.findall(source)


def extract_bullet_tokens(source: str) -> list[str]:
    """Every bare backtick identifier inside the `## How you act` section
    that is not already a call name/kwarg or on NON_TOOL_VOCABULARY."""
    section = _how_you_act_section(source)
    call_names = {name for name, _ in extract_tool_calls(section)}
    tokens = []
    for token in _BACKTICK_IDENT_RE.findall(section):
        if token in NON_TOOL_VOCABULARY or token in call_names:
            continue
        tokens.append(token)
    return tokens


def _looks_like_a_call_example(args: str, params: set[str]) -> bool:
    """Whether `name(<args>)` is prose showing a call, rather than this
    module's own Python. Every argument has to read like one: a `keyword=`,
    an elided `...`, or a bare parameter name of some real tool. That rejects
    `card(s)` (prose: "1 card(s) chosen"), `is_file()` and `join(parts)`,
    and accepts `battlefield_state(player_uid)` and
    `cast(player_uid=..., card="...", dry_run=True)`."""
    args = args.strip()
    if not args:
        # `foo()` is Python here (`readouterr()`, `to_wire()`), never a tool
        # example: every tool takes `player_uid` at least.
        return False
    for part in args.split(","):
        part = part.strip()
        if not part:
            return False
        if part == "..." or part in params or re.match(rf"^{_IDENT}\s*=", part):
            continue
        return False
    return True


def extract_call_examples(source: str, params: set[str]) -> list[tuple[str, list[str]]]:
    """Every call example whose name is worth asserting against the contract
    - (tool name, kwarg names). See `_CALL_EXAMPLE_RE`."""
    return [
        (match.group(1), _KWARG_RE.findall(match.group(2)))
        for match in _CALL_EXAMPLE_RE.finditer(source)
        if _looks_like_a_call_example(match.group(2), params)
    ]


def _verbs(text: str) -> set[str]:
    """Every whole token the prose backticks - the verbs it actually teaches,
    for exact containment. See `_BACKTICK_SPAN_RE`."""
    return {
        token for span in _BACKTICK_SPAN_RE.findall(text) for token in _VERB_TOKEN_RE.findall(span)
    }


def test_prompt_source_is_readable():
    assert PROMPT_SOURCE.is_file()


def test_every_call_example_names_a_real_tool_and_real_parameters():
    source = PROMPT_SOURCE.read_text()
    contract = _load_contract()
    calls = extract_tool_calls(source)
    assert calls, "extract_tool_calls found nothing - this guard is asserting on an empty list"
    for name, kwargs in calls:
        if name not in contract:
            # Not every `name(...)` in prose is a tool call (e.g. a Python
            # builtin used in an aside) - only assert on names that are
            # backticked or indented as a tool-call example elsewhere too.
            # The *names* are asserted by
            # `test_every_call_example_names_a_tool_that_exists`, which is
            # what this `continue` used to leave to nobody.
            continue
        for kwarg in kwargs:
            assert kwarg in contract[name], (
                f"prompt.py calls `{name}({kwarg}=...)`, but `{kwarg}` is not "
                f"a real parameter of `{name}` in contract/tools.json "
                f"(real parameters: {sorted(contract[name])})"
            )


def test_every_call_example_names_a_tool_that_exists():
    """The hole this test closes, measured: renaming `battlefield_state` in
    `contract/tools.json` produced ZERO failures in this whole file. That
    name appears in the prose only as `battlefield_state(player_uid)`, and a
    call example's *name* was checked by nothing - `:160` skipped every name
    absent from the contract (which is exactly a drifted one) and the
    bullet-token scan skipped every name a call example had already claimed.
    So the one tool this file's entire doctrine is about was the one with no
    name check, and a fabricated `board_snapshot(player_uid, deep=True)`
    dropped into `## How you act` failed nothing either."""
    contract = _load_contract()
    params = {name for names in contract.values() for name in names}
    calls = extract_call_examples(PROMPT_SOURCE.read_text(), params)
    assert calls, (
        "extract_call_examples found no call example at all in prompt.py - "
        "either the prose stopped showing calls or _CALL_EXAMPLE_RE stopped "
        "matching them, and either way this guard is now vacuous"
    )
    for name, _ in calls:
        assert name in contract, (
            f"prompt.py shows `{name}(...)` as a call the agent can make, but "
            f"`{name}` is not a tool in contract/tools.json (real tools: "
            f"{sorted(contract)})"
        )


def test_every_inline_kwarg_mention_is_a_real_parameter():
    source = PROMPT_SOURCE.read_text()
    contract = _load_contract()
    all_params = {name for names in contract.values() for name in names}
    mentions = extract_inline_kwargs(source)
    assert mentions, (
        "extract_inline_kwargs found nothing - this guard is asserting on an empty list"
    )
    for kwarg in mentions:
        assert kwarg in all_params, (
            f"prompt.py mentions `{kwarg}=...`, but `{kwarg}` is not a real "
            f"parameter of any tool in contract/tools.json"
        )


def test_every_how_you_act_bullet_token_is_a_real_tool_or_parameter():
    source = PROMPT_SOURCE.read_text()
    contract = _load_contract()
    known_tools = set(contract)
    all_params = {name for names in contract.values() for name in names}
    tokens = extract_bullet_tokens(source)
    assert tokens, (
        "extract_bullet_tokens found nothing in '## How you act' - this guard "
        "is asserting on an empty list"
    )
    for token in tokens:
        assert token in known_tools or token in all_params, (
            f"prompt.py's '## How you act' section names `{token}`, which is "
            f"neither a tool nor a parameter in contract/tools.json"
        )


# -- M4: the two-tool loop is the default doctrine, in both briefs ----------
#
# Wave 1 made every answer carry what a seat needs to *issue* its next action,
# not merely to choose one (`mcp._action_answer`'s `board.battlefield_digest`,
# `available_actions.actions[].targets.slots[].candidates`,
# `pending_decision.answer_with`, `recent_log`). The prose taught the
# opposite, and `tests/test_seat_round_trips.py`'s own docstring names that
# instruction as a measured cause of a 65-tool-call agent activation: "read
# `battlefield_state`, read `game_log`". These two tests pin the replacement -
# that the loop is stated in terms of the real tools, and that the retired
# instruction cannot silently come back.

BRIEF = REPO_ROOT / "src" / "magic_cards_model_api" / "docs" / "ai_seat_brief.md"
BUDDY_BRIEF = REPO_ROOT / "src" / "magic_cards_model_api" / "docs" / "ai_buddy_brief.md"

#: Every word an agent seat is given about what it may do, and in whose
#: spelling. `prompt.py` drives MCP tools; the brief drives `mcp-seat`
#: subcommands (`ai_seat.command_for` allows the agent `Bash` and nothing
#: else), so the same doctrine has to be stated twice, in two vocabularies.
DOCTRINE_SOURCES = {
    "src/mtg_agent/driver/prompt.py": PROMPT_SOURCE,
    "src/magic_cards_model_api/docs/ai_seat_brief.md": BRIEF,
}

#: The loop, per source: the wait verb(s) first, then every committing verb.
#: A wait then EXACTLY ONE of these, and nothing in between.
LOOP_VERBS = {
    "src/mtg_agent/driver/prompt.py": (
        ("pass_until", "pass"),
        ("cast", "decide", "activate", "play_land", "declare_attackers", "declare_blockers"),
    ),
    "src/magic_cards_model_api/docs/ai_seat_brief.md": (
        ("pass-until", "pass"),
        ("cast", "decide", "activate", "play-land", "attackers", "blockers"),
    ),
}

#: The answer key that replaces each call the loop forbids, per source. The
#: two transports genuinely differ here and the prose has to be true of the
#: one it is written for: `mcp._action_answer` narrows the board to
#: `board.battlefield_digest`, while `seat_cli` prints `run_action`/
#: `pass_until`'s own dict, which carries `state` whole. Both carry
#: `recent_log`, both carry `pending_decision.answer_with`, and both carry a
#: target slot's `truncated`.
ANSWER_KEYS_THAT_REPLACE_A_CALL = {
    "src/mtg_agent/driver/prompt.py": (
        "battlefield_digest",
        "recent_log",
        "answer_with",
        "truncated",
    ),
    "src/magic_cards_model_api/docs/ai_seat_brief.md": (
        "state",
        "recent_log",
        "answer_with",
        "truncated",
    ),
}

#: The instruction wave 1 exists to remove, in every spelling it has been
#: written in, plus the framing that made an unconditional board read read as
#: the right thing to do. None of these may appear in any brief again.
#:
#: The unbackticked spellings are here because the buddy brief used one: "read
#: the state and the log, work it out". Only the *board* half of that is
#: retired, on every surface - the buddy's log read is a genuine exception and
#: `test_buddy_lifecycle.py` says what makes it one.
RETIRED_ROUTINE_READS = (
    "read `battlefield_state`, read `game_log`",
    "read `battlefield_state` and then `game_log`",
    "read `state`, read the `log`",
    "read `state`, read the `log` for everything",
    "read the state and the log",
    "read the state, read the log",
    "read the state and log",
    "which is the right default",
)

#: Every brief the retired instruction must never reappear in. Wider than
#: `DOCTRINE_SOURCES`, which is the two-tool loop's own sources: the buddy
#: brief does not run that loop (its wait is not `pass_until` - that tool is
#: refused outright for an advise seat) but it is prose an agent obeys just
#: the same, and leaving it out is how the instruction the seat's brief had
#: just deleted came straight back in the buddy's, in the same branch.
RETIRED_READ_SOURCES = {
    **DOCTRINE_SOURCES,
    "src/magic_cards_model_api/docs/ai_buddy_brief.md": BUDDY_BRIEF,
}


def _flat(path: Path) -> str:
    return " ".join(path.read_text().split())


def _loop_section(text: str) -> str:
    """The prose stating the loop - `## The loop` through the next heading."""
    start = text.index("## The loop")
    return text[start : text.index("\n## ", start + len("## The loop"))]


def test_neither_brief_instructs_a_routine_board_or_log_read():
    """The measured defect: `mtg-seat-3b6613e0` spent 65 tool calls on one
    agent activation, and "what happened, and what can I do" was two of every
    three of them because the brief said to read the board and then the log.
    Both facts ride every answer now (`board.battlefield_digest`,
    `recent_log`), so the instruction is not merely redundant - it is the
    behaviour wave 1 was built to delete."""
    for name, path in RETIRED_READ_SOURCES.items():
        flat = _flat(path)
        for retired in RETIRED_ROUTINE_READS:
            assert retired not in flat, (
                f"{name} still tells a seat to read the board and the log as a "
                f"matter of course ({retired!r}) - that instruction is the "
                "measured cause of the 65-call activation in "
                "tests/test_seat_round_trips.py"
            )


def test_both_briefs_forbid_the_calls_every_answer_already_answers():
    """Not enough that the old instruction is gone: a seat with no
    instruction either way reaches for the board, because reading the board
    before acting is what a player does. The prohibition has to be explicit,
    and it has to name the key that replaces each call."""
    for name, path in DOCTRINE_SOURCES.items():
        flat = _flat(path)
        for key in ANSWER_KEYS_THAT_REPLACE_A_CALL[name]:
            assert key in flat, f"{name} never names `{key}`, the answer key that replaces a call"
        assert flat.count("Do NOT") >= 4, (
            f"{name} does not state the loop's prohibitions as prohibitions"
        )
        assert "mana_sources" in flat or "--sources" in flat, (
            f"{name} never tells a seat to leave the mana sources empty"
        )
        assert "sources_or_all" in flat, (
            f"{name} never says which code offers every untapped source on the seat's behalf"
        )
        assert "verbatim" in flat, f"{name} never says to use the answer_with template verbatim"


def test_both_briefs_state_the_loop_in_terms_of_the_real_verbs():
    """The loop is a wait then EXACTLY ONE committing call, and it has to be
    stated with the names a seat actually types - pinned against
    `contract/tools.json` for the MCP driver and against `seat_cli`'s own
    subparsers for the brief, so neither can name a verb that does not
    exist."""
    from magic_cards_model_api import seat_cli

    contract = _load_contract()
    subcommands = set(
        next(
            action
            for action in seat_cli.build_parser()._actions
            if getattr(action, "choices", None)
        ).choices
    )
    surfaces = {
        "src/mtg_agent/driver/prompt.py": set(contract),
        "src/magic_cards_model_api/docs/ai_seat_brief.md": subcommands,
    }
    for name, path in DOCTRINE_SOURCES.items():
        section = _loop_section(path.read_text())
        flat = " ".join(section.split())
        waits, committing = LOOP_VERBS[name]
        # Exact tokens, never substrings: `verb in flat` let `pass` be
        # satisfied by `pass_until` and `cast` by `casting`, so a loop
        # section that had stopped naming the bare wait still passed.
        named = _verbs(flat)
        for verb in (*waits, *committing):
            assert verb in surfaces[name], (
                f"the loop test names `{verb}`, which is not a real verb of {name}'s surface"
            )
            assert verb in named, f"{name}'s '## The loop' section never names `{verb}`"
        assert "EXACTLY ONE" in section, (
            f"{name}'s loop section does not say exactly one committing call"
        )
        assert "nothing in between" in flat, (
            f"{name}'s loop section never says there is no third call between the two"
        )


# -- M4 review: `wait` is the one answer that carries neither ----------------
#
# The loop's prohibitions are only safe where they are true. `seat_cli`'s
# `wait` is the exception: `seatkit.block_until_wake` returns `recent_log` as
# a *sibling* of `state` (its own docstring says why it can never be a key
# inside it) and never computes `available_actions` at all, and the `wait`
# branch prints `woken["state"]` alone. So a seat told "do not run `state`,
# every command including `wait` prints it alongside `available_actions`" and
# "`recent_log` rides every one of those answers" is told two false things
# about `wait` - and the brief itself sends a seat to `wait --max-seconds 20`
# whenever an action woke with nothing new. Obeying both leaves it holding an
# answer with no action list and no log tail and no sanctioned way to get
# either. The MCP side never had this: `prompt.py`'s loop names only
# `pass_until`/`pass` as the wait, and both of those go through `pass_until`,
# which does carry all three.

#: The keys a `wait` answer does NOT carry. Asserted against the running code
#: below, then used to pin the prose.
WAIT_CARRIES_NEITHER = ("available_actions", "recent_log")


def _brief_bullet(marker: str) -> str:
    """One top-level markdown bullet of the brief, `marker` through the line
    before the next `- ` at column zero."""
    text = BRIEF.read_text()
    start = text.index(marker)
    end = text.index("\n- ", start + len(marker))
    return " ".join(text[start:end].split())


def test_wait_is_the_one_answer_carrying_neither_actions_nor_log(client, game, monkeypatch, capsys):
    """The behavioural fact the prose has to match, read off the real CLI:
    `pass-until`'s answer carries `available_actions` and `recent_log`, and
    `wait`'s answer carries neither."""
    import httpx
    from fastapi.testclient import TestClient

    from magic_cards_model_api import seat_cli
    from magic_cards_model_api.main import app
    from magic_cards_model_api.store import store

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    uids = {name: uid for uid, name in store.seat_uids(game).items()}

    assert seat_cli.run(["--uid", uids["alice"], "--game", game, "wait"]) == 0
    waited = json.loads(capsys.readouterr().out)
    for key in WAIT_CARRIES_NEITHER:
        assert key not in waited, (
            f"`wait` now carries `{key}` - if that is deliberate, the brief's "
            "exception for it is what has to change"
        )

    assert seat_cli.run(["--uid", uids["alice"], "--game", game, "pass-until", "end_step"]) == 0
    passed = json.loads(capsys.readouterr().out)
    for key in WAIT_CARRIES_NEITHER:
        assert key in passed, f"`pass-until` no longer carries `{key}`, which the loop leans on"


def test_a_wait_answer_still_carries_the_decision_that_woke_it(client, game, monkeypatch, capsys):
    """What a `wait` answer *is* good for, and the reason the brief can keep
    sending a seat to it: `pending_decision` is part of the state itself, so
    the paste-ready `answer_with` rides a bare `wait` even though no action
    list does."""
    import httpx
    from fastapi.testclient import TestClient
    from test_decisions import advance_into_cleanup

    from magic_cards_model_api import seat_cli
    from magic_cards_model_api.main import app
    from magic_cards_model_api.store import store

    for _ in range(8):
        client.post(
            f"/api/games/{game}/cards",
            json={"card": "Mountain", "player": "alice", "zone": "hand"},
        )
    assert advance_into_cleanup(game) is not None, "cleanup never asked alice to discard"

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    assert seat_cli.run(["--uid", uids["alice"], "--game", game, "wait"]) == 0
    waited = json.loads(capsys.readouterr().out)

    pending = waited.get("pending_decision")
    assert pending, "a bare `wait` no longer shows the decision that woke it"
    assert pending["answer_with"], "a `wait` answer's decision carries no paste-ready call"


def test_the_brief_never_promises_wait_an_action_list_or_a_log():
    """The two bullets that forbid `state` and `log` enumerate the commands
    whose own answer replaces them. `wait` is not one of them (above), so it
    must not be enumerated there - and because the brief elsewhere tells a
    seat to reach for `wait --max-seconds 20`, the exception has to be stated
    out loud rather than merely left out."""
    for marker in ("- **Do NOT run `state`.**", "- **Do NOT run `log`.**"):
        bullet = _brief_bullet(marker)
        assert "`wait`" not in bullet, (
            f"the brief's {marker!r} bullet still enumerates `wait` among the "
            "answers that carry an action list and a log tail; it carries "
            "neither (test_wait_is_the_one_answer_carrying_neither_actions_nor_log)"
        )

    flat = _flat(BRIEF)
    assert "carries neither" in flat, (
        "the brief never says which answer carries neither an action list nor "
        "a log tail - a seat sent to `wait` by the brief's own advice needs to "
        "be told what it is holding"
    )
    exception = flat[flat.index("carries neither") - 400 : flat.index("carries neither") + 400]
    for key in WAIT_CARRIES_NEITHER:
        assert key in exception, (
            f"the brief's `wait` exception never names `{key}` as the thing that answer lacks"
        )
    assert "`wait`" in exception, "the brief's exception never says it is `wait` that is the one"


def test_the_brief_sends_a_nothing_new_yet_seat_to_the_wait_that_carries_something():
    """The other half of the same defect: it is the brief itself that tells a
    seat what to run when an action comes back `nothing new yet`. Sending it
    to a bare `wait` there and then forbidding `state` and `log` leaves it
    holding an answer with no action list and no log tail, so every such
    passage has to name `pass-until` - the wait whose own answer carries
    both."""
    text = BRIEF.read_text()
    passages = [
        " ".join(block.split())
        for block in re.split(r"\n\s*\n", text)
        if "nothing new yet" in block
    ]
    assert passages, "the brief no longer says what to do when nothing has changed yet"
    for passage in passages:
        assert "pass-until" in passage, (
            "a passage about `nothing new yet` never names `pass-until`, the "
            f"only wait whose answer carries {' and '.join(WAIT_CARRIES_NEITHER)}: {passage!r}"
        )


def test_the_mcp_wait_carries_the_log_but_no_action_list(client, game, monkeypatch):
    """The same question asked of the MCP transport, because the answer there
    is different and the prose has to match the transport it is written for:
    `mcp.wait_for_priority` merges `recent_log` into the state view it hands
    back (so "recent_log rides every answer" is true), but a state view is
    not an action list - `available_actions` comes with `pass_until`/`pass`
    and with every action's own answer."""
    import asyncio

    import httpx
    from fastapi.testclient import TestClient

    from magic_cards_model_api.main import app
    from magic_cards_model_api.mcp import build_server
    from magic_cards_model_api.seatkit import Seats
    from magic_cards_model_api.store import store

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    server = build_server(Seats(api="http://testserver", game_id=game))
    raw = asyncio.run(server.call_tool("wait_for_priority", {"player_uid": uids["alice"]}))
    answer = json.loads(raw.content[0].text)

    assert "recent_log" in answer, "the MCP wait no longer carries the log tail the prose promises"
    assert "available_actions" not in answer, (
        "the MCP wait now carries an action list - then the prompt's note that "
        "it does not is what has to change"
    )

    flat = _flat(PROMPT_SOURCE)
    bullet_start = flat.index("`wait_for_priority(player_uid, max_seconds=20)`")
    bullet = flat[bullet_start : bullet_start + 1600]
    assert "available_actions" in bullet, (
        "prompt.py never tells a seat that the wait it opens with hands back "
        "no `available_actions` - the action list rides `pass_until`/`pass` "
        "and every action's own answer"
    )
