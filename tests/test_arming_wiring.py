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

import threading
import time
from typing import Any

import pytest
from conftest import add_card, find, force_turn_position, state
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
    tap_island_script,
    wired,
)

from magic_cards_model_api import seat_cli, seatkit
from magic_cards_model_api.main import app
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


# -- finding 2: a theirs/either destination waits for OWN priority to fire ---


def test_a_theirs_end_step_script_fires_once_the_opponent_passes_priority_there(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 arming re-review finding 2: `autopass_stop_reason` stops a
    `pass_until` run the instant the named destination step begins,
    board-wide - for a `theirs`/`either` destination that is usually still
    the active opponent's own first priority in it (CR 117.3a), the old code
    treated that as the run's own finish and never even tried arming
    (`_arming_moment` false there). The run must instead keep waiting,
    inside its own bounded call, until this seat holds priority in that same
    occurrence - which happens here the moment bob's `Opponent` thread passes
    its own first priority in its end step."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
    register(
        client,
        game,
        alice,
        tap_island_script(
            "theirs-end", when={"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"}
        ),
    )
    add_card(client, game, "Island", "alice", "battlefield")
    stock_libraries(client, game)
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice)

    with Opponent(game, "bob"):
        result = pass_until(
            seat,
            until_step="end_step",
            whose_turn="opponent",
            corpora=seats.script_corpora,
            arm=["theirs-end"],
            arm_reasoning=ARM_REASONING,
            max_seconds=10.0,
        )

    assert result["finished"] is True, result
    assert result.get("armed_conflict") is None, result
    [entry] = result["armed"]
    assert entry["script"] == "theirs-end"
    assert entry["exit"] == "chain_complete", entry
    assert hook.posts("/actions/activate"), "the armed script actually activated the island"
    assert result["state"]["step"] == "end_step"


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


# -- S2 arming re-review finding 6: one shared call budget over wait + arming --


class _MonotonicSequence:
    """A `time.monotonic` stand-in that hands back each of `readings` in
    turn, then repeats the last one - enough to make `wait_and_arm`/
    `pass_until`'s own "elapsed since call start" measurement deterministic
    without a real clock or a real wait."""

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)
        self._index = 0

    def monotonic(self) -> float:
        value = self._readings[min(self._index, len(self._readings) - 1)]
        self._index += 1
        return value

    def sleep(self, _seconds: float) -> None:
        return None


