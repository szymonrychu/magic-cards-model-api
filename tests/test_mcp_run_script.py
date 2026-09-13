"""`run_script` and `list_scripts`: the play-script runner and its corpus
listing, as MCP tools.

Every call here goes through a real `build_server` over the in-process app
(the `httpx.Client` patch `test_mcp_input_required.wired` uses), onto a real
board, with the corpus registered through the real `PUT .../scripts` route.
The runner itself is `playscripts/runtime.py` and is tested there; what this
file pins is the tool surface over it: the answer is action-shaped (the
narrowed `board` plus `script_exit`, never the whole `state`), every exit
arrives typed, a cut-off run hands back a `resume` token the next call
continues, an advisor gets its proposal, a disabled script or a switched-off
deployment is refused, and `inputs` (with `opposing_archetype` among them)
reaches the conditions.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_cast_cost_choices import answer
from test_mcp_input_required import call_tool, wired
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

from magic_cards_model_api import seatkit
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.playscripts import runtime
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.store import store


@pytest.fixture(autouse=True)
def _never_available_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the runner nor the listing ever pays for `available_actions`."""

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a script tool called available_actions")

    monkeypatch.setattr(seatkit, "available_actions", refuse)


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


def server_for(client: TestClient, monkeypatch: pytest.MonkeyPatch, game_id: str) -> Any:
    seats, _ = wired(client, monkeypatch, game_id)
    return seats, build_server(seats)


def run(server: Any, uid: str, script: str, **extra: Any) -> dict[str, Any]:
    arguments = {"player_uid": uid, "script": script, "reasoning": REASONING, **extra}
    return answer(call_tool(server, "run_script", arguments))


def listing(server: Any, uid: str, **extra: Any) -> dict[str, Any]:
    return answer(call_tool(server, "list_scripts", {"player_uid": uid, **extra}))


# -- the answer's shape ---------------------------------------------------------


def test_a_run_answers_with_the_narrowed_board_and_script_exit_only(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)

    payload = run(server, alice, "drop")

    assert payload["script_exit"] == {
        "exit": "chain_complete",
        "script": "drop",
        "moves_done": 1,
        "moves_total": 1,
    }, payload["script_exit"]
    assert set(payload) == {"script_exit", "board"}, list(payload)
    board_block = payload["board"]
    assert board_block["step"] == "precombat_main" and board_block["priority_holder"] == "alice"
    assert "battlefield" in board_block["omitted_sections"]
    assert [card["name"] for card in state(client, board)["battlefield"]] == ["Island"]
    assert [row["exit"] for row in finished(board)] == ["chain_complete"]


# -- exit propagation -----------------------------------------------------------


def test_a_failing_condition_propagates_as_not_applicable_with_its_condition(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, tap_island_script("float"))
    _, server = server_for(client, monkeypatch, board)

    out = run(server, alice, "float")["script_exit"]

    assert out["exit"] == "not_applicable"
    assert out["failing_condition"] == {"list": "true", "index": 0, "seen": 0}
    assert out["moves_done"] == 0 and out["moves_total"] == 1
    assert "refusal" not in out and "resume" not in out


def test_an_ambiguous_next_propagates_its_stop_reason_and_the_tied_plays(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        land_script("drop", next=["float-a", "float-b"]),
        tap_island_script("float-a"),
        tap_island_script("float-b"),
    )
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)

    out = run(server, alice, "drop")["script_exit"]

    assert out["exit"] == "ambiguous"
    assert out["stop_reason"]
    assert out["applicable_next"] == ["float-a", "float-b"]


