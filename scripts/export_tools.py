"""Statically export the `mtg` MCP server's tool surface as `contract/tools.json`.

Closes the hole `docs/2b-plan.md`'s T3 names: `src/mtg_agent/driver/prompt.py`
names MCP tool and parameter names in prose the agent obeys as instructions,
with no import, no type and no test tying that prose to the 22 (now 18)
`@server.tool()` registrations in `src/magic_cards_model_api/mcp.py` - so
renaming a tool there told the agent something false and nothing failed. This
module is the artifact half of the fix: `tests/test_prompt_matches_tools.py`
is the checker that reads it back.

Purely static, on purpose: this walks `mcp.py`'s AST rather than importing
and introspecting a running `MCPServer`. Importing `mcp.py` pulls in the
`magic_cards_model` engine (the sibling path dependency) and constructs real
`Seats`/httpx machinery just to read function signatures that are already
sitting in the source text - static reading is both simpler and exactly what
`tests/test_prompt_matches_tools.py` needs to check prose against, since a
human reading the prose reads the source too, not a live server's registry.

Deterministic by construction: tools are sorted by name (registration order
in `mcp.py` is not part of the contract - reordering two `@server.tool()`
defs must not change this file), parameters are kept in their declared
signature order (that IS part of the contract - it is what a positional
caller relies on), and the JSON is written with sorted keys and no
timestamp. Running this twice back to back produces byte-identical output.

A parameter with no static type annotation fails closed: its `type` is
`null` and `type_error` says why, rather than guessing a type from the
default value's own Python type (a `= ""` default does not prove `str` -
nothing here should print a class it did not actually see written down).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_SOURCE = REPO_ROOT / "src" / "magic_cards_model_api" / "mcp.py"
OUTPUT = REPO_ROOT / "contract" / "tools.json"

_BUILD_SERVER = "build_server"


def _is_tool_decorator(node: ast.expr) -> bool:
    """True for `@server.tool()` or `@server.tool(name="...")` - the only
    decorator shape this registers tools with. `@_guarded(seats)`, the other
    decorator every tool carries, is deliberately not this."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "tool"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "server"
    )


def _tool_name_override(call: ast.Call) -> str | None:
    """The `name=` a tool registers under when it differs from its own
    function name - `pass_once` is registered as `"pass"` because `pass` is
    a Python keyword and cannot be a function name."""
    for kw in call.keywords:
        if (
            kw.arg == "name"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return None


def _default_value(node: ast.expr | None) -> object:
    """The default's value where it is a plain literal (a string, number,
    bool, or `None`), else the source text of the expression that computes
    it. Never a guess at the parameter's *type* - `_param_record` owns that,
    from the annotation alone."""
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return ast.unparse(node)


def _param_record(arg: ast.arg, default: ast.expr | None) -> dict[str, object]:
    if arg.annotation is None:
        return {
            "name": arg.arg,
            "type": None,
            "type_error": "no static annotation on this parameter",
            "has_default": default is not None,
            "default": _default_value(default),
        }
    return {
        "name": arg.arg,
        "type": ast.unparse(arg.annotation),
        "type_error": None,
        "has_default": default is not None,
        "default": _default_value(default),
    }


def _param_records(fn: ast.FunctionDef) -> list[dict[str, object]]:
    """Every parameter, in declared signature order. Positional-or-keyword
    and keyword-only args are both covered (every tool here is the former;
    keyword-only is handled all the same in case one is ever added). `*args`/
    `**kwargs` are not - no tool takes either, and a future one that did
    would need a deliberate decision about what a variadic parameter even
    means in this contract, not a silent guess here."""
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    pad = [None] * (len(positional) - len(args.defaults))
    aligned_defaults: list[ast.expr | None] = [*pad, *args.defaults]
    records = [
        _param_record(arg, default)
        for arg, default in zip(positional, aligned_defaults, strict=True)
    ]
    records += [
        _param_record(arg, default)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    ]
    return records


def _docstring_first_line(fn: ast.FunctionDef) -> str:
    doc = ast.get_docstring(fn, clean=True) or ""
    return doc.split("\n", 1)[0].strip()


def build_contract() -> dict[str, object]:
    """Walk `mcp.py`'s AST and return the contract as a plain dict, ready for
    `render_contract` or direct comparison in tests - no filesystem write."""
    tree = ast.parse(MCP_SOURCE.read_text())
    build_server = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == _BUILD_SERVER
    )
    tools = []
    for node in build_server.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        tool_decorator = next((d for d in node.decorator_list if _is_tool_decorator(d)), None)
        if tool_decorator is None:
            continue
        tools.append(
            {
                "name": _tool_name_override(tool_decorator) or node.name,
                "docstring_first_line": _docstring_first_line(node),
                "parameters": _param_records(node),
            }
        )
    tools.sort(key=lambda tool: tool["name"])
    return {"tools": tools}


def render_contract(contract: dict[str, object]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True) + "\n"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render_contract(build_contract()))


if __name__ == "__main__":
    main()
