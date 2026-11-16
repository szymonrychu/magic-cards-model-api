"""Claude-seat sim matches: the pieces they share with the lobby."""

from __future__ import annotations

import pytest
from sim_claude_kit import (
    GREEN,
    OPUS_LOW,
    RED,
    Clock,
    _assert_no_uids,
    _call_tool,
    _end_game,
    _finish,
    _game_id,
    _gated_boot,
    _manager,
    _start,
    _tool_server,
    _wait,
)
from sim_claude_kit import fake_registry as fixture_registry

fake_registry = fixture_registry  # the fixture, bound under the name tests request


def test_boot_decklist_game_registers_no_ui_game():
    from magic_cards_model_api.decks_api import boot_decklist_game
    from magic_cards_model_api.store import store

    game_id, seat_uids, match = boot_decklist_game(
        [("a", RED, True), ("b", GREEN, True)], seed=1, owner_user_id=None
    )
    try:
        assert game_id not in store.ui_games()
        assert store.ai_players(game_id) == {"a", "b"}
        assert set(seat_uids) == {"a", "b"}
        assert match.games[-1] is not None
    finally:
        store.delete(game_id)


# -- ClaudeMatchManager and the sim_match_claude_* tools (Task 14) ----------


import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from typing import Any  # noqa: E402


def test_start_boots_two_ai_seats_outside_the_lobby(fake_registry):
    from magic_cards_model_api.sim.claude import SIM_OWNER
    from magic_cards_model_api.store import store

    manager = _manager()
    out = _start(manager, RED, GREEN, seed=1)
    assert out["status"] == "starting"
    assert re.fullmatch(r"claude-[0-9a-f]{32}", out["match_id"])
    assert out["expected_minutes"] == "33-55 per game"
    assert out["poll_every_s"] == 300
    game_id = _game_id(manager, out["match_id"])
    assert game_id not in store.ui_games()
    assert store.owner_user_id(game_id) == SIM_OWNER
    assert store.ai_players(game_id) == {"a", "b"}
    uids = store.seat_uids(game_id)
    assert {s["uid"] for s in fake_registry.started} == set(uids)
    assert {s["player"] for s in fake_registry.started} == {"a", "b"}
    assert all(s["game_id"] == game_id for s in fake_registry.started)
    _finish(manager, out["match_id"])


def test_live_ids_lists_the_held_matches(fake_registry):
    manager = _manager()
    assert manager.live_ids() == []

    out = _start(manager, RED, GREEN, seed=1)

    assert manager.live_ids() == [out["match_id"]]
    _finish(manager, out["match_id"])


def test_second_claude_match_refused(fake_registry):
    from magic_cards_model_api.sim.manager import SimRefused

    clock = Clock()
    manager = _manager(clock)
    first = _start(manager, RED, GREEN, seed=1)
    clock.now += 12 * 60
    with pytest.raises(SimRefused, match="12 min"):
        manager.start(RED, GREEN, seed=2)
    assert len(fake_registry.started) == 2
    _finish(manager, first["match_id"])


def test_refused_outside_kubernetes_mode(fake_registry, monkeypatch):
    from magic_cards_model_api import ai_seat
    from magic_cards_model_api.sim.manager import SimRefused

    monkeypatch.setattr(ai_seat, "ai_seat_mode", lambda: "subprocess")
    with pytest.raises(SimRefused, match="kubernetes"):
        _manager().start(RED, GREEN, seed=1)
    assert fake_registry.started == []


def test_status_reports_running_then_done_and_cleans_up(fake_registry):
    from magic_cards_model_api.store import store

    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    uids = set(store.seat_uids(game_id))
    manager.reap()  # the reaper reads the seats; a poll answers from its cache
    running = manager.status(match_id)
    assert running["status"] == "running"
    assert running["seats"] == {
        "a": {"state": "thinking", **OPUS_LOW},
        "b": {"state": "thinking", **OPUS_LOW},
    }
    assert "support_warnings" in running
    assert fake_registry.stopped == []

    _end_game(game_id, "b")
    manager.reap()
    done = manager.status(match_id)
    assert done["status"] == "done"
    assert done["result"]["winner"] == "a"
    assert done["result"]["drawn"] is False
    assert 1 <= len(done["result"]["last_log"]) <= 10
    assert sorted(fake_registry.stopped) == sorted(uids)
    assert store.get(game_id) is None
    again = manager.status(match_id)
    assert again["status"] == "done" and again["result"] == done["result"]
    assert len(fake_registry.stopped) == 2


def test_deadline_stops_seats_and_reports_deadline(fake_registry):
    from magic_cards_model_api.store import store

    clock = Clock()
    manager = _manager(clock, MTG_SIM_CLAUDE_DEADLINE_S=600)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    clock.now += 599
    assert manager.status(match_id)["status"] == "running"
    clock.now += 2
    manager.reap()  # the reaper applies the same transition without a poll
    out = manager.status(match_id)
    assert out["status"] == "deadline"
    assert len(fake_registry.stopped) == 2
    assert store.get(game_id) is None
    assert out["result"]["winner"] is None


def test_finished_result_dropped_after_ttl(fake_registry):
    from datetime import UTC, datetime, timedelta

    from magic_cards_model_api.sim.manager import SimRefused
    from magic_cards_model_api.store import store

    clock = Clock()
    manager = _manager(clock, MTG_SIM_RESULT_TTL_S=60)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    _end_game(_game_id(manager, match_id), "a")
    manager.reap()
    assert manager.status(match_id)["result"]["winner"] == "b"
    clock.now += 61
    manager.reap()
    manager.flush_persist()
    assert match_id not in manager.live_ids()  # the memory copy is gone; the row answers
    assert manager.status(match_id)["result"]["winner"] == "b"
    store.delete_sim_claude_matches_before(datetime.now(UTC) + timedelta(days=1))
    with pytest.raises(SimRefused):
        manager.status(match_id)


