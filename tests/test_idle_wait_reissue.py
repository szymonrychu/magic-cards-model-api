"""L2: a capped wait with nothing new is re-issued below the model.

Prod, game 9cec3866 (2026-09-23): engine and api hops are ~0.4s, but every
model reply costs 27-100s of inference. A `pass_until` capped at
`seatkit.MAX_WAIT_SECONDS` (25s) came back `NOTHING_NEW_YET`-shaped while the
opponent was still thinking, the model spent a whole inference deciding to
call again, and the owner's Karn -2 sat unseen for 45.6s.

The loop lives in seatkit's own wait wrappers (`wait_and_arm`, `pass_until`),
driven by `reissue_seconds`, which the MCP server sets per transport: the
model's MCP client calls the tool itself, so no driver or wrapper sits
between the call and its answer to re-issue it. These tests use a fake clock
and fake seats (fakes are fine in this repo - the no-mocks rule is the
engine's card-rules tests only), plus one real game driven through the MCP
server.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi.testclient import TestClient

from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import NOTHING_NEW_YET, WAIT_INTERRUPTED, Seats, run_action

#: The simulated opponent thinks this long before acting.
OPPONENT_THINKS_SECONDS = 90.0
#: What the model asks for, as the prompt tells it to.
MODEL_MAX_SECONDS = 20.0


class FakeClock:
    """`seatkit.time`, with `sleep` advancing `monotonic` instead of blocking."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def fake_clock(monkeypatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(seatkit, "time", clock)
    return clock


class IdleOpponentSeat:
    """alice's seat while bob holds priority and thinks for `acts_at` seconds,
    then passes: priority comes back to alice on a changed board."""

    advise = False
    uid = "alice-uid"
    player = "alice"

    def __init__(self, clock: FakeClock, acts_at: float = OPPONENT_THINKS_SECONDS) -> None:
        self.clock = clock
        self.acts_at = acts_at
        self._wake_signatures: dict[str, Any] = {}
        self.last_state_status = None
        self.fetches = 0

    def fetch_state(self) -> dict[str, Any]:
        self.fetches += 1
        if self.clock.now >= self.acts_at:
            return {"priority_holder": "alice", "step": "precombat_main", "life": {"bob": 17}}
        return {"priority_holder": "bob", "step": "precombat_main", "life": {"bob": 20}}

    def recent_log(self) -> list[Any]:
        return []


def returns_until_real_wake(call) -> tuple[int, dict[str, Any]]:
    """What the model does: call, and call again while the answer is idle.
    Each call is one model-facing return, i.e. one inference."""
    returns = 0
    while True:
        answer = call()
        returns += 1
        if answer.get("wake_reason", NOTHING_NEW_YET) != NOTHING_NEW_YET:
            return returns, answer
        if answer.get("finished") is True:
            return returns, answer
        assert returns < 50, "the model loop never woke"


# -- wait_for_priority's wait (`wait_and_arm`) --------------------------------


def test_a_90s_idle_opponent_costs_the_model_exactly_one_return(monkeypatch):
    clock = fake_clock(monkeypatch)
    seat = IdleOpponentSeat(clock)

    returns, answer = returns_until_real_wake(
        lambda: seatkit.wait_and_arm(
            seat, before=None, max_seconds=MODEL_MAX_SECONDS, reissue_seconds=120.0
        )
    )

    assert returns == 1, f"{returns} model-facing returns for one opponent action"
    assert "you have priority" in answer["wake_reason"]
    assert OPPONENT_THINKS_SECONDS <= clock.now < OPPONENT_THINKS_SECONDS + 2


def test_without_reissue_the_same_idle_opponent_costs_several_returns(monkeypatch):
    """The baseline this fixes: the bare capped wait hands back every 20s."""
    clock = fake_clock(monkeypatch)
    seat = IdleOpponentSeat(clock)

    returns, _ = returns_until_real_wake(
        lambda: seatkit.wait_and_arm(seat, before=None, max_seconds=MODEL_MAX_SECONDS)
    )

    assert returns >= 4


def test_the_reissued_wait_is_still_bounded(monkeypatch):
    clock = fake_clock(monkeypatch)
    seat = IdleOpponentSeat(clock, acts_at=10_000.0)

    answer = seatkit.wait_and_arm(
        seat, before=None, max_seconds=MODEL_MAX_SECONDS, reissue_seconds=120.0
    )

    assert answer["wake_reason"] == NOTHING_NEW_YET
    assert 120.0 <= clock.now <= 121.0, f"outer bound 120s, returned at {clock.now}s"


