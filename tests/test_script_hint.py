"""T1: a script applicability hint on every wake/action answer.

The measured problem: a seat cracks a fetch land by hand (`play_land` then
`activate`) because nothing in its wake/action answers says a registered
script applies right now - finding out costs a separate `list_scripts` call
the seat never makes. Every MCP answer that reaches `mcp._tool_result` for a
seat now carries `scripts: {"applicable": [...]}` when at least one
registered script applies, using the same applicability judgement
`list_scripts` makes, built from the state the answer already carries - never
an extra board fetch.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import add_card
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_mcp_input_required import call_tool
from test_playscript_runtime import (
    fetch_shock,
    land_script,
    register,
    seat_uid,
    strand_board,
    wired,
)

from magic_cards_model_api import seatkit
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.playscripts import script_tools


@pytest.fixture(autouse=True)
def _never_available_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("computing the hint called available_actions")

    monkeypatch.setattr(seatkit, "available_actions", refuse)


def server_for(monkeypatch: pytest.MonkeyPatch, game_id: str) -> Any:
    seats = wired(monkeypatch, game_id)
    return seats, build_server(seats)


def wait(server: Any, uid: str) -> dict[str, Any]:
    result = call_tool(server, "wait_for_priority", {"player_uid": uid})
    return json.loads(result.content[0].text)


def force_precombat_main(game_id: str) -> None:
    from conftest import force_turn_position

    force_turn_position(game_id, Step.PRECOMBAT_MAIN, "alice")


def test_a_fetch_land_on_the_battlefield_hints_the_run_script_call(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    script = fetch_shock()
    register(client, game, alice, script)
    strand_board(client, game)
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" in answer, answer
    [entry] = answer["scripts"]["applicable"]
    assert entry["id"] == script["id"]
    assert entry["name"] == script["name"]
    assert "conflict" not in entry
    assert entry["call"] == (
        f'run_script(player_uid="{alice}", script="{script["id"]}", '
        'reasoning="<say why>", opposing_archetype="*")'
    )


def test_the_hint_uses_the_archetype_last_asserted_to_list_scripts(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A script keyed to one archetype does not apply under the `*` default
    - but once this seat has asserted that archetype to `list_scripts` (or
    `run_script`), the hint judges against it too, and the `call` template
    names it."""
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    frog_only = land_script("frog-only", opposing_archetypes=["dimir-frog"])
    register(client, game, alice, frog_only)
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    assert "scripts" not in wait(server, alice)

    listed = call_tool(
        server, "list_scripts", {"player_uid": alice, "opposing_archetype": "dimir-frog"}
    )
    assert "dimir-frog" in listed.content[0].text

    answer = wait(server, alice)

    [entry] = answer["scripts"]["applicable"]
    assert entry["id"] == "frog-only"
    assert 'opposing_archetype="dimir-frog"' in entry["call"]


def test_the_wrong_step_carries_no_hint(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import force_turn_position

    force_turn_position(game, Step.UPKEEP, "alice")
    alice = seat_uid(game, "alice")
    script = fetch_shock()
    register(client, game, alice, script)
    strand_board(client, game)
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" not in answer, answer


def test_no_corpus_registered_carries_no_hint(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" not in answer, answer


def test_scripts_disabled_server_side_carries_no_hint(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, fetch_shock())
    strand_board(client, game)
    _, server = server_for(monkeypatch, game)
    monkeypatch.setenv("SCRIPTS_ENABLED", "false")

    answer = wait(server, alice)

    assert "scripts" not in answer, answer


def test_two_conflicting_plays_are_flagged_and_carry_no_call(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop-a"), land_script("drop-b", priority=45))
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    entries = {entry["id"]: entry for entry in answer["scripts"]["applicable"]}
    assert set(entries) == {"drop-a", "drop-b"}
    for entry in entries.values():
        assert entry["conflict"] is True
        assert "call" not in entry


def test_the_applicable_list_is_capped_and_the_answer_stays_bounded(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    scripts = [land_script(f"drop-{i}", priority=50 - i) for i in range(8)]
    register(client, game, alice, *scripts)
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    applicable = answer["scripts"]["applicable"]
    assert len(applicable) == script_tools.MAX_HINT_SCRIPTS
    assert len(json.dumps(answer)) < 20_000


def test_a_script_needing_contents_the_hint_never_fetched_is_skipped_not_crashed(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A play whose condition reads the viewer's own library contents
    (`conditions.needs_contents`) normally needs `include=contents` - `listing`
    fetches it, but the hint never fetches anything past what the answer
    already carries, so this play must come back "not applying" rather than
    raise `conditions.ContentsRequired`."""
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    needs_contents = land_script(
        "needs-library-count",
        true=[{"kind": "count", "zone": "library", "side": "me", "op": ">=", "n": 1, "select": {}}],
    )
    register(client, game, alice, needs_contents)
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" not in answer, answer


def test_no_extra_state_fetch_is_made_computing_the_hint(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, fetch_shock())
    strand_board(client, game)
    seats, server = server_for(monkeypatch, game)
    seat = seats.seat_for(alice)
    assert seat is not None
    calls = {"n": 0}
    real_fetch_state = seat.__class__.fetch_state

    def counting(self: Any, *a: Any, **kw: Any) -> Any:
        calls["n"] += 1
        return real_fetch_state(self, *a, **kw)

    monkeypatch.setattr(seat.__class__, "fetch_state", counting)

    wait(server, alice)

    # `block_until_wake` itself fetches the board once to check the wake
    # condition; the hint must not add a second fetch on top of it.
    assert calls["n"] == 1, f"the hint cost {calls['n']} state fetches, expected exactly 1"