def test_sweep_orphans_deletes_marked_games(fake_registry):
    from magic_cards_model_api.decks_api import boot_decklist_game
    from magic_cards_model_api.sim.claude import SIM_OWNER
    from magic_cards_model_api.store import store

    orphan, orphan_uids, _ = boot_decklist_game(
        [("a", RED, True), ("b", GREEN, True)], seed=1, owner_user_id=SIM_OWNER
    )
    other, _, _ = boot_decklist_game(
        [("a", RED, True), ("b", GREEN, True)], seed=1, owner_user_id="someone"
    )
    try:
        swept = _manager().sweep_orphans()
        assert orphan in swept and other not in swept
        assert store.get(orphan) is None
        assert store.get(other) is not None
        assert set(orphan_uids.values()) <= set(fake_registry.stopped)
    finally:
        store.delete(other)


def test_claude_answers_have_no_seat_uids(fake_registry):
    from magic_cards_model_api.sim.config import SimLimits
    from magic_cards_model_api.sim.manager import SandboxManager
    from magic_cards_model_api.sim.server import build_sim_server
    from magic_cards_model_api.store import store

    limits = SimLimits.from_env({})
    manager = _manager()
    server = build_sim_server(SandboxManager(limits), limits, claude=manager)

    def call(name: str, **kw: Any) -> dict[str, Any]:
        result = asyncio.run(server.call_tool(name, kw))
        return json.loads(str(result.content[0].text))

    answers = [call("sim_match_claude_start", deck_a=RED, deck_b=GREEN, seed=1)]
    match_id = answers[0]["match_id"]
    answers.append(call("sim_match_claude_status", match_id=match_id))  # starting or running
    _wait(manager, match_id)
    game_id = _game_id(manager, match_id)
    secrets = set(store.seat_uids(game_id)) | {store.admin_key(game_id)}
    manager.reap()
    answers.append(call("sim_match_claude_status", match_id=match_id))  # running, from cache
    _end_game(game_id, "b")
    manager.reap()
    answers.append(call("sim_match_claude_status", match_id=match_id))
    assert answers[-1]["status"] == "done"
    match_ids = [match_id]

    # The failed answers: a seat fallback at start (its reason names the uid), then both
    # seats gone mid-game.
    fake_registry.fail_player = "b"
    failed_start = call("sim_match_claude_start", deck_a=RED, deck_b=GREEN, seed=2)
    match_ids.append(failed_start["match_id"])
    _wait(manager, failed_start["match_id"])
    secrets |= set(manager._matches[failed_start["match_id"]].uids.values())
    answers += [failed_start, call("sim_match_claude_status", match_id=failed_start["match_id"])]
    assert answers[-1]["status"] == "failed"
    fake_registry.fail_player = None
    dead = call("sim_match_claude_start", deck_a=RED, deck_b=GREEN, seed=3)
    match_ids.append(dead["match_id"])
    _wait(manager, dead["match_id"])
    secrets |= set(manager._matches[dead["match_id"]].uids.values())
    fake_registry.state = "gone"
    manager.reap()
    answers += [dead, call("sim_match_claude_status", match_id=dead["match_id"])]
    assert answers[-1]["status"] == "failed"

    text = json.dumps(answers)
    for key in ("admin_key", "seat_uids", "uid", "key", "advisor_uids", "job"):
        assert f'"{key}"' not in text
    for secret in secrets:
        assert secret not in text and secret[:8] not in text
    for mid in match_ids:
        text = text.replace(mid, "")
    assert not re.findall(r"\b[0-9a-f]{32}\b", text)


def test_status_unknown_match_id_refused(fake_registry):
    from magic_cards_model_api.sim.manager import SimRefused

    with pytest.raises(SimRefused, match="sim_match_claude_start"):
        _manager().status("claude-unknown")


def test_app_lifespan_sweeps_orphaned_claude_games(fake_registry):
    from fastapi.testclient import TestClient

    from magic_cards_model_api.decks_api import boot_decklist_game
    from magic_cards_model_api.main import app
    from magic_cards_model_api.sim.claude import SIM_OWNER
    from magic_cards_model_api.store import store

    orphan, _, _ = boot_decklist_game(
        [("a", RED, True), ("b", GREEN, True)], seed=1, owner_user_id=SIM_OWNER
    )
    with TestClient(app):
        assert store.get(orphan) is None


# -- Fix round 1: bounded start, pregame backstop, dead seats ----------------


def test_start_answers_starting_while_boot_is_slow(fake_registry, monkeypatch):
    import threading
    import time

    from magic_cards_model_api.sim.manager import SimRefused

    gate = threading.Event()
    _gated_boot(monkeypatch, gate)
    manager = _manager()
    began = time.monotonic()
    out = manager.start(RED, GREEN, seed=1)
    assert time.monotonic() - began < 5
    assert out["status"] == "starting"
    assert set(out) >= {"match_id", "expected_minutes", "poll_every_s"}
    polled = manager.status(out["match_id"])
    assert polled["status"] == "starting"
    assert polled["seats"] == {
        "a": {"state": "starting", **OPUS_LOW},
        "b": {"state": "starting", **OPUS_LOW},
    }
    assert fake_registry.started == []
    with pytest.raises(SimRefused, match="being started"):
        manager.start(RED, GREEN, seed=2)  # the slot is held while booting
    gate.set()
    _wait(manager, out["match_id"])
    manager.reap()
    running = manager.status(out["match_id"])
    assert running["status"] == "running"
    assert running["seats"] == {
        "a": {"state": "thinking", **OPUS_LOW},
        "b": {"state": "thinking", **OPUS_LOW},
    }
    assert len(fake_registry.started) == 2
    _finish(manager, out["match_id"])


