"""Drift guard for `contract/tools.json` (T3): `mise run tools:export` writes
that file by statically reading `src/magic_cards_model_api/mcp.py`'s
`@server.tool()` registrations (`scripts/export_tools.py`). Nothing stops a
tool's signature or docstring changing without the export being re-run, so
this test regenerates the contract in memory and asserts it is byte-identical
to the committed file - the same check `mise run tools:export` run twice
would show as a clean `git status`.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.export_tools import OUTPUT, build_contract, render_contract

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_committed_contract_matches_a_fresh_export():
    committed = OUTPUT.read_text()
    fresh = render_contract(build_contract())
    assert fresh == committed, (
        "contract/tools.json is stale - run `mise run tools:export` and commit the result"
    )


def test_export_is_deterministic():
    """Same input, byte-identical output, twice in a row - the property that
    lets `mise run tools:export` run repeatedly with a clean `git status`."""
    first = render_contract(build_contract())
    second = render_contract(build_contract())
    assert first == second


def test_contract_covers_every_registered_tool():
    """A sanity floor, independent of the file on disk: whatever
    `build_contract` finds in `mcp.py` right now must be a non-empty set of
    uniquely named tools, each carrying at least the `player_uid` seat
    parameter every tool in this server takes."""
    contract = build_contract()
    names = [tool["name"] for tool in contract["tools"]]
    assert len(names) == len(set(names)) and len(names) > 0
    for tool in contract["tools"]:
        param_names = {p["name"] for p in tool["parameters"]}
        assert "player_uid" in param_names, tool["name"]


def test_output_is_valid_json_on_disk():
    json.loads(OUTPUT.read_text())
