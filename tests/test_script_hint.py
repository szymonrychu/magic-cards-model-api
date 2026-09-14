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
from conftest import add_card, force_turn_position
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_cast_cost_choices import answer
from test_mcp_input_required import call_tool
from test_playscript_end_to_end import GUIDES_REVISION, fetch_shock_board, play_of
from test_playscript_runtime import (
    PREFERENCE,
    REASONING,
    fetch_shock,
    input_script,
    land_script,
    register,
    seat_uid,
    strand_board,
    tap_island_script,
    wired,
)

from magic_cards_model_api import seatkit
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.playscripts import runtime, script_tools


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
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, "alice")


def counting_fetch_state(monkeypatch: pytest.MonkeyPatch, seat: Any) -> dict[str, int]:
    """Patch `seat`'s whole class so every `Seat.fetch_state` call, on any
    `Seat` instance this test mints, is counted - `seat_for` remakes a fresh
    `Seat` per tool call, so patching one instance would miss the rest."""
    calls = {"n": 0}
    real_fetch_state = seat.__class__.fetch_state

    def counting(self: Any, *a: Any, **kw: Any) -> Any:
        calls["n"] += 1
        return real_fetch_state(self, *a, **kw)

    monkeypatch.setattr(seat.__class__, "fetch_state", counting)
    return calls


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