def test_a_zero_second_poll_is_never_reissued(monkeypatch):
    clock = fake_clock(monkeypatch)
    seat = IdleOpponentSeat(clock)

    answer = seatkit.wait_and_arm(seat, before=None, max_seconds=0.0, reissue_seconds=120.0)

    assert answer["wake_reason"] == NOTHING_NEW_YET
    assert clock.now == 0.0
    assert seat.fetches == 1


def test_an_interrupt_between_reissued_waits_ends_the_call(monkeypatch):
    clock = fake_clock(monkeypatch)
    seat = IdleOpponentSeat(clock)

    answer = seatkit.wait_and_arm(
        seat,
        before=None,
        max_seconds=MODEL_MAX_SECONDS,
        reissue_seconds=120.0,
        should_stop=lambda: clock.now >= 30.0,
    )

    assert answer["wake_reason"] == WAIT_INTERRUPTED
    assert clock.now < 32.0


def test_armed_scripts_are_judged_once_on_the_real_wake_never_on_an_idle_reissue(monkeypatch):
    clock = fake_clock(monkeypatch)
    seat = IdleOpponentSeat(clock)
    judged_at: list[float] = []

    def judge(*args: Any, **kwargs: Any) -> dict[str, Any]:
        judged_at.append(clock.now)
        return {"applying": []}

    monkeypatch.setattr(seatkit, "_judge_armed_scripts", judge)

    answer = seatkit.wait_and_arm(
        seat,
        before=None,
        max_seconds=MODEL_MAX_SECONDS,
        reissue_seconds=120.0,
        arm=["drop"],
        arm_reasoning="land drop if it applies",
    )

    assert "you have priority" in answer["wake_reason"]
    assert len(judged_at) == 1
    assert judged_at[0] >= OPPONENT_THINKS_SECONDS


# -- pass_until -----------------------------------------------------------------


def hand_back(resume: dict[str, Any], passes: int) -> dict[str, Any]:
    return {
        "stopped_because": (f"bob holds priority{seatkit._not_waiting_yet_note(passes)}"),
        "passes": passes,
        "finished": False,
        "until": {"step": "precombat_main", "whose_turn": "mine"},
        "state": {"priority_holder": "bob"},
        "available_actions": {},
        "recent_log": {},
        "resume": resume,
        "retryable": True,
    }


def test_pass_until_reissues_with_the_same_resume_token_until_a_real_stop(monkeypatch):
    clock = fake_clock(monkeypatch)
    seen: list[Any] = []

    def fake_once(seat: Any, *, resume: Any = None, **kwargs: Any) -> dict[str, Any]:
        seen.append(resume)
        token = {"started": "upkeep-3", "hops": len(seen)}
        if clock.now >= OPPONENT_THINKS_SECONDS:
            return {
                "stopped_because": "reached precombat_main",
                "passes": 1,
                "finished": True,
                "until": {"step": "precombat_main", "whose_turn": "mine"},
                "state": {"priority_holder": "alice"},
                "available_actions": {},
                "recent_log": {},
            }
        return hand_back(token, 1 if len(seen) == 1 else 0)

    monkeypatch.setattr(seatkit, "_pass_until_once", fake_once)
    callers_token = {"started": "upkeep-3"}
    answers: list[dict[str, Any]] = []

    def call() -> dict[str, Any]:
        answer = seatkit.pass_until(
            SimpleNamespace(advise=False),
            until_step="precombat_main",
            resume=answers[-1]["resume"] if answers else callers_token,
            max_seconds=MODEL_MAX_SECONDS,
            reissue_seconds=120.0,
        )
        answers.append(answer)
        return answer

    returns, answer = returns_until_real_wake(call)

    assert returns == 1
    assert answer["finished"] is True
    assert answer["passes"] == 2, "passes add up across every re-issued attempt"
    assert seen[0] == callers_token
    assert None not in seen, "a re-issue must never be a fresh run that re-stamps the window"
    for index in range(1, len(seen)):
        assert seen[index] == {"started": "upkeep-3", "hops": index}, (
            "every re-issue carries the previous attempt's own resume token"
        )


def test_pass_until_still_hands_back_with_resume_once_the_outer_bound_runs_out(monkeypatch):
    clock = fake_clock(monkeypatch)

    def fake_once(seat: Any, *, resume: Any = None, **kwargs: Any) -> dict[str, Any]:
        return hand_back({"started": "upkeep-3"}, 0)

    monkeypatch.setattr(seatkit, "_pass_until_once", fake_once)

    answer = seatkit.pass_until(
        SimpleNamespace(advise=False),
        until_step="precombat_main",
        max_seconds=MODEL_MAX_SECONDS,
        reissue_seconds=120.0,
    )

    assert answer["finished"] is False
    assert answer["resume"] == {"started": "upkeep-3"}
    assert 120.0 <= clock.now <= 121.0
    assert "waited about 120s" in answer["stopped_because"], answer["stopped_because"]
    assert "retryable" not in answer


