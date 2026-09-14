"""S2 A3: wire `playscripts.arming.arm` into the waits.

An armed script only ever fires at the moment the answer is about to go back
to the seat holding priority, or with a decision pending for it - after
`seatkit.block_until_wake` (wrapped as `wait_and_arm`) returns a real wake,
or when `pass_until` finishes (or hits the `MAX_PASSES_PER_CALL` hand-back).
Never on a retryable hand-back, where the opponent still holds priority: that
is not this seat's moment, and arming firing there would mean arming decided
something the wait itself was built to avoid deciding.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from conftest import add_card, force_turn_position
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_autopass import Opponent, stock_libraries
from test_playscript_runtime import (
    FakeResponse,
    hooked_seat,
    land_script,
    register,
    seat_uid,
    started,
    wired,
)

from magic_cards_model_api import seat_cli, seatkit
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.seatkit import block_until_wake, pass_until, wait_and_arm
from magic_cards_model_api.store import store

ARM_REASONING = "the human is thinking; fire this the moment it is legal"


def drop_island_script(script_id: str) -> dict[str, Any]:
    """`land_script`, narrowed to Island specifically - a run that crosses a
    turn boundary draws a card, and `land_script`'s own bare `types_all:
    Land` selector ties (ambiguous) once a second, differently-named land is
    in hand."""
    doc = land_script(script_id)
    doc["moves"] = [
        {
            "kind": "play_land",
            "card": {"select": {"types_all": ["Land"], "subtypes_any": ["Island"]}},
        }
    ]
    return doc


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


# -- pass_until -------------------------------------------------------------


def test_a_fetch_land_armed_from_the_opponents_end_step_fires_in_my_own_main(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    force_turn_position(game, Step.END_STEP, "bob")
    register(client, game, alice, drop_island_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    stock_libraries(client, game)
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice)

    with Opponent(game, "bob"):
        result = pass_until(
            seat,
            until_step="precombat_main",
            whose_turn="mine",
            corpora=seats.script_corpora,
            arm=["drop"],
            arm_reasoning=ARM_REASONING,
            max_seconds=10.0,
        )

    assert result["finished"] is True, result
    assert result.get("armed_conflict") is None, result
    [entry] = result["armed"]
    assert entry["script"] == "drop"
    assert entry["exit"] == "chain_complete", entry
    assert hook.posts("/actions/play-land"), "the armed script actually played the land"
    assert result["state"]["step"] == "precombat_main"


def test_a_play_whose_when_does_not_match_does_not_fire(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A script keyed to `theirs`/`end_step` is armed on the same run that
    reaches the seat's own precombat_main (the same real crossing as the
    fires-for-real test above): it never matches there, so `armed` comes
    back empty rather than firing anything."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    force_turn_position(game, Step.END_STEP, "bob")
    register(
        client,
        game,
        alice,
        land_script(
            "end-step-only", when={"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"}
        ),
    )
    add_card(client, game, "Island", "alice", "hand")
    stock_libraries(client, game)
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice)

    with Opponent(game, "bob"):
        result = pass_until(
            seat,
            until_step="precombat_main",
            whose_turn="mine",
            corpora=seats.script_corpora,
            arm=["end-step-only"],
            arm_reasoning=ARM_REASONING,
            max_seconds=10.0,
        )

    assert result["finished"] is True, result
    assert result["armed"] == []
    assert hook.posts("/actions/play-land") == []


def test_nothing_fires_on_a_retryable_hand_back(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pass_until` hands the board straight back the moment it would have to
    wait for the opponent (no `max_seconds`, no `Opponent` thread here) - a
    retryable hand-back, and arming must not even look at the board then."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = pass_until(
        seat,
        until_step="end_step",
        whose_turn="mine",
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert result["finished"] is False
    assert "armed" not in result, result
    assert "armed_conflict" not in result, result
    assert hook.posts("/actions/play-land") == []