def test_boot_failure_reports_failed_by_type_and_frees_the_slot(fake_registry, monkeypatch):
    import threading

    gate = threading.Event()
    gate.set()
    _gated_boot(monkeypatch, gate, fail=RuntimeError("cluster said: token-xyz"))
    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    out = manager.status(match_id)
    assert out["status"] == "failed"
    assert "RuntimeError" in out["error"] and "token-xyz" not in out["error"]
    assert fake_registry.started == [] and fake_registry.pregame == []
    monkeypatch.undo()  # real boot again, but keep the fake registry
    from magic_cards_model_api import ai_pregame, ai_seat

    monkeypatch.setattr(ai_seat, "registry", fake_registry)
    monkeypatch.setattr(ai_seat, "ai_seat_mode", lambda: "kubernetes")
    monkeypatch.setattr(ai_pregame, "start", lambda game_id, players: None)
    monkeypatch.setattr(ai_pregame, "stop", lambda game_id: None)
    again = _start(manager, RED, GREEN, seed=2)  # the slot is free
    _finish(manager, again["match_id"])


def test_seat_fallback_fails_the_match_and_cleans_up(fake_registry):
    from magic_cards_model_api.store import store

    fake_registry.fail_player = "b"
    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    uids = set(manager._matches[match_id].uids.values())
    out = manager.status(match_id)
    assert out["status"] == "failed"
    assert "Seat b" in out["error"] and "refused" not in out["error"]
    assert set(fake_registry.stopped) == uids
    assert store.get(game_id) is None
    assert ("stop", game_id) in fake_registry.pregame
    fake_registry.fail_player = None
    again = _start(manager, RED, GREEN, seed=2)  # the slot is free
    _finish(manager, again["match_id"])


def test_pregame_backstop_started_after_boot_and_stopped_on_cleanup(fake_registry):
    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    assert fake_registry.pregame == [("start", game_id, ("a", "b"))]
    _finish(manager, match_id)
    assert fake_registry.pregame[-1] == ("stop", game_id)


@pytest.mark.parametrize("dead", ["gone", "error"])
def test_both_seats_dead_fails_the_match_and_frees_the_slot(fake_registry, dead):
    from magic_cards_model_api.store import store

    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    fake_registry.states = {"a": dead}
    manager.reap()
    assert manager.status(match_id)["status"] == "running"  # one seat is still playing
    fake_registry.states = {"a": dead, "b": "gone"}
    manager.reap()
    out = manager.status(match_id)
    assert out["status"] == "failed"
    assert "seats" in out["error"]
    assert out["result"]["winner"] is None
    assert len(fake_registry.stopped) == 2
    assert store.get(game_id) is None
    assert ("stop", game_id) in fake_registry.pregame
    fake_registry.states = {}
    again = _start(manager, RED, GREEN, seed=2)  # the slot is free
    _finish(manager, again["match_id"])


def test_reaper_fails_a_match_whose_seats_are_dead(fake_registry):
    from magic_cards_model_api.store import store

    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    fake_registry.state = "gone"
    manager.reap()
    assert store.get(game_id) is None
    assert len(fake_registry.stopped) == 2
    fake_registry.state = "thinking"
    assert manager.status(match_id)["status"] == "failed"


# -- Final review fix wave: status from cache, start deadline ----------------


def test_status_answers_from_cache_while_a_seat_read_blocks(fake_registry):
    import threading
    import time

    clock = Clock()
    manager = _manager(clock)
    server = _tool_server(manager)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.reap()  # one reaper pass fills the cache
    clock.now += 7
    gate = threading.Event()
    reads: list[str] = []

    def slow_status(uid: str) -> dict[str, Any]:
        reads.append(uid)
        assert gate.wait(30)
        return {"state": "thinking"}

    fake_registry.status = slow_status
    reaper = threading.Thread(target=manager.reap, daemon=True)
    reaper.start()
    began = time.monotonic()
    out = _call_tool(server, "sim_match_claude_status", match_id=match_id)
    took = time.monotonic() - began
    gate.set()
    reaper.join(timeout=30)
    assert took < 2, took
    assert out["status"] == "running"
    assert out["seats"] == {
        "a": {"state": "thinking", **OPUS_LOW},
        "b": {"state": "thinking", **OPUS_LOW},
    }
    assert out["checked_s_ago"] == 7
    assert out["turn"] is not None
    reads.clear()
    manager.status(match_id)
    assert reads == []  # a poll never reads a seat
    _finish(manager, match_id)


def test_status_does_not_wait_for_cleanup(fake_registry):
    import threading
    import time

    from magic_cards_model_api.store import store

    manager = _manager()
    server = _tool_server(manager)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    gate = threading.Event()
    real_stop = fake_registry.stop

    def slow_stop(uid: str) -> bool:
        assert gate.wait(30)
        return real_stop(uid)

    fake_registry.stop = slow_stop
    _end_game(game_id, "b")
    assert manager.status(match_id)["status"] == "running"  # nothing advanced without the reaper
    reaper = threading.Thread(target=manager.reap, daemon=True)
    reaper.start()
    deadline = time.monotonic() + 10
    while manager._matches[match_id].status == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    began = time.monotonic()
    out = _call_tool(server, "sim_match_claude_status", match_id=match_id)
    took = time.monotonic() - began
    assert reaper.is_alive()  # cleanup is still stuck in the seat stop
    gate.set()
    reaper.join(timeout=30)
    assert took < 2, took
    assert out["status"] == "done"
    assert out["result"]["winner"] == "a"
    assert len(fake_registry.stopped) == 2
    assert store.get(game_id) is None