def test_an_unknown_script_propagates_the_runners_refusal(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    _, server = server_for(client, monkeypatch, board)

    out = run(server, alice, "nope")["script_exit"]

    assert out["exit"] == "refused"
    assert "nope" in out["refusal"] and out["refusal_source"] == "runner"


def test_an_empty_reasoning_is_refused_and_still_carries_the_board(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    _, server = server_for(client, monkeypatch, board)

    payload = run(server, alice, "drop", reasoning="  ")

    assert payload["script_exit"]["exit"] == "refused"
    assert payload["script_exit"]["refusal"] == runtime.EMPTY_REASONING
    assert payload["board"]["step"] == "precombat_main"
    assert started(board) == []


def test_a_malformed_resume_is_refused_before_anything_runs(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    _, server = server_for(client, monkeypatch, board)

    for bad in ("{not json", "[1, 2]"):
        text = (
            call_tool(
                server,
                "run_script",
                {"player_uid": alice, "script": "drop", "reasoning": REASONING, "resume": bad},
            )
            .content[0]
            .text
        )
        assert "`resume`" in text, text
    assert started(board) == []


def test_an_unknown_uid_is_no_seat(client: TestClient, board: str, monkeypatch) -> None:
    _, server = server_for(client, monkeypatch, board)
    text = (
        call_tool(
            server, "run_script", {"player_uid": "nobody", "script": "drop", "reasoning": REASONING}
        )
        .content[0]
        .text
    )
    assert text == seatkit.NO_SUCH_SEAT
    text = call_tool(server, "list_scripts", {"player_uid": "nobody"}).content[0].text
    assert text == seatkit.NO_SUCH_SEAT


# -- resume ----------------------------------------------------------------------


def test_a_capped_run_hands_back_a_resume_token_the_next_call_completes(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_shock()
    register(client, board, alice, script)
    strand_board(client, board)
    _, server = server_for(client, monkeypatch, board)

    cut = run(server, alice, script["id"], max_seconds=0.5)["script_exit"]

    assert cut["exit"] is None, cut
    assert cut["script"] == script["id"]
    # B7-14: the fetch's own activation was already accepted (land sacrificed,
    # life paid) before the cap cut the run off mid-resolution, so this
    # counts as a done move even though the run itself is not finished.
    assert cut["moves_done"] == 1 and cut["moves_total"] == 1
    assert cut["stop_reason"]
    token = cut["resume"]
    assert set(runtime.RESUME_FIELDS) <= set(token)
    assert token["move_index"] == 1

    with opponent_passes(board):
        done = run(server, alice, script["id"], resume=json.dumps(token))["script_exit"]

    assert done["exit"] == "chain_complete", done
    assert done["moves_done"] == 1 and "resume" not in done
    [finish] = finished(board)
    assert finish["resumes"] == 1 and finish["run_id"] == token["run_id"]
    assert len(started(board)) == 1
    assert cards_named(state(client, board)["battlefield"], "Steam Vents")


# -- the advisor ------------------------------------------------------------------


def test_an_advisor_gets_move_one_proposed_and_the_rest_unvalidated(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    advisor = "advisor-uid"
    store.register_advisors(board, {advisor: Advisor(player="alice")})
    script = fetch_shock()
    script["moves"].append({"kind": "play_land", "card": {"select": {"types_all": ["Land"]}}})
    register(client, board, advisor, script)
    strand_board(client, board)
    before = state(client, board)
    _, server = server_for(client, monkeypatch, board)

    payload = run(server, advisor, script["id"])

    out = payload["script_exit"]
    assert out["exit"] == "advised", out
    assert out["proposal"]["proposed"] is True
    assert [entry["kind"] for entry in out["unvalidated"]] == ["play_land"]
    assert out["moves_done"] == 0 and out["moves_total"] == 2
    assert payload["board"]["you"] == "alice"
    assert state(client, board)["battlefield"] == before["battlefield"]
    [finish] = finished(board)
    assert finish["role"] == "advisor" and finish["exit"] == "advised"


def test_an_advisor_lists_the_corpus_it_registered(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    advisor = "advisor-uid"
    store.register_advisors(board, {advisor: Advisor(player="alice")})
    register(client, board, advisor, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)

    listed = listing(server, advisor)

    assert [entry["id"] for entry in listed["scripts"]] == ["drop"]
    assert listed["scripts"][0]["applies"] is True


# -- disabled scripts ---------------------------------------------------------------


def test_a_priority_zero_script_is_refused_as_disabled(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("off", priority=0))
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)

    out = run(server, alice, "off")["script_exit"]

    assert out["exit"] == "refused"
    assert out["refusal"] == "priority 0: disabled"
    assert state(client, board)["battlefield"] == []


def test_a_switched_off_deployment_refuses_the_run_with_the_servers_words(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)
    monkeypatch.setenv("SCRIPTS_ENABLED", "false")

    out = run(server, alice, "drop")["script_exit"]

    assert out["exit"] == "refused", out
    assert out["refusal_source"] == "server"
    assert "SCRIPTS_ENABLED" in out["refusal"]
    assert state(client, board)["battlefield"] == []


def test_a_disabled_script_is_listed_as_not_applying_because_of_its_priority(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("off", priority=0))
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)

    [entry] = listing(server, alice)["scripts"]

    assert entry["applies"] is False
    assert entry["failing_condition"] == {"list": "priority", "index": None, "seen": 0}


# -- inputs -------------------------------------------------------------------------


def frog_only() -> dict[str, Any]:
    return land_script("frog-only", opposing_archetypes=["dimir-frog"])


def test_opposing_archetype_passed_as_an_explicit_input_reaches_the_runner(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, frog_only())
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)

    out = run(server, alice, "frog-only", inputs={"opposing_archetype": "dimir-frog"})

    assert out["script_exit"]["exit"] == "chain_complete", out["script_exit"]
    assert finished(board)[0]["opposing_archetype_asserted"] == "dimir-frog"


def test_the_opposing_archetype_parameter_reaches_the_runner(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    # Two ids: a run that does not apply still records its start at this turn
    # and step, so the same id run again would be `already_ran` either way.
    register(client, board, alice, frog_only(), {**frog_only(), "id": "frog-only-again"})
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)

    unasserted = run(server, alice, "frog-only")["script_exit"]
    assert unasserted["exit"] == "not_applicable"
    assert unasserted["failing_condition"]["list"] == "opposing_archetypes"
    assert unasserted["failing_condition"]["seen"] == "*"

    asserted = run(server, alice, "frog-only-again", opposing_archetype="dimir-frog")
    assert asserted["script_exit"]["exit"] == "chain_complete", asserted["script_exit"]
    assert finished(board)[1]["opposing_archetype_asserted"] == "dimir-frog"


def test_a_parameter_and_an_input_naming_different_archetypes_are_refused(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, frog_only())
    _, server = server_for(client, monkeypatch, board)

    for tool, extra in (
        ("run_script", {"script": "frog-only", "reasoning": REASONING}),
        ("list_scripts", {}),
    ):
        text = (
            call_tool(
                server,
                tool,
                {
                    "player_uid": alice,
                    "opposing_archetype": "living-end",
                    "inputs": {"opposing_archetype": "dimir-frog"},
                    **extra,
                },
            )
            .content[0]
            .text
        )
        assert "opposing_archetype" in text and "living-end" in text and "dimir-frog" in text, text
    assert started(board) == []


def test_declared_inputs_and_the_supplied_ones_reach_the_conditions(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The corpus's declared inputs (`inputs`, beside `scripts`) say which
    input is required; the tool's own `inputs` supplies it."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, input_script(), {**input_script(), "id": "tempo-drop-again"})
    add_card(client, board, "Island", "alice", "hand")
    seats, server = server_for(client, monkeypatch, board)
    corpus = client.get(f"/api/games/{board}/seats/{alice}/scripts", params={"uid": alice}).json()
    seats.script_corpora[(board, alice)] = {**corpus, "inputs": [{**PREFERENCE, "required": True}]}

    missing = run(server, alice, "tempo-drop")["script_exit"]
    assert missing["exit"] == "needs_input" and missing["needs_input"] == "prefer_tempo"
    entries = listing(server, alice)["scripts"]
    assert [(e["applies"], e.get("needs_input")) for e in entries] == [(False, "prefer_tempo")] * 2

    entries = listing(server, alice, inputs={"prefer_tempo": True})["scripts"]
    assert [(e["applies"], e["failing_condition"]) for e in entries] == [(True, None)] * 2

    supplied = run(server, alice, "tempo-drop-again", inputs={"prefer_tempo": True})
    assert supplied["script_exit"]["exit"] == "chain_complete", supplied["script_exit"]


# -- list_scripts -------------------------------------------------------------------


def test_scripts_are_listed_by_priority_with_whether_each_applies_and_why_not(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        tap_island_script("float"),
        land_script("off", priority=0),
        fetch_shock(),
        land_script("drop"),
    )
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)

    listed = listing(server, alice)

    scripts = listed["scripts"]
    assert [(entry["id"], entry["priority"]) for entry in scripts] == [
        ("fetch-untapped-shock", 60),
        ("drop", 50),
        ("float", 40),
        ("off", 0),
    ]
    by_id = {entry["id"]: entry for entry in scripts}
    assert by_id["drop"]["applies"] is True and by_id["drop"]["failing_condition"] is None
    assert by_id["float"]["failing_condition"] == {"list": "true", "index": 0, "seen": 0}
    assert by_id["fetch-untapped-shock"]["failing_condition"]["list"] == "true"
    assert listed["applicable"] == ["drop"]
    assert listed["runtime_conflict"] is False
    assert all(entry["runtime_conflict"] is False for entry in scripts)
    assert started(board) == [], "listing records no run"


def test_a_script_off_its_step_is_listed_with_the_when_that_stops_it(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_turn_position(game, Step.UPKEEP, "alice")
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))
    _, server = server_for(client, monkeypatch, game)

    [entry] = listing(server, alice)["scripts"]

    assert entry["applies"] is False
    assert entry["failing_condition"]["list"] == "when"
    assert entry["failing_condition"]["seen"]["step"] == "upkeep"


def test_scripts_list_their_static_conflicts_and_the_runtime_flag(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop-a"), land_script("drop-b", priority=45))
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)

    listed = listing(server, alice)

    a, b = listed["scripts"]
    for entry in (a, b):
        [conflict] = entry["conflicts"]
        assert conflict["code"] == "play-conflict"
        assert conflict["plays"] == ["drop-a", "drop-b"]
        assert conflict["dimensions"] and conflict["message"]
        assert entry["runtime_conflict"] is True
    assert listed["applicable"] == ["drop-a", "drop-b"]
    assert listed["runtime_conflict"] is True


def test_exclusive_scripts_list_no_conflict_and_inputs_decide_which_applies(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    not_frog = land_script(
        "not-frog",
        priority=40,
        true=[
            {"kind": "land_drop_available", "value": True},
            {"kind": "input", "name": "opposing_archetype", "op": "ne", "value": "dimir-frog"},
        ],
    )
    register(client, board, alice, frog_only(), not_frog)
    add_card(client, board, "Island", "alice", "hand")
    _, server = server_for(client, monkeypatch, board)

    frog = listing(server, alice, inputs={"opposing_archetype": "dimir-frog"})
    assert [entry["conflicts"] for entry in frog["scripts"]] == [[], []]
    assert frog["applicable"] == ["frog-only"]
    not_frog_entry = frog["scripts"][1]
    assert not_frog_entry["failing_condition"] == {"list": "true", "index": 1, "seen": "dimir-frog"}

    other = listing(server, alice, opposing_archetype="living-end")
    assert other["applicable"] == ["not-frog"]
    assert other["runtime_conflict"] is False


def test_next_conflicts_are_listed_on_every_script_they_involve(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        land_script("drop", next=["float-a", "float-b"]),
        tap_island_script("float-a", priority=0),
        tap_island_script("float-b", priority=0),
    )
    _, server = server_for(client, monkeypatch, board)

    by_id = {entry["id"]: entry for entry in listing(server, alice)["scripts"]}

    for script_id in ("drop", "float-a", "float-b"):
        codes = [conflict["code"] for conflict in by_id[script_id]["conflicts"]]
        assert codes == ["next-conflict"], (script_id, by_id[script_id]["conflicts"])
        assert by_id[script_id]["conflicts"][0]["plays"] == ["drop", "float-a", "float-b"]


def test_nothing_registered_is_said_plainly(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    _, server = server_for(client, monkeypatch, board)

    text = call_tool(server, "list_scripts", {"player_uid": alice}).content[0].text
    assert "no scripts are registered" in text, text
    out = run(server, alice, "drop")["script_exit"]
    assert out["exit"] == "refused" and "no scripts are registered" in out["refusal"]