def test_arming_is_skipped_entirely_when_arm_is_empty(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = pass_until(
        seat,
        until_step="end_step",
        whose_turn="mine",
        corpora=seats.script_corpora,
    )

    # No `Opponent` thread here on purpose: whatever this hand-back looks
    # like, an empty `arm` must never even try arming.
    assert "armed" not in result
    assert hook.posts("/actions/play-land") == []


def test_advisor_pass_until_is_still_refused_even_with_arm_given(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    advisor = "arming-advisor-uid"
    store.register_advisors(board, {advisor: Advisor(player="alice")})
    register(client, board, advisor, land_script("drop"))
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, advisor)

    result = pass_until(
        seat,
        until_step="precombat_main",
        whose_turn="mine",
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert result["finished"] is True
    assert result["stopped_because"] == seatkit.ADVISE_NO_PASS_UNTIL
    assert "armed" not in result
    assert hook.posts("/actions/play-land") == []


# -- exceptions raised while arming ----------------------------------------


def _boom(*_: Any, **__: Any) -> Any:
    raise RuntimeError("arming blew up")


def test_wait_and_arm_survives_arming_raising(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """S2 review finding 2: nothing between `wait_and_arm` and
    `playscripts.arming.arm` caught an exception (`_guarded`, the MCP
    decorator, only catches `httpx.HTTPError`), so a bug in arming crashed
    the whole wait - losing the board it had already fetched - rather than
    just failing to arm. A raise there must not cost the caller the wait's
    own answer."""
    from magic_cards_model_api.playscripts import arming

    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    monkeypatch.setattr(arming, "arm", _boom)

    import logging

    with caplog.at_level(logging.ERROR, logger="mcm.seatkit"):
        woken = wait_and_arm(
            seat,
            before=None,
            corpora=seats.script_corpora,
            arm=["drop"],
            arm_reasoning=ARM_REASONING,
        )

    assert woken["wake_reason"] != seatkit.NOTHING_NEW_YET
    assert "state" in woken and woken["state"] is not None
    assert "armed" not in woken
    assert "arming blew up" in woken.get("armed_error", "")
    assert hook.posts("/actions/play-land") == []
    seatkit_records = [r for r in caplog.records if r.name == "mcm.seatkit"]
    assert len(seatkit_records) == 1, caplog.records


def test_pass_until_survives_arming_raising(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """The same protection on `pass_until`'s own arming call: a raise there
    must not cost the run its own `stopped_because`/`passes`/`state`. A real
    finished crossing (not a retryable hand-back), so arming is genuinely
    attempted."""
    from magic_cards_model_api.playscripts import arming

    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    force_turn_position(game, Step.END_STEP, "bob")
    register(client, game, alice, drop_island_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    stock_libraries(client, game)
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice)
    monkeypatch.setattr(arming, "arm", _boom)

    import logging

    with caplog.at_level(logging.ERROR, logger="mcm.seatkit"), Opponent(game, "bob"):
        result = pass_until(
            seat,
            until_step="precombat_main",
            whose_turn="mine",
            corpora=seats.script_corpora,
            arm=["drop"],
            arm_reasoning=ARM_REASONING,
            max_seconds=10.0,
        )

    assert result["finished"] is True, result
    assert result["stopped_because"]
    assert "armed" not in result
    assert "arming blew up" in result.get("armed_error", "")
    assert hook.posts("/actions/play-land") == []
    seatkit_records = [r for r in caplog.records if r.name == "mcm.seatkit"]
    assert len(seatkit_records) == 1, caplog.records


# -- wait_and_arm -------------------------------------------------------------


def test_wait_and_arm_fires_on_a_real_wake(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    woken = wait_and_arm(
        seat,
        before=None,
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert woken["wake_reason"] != seatkit.NOTHING_NEW_YET
    [entry] = woken["armed"]
    assert entry["exit"] == "chain_complete", entry
    assert hook.posts("/actions/play-land")


def test_wait_and_arm_never_fires_on_nothing_new_yet(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`before` is this same board's own signature, so the wake condition is
    already satisfied and the bare check finds nothing new - arming must not
    run against a board nothing changed on."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    baseline = seat.fetch_state()

    woken = wait_and_arm(
        seat,
        before=baseline,
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert woken["wake_reason"] == seatkit.NOTHING_NEW_YET
    assert "armed" not in woken
    assert hook.posts("/actions/play-land") == []


def test_an_advisors_armed_script_posts_an_advice_row(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    advisor = "arming-advisor-wait-uid"
    store.register_advisors(board, {advisor: Advisor(player="alice")})
    register(client, board, advisor, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, advisor)

    woken = wait_and_arm(
        seat,
        before=None,
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert woken["wake_reason"] != seatkit.NOTHING_NEW_YET
    [entry] = woken["armed"]
    assert entry["exit"] == "advised", entry

    rows = [
        row
        for row in seatkit_audit_entries(board)
        if row["action"] == "script_run" and row["detail"].get("role") == "advisor"
    ]
    assert rows and rows[-1]["detail"]["invoked_via"] == "armed"


def seatkit_audit_entries(game_id: str) -> list[dict[str, Any]]:
    from magic_cards_model_api import audit

    return list(audit.entries(game_id))


def test_bare_block_until_wake_is_unaffected(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`block_until_wake` itself - `run_action`'s own post-action wake check
    - is untouched: it takes no `arm` at all, so nothing here can fire from
    inside an ordinary action's own follow-up check."""
    alice = seat_uid(board, "alice")
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)

    woken = block_until_wake(seat, before=None)

    assert "armed" not in woken


# -- finding 6: never arms on a stop without priority or a real decision ------


def test_wait_and_arm_never_fires_on_a_game_over_wake(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 review finding 6: `compute_wake_reason` answers a bare `"game
    over"` once a winner or a draw is set - a real wake, not `NOTHING_NEW_
    YET`/`GAME_GONE`/`WAIT_INTERRUPTED` - so arming used to fire on it too,
    even though there is nothing left to play. No armed row is written."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    engine_game = store.get(board)
    assert engine_game is not None
    engine_game.winner = "alice"

    woken = wait_and_arm(
        seat,
        before=None,
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert woken["wake_reason"] == "game over"
    assert "armed" not in woken
    assert hook.posts("/actions/play-land") == []
    assert started(board) == []


def test_wait_and_arm_never_fires_on_game_gone(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    client.delete(f"/api/games/{board}", params={"uid": alice})

    woken = wait_and_arm(
        seat,
        before=None,
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert woken["wake_reason"] == seatkit.GAME_GONE
    assert "armed" not in woken
    assert hook.posts("/actions/play-land") == []


def test_pass_until_never_fires_on_game_gone(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    client.delete(f"/api/games/{board}", params={"uid": alice})

    result = pass_until(
        seat,
        until_step="end_step",
        whose_turn="mine",
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert result["finished"] is True
    assert result["stopped_because"] == seatkit.GAME_GONE
    assert "armed" not in result
    assert hook.posts("/actions/play-land") == []
    assert started(board) == []


def test_pass_until_never_fires_on_an_unknown_outcome(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)

    def unknown_outcome(method: str, path: str, body: Any) -> Any:
        if method == "POST" and path.endswith("/pass-priority"):
            return FakeResponse(200, {"allowed": False, "unknown_outcome": True, "reason": "?"})
        return None

    seat, hook = hooked_seat(seats, alice, answer=unknown_outcome)

    result = pass_until(
        seat,
        until_step="end_step",
        whose_turn="mine",
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert result["finished"] is True
    assert result["stopped_because"] == seatkit.UNKNOWN_OUTCOME
    assert "armed" not in result
    assert hook.posts("/actions/play-land") == []
    assert started(board) == []


def test_pass_until_never_fires_on_a_refused_pass(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)

    def refused(method: str, path: str, body: Any) -> Any:
        if method == "POST" and path.endswith("/pass-priority"):
            return FakeResponse(200, {"allowed": False, "reason": "test refusal"})
        return None

    seat, hook = hooked_seat(seats, alice, answer=refused)

    result = pass_until(
        seat,
        until_step="end_step",
        whose_turn="mine",
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert result["finished"] is True
    assert "a pass was refused" in result["stopped_because"]
    assert "armed" not in result
    assert hook.posts("/actions/play-land") == []
    assert started(board) == []


# -- finding 7c: an invalid arm request is refused before any wait or pass ----


def test_wait_and_arm_refuses_a_blank_arm_reasoning_before_waiting(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 review finding 7: `arm_reasoning` used to be validated only once
    `arm()` was finally reached, *after* the whole wait had already run -
    so an invalid arm request still paid for the entire bound. Force bob's
    turn (nothing here would wake alice on its own) and ask for a long
    `max_seconds`: a call that actually waited for it would take seconds;
    a call refused up front returns at once."""
    alice = seat_uid(game, "alice")
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
    register(client, game, alice, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice)

    started_at = time.monotonic()
    woken = wait_and_arm(
        seat,
        before=None,
        max_seconds=5.0,
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning="",
    )
    elapsed = time.monotonic() - started_at

    assert elapsed < 1.0, f"waited {elapsed:.2f}s despite the invalid arm_reasoning"
    assert "armed" not in woken
    assert woken.get("armed_refusal")
    assert hook.posts("/actions/play-land") == []


def test_pass_until_refuses_a_blank_arm_reasoning_before_any_pass(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = pass_until(
        seat,
        until_step="end_step",
        whose_turn="mine",
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning="",
    )

    assert result["finished"] is True
    assert "armed" not in result
    assert result.get("armed_refusal")
    assert result["passes"] == 0
    assert hook.posts("/actions/pass-priority") == []
    assert hook.posts("/actions/play-land") == []


def test_pass_until_refuses_disagreeing_opposing_archetype_and_inputs_before_any_pass(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = pass_until(
        seat,
        until_step="end_step",
        whose_turn="mine",
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
        opposing_archetype="dimir-frog",
        inputs={"opposing_archetype": "boros-aggro"},
    )

    assert result["finished"] is True
    assert "armed" not in result
    assert result.get("armed_refusal")
    assert hook.posts("/actions/pass-priority") == []


# -- CLI ----------------------------------------------------------------------


def test_cli_wait_passes_arm_through(
    client: TestClient,
    board: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from test_front_end_parity import wire

    wire(monkeypatch)
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")

    exit_code = seat_cli.run(
        [
            "--uid",
            alice,
            "--game",
            board,
            "wait",
            "--arm",
            "drop",
            "--arm-reasoning",
            ARM_REASONING,
        ]
    )

    assert exit_code == 0
    import json

    printed = json.loads(capsys.readouterr().out)
    [entry] = printed["armed"]
    assert entry["script"] == "drop"
    assert entry["exit"] == "chain_complete", entry


def test_cli_pass_until_arm_is_repeatable(
    client: TestClient,
    game: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two `--arm` flags collect into one list rather than the second
    overwriting the first - proven by both being judged: the unregistered
    `another-one` is silently ignored (it never becomes a firing candidate)
    while `drop` still fires once the crossing reaches alice's own main."""
    from test_front_end_parity import wire

    wire(monkeypatch)
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    force_turn_position(game, Step.END_STEP, "bob")
    register(client, game, alice, drop_island_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    stock_libraries(client, game)

    with Opponent(game, "bob"):
        exit_code = seat_cli.run(
            [
                "--uid",
                alice,
                "--game",
                game,
                "pass-until",
                "precombat_main",
                "--arm",
                "drop",
                "--arm",
                "another-one",
                "--arm-reasoning",
                ARM_REASONING,
                "--max-seconds",
                "10.0",
            ]
        )

    assert exit_code == 0
    import json

    printed = json.loads(capsys.readouterr().out)
    assert printed["finished"] is True, printed
    [entry] = printed["armed"]
    assert entry["script"] == "drop"
    assert entry["exit"] == "chain_complete", entry


# -- MCP tools ------------------------------------------------------------------


def test_mcp_wait_for_priority_fires_an_armed_script(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from test_mcp_input_required import call_tool

    from magic_cards_model_api.mcp import build_server

    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    server = build_server(seats)

    result = call_tool(
        server,
        "wait_for_priority",
        {"player_uid": alice, "arm": "drop", "arm_reasoning": ARM_REASONING},
    )

    answer = json.loads(result.content[0].text)
    [entry] = answer["armed"]
    assert entry["script"] == "drop"
    assert entry["exit"] == "chain_complete", entry


def test_mcp_pass_until_fires_an_armed_script_on_a_real_crossing(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from test_mcp_input_required import call_tool

    from magic_cards_model_api.mcp import build_server

    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    force_turn_position(game, Step.END_STEP, "bob")
    register(client, game, alice, drop_island_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    stock_libraries(client, game)
    seats = wired(monkeypatch, game)
    server = build_server(seats)

    with Opponent(game, "bob"):
        result = call_tool(
            server,
            "pass_until",
            {
                "player_uid": alice,
                "step": "precombat_main",
                "arm": "drop",
                "arm_reasoning": ARM_REASONING,
                "max_seconds": 10.0,
            },
        )

    answer = json.loads(result.content[0].text)
    assert answer["finished"] is True, answer
    [entry] = answer["armed"]
    assert entry["script"] == "drop"
    assert entry["exit"] == "chain_complete", entry


def _parse_call(call: str) -> tuple[str, dict[str, Any]]:
    """Parse a `script_tools._arm_call_template`-rendered call string into
    `(tool_name, kwargs)` the way an agent reading it would: split the
    top-level `name=value` pairs (respecting brackets and quotes) and
    `json.loads` each value half - proving the template's argument values
    parse back to real Python objects (a list for `arm`), not merely that
    the string looks right (S2 review finding 1)."""
    import json as _json

    verb, _, rest = call.partition("(")
    rest = rest[:-1] if rest.endswith(")") else rest
    parts: list[str] = []
    depth = 0
    in_string = False
    current = ""
    for ch in rest:
        if in_string:
            current += ch
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            current += ch
        elif ch in "[{":
            depth += 1
            current += ch
        elif ch in "]}":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    kwargs: dict[str, Any] = {}
    for part in parts:
        name, _, value = part.partition("=")
        kwargs[name.strip()] = _json.loads(value.strip())
    return verb.strip(), kwargs


def test_the_armable_hints_own_call_template_actually_invokes(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 review finding 1: `arm_call` renders `arm` as a JSON list literal
    (`arm=["drop"]`), and an agent that reads that template and calls the
    tool with those exact arguments hands the MCP tool an actual Python
    list, not a string. Prove the template really works by parsing its own
    emitted call the way an agent would and invoking the real MCP tool
    function through FastMCP's own argument validation - not by asserting
    the string looks right and stopping there."""
    import json as _json

    from test_mcp_input_required import call_tool

    from magic_cards_model_api.mcp import build_server

    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    force_turn_position(game, Step.END_STEP, "bob")
    register(client, game, alice, drop_island_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    stock_libraries(client, game)
    seats = wired(monkeypatch, game)
    server = build_server(seats)

    hinted = call_tool(server, "wait_for_priority", {"player_uid": alice})
    hint_answer = _json.loads(hinted.content[0].text)
    arm_call = hint_answer["scripts"]["arm_call"]
    assert arm_call == (
        f'pass_until(player_uid="{alice}", step="precombat_main", whose_turn="mine", '
        'arm=["drop"], arm_reasoning="<say why>", opposing_archetype="*")'
    )

    verb, kwargs = _parse_call(arm_call)
    assert isinstance(kwargs["arm"], list)
    kwargs["arm_reasoning"] = ARM_REASONING
    # The template names only the arming arguments; a real caller still adds
    # its own `max_seconds` on top, same as any other `pass_until` call.
    kwargs["max_seconds"] = 10.0

    with Opponent(game, "bob"):
        result = call_tool(server, verb, kwargs)

    answer = _json.loads(result.content[0].text)
    assert answer["finished"] is True, answer
    [entry] = answer["armed"]
    assert entry["script"] == "drop"
    assert entry["exit"] == "chain_complete", entry


def test_arm_still_accepts_a_comma_separated_string(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backward compatibility (S2 review finding 1): the CLI and any
    existing caller that already sends `arm` as a comma-separated string
    keeps working once `arm` also accepts a list."""
    from magic_cards_model_api.seatkit import _parse_ids

    assert _parse_ids("a, b") == ["a", "b"]
    assert _parse_ids(["a", "b"]) == ["a", "b"]
    assert _parse_ids([]) == []


def test_mcp_pass_until_never_fires_on_a_retryable_hand_back(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from test_mcp_input_required import call_tool

    from magic_cards_model_api.mcp import build_server

    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    server = build_server(seats)

    # No `Opponent` here: alice's own single pass hands the board straight
    # back "you do not hold priority", a retryable hand-back.
    result = call_tool(
        server,
        "pass_until",
        {
            "player_uid": alice,
            "step": "precombat_main",
            "arm": "drop",
            "arm_reasoning": ARM_REASONING,
        },
    )

    answer = json.loads(result.content[0].text)
    assert "armed" not in answer, answer
