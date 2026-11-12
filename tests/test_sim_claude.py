"""Claude-seat sim matches: the pieces they share with the lobby."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
RED = (FIXTURES / "red_burn.txt").read_text()
GREEN = (FIXTURES / "green_stompy.txt").read_text()
OPUS_LOW = {"model": "claude-opus-5-5", "effort": "low"}


def test_boot_decklist_game_registers_no_ui_game():
    from magic_cards_model_api.decks_api import boot_decklist_game
    from magic_cards_model_api.store import store

    game_id, seat_uids, match = boot_decklist_game(
        [("a", RED, True), ("b", GREEN, True)], seed=1, owner_user_id=None
    )
    assert game_id not in store.ui_games()
    assert store.ai_players(game_id) == {"a", "b"}
    assert set(seat_uids) == {"a", "b"}
    assert match.games[-1] is not None


# -- ClaudeMatchManager and the sim_match_claude_* tools (Task 14) ----------


import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402


class FakeRegistry:
    """Stands in for `ai_seat.registry`: records start/stop, answers a seat state."""

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.state = "thinking"
        self.states: dict[str, str] = {}  # per-seat override, by player name
        self.players: dict[str, str] = {}  # uid -> player
        self.pregame: list[tuple[Any, ...]] = []
        self.fail_player: str | None = None

    def start(self, **kw: Any) -> dict[str, Any]:
        self.started.append(kw)
        self.players[kw["uid"]] = kw["player"]
        if kw["player"] == self.fail_player:
            return {"uid": kw["uid"], "mechanism": "fallback", "reason": f"job {kw['uid']} refused"}
        return {
            "uid": kw["uid"],
            "mechanism": "kubernetes",
            "state": "pending",
            "job": f"mtg-seat-{kw['game_id'][:8]}-{kw['uid'][:8]}",
        }

    def status(self, uid: str) -> dict[str, Any]:
        state = self.states.get(self.players.get(uid, ""), self.state)
        return {"uid": uid, "mechanism": "kubernetes", "state": state, "job": uid}

    def stop(self, uid: str) -> bool:
        self.stopped.append(uid)
        return True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake_registry(monkeypatch):
    from magic_cards_model_api import ai_pregame, ai_seat

    fake = FakeRegistry()
    monkeypatch.setattr(ai_seat, "registry", fake)
    monkeypatch.setattr(ai_seat, "ai_seat_mode", lambda: "kubernetes")
    monkeypatch.setattr(
        ai_pregame,
        "start",
        lambda game_id, players: fake.pregame.append(("start", game_id, tuple(players))),
    )
    monkeypatch.setattr(ai_pregame, "stop", lambda game_id: fake.pregame.append(("stop", game_id)))
    return fake


def _manager(clock=None, **env):
    from magic_cards_model_api.sim.claude import ClaudeMatchManager
    from magic_cards_model_api.sim.config import SimLimits

    limits = SimLimits.from_env({k: str(v) for k, v in env.items()})
    return ClaudeMatchManager(limits, clock=clock or Clock())


def _wait(manager, match_id: str) -> None:
    """Join the background boot of a started match."""
    manager._matches[match_id].booting.join(timeout=30)


def _start(manager, *args: Any, **kw: Any) -> dict[str, Any]:
    out = manager.start(*args, **kw)
    _wait(manager, out["match_id"])
    return out


def _game_id(manager, match_id: str) -> str:
    return manager._matches[match_id].game_id


def _end_game(game_id: str, loser: str) -> None:
    from magic_cards_model_api.store import store

    game = store.get(game_id)
    game.concede(loser, by=loser)
    store.replace(game_id, game)


def _finish(manager, match_id: str) -> None:
    """End a test match through the engine and let the reaper clean it up."""
    _end_game(_game_id(manager, match_id), "b")
    manager.reap()
    assert manager.status(match_id)["status"] == "done"


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
    from magic_cards_model_api.sim.manager import SimRefused

    clock = Clock()
    manager = _manager(clock, MTG_SIM_RESULT_TTL_S=60)
    match_id = _start(manager, RED, GREEN, seed=1)["match_id"]
    _end_game(_game_id(manager, match_id), "a")
    manager.reap()
    assert manager.status(match_id)["result"]["winner"] == "b"
    clock.now += 61
    manager.reap()
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
    swept = _manager().sweep_orphans()
    assert orphan in swept and other not in swept
    assert store.get(orphan) is None
    assert store.get(other) is not None
    assert set(orphan_uids.values()) <= set(fake_registry.stopped)
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


def _gated_boot(monkeypatch, gate, fail: BaseException | None = None):
    """Make `boot_decklist_game` block on `gate` (then boot for real, or raise `fail`)."""
    from magic_cards_model_api import decks_api

    real = decks_api.boot_decklist_game

    def boot(*args: Any, **kw: Any):
        assert gate.wait(30)
        if fail is not None:
            raise fail
        return real(*args, **kw)

    monkeypatch.setattr(decks_api, "boot_decklist_game", boot)


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


def _call_tool(server, name: str, **kw: Any) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, kw))
    return json.loads(str(result.content[0].text))


def _tool_server(manager):
    from magic_cards_model_api.sim.config import SimLimits
    from magic_cards_model_api.sim.manager import SandboxManager
    from magic_cards_model_api.sim.server import build_sim_server

    limits = SimLimits.from_env({})
    return build_sim_server(SandboxManager(limits), limits, claude=manager)


def _assert_no_uids(answer: dict[str, Any], uids: Any) -> None:
    text = json.dumps(answer)
    for uid in uids:
        assert uid not in text and uid[:8] not in text


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
