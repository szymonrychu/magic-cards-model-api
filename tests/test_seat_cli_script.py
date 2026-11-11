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

    # The cap is what cuts the run (no opponent passes, so it waits at move 1):
    # roomy enough that the first move finishes on a loaded runner (0.5s did not).
    assert cli(board, alice, "run", script["id"], "--max-seconds", "4") == 0
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


# ======================================================================
# FD7 (2): `plays no-change --decision` and `plays priority --game-id`.
#
# Both are offline commands (no `--uid`, no game, no API - see this module's
# own docstring and `seat_cli.build_parser`'s "plays" help), so these wire
# `seat_cli.run` straight to a monkeypatched `playscripts_admit`, the way the
# rest of this file drives `mcp.py` through a real game: proof of the CLI's
# own argument wiring, not a second copy of `admit.py`'s own git-checkout
# tests (`tests/test_plays_admit.py`).
# ======================================================================


def test_plays_no_change_by_decision_forwards_the_seq(monkeypatch, capsys, tmp_path):
    captured: dict[str, Any] = {}

    def fake_no_change(root, deck, game_id, *, play, line, reason, decision=None):
        captured.update(
            root=root,
            deck=deck,
            game_id=game_id,
            play=play,
            line=line,
            reason=reason,
            decision=decision,
        )
        return {"result": "recorded", "message": "ok", "candidate": f"decision:{decision}"}

    monkeypatch.setattr(seat_cli.playscripts_admit, "no_change", fake_no_change)

    rc = seat_cli.run(
        [
            "plays",
            "no-change",
            "--decision",
            "42",
            "--reason",
            "fine as is",
            "--deck",
            "dimir-frog",
            "--game-id",
            "g1",
            "--root",
            str(tmp_path),
        ]
    )

    assert rc == 0
    assert captured["decision"] == 42
    assert captured["play"] is None and captured["line"] is None
    assert printed(capsys)["candidate"] == "decision:42"


def test_plays_no_change_still_takes_play_or_line_exclusively_of_decision(capsys, tmp_path):
    with pytest.raises(SystemExit):
        seat_cli.run(
            [
                "plays",
                "no-change",
                "--play",
                "fetch-shock",
                "--decision",
                "1",
                "--reason",
                "x",
                "--deck",
                "dimir-frog",
                "--game-id",
                "g1",
                "--root",
                str(tmp_path),
            ]
        )
    assert "not allowed with argument" in capsys.readouterr().err


def test_plays_priority_forwards_an_explicit_game_id(monkeypatch, capsys, tmp_path):
    captured: dict[str, Any] = {}

    def fake_set_priority(root, deck, play_id, *, delta, value, game_id):
        captured.update(game_id=game_id)
        return {"result": "priority_set", "message": "ok", "id": play_id, "old": 50, "new": 55}

    monkeypatch.setattr(seat_cli.playscripts_admit, "set_priority", fake_set_priority)

    rc = seat_cli.run(
        [
            "plays",
            "priority",
            "some-play",
            "--delta",
            "5",
            "--game-id",
            "g-explicit",
            "--deck",
            "dimir-frog",
            "--root",
            str(tmp_path),
        ]
    )

    assert rc == 0
    assert captured["game_id"] == "g-explicit"


def test_plays_priority_falls_back_to_mtg_game_id_env(monkeypatch, capsys, tmp_path):
    captured: dict[str, Any] = {}
    monkeypatch.setenv("MTG_GAME_ID", "g-env")

    def fake_set_priority(root, deck, play_id, *, delta, value, game_id):
        captured.update(game_id=game_id)
        return {"result": "priority_set", "message": "ok", "id": play_id, "old": 50, "new": 55}

    monkeypatch.setattr(seat_cli.playscripts_admit, "set_priority", fake_set_priority)

    rc = seat_cli.run(
        [
            "plays",
            "priority",
            "some-play",
            "--delta",
            "5",
            "--deck",
            "dimir-frog",
            "--root",
            str(tmp_path),
        ]
    )

    assert rc == 0
    assert captured["game_id"] == "g-env"