def test_pass_until_never_reissues_a_work_cap_hand_back(monkeypatch):
    fake_clock(monkeypatch)
    calls: list[Any] = []

    def fake_once(seat: Any, *, resume: Any = None, **kwargs: Any) -> dict[str, Any]:
        calls.append(resume)
        answer = hand_back({"started": "upkeep-3"}, 16)
        answer["retryable"] = False
        return answer

    monkeypatch.setattr(seatkit, "_pass_until_once", fake_once)

    answer = seatkit.pass_until(
        SimpleNamespace(advise=False),
        until_step="precombat_main",
        max_seconds=MODEL_MAX_SECONDS,
        reissue_seconds=120.0,
    )

    assert answer["finished"] is False
    assert len(calls) == 1


def test_pass_until_never_reissues_an_interrupted_wait(monkeypatch):
    clock = fake_clock(monkeypatch)
    calls: list[Any] = []

    def fake_once(seat: Any, *, resume: Any = None, **kwargs: Any) -> dict[str, Any]:
        calls.append(resume)
        return hand_back({"started": "upkeep-3"}, 0)

    monkeypatch.setattr(seatkit, "_pass_until_once", fake_once)

    answer = seatkit.pass_until(
        SimpleNamespace(advise=False),
        until_step="precombat_main",
        max_seconds=MODEL_MAX_SECONDS,
        reissue_seconds=120.0,
        should_stop=lambda: clock.now >= 5.0,
    )

    assert answer["stopped_because"] == WAIT_INTERRUPTED
    assert clock.now < 7.0


def test_an_armed_destination_wait_is_reissued_into_the_same_window(monkeypatch):
    """`_wait_for_own_priority_at_destination` gives up with `finished: false`
    and the run's ORIGINAL stamp as `resume` (S2 arming re-review finding 1).
    The re-issue must hand that very stamp back - never a fresh run, which
    would exempt the window and skip to the next occurrence - and arming must
    judge exactly once, when this seat finally holds priority there."""
    clock = fake_clock(monkeypatch)
    original_run = {"started_step": "upkeep", "started_turn": 3, "known_stack_ids": []}
    seen: list[Any] = []
    judged_at: list[float] = []

    def fake_once(seat: Any, *, resume: Any = None, **kwargs: Any) -> dict[str, Any]:
        seen.append(resume)
        return {
            "stopped_because": "reached end_step",
            "passes": 1,
            "finished": True,
            "until": {"step": "end_step", "whose_turn": "opponent"},
            "state": {"priority_holder": "bob", "step": "end_step"},
            "available_actions": {},
            "recent_log": {},
            "_reached_run": dict(original_run),
        }

    def fake_destination_wait(
        seat, result, until_step, deadline, should_stop, include_actions, run, stop_on_stack
    ):
        if deadline > OPPONENT_THINKS_SECONDS:
            clock.now = max(clock.now, OPPONENT_THINKS_SECONDS)
            result["state"] = {"priority_holder": "alice", "step": "end_step"}
            return result
        clock.now = deadline
        result["finished"] = False
        result["stopped_because"] = seatkit._own_priority_wait_note(until_step)
        result["resume"] = dict(run)
        return result

    def judge(*args: Any, **kwargs: Any) -> dict[str, Any]:
        judged_at.append(clock.now)
        return {"applying": []}

    monkeypatch.setattr(seatkit, "_pass_until_once", fake_once)
    monkeypatch.setattr(seatkit, "_wait_for_own_priority_at_destination", fake_destination_wait)
    monkeypatch.setattr(seatkit, "_judge_armed_scripts", judge)

    answer = seatkit.pass_until(
        SimpleNamespace(advise=False, player="alice"),
        until_step="end_step",
        whose_turn="opponent",
        max_seconds=MODEL_MAX_SECONDS,
        reissue_seconds=120.0,
        arm=["flash-in"],
        arm_reasoning="flash a threat in at their end step",
    )

    assert answer["finished"] is True
    assert answer["state"]["priority_holder"] == "alice"
    assert seen[0] is None
    assert len(seen) >= 2
    assert all(token == original_run for token in seen[1:]), seen
    assert judged_at == [OPPONENT_THINKS_SECONDS]