def test_start_stuck_past_the_start_deadline_fails_and_late_thread_cleans_up(
    fake_registry, monkeypatch
):
    import threading

    from magic_cards_model_api import decks_api
    from magic_cards_model_api.sim.claude import START_DEADLINE_S
    from magic_cards_model_api.store import store

    gate = threading.Event()
    real = decks_api.boot_decklist_game
    booted: dict[int, tuple[str, dict[str, str]]] = {}

    def boot(seats: Any, seed: int, owner: Any, agents: Any = None):
        if seed == 1:  # only the first match hangs (cold card resolution)
            assert gate.wait(30)
        game_id, uids, match = real(seats, seed, owner, agents)
        booted[seed] = (game_id, uids)
        return game_id, uids, match

    monkeypatch.setattr(decks_api, "boot_decklist_game", boot)
    clock = Clock()
    manager = _manager(clock)
    stuck = manager.start(RED, GREEN, seed=1)["match_id"]
    clock.now += START_DEADLINE_S - 1
    manager.reap()
    assert manager.status(stuck)["status"] == "starting"
    clock.now += 2
    manager.reap()
    out = manager.status(stuck)
    assert out["status"] == "failed"
    assert "start" in out["error"]
    assert out["seats"] == {
        "a": {"state": "stopped", **OPUS_LOW},
        "b": {"state": "stopped", **OPUS_LOW},
    }

    again = _start(manager, RED, GREEN, seed=2)  # the slot is free
    assert manager.status(again["match_id"])["status"] == "running"

    gate.set()  # the stuck start finishes late: it must clean up, not resurrect the match
    _wait(manager, stuck)
    late_game, late_uids = booted[1]
    late = manager.status(stuck)
    assert late["status"] == "failed"
    _assert_no_uids(late, late_uids.values())
    assert store.get(late_game) is None
    assert set(late_uids.values()) <= set(fake_registry.stopped)
    assert ("stop", late_game) in fake_registry.pregame
    _finish(manager, again["match_id"])


def test_seat_start_stuck_past_the_start_deadline_cleans_up_the_game(fake_registry):
    import threading

    from magic_cards_model_api.sim.claude import START_DEADLINE_S
    from magic_cards_model_api.store import store

    gate = threading.Event()
    real_start = fake_registry.start

    def slow_start(**kw: Any) -> dict[str, Any]:
        assert gate.wait(30)  # a Job create with no request timeout
        return real_start(**kw)

    fake_registry.start = slow_start
    clock = Clock()
    manager = _manager(clock)
    match_id = manager.start(RED, GREEN, seed=1)["match_id"]
    match = manager._matches[match_id]
    for _ in range(3000):
        if match.game_id is not None:
            break
        threading.Event().wait(0.01)
    game_id = match.game_id
    assert game_id is not None
    clock.now += START_DEADLINE_S + 1
    manager.reap()
    assert manager.status(match_id)["status"] == "failed"
    assert store.get(game_id) is None
    gate.set()
    _wait(manager, match_id)
    late = manager.status(match_id)
    assert late["status"] == "failed"
    _assert_no_uids(late, match.uids.values())
    assert set(match.uids.values()) <= set(fake_registry.stopped)
    # Seat a's Job create returned after the deadline cleanup: the thread stopped it again and
    # never started seat b.
    assert [s["player"] for s in fake_registry.started] == ["a"]
    assert fake_registry.stopped.count(match.uids["a"]) == 2


# -- per-call model and effort of the Claude seats --------------------------------


def _seat_agents(fake_registry) -> dict[str, Any]:
    return {s["player"]: s["agent"] for s in fake_registry.started}


def test_default_seat_settings_come_from_the_sim_env(fake_registry, monkeypatch):
    monkeypatch.setenv("AGENT_DEFAULT_MAX_THINKING_TOKENS", "1234")
    manager = _manager()
    out = _start(manager, RED, GREEN, seed=1)
    agents = _seat_agents(fake_registry)
    for seat in ("a", "b"):
        assert (agents[seat].model, agents[seat].effort) == ("claude-opus-5-5", "low")
        assert agents[seat].max_thinking_tokens == 1234  # other fields stay from the default
        assert out["seats"][seat] == {"model": "claude-opus-5-5", "effort": "low"}
    assert manager.status(out["match_id"])["seats"]["a"]["model"] == "claude-opus-5-5"
    _finish(manager, out["match_id"])


def test_env_defaults_and_call_overrides_and_per_seat_overrides(fake_registry):
    from magic_cards_model_api.store import store

    manager = _manager(MTG_SIM_CLAUDE_MODEL="claude-sonnet-5", MTG_SIM_CLAUDE_EFFORT="medium")
    out = _start(
        manager,
        RED,
        GREEN,
        seed=1,
        model="claude-opus-5",
        effort="high",
        seat_b={"model": "claude-haiku-4-5"},
    )
    agents = _seat_agents(fake_registry)
    assert (agents["a"].model, agents["a"].effort) == ("claude-opus-5", "high")
    assert (agents["b"].model, agents["b"].effort) == ("claude-haiku-4-5", "high")
    assert out["seats"] == {
        "a": {"model": "claude-opus-5", "effort": "high"},
        "b": {"model": "claude-haiku-4-5", "effort": "high"},
    }
    status = manager.status(out["match_id"])
    assert status["seats"]["b"] == {
        "state": status["seats"]["b"]["state"],
        "model": "claude-haiku-4-5",
        "effort": "high",
    }
    game_id = _game_id(manager, out["match_id"])
    recorded = store.agent_settings(game_id)
    assert recorded["b"].model == "claude-haiku-4-5" and recorded["a"].model == "claude-opus-5"
    _finish(manager, out["match_id"])


def test_env_defaults_apply_without_call_arguments(fake_registry):
    manager = _manager(MTG_SIM_CLAUDE_MODEL="claude-sonnet-5", MTG_SIM_CLAUDE_EFFORT="medium")
    out = _start(manager, RED, GREEN, seed=1, seat_a={"effort": "max"})
    agents = _seat_agents(fake_registry)
    assert (agents["a"].model, agents["a"].effort) == ("claude-sonnet-5", "max")
    assert (agents["b"].model, agents["b"].effort) == ("claude-sonnet-5", "medium")
    _finish(manager, out["match_id"])


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"model": "gpt-9"}, "claude-opus-5-5"),
        ({"effort": "extreme"}, "xhigh"),
        ({"seat_a": {"model": "gpt-9"}}, "seat_a"),
        ({"seat_b": {"effort": "extreme"}}, "seat_b"),
        ({"seat_a": {"temperature": 1}}, "seat_a"),
    ],
)
def test_bad_model_or_effort_is_refused_listing_the_allowed_values(fake_registry, kw, needle):
    from magic_cards_model_api.sim.manager import SimRefused

    manager = _manager()
    with pytest.raises(SimRefused, match=needle):
        manager.start(RED, GREEN, 1, **kw)
    assert fake_registry.started == []
    assert manager._matches == {}  # a refused call holds no slot