def test_wait_and_arm_hands_arming_only_the_remainder_of_one_shared_call_budget(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 arming re-review finding 6: arming used to get a full, fresh
    `RUN_CAP_SECONDS` (25s) on top of whatever the wait itself already spent
    (up to its own `MAX_WAIT_SECONDS`, also 25s) - up to ~50s for one call,
    breaking `MAX_WAIT_SECONDS`'s own rationale of staying comfortably under
    the ~30s client/transport timeout. `wait_and_arm` now shares one
    `TOTAL_CALL_BUDGET_SECONDS` deadline across the wait and arming: arming
    gets only whatever the wait left of it - here, the wait "spent" 20 of the
    call's 29-second total, leaving arming only 9, never a fresh 25."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)
    woken_state = seat.fetch_state()

    clock = _MonotonicSequence(0.0, 20.0)
    monkeypatch.setattr(seatkit, "time", clock)
    monkeypatch.setattr(
        seatkit,
        "block_until_wake",
        lambda *a, **kw: {"state": woken_state, "wake_reason": "priority", "recent_log": []},
    )
    captured: dict[str, Any] = {}

    def fake_arm(*a: Any, **kw: Any) -> Any:
        captured["max_seconds"] = kw.get("max_seconds")
        return {"armed": []}

    from magic_cards_model_api.playscripts import arming as playscript_arming

    monkeypatch.setattr(playscript_arming, "arm", fake_arm)

    seatkit.wait_and_arm(
        seat,
        before=None,
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert captured["max_seconds"] == pytest.approx(seatkit.TOTAL_CALL_BUDGET_SECONDS - 20.0)
    assert captured["max_seconds"] < seatkit.MAX_WAIT_SECONDS, (
        "arming must never get a fresh RUN_CAP_SECONDS on top of what the wait already spent"
    )


def test_wait_and_arm_skips_arming_when_too_little_budget_remains(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: once the wait alone has spent enough of the shared
    budget that less than `MIN_ARMED_RUN_SECONDS` is left, arming is skipped
    entirely - never even attempted - with a note telling the caller to call
    again (the hint's own `arm_call` is still valid)."""
    from magic_cards_model_api.playscripts.arming import MIN_ARMED_RUN_SECONDS

    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    woken_state = seat.fetch_state()

    spent = seatkit.TOTAL_CALL_BUDGET_SECONDS - (MIN_ARMED_RUN_SECONDS - 0.5)
    clock = _MonotonicSequence(0.0, spent)
    monkeypatch.setattr(seatkit, "time", clock)
    monkeypatch.setattr(
        seatkit,
        "block_until_wake",
        lambda *a, **kw: {"state": woken_state, "wake_reason": "priority", "recent_log": []},
    )

    def never_called(*a: Any, **kw: Any) -> Any:
        raise AssertionError("arm() must never be called on too little remaining budget")

    from magic_cards_model_api.playscripts import arming as playscript_arming

    monkeypatch.setattr(playscript_arming, "arm", never_called)

    woken = seatkit.wait_and_arm(
        seat,
        before=None,
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert "armed" not in woken
    assert woken.get("armed_skipped") == seatkit.ARMING_SKIPPED_LOW_BUDGET
    assert hook.posts("/actions/play-land") == []


def test_pass_until_hands_arming_only_the_remainder_of_one_shared_call_budget(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same sharing, on `pass_until`'s own arming call."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)
    finished_state = seat.fetch_state()

    clock = _MonotonicSequence(0.0, 0.0, 18.0, 18.0)
    monkeypatch.setattr(seatkit, "time", clock)
    monkeypatch.setattr(
        seatkit,
        "_pass_until_once",
        lambda *a, **kw: {
            "stopped_because": "reached your postcombat main",
            "passes": 3,
            "finished": True,
            "until": {"step": "postcombat_main", "whose_turn": "mine"},
            "state": finished_state,
            "recent_log": [],
        },
    )
    captured: dict[str, Any] = {}

    def fake_arm(*a: Any, **kw: Any) -> Any:
        captured["max_seconds"] = kw.get("max_seconds")
        return {"armed": []}

    from magic_cards_model_api.playscripts import arming as playscript_arming

    monkeypatch.setattr(playscript_arming, "arm", fake_arm)

    pass_until(
        seat,
        until_step="postcombat_main",
        whose_turn="mine",
        corpora=seats.script_corpora,
        arm=["drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert captured["max_seconds"] == pytest.approx(seatkit.TOTAL_CALL_BUDGET_SECONDS - 18.0)
    assert captured["max_seconds"] < seatkit.MAX_WAIT_SECONDS


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
        'arm=["drop"], arm_reasoning="<say why>", opposing_archetype="*", max_seconds=25.0)'
    )

    verb, kwargs = _parse_call(arm_call)
    assert isinstance(kwargs["arm"], list)
    assert kwargs["max_seconds"] == 25.0, (
        "S2 arming re-review finding 1: the template's own max_seconds - without it, "
        "the default 0.0 would never even run _wait_for_own_priority_at_destination's "
        "own wait loop once"
    )
    kwargs["arm_reasoning"] = ARM_REASONING
    # This test's own Opponent thread answers promptly; a much shorter bound
    # than the template's own 25.0 keeps the test fast without changing what
    # it proves.
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


# -- S2 arming re-review finding 1: waiting for OWN priority at a theirs/either
# -- destination must never silently drop the window --------------------------


def _pass_bob(client: TestClient, game_id: str, bob: str) -> None:
    """One real, synchronous pass-priority for bob - not a background
    thread: every test below drives bob's side by hand, exactly once at a
    time, so each step is reached deterministically rather than raced
    against a thread's own poll interval."""
    passed = client.post(
        f"/api/games/{game_id}/actions/pass-priority",
        params={"uid": bob},
        json={"player": "bob"},
    )
    assert passed.status_code == 200 and passed.json()["allowed"] is True, passed.text


def test_default_max_seconds_returns_resume_that_fires_in_the_same_end_step(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 arming re-review finding 1: reaching a `theirs`/`either`
    destination without this seat holding priority there yet used to answer
    `finished: true` outright whenever `max_seconds` was left at its default
    `0.0` - `_wait_for_own_priority_at_destination`'s own wait never even
    ran, so the answer carried no `armed`, no note, and critically no
    `resume`. A caller that took that at face value and called `pass_until`
    again with no `resume` would get a fresh run stamped from the board it
    is standing on (`start_autopass`), which exempts THIS very occurrence of
    the destination as "the step the run started in" and skips straight to
    the NEXT one - a whole turn later. The fix: `finished: false` with a
    `resume` carrying the run's ORIGINAL starting stamp, so a follow-up call
    that hands it straight back still arms in this same end step, the
    moment this seat actually gets priority there.

    `force_turn_position` positions the board at bob's postcombat_main
    (still holding priority there) rather than his end_step directly: naming
    the step a run is already sitting in as its own destination is exempted
    as "the step the run started in" (`start_autopass`) and would never
    "reach" it at all - one step earlier, and one real (synchronous, not
    threaded) pass for bob, is what makes both calls below deterministic
    rather than raced against a background driver."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    bob = seat_uid(game, "bob")
    force_turn_position(game, Step.POSTCOMBAT_MAIN, "bob")
    register(
        client,
        game,
        alice,
        tap_island_script(
            "theirs-end",
            when={"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"},
        ),
    )
    add_card(client, game, "Island", "alice", "battlefield")
    stock_libraries(client, game)
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice)

    _pass_bob(client, game, bob)  # bob's own postcombat_main priority

    result = pass_until(
        seat,
        until_step="end_step",
        whose_turn="opponent",
        corpora=seats.script_corpora,
        arm=["theirs-end"],
        arm_reasoning=ARM_REASONING,
        # max_seconds left at its default 0.0 on purpose.
    )
    assert result["finished"] is False, result
    assert "armed" not in result, result
    assert result.get("resume") is not None, result
    assert result["state"]["step"] == "end_step"
    assert "call again with resume" in result["stopped_because"], result
    starting_turn = result["state"]["turn_number"]

    _pass_bob(client, game, bob)  # bob's own end_step priority - alice's window

    result2 = pass_until(
        seat,
        until_step="end_step",
        whose_turn="opponent",
        corpora=seats.script_corpora,
        arm=["theirs-end"],
        arm_reasoning=ARM_REASONING,
        resume=result["resume"],
    )

    assert result2["finished"] is True, result2
    [entry] = result2["armed"]
    assert entry["script"] == "theirs-end"
    assert hook.posts("/actions/activate"), "fired in the SAME end step, not a later one"
    assert result2["state"]["step"] == "end_step"
    assert result2["state"]["turn_number"] == starting_turn, (
        "must not have skipped to the next occurrence"
    )


def test_pass_until_hands_back_resume_when_the_human_thinks_longer_than_the_wait(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 arming re-review finding 1: a bounded wait that genuinely runs out
    without this seat ever getting priority at the destination - the human
    thinking longer than even a real, positive `max_seconds` - must still
    keep `finished: false` with a `resume`, not silently fall back to the
    old `finished: true` behaviour once the deadline (rather than the
    default `0.0`) is what ends the wait. Nobody ever passes bob's own
    end_step priority here - the human who never finishes thinking within
    the call's own bound."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    bob = seat_uid(game, "bob")
    force_turn_position(game, Step.POSTCOMBAT_MAIN, "bob")
    register(
        client,
        game,
        alice,
        tap_island_script(
            "theirs-end",
            when={"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"},
        ),
    )
    add_card(client, game, "Island", "alice", "battlefield")
    stock_libraries(client, game)
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice)

    _pass_bob(client, game, bob)

    result = pass_until(
        seat,
        until_step="end_step",
        whose_turn="opponent",
        corpora=seats.script_corpora,
        arm=["theirs-end"],
        arm_reasoning=ARM_REASONING,
        max_seconds=0.3,
    )

    assert result["finished"] is False, result
    assert "armed" not in result, result
    assert result.get("resume") is not None, result
    assert result["state"]["step"] == "end_step"
    assert "waiting for your priority at end_step" in result["stopped_because"], result
    assert "call again with resume" in result["stopped_because"], result
    assert not hook.posts("/actions/activate")


def test_pass_until_interrupted_waiting_for_own_priority_keeps_resume(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 arming re-review finding 1: `should_stop` firing while this call
    is inside `_wait_for_own_priority_at_destination` used to keep the
    `finished: true` this result already carried from `_pass_until_once`'s
    own "reached" answer, only overwriting `stopped_because` with
    `WAIT_INTERRUPTED` - which drops `resume` (only ever attached to a
    `finished: false` answer), unlike the outer retry loop's own interrupt
    handling, which already kept both. The fix makes the two agree."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    bob = seat_uid(game, "bob")
    force_turn_position(game, Step.POSTCOMBAT_MAIN, "bob")
    register(
        client,
        game,
        alice,
        tap_island_script(
            "theirs-end",
            when={"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"},
        ),
    )
    add_card(client, game, "Island", "alice", "battlefield")
    stock_libraries(client, game)
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice)

    _pass_bob(client, game, bob)

    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    result = pass_until(
        seat,
        until_step="end_step",
        whose_turn="opponent",
        corpora=seats.script_corpora,
        arm=["theirs-end"],
        arm_reasoning=ARM_REASONING,
        max_seconds=5.0,
        should_stop=should_stop,
    )

    assert result["finished"] is False, result
    assert "armed" not in result, result
    assert result.get("resume") is not None, result
    assert result["stopped_because"] == seatkit.WAIT_INTERRUPTED, result
    assert not hook.posts("/actions/activate")


def test_a_stack_arrival_while_waiting_for_own_priority_is_reported(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 arming re-review finding 1's third bug: bob casting something and
    passing, while this seat waits for its own priority at the destination,
    used to hand the board straight back with the STALE "reached ..."
    `stopped_because` from before the wait even began - the new stack object
    silently unreported, even though `stop_on_stack` (the default) asks for
    exactly that. The fix reports the same stop reason `autopass_stop_reason`
    itself would have; arming still runs on the resulting board, and a
    script whose own `when` needs an empty stack is judged and skipped on
    its own terms, never hard-skipped here."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    alice = seat_uid(game, "alice")
    bob = seat_uid(game, "bob")
    force_turn_position(game, Step.POSTCOMBAT_MAIN, "bob")
    register(
        client,
        game,
        alice,
        tap_island_script(
            "theirs-end",
            when={"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"},
        ),
    )
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")["id"]
    bolt = find(board["hands"]["bob"], "Lightning Bolt")["id"]
    stock_libraries(client, game)
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice)

    _pass_bob(client, game, bob)

    first = pass_until(
        seat,
        until_step="end_step",
        whose_turn="opponent",
        corpora=seats.script_corpora,
        arm=["theirs-end"],
        arm_reasoning=ARM_REASONING,
    )
    assert first["finished"] is False, first
    assert first.get("resume") is not None, first
    assert first["state"]["step"] == "end_step"

    cast = threading.Event()

    def bob_bolts_then_passes() -> None:
        bob_client = TestClient(app)
        bolted = False
        for _ in range(4000):
            b = bob_client.get(f"/api/games/{game}").json()
            if b.get("priority_holder") != "bob":
                time.sleep(0.01)
                continue
            if not bolted:
                bob_client.post(
                    f"/api/games/{game}/actions/cast",
                    json={
                        "player": "bob",
                        "card_id": bolt,
                        "targets": ["alice"],
                        "mana_sources": [mountain],
                    },
                )
                bolted = True
                cast.set()
                continue
            bob_client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
            return

    thread = threading.Thread(target=bob_bolts_then_passes, daemon=True)
    thread.start()
    try:
        result = pass_until(
            seat,
            until_step="end_step",
            whose_turn="opponent",
            corpora=seats.script_corpora,
            arm=["theirs-end"],
            arm_reasoning=ARM_REASONING,
            max_seconds=5.0,
            resume=first["resume"],
        )
    finally:
        thread.join(timeout=10)

    assert cast.is_set(), "bob never got the chance to cast, so nothing was tested"
    assert result["finished"] is True, result
    assert result["stopped_because"] == "something new went on the stack: Lightning Bolt", result
    assert [obj["name"] for obj in result["state"]["stack"]] == ["Lightning Bolt"]
    assert result["armed"] == [], (
        "the script's own `when` needs an empty stack, judged and skipped on its own "
        "terms rather than hard-skipped by the wait"
    )
    assert not hook.posts("/actions/activate")
