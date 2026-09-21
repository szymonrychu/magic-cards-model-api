"""The CLI twin of `tests/test_script_hint.py`: the same `applicable`/
`armable`/`arm_call` hint (`script_tools.applicable_hint`), now attached to
`seat_cli.py`'s own action and `wait` answers too - S1 gave every MCP answer
`scripts: {...}` via `mcp._attach_script_hint`, but the CLI transport
(`mcp-seat`, the one `src/mtg_agent`'s driver actually drives) never got it at
all. The one thing genuinely different here: `call`/`arm_call` render as
`mcp-seat` command lines (`cli=True`), never the MCP `run_script(...)`/
`pass_until(...)` call syntax.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import add_card, force_turn_position
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_front_end_parity import wire
from test_playscript_runtime import REASONING, land_script, register, seat_uid, tap_island_script

from magic_cards_model_api import seat_cli
from magic_cards_model_api.playscripts import runtime


def cli(game_id: str, uid: str, *argv: str, reasoning: str | None = REASONING) -> int:
    head = ["--uid", uid, "--game", game_id]
    if reasoning is not None:
        head += ["--reasoning", reasoning]
    return seat_cli.run([*head, *argv])


def printed(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def test_a_cli_action_answer_hints_the_mcp_seat_script_run_call(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """An action that goes through the shared `act()` closure (`play-land`
    holds priority under CR 116.3, so alice still holds it afterward) carries
    the same `applicable` hint an MCP `run_action`-backed answer would, but
    `call` is an `mcp-seat script run` command line, never `run_script(`."""
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    wire(monkeypatch)
    alice = seat_uid(game, "alice")
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "hand")
    register(client, game, alice, tap_island_script("float"))

    assert cli(game, alice, "play-land", "Mountain") == 0
    out = printed(capsys)

    assert "scripts" in out, out
    [entry] = out["scripts"]["applicable"]
    assert entry["id"] == "float"
    assert "conflict" not in entry
    # API-4: `why` (here, `float`'s own play has no `note`/`intent`, so it
    # falls back to its bare `name`) bolts straight through this transport
    # too - `_attach_script_hint` forwards `applicable_hint`'s whole answer
    # unchanged, and this CLI's own JSON output prints it as-is.
    assert entry["why"] == "Float blue"
    assert entry["call"] == (
        f'mcp-seat script run float --reasoning "{runtime.REASONING_PLACEHOLDER}" '
        "--opposing-archetype *"
    )
    assert "run_script(" not in entry["call"]


def test_a_cli_wait_answer_hints_the_mcp_seat_pass_until_arm_call(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """S2's `armable`/`arm_call`, on the CLI `wait` answer: the opponent's own
    end step is one of `armable`'s two windows, so this seat's land drop -
    off its `when` right now - is offered to arm for the wait ahead, as an
    `mcp-seat pass-until` command line rather than `pass_until(`."""
    force_turn_position(game, Step.END_STEP, "bob")
    wire(monkeypatch)
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")

    assert cli(game, alice, "wait", reasoning=None) == 0
    out = printed(capsys)

    assert "scripts" in out, out
    assert "applicable" not in out["scripts"], out["scripts"]
    [entry] = out["scripts"]["armable"]
    assert entry["id"] == "drop"
    assert out["scripts"]["arm_call"] == (
        f"mcp-seat pass-until precombat_main --whose-turn mine --arm drop "
        f'--arm-reasoning "{runtime.REASONING_PLACEHOLDER}" --opposing-archetype * '
        "--max-seconds 25.0"
    )
    assert "pass_until(" not in out["scripts"]["arm_call"]
    assert "wait_for_priority(" not in out["scripts"]["arm_call"]