def test_model_allow_list_is_configurable(fake_registry):
    from magic_cards_model_api.sim.manager import SimRefused

    manager = _manager(MTG_SIM_CLAUDE_MODELS="claude-sonnet-5")
    with pytest.raises(SimRefused, match="claude-sonnet-5"):
        manager.start(RED, GREEN, 1, model="claude-opus-5")


def test_start_tool_takes_model_effort_and_seat_overrides(fake_registry):
    from magic_cards_model_api.sim.config import SimLimits
    from magic_cards_model_api.sim.manager import SandboxManager
    from magic_cards_model_api.sim.server import build_sim_server

    limits = SimLimits.from_env({})
    manager = _manager()
    server = build_sim_server(SandboxManager(limits), limits, claude=manager)

    def call(**kw: Any) -> dict[str, Any]:
        result = asyncio.run(server.call_tool("sim_match_claude_start", kw))
        return json.loads(str(result.content[0].text))

    refused = call(deck_a=RED, deck_b=GREEN, model="gpt-9")
    assert "claude-haiku-4-5" in refused["error"]
    out = call(deck_a=RED, deck_b=GREEN, seed=1, effort="high", seat_a={"model": "claude-opus-5"})
    assert out["seats"]["a"] == {"model": "claude-opus-5", "effort": "high"}
    assert out["seats"]["b"] == {"model": "claude-opus-5-5", "effort": "high"}
    _wait(manager, out["match_id"])
    _finish(manager, out["match_id"])


# -- S-69: shared finish path and cancel --


def _matches_count(status: str) -> float:
    from magic_cards_model_api.match_metrics import REGISTRY

    value = REGISTRY.get_sample_value("mtg_sim_claude_matches_total", {"status": status})
    return value or 0.0


def test_cancel_running_match_stops_seats_and_keeps_the_log(fake_registry):
    from magic_cards_model_api import audit
    from magic_cards_model_api.store import store

    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    uids = set(store.seat_uids(game_id))
    audit.record(game_id, "pass_priority", player="a", reasoning="hold")
    out = manager.cancel(match_id, reason="hung on Emrakul")
    assert out["status"] == "cancelled"
    assert out["error"] == "cancelled by caller: hung on Emrakul"
    assert "a pass" in out["log_tail"]
    assert sorted(fake_registry.stopped) == sorted(uids)
    assert store.get(game_id) is None
    assert any(r["payload"]["action"] == "pass_priority" for r in store.records(game_id))
    again = _start(manager, RED, GREEN, seed=2)  # the slot is free
    _finish(manager, again["match_id"])


def test_cancel_reason_is_ascii_collapsed_and_trimmed(fake_registry):
    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    out = manager.cancel(match_id, reason="  caf" + chr(0xE9) + " \n\t stuck " + "x" * 300)
    assert out["error"].startswith("cancelled by caller: cafe stuck x")
    assert len(out["error"]) == len("cancelled by caller: ") + 200
    other = _start(manager, RED, GREEN, seed=2)["match_id"]
    assert manager.cancel(other)["error"] == "cancelled by caller"


def test_cancel_while_starting_is_honoured_by_the_start_thread(fake_registry, monkeypatch):
    import threading

    from magic_cards_model_api.store import store

    gate = threading.Event()
    _gated_boot(monkeypatch, gate)
    manager = _manager()
    match_id = manager.start(RED, GREEN, seed=1)["match_id"]
    out = manager.cancel(match_id)
    assert out["status"] == "cancelled"
    assert out["log_tail"] == ""
    gate.set()
    _wait(manager, match_id)
    assert manager.status(match_id)["status"] == "cancelled"
    game_id = _game_id(manager, match_id)
    assert game_id is not None and store.get(game_id) is None
    assert fake_registry.started == []


def test_cancel_terminal_match_is_a_noop(fake_registry):
    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    _finish(manager, match_id)
    stopped = len(fake_registry.stopped)
    before = manager.status(match_id)
    out = manager.cancel(match_id, reason="too late")
    assert out["status"] == "done"
    assert out.get("error") == before.get("error")
    assert len(fake_registry.stopped) == stopped


def test_cancel_unknown_match_refused(fake_registry):
    from magic_cards_model_api.sim.manager import SimRefused

    with pytest.raises(SimRefused, match="No Claude match"):
        _manager().cancel("claude-nope")


@pytest.mark.parametrize(
    ("path", "status"),
    [("done", "done"), ("deadline", "deadline"), ("dead", "failed"), ("cancel", "cancelled")],
)
def test_every_terminal_path_sets_ended_and_counts_once(fake_registry, path, status):
    clock = Clock()
    manager = _manager(clock, MTG_SIM_CLAUDE_DEADLINE_S=600)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    match = manager._matches[match_id]
    assert not match.ended.is_set()
    before = _matches_count(status)
    if path == "done":
        _end_game(_game_id(manager, match_id), "b")
        manager.reap()
    elif path == "deadline":
        clock.now += 601
        manager.reap()
    elif path == "dead":
        fake_registry.state = "gone"
        manager.reap()
    else:
        manager.cancel(match_id)
    assert manager.status(match_id)["status"] == status
    assert match.ended.is_set()
    assert _matches_count(status) == before + 1
    manager.reap()
    manager.cancel(match_id)
    assert _matches_count(status) == before + 1


