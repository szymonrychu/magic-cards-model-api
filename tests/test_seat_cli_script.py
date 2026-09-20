"""`mcp-seat script run` and `mcp-seat script list`: the play-script runner
and its corpus listing, from the shell.

The CLI twin of `tests/test_mcp_run_script.py`, on the same real boards and
the same real routes. It prints what every other CLI command prints - the
whole answer for a human who can scroll, `state` included - and its argument
names are the MCP tools' own (`tests/test_front_end_parity.py` pins that).
`--input NAME=VALUE` is repeatable, and a VALUE that reads as JSON (`true`,
`3`, `"3"`) is that JSON value, so a CLI seat supplies exactly the typed
inputs an MCP seat passes as `inputs`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_front_end_parity import wire
from test_playscript_runtime import (
    PREFERENCE,
    REASONING,
    cards_named,
    fetch_shock,
    finished,
    input_script,
    land_script,
    opponent_passes,
    register,
    seat_uid,
    started,
    strand_board,
    tap_island_script,
)

from magic_cards_model_api import seat_cli, seatkit
from magic_cards_model_api.playscripts import runtime
from magic_cards_model_api.seatkit import Seats


@pytest.fixture(autouse=True)
def _never_available_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a script command called available_actions")

    monkeypatch.setattr(seatkit, "available_actions", refuse)


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


def cli(game_id: str, uid: str, *argv: str, reasoning: str | None = REASONING) -> int:
    head = ["--uid", uid, "--game", game_id]
    if reasoning is not None:
        head += ["--reasoning", reasoning]
    return seat_cli.run([*head, "script", *argv])


def printed(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


# -- script run -----------------------------------------------------------------


def test_script_run_prints_the_exit_and_the_whole_state(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")

    assert cli(board, alice, "run", "drop") == 0
    out = printed(capsys)

    assert out["script_exit"] == {
        "exit": "chain_complete",
        "script": "drop",
        "moves_done": 1,
        "moves_total": 1,
    }
    assert set(out) == {"script_exit", "state"}
    assert [card["name"] for card in out["state"]["battlefield"]] == ["Island"]
    assert [row["exit"] for row in finished(board)] == ["chain_complete"]


def test_script_run_records_invoked_via_cli(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """S1 T3: `mcp-seat script run` reports its own runs as `"cli"`, so a
    seat (and `eval_report.py`) can tell a run it made itself apart from one
    an operator staged from this same command."""
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")

    assert cli(board, alice, "run", "drop") == 0

    [row] = finished(board)
    assert row["invoked_via"] == "cli"


def test_script_run_propagates_a_failing_condition(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, tap_island_script("float"))

    assert cli(board, alice, "run", "float") == 0
    out = printed(capsys)["script_exit"]

    assert out["exit"] == "not_applicable"
    assert out["failing_condition"] == {"list": "true", "index": 0, "seen": 0}


def test_script_run_without_reasoning_is_the_runners_refusal(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))

    assert cli(board, alice, "run", "drop", reasoning=None) == 0
    out = printed(capsys)["script_exit"]

    assert out["exit"] == "refused" and out["refusal"] == runtime.EMPTY_REASONING
    assert started(board) == []


def test_script_run_no_wait_leaves_a_script_off_its_step_not_applicable(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    force_turn_position(game, Step.UPKEEP, "alice")
    wire(monkeypatch)
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))

    assert cli(game, alice, "run", "drop", "--no-wait") == 0
    out = printed(capsys)["script_exit"]

    assert out["exit"] == "not_applicable"
    assert out["failing_condition"]["list"] == "when"
    assert state(client, game)["step"] == "upkeep", "--no-wait passed nothing"


def test_script_run_resume_round_trip(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    script = fetch_shock()
    register(client, board, alice, script)
    strand_board(client, board)

    assert cli(board, alice, "run", script["id"], "--max-seconds", "0.5") == 0
    cut = printed(capsys)["script_exit"]
    assert cut["exit"] is None and cut["resume"]["move_index"] == 1, cut

    with opponent_passes(board):
        rc = cli(board, alice, "run", script["id"], "--resume", json.dumps(cut["resume"]))
    assert rc == 0
    done = printed(capsys)["script_exit"]

    assert done["exit"] == "chain_complete", done
    [finish] = finished(board)
    assert finish["resumes"] == 1 and finish["run_id"] == cut["resume"]["run_id"]
    assert cards_named(state(client, board)["battlefield"], "Steam Vents")


def test_script_run_refuses_a_resume_that_is_not_a_token(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))

    assert cli(board, alice, "run", "drop", "--resume", "{not json") == 1
    assert "`resume`" in capsys.readouterr().out
    assert started(board) == []


# -- inputs ----------------------------------------------------------------------


def test_input_values_parse_as_json_and_fall_back_to_text() -> None:
    assert seat_cli.parse_inputs(
        [
            "prefer_tempo=true",
            "turns=3",
            'label="3"',
            "opposing_archetype=dimir-frog",
            "note=a=b",
        ]
    ) == {
        "prefer_tempo": True,
        "turns": 3,
        "label": "3",
        "opposing_archetype": "dimir-frog",
        "note": "a=b",
    }
    assert seat_cli.parse_inputs(None) is None
    assert seat_cli.parse_inputs([]) is None


@pytest.mark.parametrize("bad", ["no-equals", "=true", " =1"])
def test_a_malformed_input_is_refused(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys, bad: str
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))

    assert cli(board, alice, "run", "drop", "--input", bad) == 1
    assert "--input" in capsys.readouterr().out
    assert started(board) == []


def test_an_input_given_twice_is_refused(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))

    rc = cli(board, alice, "list", "--input", "prefer_tempo=true", "--input", "prefer_tempo=false")
    assert rc == 1
    assert "prefer_tempo" in capsys.readouterr().out


def test_opposing_archetype_as_an_explicit_input_reaches_the_runner(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    frog = land_script("frog-only", opposing_archetypes=["dimir-frog"])
    register(client, board, alice, frog)
    add_card(client, board, "Island", "alice", "hand")

    assert cli(board, alice, "run", "frog-only", "--input", "opposing_archetype=dimir-frog") == 0
    out = printed(capsys)["script_exit"]

    assert out["exit"] == "chain_complete", out
    assert finished(board)[0]["opposing_archetype_asserted"] == "dimir-frog"


def test_the_opposing_archetype_flag_and_a_different_input_are_refused(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("frog-only", opposing_archetypes=["dimir-frog"]))

    for command in (["run", "frog-only"], ["list"]):
        rc = cli(
            board,
            alice,
            *command,
            "--opposing-archetype",
            "living-end",
            "--input",
            "opposing_archetype=dimir-frog",
        )
        assert rc == 1
        said = capsys.readouterr().out
        assert "living-end" in said and "dimir-frog" in said
    assert started(board) == []


def test_declared_and_supplied_inputs_reach_the_conditions(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, input_script(), {**input_script(), "id": "tempo-drop-again"})
    add_card(client, board, "Island", "alice", "hand")
    corpus = client.get(f"/api/games/{board}/seats/{alice}/scripts", params={"uid": alice}).json()
    declared = {**corpus, "inputs": [{**PREFERENCE, "required": True}]}

    def seeded(api: str, game_id: str | None) -> Seats:
        seats = Seats(api, game_id)
        seats.script_corpora[(board, alice)] = declared
        return seats

    monkeypatch.setattr(seat_cli, "Seats", seeded)

    assert cli(board, alice, "run", "tempo-drop") == 0
    missing = printed(capsys)["script_exit"]
    assert missing["exit"] == "needs_input" and missing["needs_input"] == "prefer_tempo"

    assert cli(board, alice, "list", "--input", "prefer_tempo=true") == 0
    entries = printed(capsys)["scripts"]
    assert [(e["applies"], e["failing_condition"]) for e in entries] == [(True, None)] * 2

    assert cli(board, alice, "run", "tempo-drop-again", "--input", "prefer_tempo=true") == 0
    assert printed(capsys)["script_exit"]["exit"] == "chain_complete"


# -- script list -----------------------------------------------------------------


def test_script_list_orders_by_priority_with_applies_and_failing_condition(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        tap_island_script("float"),
        land_script("off", priority=0),
        land_script("drop"),
    )
    add_card(client, board, "Island", "alice", "hand")

    assert cli(board, alice, "list", reasoning=None) == 0
    listed = printed(capsys)

    assert [entry["id"] for entry in listed["scripts"]] == ["drop", "float", "off"]
    by_id = {entry["id"]: entry for entry in listed["scripts"]}
    assert by_id["drop"]["applies"] is True
    assert by_id["float"]["failing_condition"] == {"list": "true", "index": 0, "seen": 0}
    assert by_id["off"]["failing_condition"] == {"list": "priority", "index": None, "seen": 0}
    assert listed["applicable"] == ["drop"] and listed["runtime_conflict"] is False


def test_script_list_carries_static_conflicts_and_the_runtime_flag(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop-a"), land_script("drop-b", priority=45))
    add_card(client, board, "Island", "alice", "hand")

    assert cli(board, alice, "list") == 0
    listed = printed(capsys)

    for entry in listed["scripts"]:
        assert [c["code"] for c in entry["conflicts"]] == ["play-conflict"]
        assert entry["conflicts"][0]["plays"] == ["drop-a", "drop-b"]
        assert entry["runtime_conflict"] is True
    assert listed["runtime_conflict"] is True


def test_script_list_takes_the_archetype_flag_and_input(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("frog-only", opposing_archetypes=["dimir-frog"]))
    add_card(client, board, "Island", "alice", "hand")

    assert cli(board, alice, "list") == 0
    assert printed(capsys)["applicable"] == []
    assert cli(board, alice, "list", "--opposing-archetype", "dimir-frog") == 0
    assert printed(capsys)["applicable"] == ["frog-only"]
    assert cli(board, alice, "list", "--input", "opposing_archetype=dimir-frog") == 0
    assert printed(capsys)["applicable"] == ["frog-only"]
