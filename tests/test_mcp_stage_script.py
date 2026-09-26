"""FD10 (a): `stage_script` and `run_script(body=...)` as MCP tools.

Both are thin wrappers over `playscripts.script_tools.stage`/`run`, the same
functions `mcp-seat script stage`/`script run --body-file` call. FD5's
`POST .../scripts/adhoc` route that `script_tools.stage` posts to does not
exist yet (FD5 part 7), so `script_tools.stage` is faked here; what this file
pins is the tool surface: the arguments reach `stage` unchanged, its answer
comes back as-is, and an inline `run_script` runs the id `stage` assigned on
a real board.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_mcp_input_required import call_tool, wired
from test_playscript_runtime import REASONING, land_script, register, seat_uid

from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.playscripts import script_tools


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


def server_for(client: TestClient, monkeypatch: pytest.MonkeyPatch, game_id: str) -> Any:
    seats, _ = wired(client, monkeypatch, game_id)
    return build_server(seats)


def text(result: Any) -> str:
    return result.content[0].text


def fake_stage(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def stage(seat, corpora, *, body, reasoning):
        calls.append({"uid": seat.uid, "body": body, "reasoning": reasoning})
        return answer

    monkeypatch.setattr(script_tools, "stage", stage)
    return calls


def test_stage_script_passes_body_and_reasoning_and_answers_id_and_warnings(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    server = server_for(client, monkeypatch, board)
    staged = {"id": "adhoc-1-alice", "warnings": [{"code": "play-conflict"}]}
    calls = fake_stage(monkeypatch, staged)
    body = land_script("tmp")

    result = call_tool(
        server, "stage_script", {"player_uid": alice, "body": body, "reasoning": REASONING}
    )

    assert json.loads(text(result)) == staged
    assert calls == [{"uid": alice, "body": body, "reasoning": REASONING}]


def test_stage_script_returns_schema_and_lint_errors_as_is(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    server = server_for(client, monkeypatch, board)
    refused = {"errors": [{"code": "card-name-in-field", "path": "moves.0", "message": "x"}]}
    fake_stage(monkeypatch, refused)

    result = call_tool(
        server,
        "stage_script",
        {"player_uid": alice, "body": land_script("tmp"), "reasoning": REASONING},
    )

    assert json.loads(text(result)) == refused


def test_stage_script_returns_a_bare_refusal_string_as_is(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    server = server_for(client, monkeypatch, board)
    fake_stage(monkeypatch, "scripts are switched off on this deployment")

    result = call_tool(
        server,
        "stage_script",
        {"player_uid": alice, "body": land_script("tmp"), "reasoning": REASONING},
    )

    assert text(result) == "scripts are switched off on this deployment"


def test_stage_script_for_an_unknown_uid_is_no_seat(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = server_for(client, monkeypatch, board)
    calls = fake_stage(monkeypatch, {"id": "never", "warnings": []})

    result = call_tool(
        server,
        "stage_script",
        {"player_uid": "no-such-uid", "body": land_script("tmp"), "reasoning": REASONING},
    )

    assert "no-such-uid" in text(result) or "seat" in text(result).lower()
    assert calls == []


def test_run_script_with_a_body_stages_it_and_runs_the_assigned_id(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    server = server_for(client, monkeypatch, board)
    calls = fake_stage(monkeypatch, {"id": "drop", "warnings": []})
    body = land_script("tmp")

    result = call_tool(
        server, "run_script", {"player_uid": alice, "body": body, "reasoning": REASONING}
    )

    payload = json.loads(text(result))
    assert payload["script_exit"]["exit"] == "chain_complete", payload
    assert payload["script_exit"]["script"] == "drop"
    assert calls == [{"uid": alice, "body": body, "reasoning": REASONING}]
    assert [card["name"] for card in state(client, board)["battlefield"]] == ["Island"]


def test_run_script_returns_the_stage_steps_errors_without_running(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    server = server_for(client, monkeypatch, board)
    refused = {"errors": [{"code": "repeat-bound", "path": "moves.0", "message": "x"}]}
    fake_stage(monkeypatch, refused)

    result = call_tool(
        server,
        "run_script",
        {"player_uid": alice, "body": land_script("tmp"), "reasoning": REASONING},
    )

    assert json.loads(text(result)) == refused


@pytest.mark.parametrize("with_script", [True, False])
def test_run_script_takes_exactly_one_of_script_or_body(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, with_script: bool
) -> None:
    alice = seat_uid(board, "alice")
    server = server_for(client, monkeypatch, board)
    calls = fake_stage(monkeypatch, {"id": "never", "warnings": []})
    arguments: dict[str, Any] = {"player_uid": alice, "reasoning": REASONING}
    if with_script:
        arguments |= {"script": "drop", "body": land_script("tmp")}

    result = call_tool(server, "run_script", arguments)

    assert "exactly one of `script`" in text(result)
    assert calls == []