def test_cancel_racing_the_reaper_claims_once(fake_registry):
    import threading

    from magic_cards_model_api.store import store

    manager = _manager()
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    game_id = _game_id(manager, match_id)
    uids = list(store.seat_uids(game_id))
    before = _matches_count("done") + _matches_count("cancelled")
    _end_game(game_id, "b")
    barrier = threading.Barrier(2)

    def run(call):
        barrier.wait(10)
        call()

    threads = [
        threading.Thread(target=run, args=(manager.reap,)),
        threading.Thread(target=run, args=(lambda: manager.cancel(match_id),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert manager.status(match_id)["status"] in {"done", "cancelled"}
    assert _matches_count("done") + _matches_count("cancelled") == before + 1
    assert sorted(fake_registry.stopped) == sorted(uids)


def test_cancel_tool_is_listed_and_answers_text(fake_registry):
    from magic_cards_model_api.store import store

    manager = _manager()
    server = _tool_server(manager)
    assert "error" in _call_tool(server, "sim_match_claude_cancel", match_id="claude-nope")
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    uids = list(store.seat_uids(_game_id(manager, match_id)))
    out = _call_tool(server, "sim_match_claude_cancel", match_id=match_id, reason="stop")
    assert out["status"] == "cancelled"
    assert isinstance(out["log_tail"], str)
    _assert_no_uids(out, uids)


# -- S-69: persistence --


def _put_row(match_id: str, status: str, started, finished=None, **kw):
    from magic_cards_model_api.store import store

    row = {
        "match_id": match_id,
        "game_id": None,
        "decks": {"a": "Red", "b": "Green"},
        "seed": 7,
        "seats": {"a": dict(OPUS_LOW), "b": dict(OPUS_LOW)},
        "status": status,
        "error": None,
        "result": None,
        "started_at": started,
        "finished_at": finished,
        "last_seq": None,
        "last_progress_at": None,
        "stalled_reason": None,
    }
    row.update(kw)
    store.put_sim_claude_match(row)


def test_start_and_finish_write_the_row(fake_registry):
    from magic_cards_model_api.store import store

    manager = _manager()
    out = _start(manager, RED, GREEN, seed=1)
    mid = out["match_id"]
    game_id = _game_id(manager, mid)
    uids = list(store.seat_uids(game_id))
    manager.flush_persist()
    row = store.get_sim_claude_match(mid)
    assert row["status"] == "running"
    assert row["decks"] == out["decks"] and row["seed"] == 1
    assert row["seats"] == out["seats"] and row["game_id"] == game_id
    assert row["finished_at"] is None
    _finish(manager, mid)
    manager.flush_persist()
    row = store.get_sim_claude_match(mid)
    assert row["status"] == "done" and row["result"]["winner"] == "a"
    assert row["finished_at"] is not None
    text = json.dumps(row, default=str)
    for uid in uids:
        assert uid not in text and uid[:8] not in text


def test_status_falls_back_to_the_row_after_the_result_ttl(fake_registry):
    clock = Clock()
    manager = _manager(clock, MTG_SIM_RESULT_TTL_S=100)
    mid = _start(manager, RED, GREEN, seed=1)["match_id"]
    _finish(manager, mid)
    live = manager.status(mid)
    clock.now += 101
    manager.reap()
    manager.flush_persist()
    assert mid not in manager.live_ids()
    out = manager.status(mid)
    assert out["status"] == "done"
    assert out["result"] == live["result"] and out["decks"] == live["decks"]
    assert out["seats"] == live["seats"] and out["seed"] == live["seed"]


def test_restart_marks_live_rows_failed_with_the_restart_message(fake_registry):
    from datetime import UTC, datetime

    from magic_cards_model_api.sim.claude import RESTART_ERROR

    _put_row("claude-orphan-1", "running", datetime.now(UTC))
    manager = _manager()
    kept = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.sweep_orphans()
    out = manager.status("claude-orphan-1")
    assert out["status"] == "failed" and out["error"] == RESTART_ERROR
    assert manager.status(kept)["status"] == "running"
    _finish(manager, kept)


def test_app_lifespan_marks_orphaned_rows_failed(fake_registry):
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    from magic_cards_model_api.main import app
    from magic_cards_model_api.sim.claude import RESTART_ERROR
    from magic_cards_model_api.store import store

    _put_row("claude-orphan-2", "running", datetime.now(UTC))
    with TestClient(app):
        row = store.get_sim_claude_match("claude-orphan-2")
    assert row["status"] == "failed" and row["error"] == RESTART_ERROR


def test_reap_drops_rows_past_retention(fake_registry, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from magic_cards_model_api.store import store

    now = [datetime(2026, 11, 16, 12, 0, tzinfo=UTC)]
    clock = Clock()
    manager = _manager(clock, wall=lambda: now[0])
    _put_row("claude-old", "done", now[0] - timedelta(days=32), now[0] - timedelta(days=31))
    _put_row("claude-new", "done", now[0] - timedelta(days=2), now[0] - timedelta(days=1))
    calls = []
    real = store.delete_sim_claude_matches_before
    monkeypatch.setattr(
        store, "delete_sim_claude_matches_before", lambda c: calls.append(c) or real(c)
    )
    manager.reap()
    assert store.get_sim_claude_match("claude-old") is None
    assert store.get_sim_claude_match("claude-new") is not None
    manager.reap()
    assert len(calls) == 1
    clock.now += 3601
    manager.reap()
    assert len(calls) == 2


def test_persist_failure_does_not_stop_the_match(fake_registry, monkeypatch, caplog):
    from magic_cards_model_api.store import store

    def down(*a, **kw):
        raise RuntimeError("db down")

    for name in (
        "put_sim_claude_match",
        "get_sim_claude_match",
        "list_sim_claude_matches",
        "fail_live_sim_claude_matches",
        "delete_sim_claude_matches_before",
    ):
        monkeypatch.setattr(store, name, down)
    manager = _manager()
    manager._persist_retry_s = 0.01
    server = _tool_server(manager)
    with caplog.at_level("WARNING"):
        mid = _start(manager, RED, GREEN, seed=1)["match_id"]
        assert manager.status(mid)["status"] == "running"
        _finish(manager, mid)
        assert not manager.flush_persist(0.3)  # the store is down: the snapshot is kept
        assert manager.status(mid)["status"] == "done"
        manager.sweep_orphans()
        manager.reap()
        listed = _call_tool(server, "sim_match_claude_list")
        assert [m["match_id"] for m in listed["matches"]] == [mid]
        assert "error" in _call_tool(server, "sim_match_claude_status", match_id="claude-none")
        assert "error" in _call_tool(server, "sim_match_claude_cancel", match_id="claude-none")
    manager.shutdown(0.3)  # drop the undeliverable snapshot before the store comes back
    text = caplog.text
    assert "persist failed (RuntimeError)" in text and "db down" not in text


def test_cancel_of_a_persisted_only_match_answers_its_final_status(fake_registry):
    from datetime import UTC, datetime

    _put_row("claude-rowonly", "done", datetime.now(UTC), datetime.now(UTC), game_id=None)
    manager = _manager()
    out = manager.cancel("claude-rowonly", reason="late")
    assert out["status"] == "done" and out["log_tail"] == ""
    assert fake_registry.stopped == []


def test_list_tool_newest_first(fake_registry):
    from magic_cards_model_api.store import store

    manager = _manager()
    server = _tool_server(manager)
    first = _start(manager, RED, GREEN, seed=1)["match_id"]
    uids = list(store.seat_uids(_game_id(manager, first)))
    _finish(manager, first)
    second = _start(manager, RED, GREEN, seed=2)["match_id"]
    _finish(manager, second)
    out = _call_tool(server, "sim_match_claude_list", limit=2)
    ids = [m["match_id"] for m in out["matches"]]
    assert ids[:2] == [second, first]
    entry = out["matches"][1]
    assert entry["status"] == "done" and entry["winner"] == "a" and entry["turns"] is not None
    assert set(entry) == {
        "match_id", "status", "decks", "seats", "started_at", "finished_at",
        "turns", "winner", "error",
    }  # fmt: skip
    _assert_no_uids(out, uids)
    assert len(_call_tool(server, "sim_match_claude_list", limit=0)["matches"]) == 1


# -- S-69: watchdog --


def _stalled_manager(**env):
    clock = Clock()
    manager = _manager(clock, **env)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.reap()
    return clock, manager, match_id


def _stall_gauge() -> float:
    from magic_cards_model_api.match_metrics import REGISTRY

    return REGISTRY.get_sample_value("mtg_sim_claude_stall_seconds") or 0.0


def test_no_new_record_for_the_stall_window_marks_stalled_with_a_reason(fake_registry):
    from magic_cards_model_api.store import store

    clock, manager, match_id = _stalled_manager()
    assert manager.status(match_id)["status"] == "running"
    clock.now += 901
    manager.reap()
    answer = manager.status(match_id)
    assert answer["status"] == "stalled"
    assert answer["stalled_reason"].startswith("no action for 901 s at T")
    assert "seats a=thinking b=thinking" in answer["stalled_reason"]
    assert answer["seats"]["a"]["state"] == "thinking"
    assert answer["checked_s_ago"] == 0.0
    assert answer["poll_every_s"]
    assert answer["last_progress_s_ago"] == 901.0
    with pytest.raises(Exception, match="(?i)claude match"):
        manager.start(RED, GREEN, seed=2)
    manager.flush_persist()
    assert store.get_sim_claude_match(match_id)["status"] == "stalled"
    assert store.get_sim_claude_match(match_id)["stalled_reason"] == answer["stalled_reason"]
    _finish(manager, match_id)


def test_reason_names_the_pending_decision(fake_registry, monkeypatch):
    from types import SimpleNamespace

    from magic_cards_model_api.store import store

    decision = SimpleNamespace(
        player="a", kind="choose_targets", prompt="Choose targets for Emrakul cast trigger"
    )
    monkeypatch.setattr(
        store, "paused_command", lambda game_id: (None, None, SimpleNamespace(decision=decision))
    )
    clock, manager, match_id = _stalled_manager()
    clock.now += 901
    manager.reap()
    reason = manager.status(match_id)["stalled_reason"]
    assert "pending decision: choose_targets (a): Choose targets for Emrakul cast trigger" in reason
    _finish(manager, match_id)


def test_new_record_returns_a_stalled_match_to_running(fake_registry):
    from magic_cards_model_api import audit
    from magic_cards_model_api.store import store

    clock, manager, match_id = _stalled_manager()
    clock.now += 901
    manager.reap()
    assert manager.status(match_id)["status"] == "stalled"
    seq = audit.record(_game_id(manager, match_id), "pass_priority", player="a", reasoning="ok")
    manager.reap()
    answer = manager.status(match_id)
    assert answer["status"] == "running"
    assert "stalled_reason" not in answer
    assert answer["last_seq"] == seq
    assert answer["last_progress_s_ago"] == 0.0
    manager.flush_persist()
    assert store.get_sim_claude_match(match_id)["status"] == "running"
    assert store.get_sim_claude_match(match_id)["last_seq"] == seq
    _finish(manager, match_id)


def test_stall_abort_fails_the_match_and_frees_the_slot(fake_registry):
    from magic_cards_model_api.store import store

    clock, manager, match_id = _stalled_manager()
    game_id = _game_id(manager, match_id)
    uids = list(store.seat_uids(game_id))
    clock.now += 1801
    manager.reap()
    answer = manager.status(match_id)
    assert answer["status"] == "failed"
    assert answer["error"].startswith("aborted after 1800 s without progress: no action for")
    assert sorted(fake_registry.stopped) == sorted(uids)
    assert store.get(game_id) is None
    assert manager._matches[match_id].ended.is_set()
    _finish(manager, _start(manager, RED, GREEN, seed=2)["match_id"])


def test_abort_racing_a_cancel_claims_once(fake_registry):
    import threading

    from magic_cards_model_api.store import store

    clock, manager, match_id = _stalled_manager()
    uids = list(store.seat_uids(_game_id(manager, match_id)))
    clock.now += 1801
    terminal = ("failed", "cancelled")
    before = sum(_matches_count(s) for s in terminal)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(call):
        try:
            barrier.wait(10)
            call()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(manager.reap,)),
        threading.Thread(target=run, args=(lambda: manager.cancel(match_id),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert manager.status(match_id)["status"] in terminal
    assert sum(_matches_count(s) for s in terminal) == before + 1
    assert manager._matches[match_id].ended.is_set()
    assert sorted(fake_registry.stopped) == sorted(uids)


def test_a_failed_progress_read_neither_stalls_nor_aborts(fake_registry, monkeypatch):
    from magic_cards_model_api.store import store

    clock, manager, match_id = _stalled_manager()

    def boom(game_id):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "max_record_seq", boom)
    clock.now += 1801
    manager.reap()
    assert manager.status(match_id)["status"] == "running"
    monkeypatch.undo()
    _finish(manager, match_id)


def test_abort_from_an_already_stalled_match(fake_registry):
    from magic_cards_model_api.store import store

    clock, manager, match_id = _stalled_manager()
    uids = list(store.seat_uids(_game_id(manager, match_id)))
    clock.now += 901
    manager.reap()
    assert manager.status(match_id)["status"] == "stalled"
    stopped_before = len(fake_registry.stopped)
    clock.now += 900
    manager.reap()
    answer = manager.status(match_id)
    assert answer["status"] == "failed"
    assert answer["error"].startswith("aborted after 1800 s without progress: no action for")
    assert len(fake_registry.stopped) == stopped_before + len(uids)
    assert sorted(fake_registry.stopped) == sorted(uids)
    manager.reap()
    assert len(fake_registry.stopped) == stopped_before + len(uids)
    _finish(manager, _start(manager, RED, GREEN, seed=2)["match_id"])


def test_stall_gauge_tracks_the_stalled_match(fake_registry):
    from magic_cards_model_api import audit

    clock, manager, match_id = _stalled_manager()
    assert _stall_gauge() == 0
    clock.now += 901
    manager.reap()
    assert _stall_gauge() == 901
    audit.record(_game_id(manager, match_id), "pass_priority", player="a", reasoning="ok")
    manager.reap()
    assert _stall_gauge() == 0
    _finish(manager, match_id)


def test_game_over_wins_over_stall(fake_registry):
    clock, manager, match_id = _stalled_manager()
    clock.now += 901
    manager.reap()
    assert manager.status(match_id)["status"] == "stalled"
    _end_game(_game_id(manager, match_id), "b")
    manager.reap()
    assert manager.status(match_id)["status"] == "done"
    assert _stall_gauge() == 0


def test_a_hung_store_write_delays_neither_cleanup_nor_the_answers(fake_registry, monkeypatch):
    import threading

    from magic_cards_model_api.store import store

    release = threading.Event()
    entered = threading.Event()
    real = store.put_sim_claude_match

    def hang(row):
        entered.set()
        release.wait(10)
        return real(row)

    manager = _manager()
    mid = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.flush_persist()
    game_id = _game_id(manager, mid)
    monkeypatch.setattr(store, "put_sim_claude_match", hang)
    try:
        out = manager.cancel(mid, "enough")  # persists the terminal row, which now hangs
        assert entered.wait(2)
        assert out["status"] == "cancelled"
        assert len(fake_registry.stopped) == 2
        assert store.get(game_id) is None
        assert manager._find(mid).ended.is_set()
        assert manager.status(mid)["status"] == "cancelled"
        assert not manager.flush_persist(0.1)  # still stuck in the write
        second = _start(manager, GREEN, RED, seed=2)  # the slot is free; start answers at once
        assert second["status"] == "starting"
        manager.cancel(second["match_id"])
    finally:
        release.set()
    assert manager.flush_persist(5)
    assert store.get_sim_claude_match(mid)["status"] == "cancelled"
    manager.shutdown()


def test_a_failed_terminal_write_is_retried_to_the_terminal_status(fake_registry, monkeypatch):
    from magic_cards_model_api.store import store

    real = store.put_sim_claude_match
    failures = []

    def flaky(row):
        if row["status"] == "done" and len(failures) < 2:
            failures.append(1)
            raise RuntimeError("db down")
        return real(row)

    manager = _manager()
    manager._persist_retry_s = 0.01
    mid = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.flush_persist()
    monkeypatch.setattr(store, "put_sim_claude_match", flaky)
    _finish(manager, mid)
    assert manager.flush_persist(5)
    assert len(failures) == 2
    row = store.get_sim_claude_match(mid)
    assert row["status"] == "done" and row["result"]["winner"] == "a"
    manager.shutdown()


# -- fix round 2: the persist queue never regresses --


class _GatedLock:
    """Delegates to the real lock; the first entry by the gated thread parks until released."""

    def __init__(self, real, gated_thread, arrived, release):
        self._real, self._thread = real, gated_thread
        self._arrived, self._release = arrived, release
        self._used = False

    def __enter__(self):
        import threading

        if threading.current_thread() is self._thread and not self._used:
            self._used = True
            self._arrived.set()
            assert self._release.wait(5)
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


def test_an_older_snapshot_never_overwrites_a_newer_terminal_one(fake_registry):
    import threading

    from magic_cards_model_api.store import store

    manager = _manager()
    mid = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.flush_persist()
    match = manager._matches[mid]
    arrived, release = threading.Event(), threading.Event()
    older = threading.Thread(target=manager._persist, args=(match,))
    manager._wlock = _GatedLock(manager._wlock, older, arrived, release)
    older.start()
    assert arrived.wait(5)  # the older persist is parked at its enqueue
    with match.lock:
        match.status = "done"
    manager._persist(match)  # the newer, terminal persist goes through first
    release.set()
    older.join(5)
    assert manager.flush_persist()
    assert store.get_sim_claude_match(mid)["status"] == "done"
    with match.lock:
        match.status = "running"  # undo the forced status so the match can end for real
    _finish(manager, mid)


def test_a_terminal_snapshot_is_never_replaced_by_a_live_one(fake_registry):
    manager = _manager()
    mid = _start(manager, RED, GREEN, seed=1)["match_id"]
    manager.flush_persist()
    with manager._wlock:
        manager._pending[mid] = {"match_id": mid, "status": "failed"}
        manager._enqueue({"match_id": mid, "status": "running"})
        assert manager._pending[mid]["status"] == "failed"
    manager.shutdown()
    _finish(manager, mid)
