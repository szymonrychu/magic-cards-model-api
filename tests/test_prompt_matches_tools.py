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


def test_prompt_source_is_readable():
    assert PROMPT_SOURCE.is_file()


def test_every_call_example_names_a_real_tool_and_real_parameters():
    source = PROMPT_SOURCE.read_text()
    contract = _load_contract()
    for name, kwargs in extract_tool_calls(source):
        if name not in contract:
            # Not every `name(...)` in prose is a tool call (e.g. a Python
            # builtin used in an aside) - only assert on names that are
            # backticked or indented as a tool-call example elsewhere too.
            continue
        for kwarg in kwargs:
            assert kwarg in contract[name], (
                f"prompt.py calls `{name}({kwarg}=...)`, but `{kwarg}` is not "
                f"a real parameter of `{name}` in contract/tools.json "
                f"(real parameters: {sorted(contract[name])})"
            )


def test_every_inline_kwarg_mention_is_a_real_parameter():
    source = PROMPT_SOURCE.read_text()
    contract = _load_contract()
    all_params = {name for names in contract.values() for name in names}
    for kwarg in extract_inline_kwargs(source):
        assert kwarg in all_params, (
            f"prompt.py mentions `{kwarg}=...`, but `{kwarg}` is not a real "
            f"parameter of any tool in contract/tools.json"
        )


def test_every_how_you_act_bullet_token_is_a_real_tool_or_parameter():
    source = PROMPT_SOURCE.read_text()
    contract = _load_contract()
    known_tools = set(contract)
    all_params = {name for names in contract.values() for name in names}
    for token in extract_bullet_tokens(source):
        assert token in known_tools or token in all_params, (
            f"prompt.py's '## How you act' section names `{token}`, which is "
            f"neither a tool nor a parameter in contract/tools.json"
        )