# -- the MCP server: which transport gets which outer bound --------------------


def test_each_transport_gets_an_outer_bound_under_its_own_client_limit():
    """Claude Code (code.claude.com/docs/en/mcp): a stdio server has no
    per-request timer, only a 30-minute idle window; an HTTP server has a
    per-request timer of at least 60 seconds. Judging armed scripts and
    building the answer ride on top of the bound, so each keeps slack."""
    stdio = mcp_module.IDLE_REISSUE_SECONDS["stdio"]
    http = mcp_module.IDLE_REISSUE_SECONDS["http"]
    assert stdio > OPPONENT_THINKS_SECONDS
    assert stdio + 60.0 < 30 * 60
    assert http + 10.0 < 60.0
    assert http >= seatkit.MAX_WAIT_SECONDS


def test_main_wires_the_transport_bound_into_the_server(monkeypatch):
    built: dict[str, Any] = {}

    def fake_build(seats: Any, **kwargs: Any) -> Any:
        built.update(kwargs)
        return SimpleNamespace(run=lambda transport: None)

    monkeypatch.setattr(mcp_module, "build_server", fake_build)
    monkeypatch.setattr(mcp_module.seatkit, "api_trouble", lambda *a: None)
    monkeypatch.setattr(mcp_module, "_serve_http", lambda *a: None)
    for transport in ("stdio", "http"):
        monkeypatch.setattr(
            "sys.argv", ["mcp", "--transport", transport, "--game", "g", "--api", "http://x"]
        )
        mcp_module.main()
        assert built["idle_reissue_seconds"] == mcp_module.IDLE_REISSUE_SECONDS[transport]


def test_the_mcp_wait_rides_out_an_idle_opponent_in_one_tool_call(client, game, monkeypatch):
    """A real game: alice bolts bob, bob thinks, then passes. One MCP
    `wait_for_priority` call - its own capped wait shrunk to 0.2s - comes back
    once, with the real wake, not with `nothing new yet`."""
    from conftest import add_card, find, state
    from test_mcp_input_required import call_tool

    monkeypatch.setattr(seatkit, "MAX_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    from magic_cards_model_api.store import store

    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    seats = Seats(api="http://testserver", game_id=game)
    alice = seats.seat_for(uids["alice"])
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")["id"]
    bolt = board["hands"]["alice"][0]["id"]
    cast = run_action(
        alice,
        lambda: alice.call_json(
            "POST",
            f"/api/games/{game}/actions/cast",
            {"player": "alice", "card_id": bolt, "targets": ["bob"], "mana_sources": [mountain]},
        ),
    )
    assert cast["wake_reason"] == NOTHING_NEW_YET

    waits: list[str] = []
    real_block = seatkit.block_until_wake

    def counting_block(*args: Any, **kwargs: Any) -> dict[str, Any]:
        woken = real_block(*args, **kwargs)
        waits.append(woken["wake_reason"])
        return woken

    monkeypatch.setattr(seatkit, "block_until_wake", counting_block)

    def bob_passes_later() -> None:
        time.sleep(1.0)
        TestClient(app).post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})

    server = build_server(seats, idle_reissue_seconds=10.0)
    thinker = threading.Thread(target=bob_passes_later)
    thinker.start()
    try:
        result = call_tool(
            server, "wait_for_priority", {"player_uid": uids["alice"], "max_seconds": 5.0}
        )
    finally:
        thinker.join()

    answer = json.loads(result.content[0].text)
    assert "you have priority" in answer["wake_reason"], answer["wake_reason"]
    assert len(waits) >= 2, "the capped wait should have been re-issued under the tool call"
    assert waits[:-1] == [NOTHING_NEW_YET] * (len(waits) - 1)


def test_the_mcp_server_default_leaves_a_capped_wait_as_it_was(client, game, monkeypatch):
    """`build_server(seats)` without a bound keeps the historical one-shot
    wait (tests and embedders); only `main()` opts each transport in."""
    from test_mcp_input_required import call_tool

    monkeypatch.setattr(seatkit, "MAX_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    from magic_cards_model_api.store import store

    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    seats = Seats(api="http://testserver", game_id=game)
    server = build_server(seats)

    started = time.monotonic()
    result = call_tool(server, "wait_for_priority", {"player_uid": uids["bob"], "max_seconds": 5.0})

    answer = json.loads(result.content[0].text)
    assert answer["wake_reason"] == NOTHING_NEW_YET
    assert time.monotonic() - started < 2.0