def test_plays_priority_refuses_loudly_with_neither_flag_nor_env(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("MTG_GAME_ID", raising=False)

    def never(*_args, **_kwargs):
        raise AssertionError("set_priority ran with no game id resolved")

    monkeypatch.setattr(seat_cli.playscripts_admit, "set_priority", never)

    rc = seat_cli.run(
        [
            "plays",
            "priority",
            "some-play",
            "--delta",
            "5",
            "--deck",
            "dimir-frog",
            "--root",
            str(tmp_path),
        ]
    )

    assert rc == 1
    out = printed(capsys)
    assert out["result"] == "refused"
    assert "game-id" in out["message"] or "MTG_GAME_ID" in out["message"]


# ======================================================================
# FD10 (a): `mcp-seat script stage PATH|-` and `script run
# --body-file PATH|-`, mirroring the MCP tools' own `stage_script`/
# `run_script(body=...)` arguments. FD5's `POST .../scripts/adhoc` route
# does not exist yet (FD5 part 7), so these tests fake `script_tools.stage`
# itself rather than going through a real HTTP round trip to it - exactly
# the same substitution `tests/test_seat_cli_script.py`'s own
# `test_plays_priority_refuses_loudly_with_neither_flag_nor_env` already
# makes for `playscripts_admit.set_priority`.
# ======================================================================


def test_script_stage_reads_the_file_and_prints_id_and_warnings(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    calls: list[tuple[str, dict[str, Any], str]] = []

    def fake_stage(seat, corpora, *, body, reasoning):
        calls.append((seat.uid, body, reasoning))
        return {"id": "adhoc-1-x", "warnings": []}

    monkeypatch.setattr(seat_cli.script_tools, "stage", fake_stage)

    body = {"schema_version": 1, "id": "whatever", "moves": []}
    path = tmp_path / "play.json"
    path.write_text(json.dumps(body))

    assert cli(board, alice, "stage", str(path)) == 0
    assert printed(capsys) == {"id": "adhoc-1-x", "warnings": []}
    assert calls == [(alice, body, REASONING)]


def test_script_stage_reads_the_body_from_stdin_with_a_dash(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    body = {"schema_version": 1, "id": "whatever", "moves": []}

    def fake_stage(seat, corpora, *, body, reasoning):
        return {"id": "adhoc-1-x", "warnings": []}

    monkeypatch.setattr(seat_cli.script_tools, "stage", fake_stage)
    monkeypatch.setattr(seat_cli.sys, "stdin", __import__("io").StringIO(json.dumps(body)))

    assert cli(board, alice, "stage", "-") == 0
    assert printed(capsys) == {"id": "adhoc-1-x", "warnings": []}


def test_script_stage_prints_errors_and_fails_on_a_refusal(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")

    def fake_stage(seat, corpora, *, body, reasoning):
        return {"errors": [{"code": "card-name-in-field", "path": "play", "message": "no"}]}

    monkeypatch.setattr(seat_cli.script_tools, "stage", fake_stage)
    path = tmp_path / "play.json"
    path.write_text(json.dumps({"schema_version": 1, "id": "x", "moves": []}))

    assert cli(board, alice, "stage", str(path)) == 1
    out = printed(capsys)
    assert out["errors"][0]["code"] == "card-name-in-field"


def test_script_stage_refuses_invalid_json_without_calling_stage(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")

    def never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("stage ran with a body that was never valid JSON")

    monkeypatch.setattr(seat_cli.script_tools, "stage", never)
    path = tmp_path / "play.json"
    path.write_text("not json")

    assert cli(board, alice, "stage", str(path)) == 1
    out = capsys.readouterr().out
    assert "not valid JSON" in out


def test_script_run_with_body_file_stages_and_runs_in_one_call(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    body = {"schema_version": 1, "id": "whatever", "moves": []}

    stage_calls: list[Any] = []

    def fake_stage(seat, corpora, *, body, reasoning):
        stage_calls.append(body)
        return {"id": "adhoc-1-x", "warnings": []}

    run_calls: list[str] = []

    def fake_run_script(seat, *, script_id, **kwargs):
        run_calls.append(script_id)
        return runtime.RunResult(
            finished=True,
            script_exit=runtime.ScriptExit(
                exit=runtime.Exit.CHAIN_COMPLETE, script_id=script_id, moves_done=2, moves_total=2
            ),
            state={"pending_decision": None, "battlefield": []},
        )

    monkeypatch.setattr(seat_cli.script_tools, "stage", fake_stage)
    monkeypatch.setattr(runtime, "run_script", fake_run_script)
    path = tmp_path / "play.json"
    path.write_text(json.dumps(body))

    assert cli(board, alice, "run", "--body-file", str(path)) == 0
    out = printed(capsys)
    assert out["script_exit"]["script"] == "adhoc-1-x"
    assert out["script_exit"]["moves_done"] == 2
    assert stage_calls == [body]
    assert run_calls == ["adhoc-1-x"]


def test_script_run_refuses_both_positional_script_and_body_file(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    path = tmp_path / "play.json"
    path.write_text(json.dumps({"schema_version": 1, "id": "x", "moves": []}))

    assert cli(board, alice, "run", "drop", "--body-file", str(path)) == 1
    out = capsys.readouterr().out
    assert "exactly one" in out


def test_script_run_refuses_neither_positional_script_nor_body_file(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    wire(monkeypatch)
    alice = seat_uid(board, "alice")

    assert cli(board, alice, "run") == 1
    out = capsys.readouterr().out
    assert "body-file" in out
