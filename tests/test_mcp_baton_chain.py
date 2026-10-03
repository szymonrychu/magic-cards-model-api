"""S1: the baton chain in the seat MCP server.

`pass_baton` queues a `next` token instead of revoking the live one; the live
token rotates on the server at the END OF THE PRIORITY WINDOW (the acting
call that gave priority away returns), under the same lock as the per-uid
acting lock; the future agent blocks in `await_baton` until its token is
live. Concurrency is driven with threads against the in-memory test app.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from conftest import add_card, force_turn_position, state
from magic_cards_model.machines.turn import Step
from test_autopass import Opponent, stock_libraries
from test_mcp_baton import call, log_len, wired
from test_mulligans import PLAIN_DECK, boot, declare, delete
from test_playscript_runtime import REASONING, land_script, register

from magic_cards_model_api import mcp
from magic_cards_model_api.baton import (
    BATON_BUSY,
    BATON_NOT_YET,
    BATON_OK,
    BATON_REQUIRED,
    BATON_SPENT,
    BATON_STALE,
    SPENT_MEMORY,
    WINDOW_CLOSED,
    BatonStore,
)

# ---- the store ----


def test_first_baton_is_live_and_later_ones_queue_as_next():
    store = BatonStore()
    first = store.pass_baton("u")
    assert first["state"] == "live"
    second = store.pass_baton("u")
    assert second["state"] == "next"
    assert store.check("u", first["baton"]) == BATON_OK
    assert store.check("u", second["baton"]) == BATON_NOT_YET
    third = store.pass_baton("u")
    assert third["state"] == "next"
    # The earlier unpromoted next dies; the live token is never revoked.
    assert store.check("u", second["baton"]) == BATON_STALE
    assert store.check("u", third["baton"]) == BATON_NOT_YET
    assert store.check("u", first["baton"]) == BATON_OK


def test_a_window_closing_call_spends_live_and_promotes_next():
    store = BatonStore()
    live = store.issue("u")
    future = store.issue("u")
    verdict, cast = store.begin("u", "cast", live)
    assert verdict == BATON_OK and cast is not None
    assert store.finish(cast, committed=True, closes_window=False) is False
    assert store.check("u", live) == BATON_OK
    verdict, passing = store.begin("u", "pass", live)
    assert verdict == BATON_OK and passing is not None
    passing.mark_passed({"log_seq": 7})
    rotated = store.finish(passing, committed=True, closes_window=True, reasoning="r", seq=7)
    assert rotated is True
    assert store.check("u", live) == BATON_SPENT
    assert store.check("u", future) == BATON_OK
    assert store.last_commit("u") == {
        "tool": "pass",
        "reasoning": "r",
        "seq": 7,
        "window_actions": ["cast", "pass"],
    }
    assert store.spent_detail("u", live) == {"tool": "pass", "seq": 7}


def test_rotation_without_a_next_leaves_the_seat_without_a_live_token():
    store = BatonStore()
    live = store.issue("u")
    _, flight = store.begin("u", "pass", live)
    assert flight is not None
    store.finish(flight, committed=True, closes_window=True)
    assert store.check("u", "") == BATON_REQUIRED
    assert store.check("u", live) == BATON_SPENT
    assert store.pass_baton("u")["state"] == "live"


def test_the_spent_set_is_bounded():
    store = BatonStore()
    spent: list[str] = []
    for _ in range(SPENT_MEMORY + 6):
        token = store.issue("u")
        _, flight = store.begin("u", "pass", token)
        assert flight is not None
        store.finish(flight, committed=True, closes_window=True)
        spent.append(token)
    assert store.check("u", spent[-1]) == BATON_SPENT
    assert store.check("u", spent[-SPENT_MEMORY]) == BATON_SPENT
    assert store.check("u", spent[0]) == BATON_STALE


def test_the_acting_lock_refuses_a_second_call_and_releases_on_finish():
    store = BatonStore()
    live = store.issue("u")
    verdict, flight = store.begin("u", "cast", live)
    assert verdict == BATON_OK and flight is not None
    busy, other = store.begin("u", "pass", live)
    assert busy == BATON_BUSY
    assert other is not None and other.tool == "cast"
    assert store.in_flight("u") == "cast"
    store.finish(flight, committed=True, closes_window=False)
    assert store.in_flight("u") is None
    verdict, again = store.begin("u", "pass", live)
    assert verdict == BATON_OK and again is not None


def test_the_acting_lock_holds_for_an_unenforced_uid_without_enforcing_it():
    store = BatonStore()
    verdict, flight = store.begin("x", "cast", "")
    assert verdict == BATON_OK and flight is not None
    assert store.begin("x", "pass", "")[0] == BATON_BUSY
    assert not store.enforced("x")
    store.finish(flight, committed=True, closes_window=True)
    assert store.begin("x", "pass", "")[0] == BATON_OK
    assert not store.enforced("x")


def test_a_revoke_with_nothing_in_flight_applies_at_once():
    store = BatonStore()
    live = store.issue("u")
    answer = store.pass_baton("u", revoke=True)
    assert answer["revoke"] == "applied"
    assert answer["state"] == "live"
    assert store.check("u", live) == BATON_SPENT
    assert store.check("u", answer["baton"]) == BATON_OK


def test_a_revoke_behind_a_non_wait_call_is_queued_until_it_returns():
    store = BatonStore()
    live = store.issue("u")
    _, flight = store.begin("u", "cast", live)
    assert flight is not None
    answer = store.pass_baton("u", revoke=True)
    assert answer["revoke"] == "queued_behind:cast"
    assert answer["in_flight"] == "cast"
    assert not flight.interrupt.is_set()
    assert store.check("u", live) == BATON_OK
    store.finish(flight, committed=True, closes_window=False)
    assert store.check("u", live) == BATON_SPENT
    assert store.check("u", answer["baton"]) == BATON_OK


def test_a_revoke_interrupts_a_wait_and_applies_once_it_returns():
    store = BatonStore()
    live = store.issue("u")
    _, flight = store.begin("u", "wait_for_priority", live)
    assert flight is not None

    def waiter() -> None:
        assert flight.interrupt.wait(5)
        store.finish(flight, committed=False, closes_window=False)

    thread = threading.Thread(target=waiter)
    thread.start()
    answer = store.pass_baton("u", revoke=True)
    thread.join(5)
    assert answer["revoke"] == "applied"
    assert store.check("u", live) == BATON_SPENT
    assert store.check("u", answer["baton"]) == BATON_OK


def test_await_live_answers_at_once_for_a_token_that_can_never_go_live():
    store = BatonStore()
    live = store.issue("u")
    _, flight = store.begin("u", "pass", live)
    assert flight is not None
    store.finish(flight, committed=True, closes_window=True)
    started = time.monotonic()
    assert store.await_live("u", live, 30) == "spent"
    assert store.await_live("u", "bogus", 30) == "stale"
    assert time.monotonic() - started < 1
    nxt = store.issue("u")
    assert store.await_live("u", nxt, 30) == "live"


def test_await_live_times_out_as_next_and_wakes_on_rotation():
    store = BatonStore()
    live = store.issue("u")
    future = store.issue("u")
    assert store.await_live("u", future, 0.1) == "next"
    seen: list[tuple[str, float]] = []

    def wait() -> None:
        seen.append((store.await_live("u", future, 10), time.monotonic()))

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.2)
    assert not seen
    _, flight = store.begin("u", "pass", live)
    assert flight is not None
    store.finish(flight, committed=True, closes_window=True)
    rotated_at = time.monotonic()
    thread.join(5)
    assert seen[0][0] == "live"
    assert seen[0][1] - rotated_at < 1


# ---- the MCP surface ----


def served(client, game, monkeypatch, **kwargs: Any):
    seats, uids = wired(client, monkeypatch, game, **kwargs)
    return seats, uids, mcp.build_server(seats)


def baton_answer(server: Any, uid: str, **extra: Any) -> dict[str, Any]:
    return json.loads(call(server, "pass_baton", {"player_uid": uid, **extra}))


def as_json(text: str) -> dict[str, Any]:
    return json.loads(text)


def rotated(text: str) -> bool:
    return WINDOW_CLOSED in text and json.loads(text).get("baton_rotated") is True


def test_pass_baton_answers_state_holder_since_last_commit_and_in_flight(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    first = baton_answer(server, uids["alice"])
    assert first["state"] == "live"
    assert first["live_holder_since"].startswith("20")
    assert first["last_commit"] is None
    assert first["in_flight"] is None
    second = baton_answer(server, uids["alice"])
    assert second["state"] == "next"
    assert second["live_holder_since"] == first["live_holder_since"]
    assert seats.baton.check(uids["alice"], first["baton"]) == BATON_OK


def test_refusal_codes_name_the_window_and_the_fix(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    live = baton_answer(server, uid)["baton"]
    future = baton_answer(server, uid)["baton"]
    add_card(client, game, "Plains", "alice", "hand")
    land = {"player_uid": uid, "card": "Plains", "reasoning": "x"}

    assert call(server, "play_land", land).startswith("BATON_REQUIRED:")
    assert call(server, "play_land", {**land, "baton": "bogus"}).startswith("BATON_STALE:")
    assert (
        call(server, "play_land", {**land, "baton": future})
        == "BATON_NOT_YET: you are the future agent; call await_baton"
    )
    passed = call(server, "pass", {"player_uid": uid, "reasoning": "done", "baton": live})
    assert rotated(passed), passed
    seq = as_json(passed)["outcome"]["log_seq"]
    before = log_len(client, game, uid)
    assert call(server, "play_land", {**land, "baton": live}) == (
        f"BATON_SPENT: your priority window closed (pass at seq {seq}); "
        "end your run with your committed line"
    )
    assert log_len(client, game, uid) == before


# -- rotation: every path that gives priority away --


def test_pass_closes_the_window(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    live = baton_answer(server, uid)["baton"]
    future = baton_answer(server, uid)["baton"]
    passed = call(server, "pass", {"player_uid": uid, "reasoning": "nothing", "baton": live})
    assert rotated(passed), passed
    assert seats.baton.check(uid, live) == BATON_SPENT
    assert seats.baton.check(uid, future) == BATON_OK
    commit = seats.baton.last_commit(uid)
    assert commit["tool"] == "pass" and commit["reasoning"] == "nothing"
    assert commit["window_actions"] == ["pass"]


def test_a_refused_pass_does_not_close_the_window(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["bob"]  # alice holds priority: bob's pass is refused
    live = baton_answer(server, uid)["baton"]
    answer = call(server, "pass", {"player_uid": uid, "reasoning": "x", "baton": live})
    assert as_json(answer)["outcome"]["allowed"] is False
    assert "baton_rotated" not in answer
    assert seats.baton.check(uid, live) == BATON_OK


def test_actions_that_hold_priority_keep_the_same_current(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    add_card(client, game, "Plains", "alice", "hand")
    live = baton_answer(server, uid)["baton"]
    land = call(
        server, "play_land", {"player_uid": uid, "card": "Plains", "reasoning": "x", "baton": live}
    )
    assert as_json(land)["outcome"]["allowed"] is True and "baton_rotated" not in land
    cast = call(
        server,
        "cast",
        {
            "player_uid": uid,
            "card": "Shock",
            "targets": "bob",
            "hold_priority": True,
            "reasoning": "x",
            "baton": live,
        },
    )
    assert as_json(cast)["outcome"]["allowed"] is True and "baton_rotated" not in cast
    assert seats.baton.check(uid, live) == BATON_OK


def test_an_action_auto_pass_closes_the_window(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    live = baton_answer(server, uid)["baton"]
    cast = call(
        server,
        "cast",
        {"player_uid": uid, "card": "Shock", "targets": "bob", "reasoning": "x", "baton": live},
    )
    assert rotated(cast), cast
    assert seats.baton.last_commit(uid)["window_actions"] == ["cast"]


def test_then_pass_closes_the_window(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    add_card(client, game, "Plains", "alice", "hand")
    live = baton_answer(server, uid)["baton"]
    land = call(
        server,
        "play_land",
        {"player_uid": uid, "card": "Plains", "reasoning": "x", "then_pass": True, "baton": live},
    )
    assert rotated(land), land
    assert seats.baton.check(uid, live) == BATON_SPENT


def test_a_pass_inside_pass_until_closes_the_window(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    stock_libraries(client, game)
    uid = uids["alice"]
    live = baton_answer(server, uid)["baton"]
    with Opponent(game, "bob"):
        answer = call(
            server,
            "pass_until",
            {
                "player_uid": uid,
                "step": "end_step",
                "whose_turn": "mine",
                "max_seconds": 10,
                "reasoning": "nothing to do this turn",
                "baton": live,
            },
        )
    assert rotated(answer), answer
    assert seats.baton.last_commit(uid)["tool"] == "pass_until"


def test_a_run_script_whose_moves_passed_closes_the_window(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    register(
        client, game, uid, land_script("drop", moves=[*land_script("x")["moves"], {"kind": "pass"}])
    )
    add_card(client, game, "Island", "alice", "hand")
    live = baton_answer(server, uid)["baton"]
    answer = call(
        server,
        "run_script",
        {"player_uid": uid, "script": "drop", "reasoning": REASONING, "baton": live},
    )
    assert rotated(answer), answer
    assert seats.baton.last_commit(uid)["window_actions"] == ["run_script"]


def test_a_run_script_that_did_not_pass_keeps_the_window(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    register(client, game, uid, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")
    live = baton_answer(server, uid)["baton"]
    answer = call(
        server,
        "run_script",
        {"player_uid": uid, "script": "drop", "reasoning": REASONING, "baton": live},
    )
    assert as_json(answer)["script_exit"]["exit"] == "chain_complete", answer
    assert "baton_rotated" not in answer
    assert seats.baton.check(uid, live) == BATON_OK


def idle_background(client, game, monkeypatch):
    """Bob's upkeep, bob holding priority: alice's pass_until passes nothing
    and leaves a background run going."""
    seats, uids, server = served(client, game, monkeypatch)
    stock_libraries(client, game)
    force_turn_position(game, Step.UPKEEP, "bob")
    uid = uids["alice"]
    live = baton_answer(server, uid)["baton"]
    arguments = {
        "player_uid": uid,
        "step": "upkeep",
        "whose_turn": "mine",
        "stop_when_attacked": False,
        "reasoning": "nothing to do on their turn",
        "baton": live,
    }
    first = call(server, "pass_until", arguments)
    assert as_json(first)["finished"] is False
    assert seats.background.status(uid) == "running"
    return seats, uids, server, live, first


def test_the_background_runs_first_pass_closes_the_window(client, game, monkeypatch):
    seats, uids, server, live, first = idle_background(client, game, monkeypatch)
    uid = uids["alice"]
    assert "baton_rotated" not in first
    assert seats.baton.check(uid, live) == BATON_OK
    future = baton_answer(server, uid)["baton"]
    with Opponent(game, "bob"):
        give_up = time.monotonic() + 30
        while seats.baton.check(uid, live) == BATON_OK:
            assert time.monotonic() < give_up
            time.sleep(0.05)
    assert seats.baton.check(uid, live) == BATON_SPENT
    assert seats.baton.check(uid, future) == BATON_OK
    assert seats.baton.last_commit(uid)["tool"] == "pass_until"
    seats.background.stop(uid)


def test_the_pregame_call_that_completes_the_seats_pregame_closes_the_window(client, monkeypatch):
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    try:
        seats, _uids, server = served(client, info["game_id"], monkeypatch)
        uid = info["seat_uids"]["alice"]
        live = baton_answer(server, uid)["baton"]
        kept = call(
            server,
            "mulligan",
            {"player_uid": uid, "take": False, "reasoning": "fine hand", "baton": live},
        )
        assert kept.startswith("HTTP 200"), kept
        # Her CR 103.6 opening-hand finish is still owed: not complete yet.
        assert "baton_rotated" not in kept
        assert seats.baton.check(uid, live) == BATON_OK
        assert declare(client, info, "bob", take=False).status_code == 200
        done = call(
            server,
            "opening_hand_action",
            {"player_uid": uid, "reasoning": "nothing to do", "baton": live},
        )
        assert done.startswith("HTTP 200"), done
        assert "baton_rotated: true" in done and WINDOW_CLOSED in done
        assert seats.baton.check(uid, live) == BATON_SPENT
        assert seats.baton.last_commit(uid)["window_actions"] == [
            "mulligan",
            "opening_hand_action",
        ]
    finally:
        delete(client, info)


# -- the acting lock --


def test_a_second_acting_call_is_busy_and_touches_nothing(client, game, monkeypatch):
    seats, uids, server, live, _first = idle_background(client, game, monkeypatch)
    uid = uids["alice"]
    verdict, flight = seats.baton.begin(uid, "cast", live)
    assert verdict == BATON_OK and flight is not None
    before = log_len(client, game, uid)
    busy = call(server, "pass", {"player_uid": uid, "reasoning": "x", "baton": live})
    assert busy == "BATON_BUSY: cast in flight"
    assert seats.background.status(uid) == "running"
    assert log_len(client, game, uid) == before
    # Read-only tools stay unlocked.
    assert '"you": "alice"' in call(server, "battlefield_state", {"player_uid": uid})
    seats.baton.finish(flight, committed=False, closes_window=False)
    seats.background.stop(uid)


def test_the_acting_lock_holds_across_threads(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    # No baton issued: the lock holds even where tokens are not enforced.
    call(server, "pass", {"player_uid": uid, "reasoning": "over to bob"})
    out: list[str] = []
    thread = threading.Thread(
        target=lambda: out.append(
            call(server, "wait_for_priority", {"player_uid": uid, "max_seconds": 2})
        )
    )
    thread.start()
    give_up = time.monotonic() + 5
    while seats.baton.in_flight(uid) is None:
        assert time.monotonic() < give_up
        time.sleep(0.01)
    busy = call(server, "pass", {"player_uid": uid, "reasoning": "x"})
    thread.join(5)
    assert busy == "BATON_BUSY: wait_for_priority in flight"
    assert out and not out[0].startswith("BATON_")


# -- revoke (recovery only) --


def test_revoke_interrupts_an_in_flight_wait(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    force_turn_position(game, Step.UPKEEP, "bob")
    uid = uids["alice"]
    live = baton_answer(server, uid)["baton"]
    out: list[str] = []
    thread = threading.Thread(
        target=lambda: out.append(
            call(
                server,
                "wait_for_priority",
                {"player_uid": uid, "max_seconds": 20, "baton": live},
            )
        )
    )
    thread.start()
    give_up = time.monotonic() + 5
    while seats.baton.in_flight(uid) is None:
        assert time.monotonic() < give_up
        time.sleep(0.01)
    started = time.monotonic()
    answer = baton_answer(server, uid, revoke=True)
    thread.join(5)
    assert time.monotonic() - started < 2
    assert answer["revoke"] == "applied" and answer["state"] == "live"
    assert out[0].startswith("BATON_REVOKED"), out
    assert seats.baton.check(uid, live) == BATON_SPENT
    assert seats.baton.check(uid, answer["baton"]) == BATON_OK


def test_revoke_behind_an_acting_call_is_queued_not_interrupting(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    live = baton_answer(server, uid)["baton"]
    entered, release = threading.Event(), threading.Event()
    real = mcp.run_action

    def slow_run_action(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(mcp, "run_action", slow_run_action)
    out: list[str] = []
    cast = {
        "player_uid": uid,
        "card": "Shock",
        "targets": "bob",
        "hold_priority": True,
        "reasoning": "x",
        "baton": live,
    }
    thread = threading.Thread(target=lambda: out.append(call(server, "cast", cast)))
    thread.start()
    assert entered.wait(5)
    answer = baton_answer(server, uid, revoke=True)
    assert answer["revoke"] == "queued_behind:cast"
    assert answer["in_flight"] == "cast"
    assert seats.baton.check(uid, live) == BATON_OK
    release.set()
    thread.join(5)
    assert as_json(out[0])["outcome"]["allowed"] is True
    assert seats.baton.check(uid, live) == BATON_SPENT
    assert seats.baton.check(uid, answer["baton"]) == BATON_OK


def test_revoke_stops_the_background_run(client, game, monkeypatch):
    seats, uids, server, live, _first = idle_background(client, game, monkeypatch)
    uid = uids["alice"]
    answer = baton_answer(server, uid, revoke=True)
    assert answer["revoke"] == "applied"
    assert answer["background"]["stopped_because"] == "BATON_REVOKED"
    assert seats.background.status(uid) is None
    assert seats.baton.check(uid, live) == BATON_SPENT


# -- await_baton --


def test_await_baton_is_exempt_and_answers_every_state(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    live = baton_answer(server, uid)["baton"]
    future = baton_answer(server, uid)["baton"]

    def wait(token: str, seconds: float) -> dict[str, Any]:
        return as_json(
            call(
                server,
                "await_baton",
                {"player_uid": uid, "baton": token, "max_seconds": seconds},
            )
        )

    assert wait(future, 0.1) == {"live": False, "state": "next"}
    assert wait("bogus", 30) == {"live": False, "state": "stale"}
    now_live = wait(live, 30)
    assert now_live["live"] is True
    assert set(now_live) == {"live", "last_commit", "pending_decision", "board"}
    assert now_live["board"]["battlefield"] == {"alice": [], "bob": []}
    call(server, "pass", {"player_uid": uid, "reasoning": "x", "baton": live})
    started = time.monotonic()
    assert wait(live, 30) == {"live": False, "state": "spent"}
    assert time.monotonic() - started < 1
    promoted = wait(future, 30)
    assert promoted["live"] is True
    assert promoted["last_commit"]["tool"] == "pass"


def test_await_baton_caps_max_seconds_at_600(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    baton_answer(server, uid)
    future = baton_answer(server, uid)["baton"]
    seen: list[float] = []
    real = seats.baton.await_live

    def spy(uid_: str, token: str, max_seconds: float, *args: Any, **kwargs: Any) -> str:
        seen.append(max_seconds)
        return real(uid_, token, 0.0, *args, **kwargs)

    monkeypatch.setattr(seats.baton, "await_live", spy)
    call(server, "await_baton", {"player_uid": uid, "baton": future, "max_seconds": 5000})
    assert seen == [600.0]


# ---- acceptance ----


def shock_board(client, game) -> None:
    for _ in range(2):
        add_card(client, game, "Mountain", "alice", "battlefield")
        add_card(client, game, "Shock", "alice", "hand")
    stock_libraries(client, game)


def cast_shock(server: Any, uid: str, baton: str) -> str:
    return call(
        server,
        "cast",
        {
            "player_uid": uid,
            "card": "Shock",
            "targets": "bob",
            "hold_priority": True,
            "reasoning": "burn them",
            "baton": baton,
        },
    )


def await_in_thread(server: Any, uid: str, token: str) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def run() -> None:
        answer = call(server, "await_baton", {"player_uid": uid, "baton": token, "max_seconds": 30})
        out.append((json.loads(answer), time.monotonic()))

    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


def pass_until_end(server: Any, uid: str, baton: str, out: list[Any]) -> None:
    answer = call(
        server,
        "pass_until",
        {
            "player_uid": uid,
            "step": "end_step",
            "whose_turn": "mine",
            "max_seconds": 10,
            "reasoning": "let them resolve",
            "baton": baton,
        },
    )
    out.append((answer, time.monotonic()))


def test_acceptance_two_casts_then_pass_until_hand_the_window_on(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    shock_board(client, game)
    uid = uids["alice"]
    current = baton_answer(server, uid)["baton"]
    future = baton_answer(server, uid)["baton"]
    waiter, awaited = await_in_thread(server, uid, future)

    for _ in range(2):
        cast = cast_shock(server, uid, current)
        assert as_json(cast)["outcome"]["allowed"] is True, cast
        assert "baton_rotated" not in cast
        assert seats.baton.check(uid, current) == BATON_OK
    time.sleep(0.2)
    assert not awaited, "await_baton returned before the window closed"

    passed: list[Any] = []
    with Opponent(game, "bob"):
        pass_until_end(server, uid, current, passed)
    waiter.join(5)
    answer, returned_at = passed[0]
    assert rotated(answer), answer
    woken, woken_at = awaited[0]
    assert woken["live"] is True
    assert woken_at - returned_at < 1
    assert woken["last_commit"]["window_actions"] == ["cast", "cast", "pass_until"]
    assert cast_shock(server, uid, current).startswith("BATON_SPENT:")


def test_acceptance_pass_baton_mid_pass_until_queues_without_stale(client, game, monkeypatch):
    seats, uids, server = served(client, game, monkeypatch)
    shock_board(client, game)
    uid = uids["alice"]
    current = baton_answer(server, uid)["baton"]
    answers: list[str] = []
    for _ in range(2):
        answers.append(cast_shock(server, uid, current))

    passed: list[Any] = []
    runner = threading.Thread(target=pass_until_end, args=(server, uid, current, passed))
    runner.start()
    give_up = time.monotonic() + 5
    while seats.baton.in_flight(uid) != "pass_until":
        assert time.monotonic() < give_up
        time.sleep(0.01)
    queued = baton_answer(server, uid)
    assert queued["state"] == "next"
    assert queued["in_flight"] == "pass_until"
    waiter, awaited = await_in_thread(server, uid, queued["baton"])
    with Opponent(game, "bob"):
        runner.join(15)
    waiter.join(5)
    answer, returned_at = passed[0]
    answers.append(answer)
    assert rotated(answer), answer
    woken, woken_at = awaited[0]
    assert woken["live"] is True and woken_at - returned_at < 1
    answers.append(cast_shock(server, uid, current))
    assert answers[-1].startswith("BATON_SPENT:")
    assert not any("BATON_STALE" in text for text in answers), answers
    assert state(client, game)["step"] == "end_step"


# -- S4: baton refusals and rotations as structured log lines --


def _baton_lines(caplog: Any, prefix: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def test_every_baton_refusal_logs_code_tool_and_uid(client, game, monkeypatch, caplog):
    caplog.set_level("INFO", logger="mcm.mcp")
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    live = baton_answer(server, uid)["baton"]
    future = baton_answer(server, uid)["baton"]
    add_card(client, game, "Plains", "alice", "hand")
    land = {"player_uid": uid, "card": "Plains", "reasoning": "x"}

    call(server, "play_land", land)
    call(server, "play_land", {**land, "baton": "bogus"})
    call(server, "play_land", {**land, "baton": future})
    call(server, "pass", {"player_uid": uid, "reasoning": "done", "baton": live})
    call(server, "play_land", {**land, "baton": live})

    assert _baton_lines(caplog, "baton_refusal") == [
        f"baton_refusal code=BATON_REQUIRED tool=play_land uid={uid}",
        f"baton_refusal code=BATON_STALE tool=play_land uid={uid}",
        f"baton_refusal code=BATON_NOT_YET tool=play_land uid={uid}",
        f"baton_refusal code=BATON_SPENT tool=play_land uid={uid}",
    ]


def test_an_advisor_asking_for_a_baton_is_a_logged_refusal(client, game, monkeypatch, caplog):
    from magic_cards_model_api.repository import Advisor
    from magic_cards_model_api.store import store

    caplog.set_level("INFO", logger="mcm.mcp")
    seats, _uids = wired(client, monkeypatch, game, resolve_by_uid=True)
    server = mcp.build_server(seats)
    store.register_advisors(game, {"buddy-uid": Advisor(player="alice")})
    assert call(server, "pass_baton", {"player_uid": "buddy-uid"}).startswith("BATON_ADVISOR:")
    assert _baton_lines(caplog, "baton_refusal") == [
        "baton_refusal code=BATON_ADVISOR tool=pass_baton uid=buddy-uid"
    ]


def test_a_rotation_logs_the_uid_and_the_windows_action_count(client, game, monkeypatch, caplog):
    caplog.set_level("INFO", logger="mcm.mcp")
    seats, uids, server = served(client, game, monkeypatch)
    uid = uids["alice"]
    live = baton_answer(server, uid)["baton"]
    baton_answer(server, uid)
    add_card(client, game, "Plains", "alice", "hand")
    call(
        server, "play_land", {"player_uid": uid, "card": "Plains", "reasoning": "x", "baton": live}
    )
    assert _baton_lines(caplog, "baton_rotated") == []
    passed = call(server, "pass", {"player_uid": uid, "reasoning": "done", "baton": live})
    assert rotated(passed), passed
    assert _baton_lines(caplog, "baton_rotated") == [f"baton_rotated uid={uid} window_actions=2"]


def test_a_background_rotation_logs_too(client, game, monkeypatch, caplog):
    caplog.set_level("INFO", logger="mcm.mcp")
    seats, uids, server, live, _first = idle_background(client, game, monkeypatch)
    uid = uids["alice"]
    baton_answer(server, uid)
    with Opponent(game, "bob"):
        give_up = time.monotonic() + 30
        while seats.baton.check(uid, live) == BATON_OK:
            assert time.monotonic() < give_up
            time.sleep(0.05)
    seats.background.stop(uid)
    lines = _baton_lines(caplog, "baton_rotated")
    assert len(lines) == 1 and lines[0].startswith(f"baton_rotated uid={uid} window_actions=")