def test_the_wrong_step_carries_no_applicable_hint(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2: this seat's own upkeep is one of `armable`'s own two windows, so
    a script off its `when` right now (but coming up again later this same
    turn) is offered there instead - `applicable` itself still carries
    nothing, since nothing applies to run right now."""
    force_turn_position(game, Step.UPKEEP, "alice")
    alice = seat_uid(game, "alice")
    script = fetch_shock()
    register(client, game, alice, script)
    strand_board(client, game)
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" in answer, answer
    assert "applicable" not in answer["scripts"], answer["scripts"]
    [entry] = answer["scripts"]["armable"]
    assert entry["id"] == script["id"]


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


def test_a_contents_needing_play_whose_when_does_not_match_costs_no_extra_fetch(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A play whose condition reads the viewer's own library contents
    (`conditions.needs_contents`) is only worth an extra fetch once its
    priority, archetype and `when` already match (see the fixture-driven
    test below) - one whose board position is wrong right now costs no
    extra fetch even though S2's `armable` still offers it (upkeep is one of
    `armable`'s own windows): `armable` never evaluates `true`/`false` at
    all, so it never reaches the contents check either."""
    force_turn_position(game, Step.UPKEEP, "alice")
    alice = seat_uid(game, "alice")
    needs_contents = land_script(
        "needs-library-count",
        true=[{"kind": "count", "zone": "library", "side": "me", "op": ">=", "n": 1, "select": {}}],
    )
    register(client, game, alice, needs_contents)
    add_card(client, game, "Island", "alice", "hand")
    seats, server = server_for(monkeypatch, game)
    seat = seats.seat_for(alice)
    assert seat is not None
    calls = counting_fetch_state(monkeypatch, seat)

    answer = wait(server, alice)

    assert "scripts" in answer, answer
    [entry] = answer["scripts"]["armable"]
    assert entry["id"] == "needs-library-count"
    assert calls["n"] == 1, f"a play whose when never matched still cost {calls['n']} state fetches"


def test_a_fetch_untapped_shock_play_needing_contents_is_offered_with_one_extra_fetch(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured bug (S1 review finding 1): 22 of 41 real plays - every
    `fetch-untapped-shock` variant among them - read the viewer's own library
    contents (`conditions.needs_contents`), and the old `_hint_judged` caught
    `conditions.ContentsRequired` and silently marked every one of them not
    applicable, so the hint never offered the exact fetch play a seat was
    measured cracking by hand instead. Once a play's priority, archetype and
    `when` already match, the hint now pays exactly one extra
    `include=contents` fetch and judges it for real."""
    alice = seat_uid(game, "alice")
    play = play_of("boros-aggro", "fetch-untapped-shock")
    body = {
        "deck_slug": "boros-aggro",
        "guides_revision": GUIDES_REVISION,
        "scripts": [play],
        "inputs": [],
    }
    response = client.put(
        f"/api/games/{game}/seats/{alice}/scripts", params={"uid": alice}, json=body
    )
    assert response.status_code == 200, response.text
    fetch_shock_board(client, game)
    seats, server = server_for(monkeypatch, game)
    seat = seats.seat_for(alice)
    assert seat is not None
    calls = counting_fetch_state(monkeypatch, seat)

    answer = wait(server, alice)

    assert "scripts" in answer, answer
    [entry] = answer["scripts"]["applicable"]
    assert entry["id"] == "fetch-untapped-shock"
    assert calls["n"] == 2, (
        f"expected exactly one wake fetch plus one contents fetch, got {calls['n']}"
    )


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
    calls = counting_fetch_state(monkeypatch, seat)

    wait(server, alice)

    # `block_until_wake` itself fetches the board once to check the wake
    # condition; the hint must not add a second fetch on top of it.
    assert calls["n"] == 1, f"the hint cost {calls['n']} state fetches, expected exactly 1"


def test_a_hint_computation_that_raises_leaves_the_wait_answer_intact(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 review finding 2: an exception out of `applicable_hint` (an HTTP
    error, or anything `conditions.evaluate`/`static_conflicts`/`lint` can
    raise) must never replace or break an answer whose action already
    landed - it is caught, logged once, and the `scripts` key is simply left
    off."""
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, fetch_shock())
    strand_board(client, game)
    _, server = server_for(monkeypatch, game)

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(script_tools, "applicable_hint", boom)

    reply = wait(server, alice)

    assert "scripts" not in reply
    assert "priority_holder" in reply, reply


def test_a_hint_computation_that_raises_leaves_the_run_script_answer_intact(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(script_tools, "applicable_hint", boom)

    payload = answer(
        call_tool(
            server,
            "run_script",
            {"player_uid": alice, "script": "drop", "reasoning": REASONING},
        )
    )

    assert "scripts" not in payload
    assert payload["script_exit"]["exit"] == "chain_complete", payload["script_exit"]


def test_a_seat_not_holding_priority_gets_no_hint(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 review finding 4a: `runtime._drive` requires `state.priority_holder
    == seat.player` before it ever sends a script's first move, so a hint
    computed while another player holds priority would offer a call the
    runner would only park in `_wait`, not the immediate action the hint's
    wording promises."""
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, fetch_shock())
    strand_board(client, game)
    seats, _ = server_for(monkeypatch, game)
    seat = seats.seat_for(alice)
    assert seat is not None
    not_holding = {**seat.fetch_state(), "priority_holder": "bob"}

    hint = script_tools.applicable_hint(seat, seats.script_corpora, not_holding)

    assert hint is None


def test_remembered_inputs_from_run_script_are_used_and_offered_in_the_call(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 review finding 4b: the inputs a seat last supplied to `run_script`
    are remembered next to the archetype it last asserted, judged with, and
    named in the offered `call` when non-empty - a script whose condition
    reads a custom input the seat never supplied still needs_input and stays
    omitted."""
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    dummy = land_script("dummy-seed", opposing_archetypes=["nonexistent-matchup"])
    register(client, game, alice, input_script(), dummy, inputs=[{**PREFERENCE, "required": True}])
    _, server = server_for(monkeypatch, game)

    assert "scripts" not in wait(server, alice), "tempo-drop needs_input before any input is given"

    call_tool(
        server,
        "run_script",
        {
            "player_uid": alice,
            "script": "dummy-seed",
            "reasoning": REASONING,
            "inputs": {"prefer_tempo": True},
        },
    )

    reply = wait(server, alice)

    [entry] = reply["scripts"]["applicable"]
    assert entry["id"] == "tempo-drop"
    assert 'inputs={"prefer_tempo": true}' in entry["call"], entry["call"]


def test_a_script_this_process_already_ran_is_not_reoffered_at_the_same_position(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 review finding 4c: a script this MCP process ran (any exit) is not
    re-offered at the same `(turn_number, step)`, even when the board still
    satisfies its conditions - two untapped Islands mean one `activate` still
    leaves the count condition true, so this proves the suppression is the
    run record, not the board."""
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, tap_island_script("float"))
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "battlefield")
    _, server = server_for(monkeypatch, game)

    before = wait(server, alice)
    assert [entry["id"] for entry in before["scripts"]["applicable"]] == ["float"]

    ran = answer(
        call_tool(
            server,
            "run_script",
            {"player_uid": alice, "script": "float", "reasoning": REASONING},
        )
    )
    assert ran["script_exit"]["exit"] == "chain_complete", ran["script_exit"]

    after = wait(server, alice)

    assert "scripts" not in after, after


def test_the_hint_never_computes_static_conflicts(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 review finding 5: `static_conflicts` runs `plays check`'s conflict
    lint over the whole corpus - expensive, and its result
    (`entry["conflicts"]`) is never read out of the hint's own items, so the
    hint must never pay for it."""
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, fetch_shock())
    strand_board(client, game)
    _, server = server_for(monkeypatch, game)

    def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("the hint computed static_conflicts")

    monkeypatch.setattr(script_tools, "static_conflicts", boom)

    reply = wait(server, alice)

    assert "scripts" in reply, reply


def test_normalised_plays_are_cached_on_the_corpus_not_revalidated_per_answer(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1 review finding 5: `runtime._normalised` validates a play against
    the `Play` schema - not free, and the same registered docs are judged on
    every single wake/action answer, so the hint must validate each once and
    cache the normalised form on the corpus rather than repeat it per call."""
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, fetch_shock())
    strand_board(client, game)
    _, server = server_for(monkeypatch, game)
    calls = {"n": 0}
    real_normalised = runtime._normalised

    def counting(raw: Any) -> Any:
        calls["n"] += 1
        return real_normalised(raw)

    monkeypatch.setattr(runtime, "_normalised", counting)

    wait(server, alice)
    wait(server, alice)

    assert calls["n"] == 1, f"the corpus's one script was normalised {calls['n']} times"


def test_a_long_script_name_is_truncated_in_the_hint(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    long_name = "Crack the fetch " * 20
    register(client, game, alice, land_script("drop", name=long_name))
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    reply = wait(server, alice)

    [entry] = reply["scripts"]["applicable"]
    assert len(entry["name"]) <= script_tools.MAX_HINT_NAME_CHARS
    assert entry["name"] != long_name


def test_the_scripts_block_drops_entries_past_its_byte_budget(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    long_name = "y" * script_tools.MAX_HINT_NAME_CHARS
    scripts = [land_script(f"drop-{i}", priority=50 - i, name=long_name) for i in range(5)]
    register(client, game, alice, *scripts)
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)
    monkeypatch.setattr(script_tools, "MAX_HINT_BYTES", 150)

    reply = wait(server, alice)

    applicable = reply["scripts"]["applicable"]
    assert 0 < len(applicable) < 5, applicable


# -- S2 A4: armable ------------------------------------------------------------


def force_theirs_end_step(game_id: str) -> None:
    force_turn_position(game_id, Step.END_STEP, "bob")


def force_my_upkeep(game_id: str) -> None:
    force_turn_position(game_id, Step.UPKEEP, "alice")


def force_my_draw(game_id: str) -> None:
    force_turn_position(game_id, Step.DRAW, "alice")


def test_armable_during_the_opponents_turn(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_theirs_end_step(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" in answer, answer
    assert "applicable" not in answer["scripts"], answer
    [entry] = answer["scripts"]["armable"]
    assert entry["id"] == "drop"
    assert entry["name"] == "Make the land drop"
    assert entry["when"] == {
        "whose_turn": "mine",
        "steps": ["precombat_main"],
        "stack": "empty",
    }
    assert answer["scripts"]["arm_call"] == (
        f'pass_until(player_uid="{alice}", step="precombat_main", whose_turn="mine", '
        'arm=["drop"], arm_reasoning="<say why>", opposing_archetype="*")'
    )


def test_armable_during_my_own_upkeep(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_my_upkeep(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" in answer, answer
    [entry] = answer["scripts"]["armable"]
    assert entry["id"] == "drop"


def test_armable_during_my_own_draw(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_my_draw(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" in answer, answer
    [entry] = answer["scripts"]["armable"]
    assert entry["id"] == "drop"


def test_no_armable_outside_the_two_windows(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """My own precombat_main is neither the opponent's turn nor my own
    upkeep/draw - a script that does not apply now (wrong `when`) must not be
    offered as armable there."""
    force_precombat_main(game)
    alice = seat_uid(game, "alice")
    register(
        client,
        game,
        alice,
        land_script(
            "end-step-only", when={"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"}
        ),
    )
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" not in answer or "armable" not in answer.get("scripts", {}), answer


def test_a_script_applying_now_is_never_armable(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `theirs`/`end_step` script that already applies right now (this
    seat holds priority during the opponent's end step) is `applicable`, not
    `armable` - armable is only for what does not apply yet.

    `tap_island_script`, not `land_script`: a land drop needs a main phase
    (CR 305.1), so a `land_script` can never apply during any `end_step`
    regardless of whose turn - an activated mana ability has no such
    restriction."""
    force_theirs_end_step(game)
    alice = seat_uid(game, "alice")
    bob = seat_uid(game, "bob")
    register(
        client,
        game,
        alice,
        tap_island_script(
            "end-step-tap",
            when={"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"},
        ),
    )
    add_card(client, game, "Island", "alice", "battlefield")
    # `force_turn_position` grants priority to the active player (bob) - one
    # real pass moves it to alice, who then genuinely holds priority in this
    # same end_step.
    passed = client.post(
        f"/api/games/{game}/actions/pass-priority", params={"uid": bob}, json={"player": "bob"}
    )
    assert passed.status_code == 200 and passed.json()["allowed"] is True, passed.text
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" in answer, answer
    [entry] = answer["scripts"]["applicable"]
    assert entry["id"] == "end-step-tap"
    assert "armable" not in answer["scripts"], answer["scripts"]


def test_priority_zero_and_wrong_archetype_scripts_are_never_armable(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_theirs_end_step(game)
    alice = seat_uid(game, "alice")
    register(
        client,
        game,
        alice,
        land_script("disabled", priority=0),
        land_script("wrong-archetype", opposing_archetypes=["dimir-frog"]),
    )
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    assert "scripts" not in answer or "armable" not in answer.get("scripts", {}), answer


def test_armable_is_highest_priority_first(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_theirs_end_step(game)
    alice = seat_uid(game, "alice")
    register(
        client,
        game,
        alice,
        land_script("drop-low", priority=10),
        land_script("drop-high", priority=90),
    )
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    ids = [entry["id"] for entry in answer["scripts"]["armable"]]
    assert ids == ["drop-high", "drop-low"]


def test_armable_shares_the_cap_with_applicable(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_theirs_end_step(game)
    alice = seat_uid(game, "alice")
    scripts = [land_script(f"drop-{i}", priority=50 - i) for i in range(8)]
    register(client, game, alice, *scripts)
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, alice)

    armable = answer["scripts"]["armable"]
    assert len(armable) == script_tools.MAX_HINT_SCRIPTS
    assert len(json.dumps(answer)) < 20_000


def test_advisor_armable_template_uses_wait_for_priority(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from magic_cards_model_api.repository import Advisor
    from magic_cards_model_api.store import store

    force_theirs_end_step(game)
    advisor = "armable-advisor-uid"
    store.register_advisors(game, {advisor: Advisor(player="alice")})
    register(client, game, advisor, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    _, server = server_for(monkeypatch, game)

    answer = wait(server, advisor)

    assert answer["scripts"]["arm_call"] == (
        f'wait_for_priority(player_uid="{advisor}", arm=["drop"], '
        'arm_reasoning="<say why>", opposing_archetype="*")'
    )


def test_no_extra_state_fetch_computing_armable(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_theirs_end_step(game)
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    seats, server = server_for(monkeypatch, game)
    seat = seats.seat_for(alice)
    assert seat is not None
    calls = counting_fetch_state(monkeypatch, seat)

    wait(server, alice)

    assert calls["n"] == 1, f"armable cost {calls['n']} state fetches, expected exactly 1"
