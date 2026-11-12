"""Claude-seat sim matches: the pieces they share with the lobby."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
RED = (FIXTURES / "red_burn.txt").read_text()
GREEN = (FIXTURES / "green_stompy.txt").read_text()


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
    """End a test match through the engine and let a poll clean it up."""
    _end_game(_game_id(manager, match_id), "b")
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
    running = manager.status(match_id)
    assert running["status"] == "running"
    assert running["seats"] == {"a": {"state": "thinking"}, "b": {"state": "thinking"}}
    assert "support_warnings" in running
    assert fake_registry.stopped == []

    _end_game(game_id, "b")
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
    answers.append(call("sim_match_claude_status", match_id=match_id))
    _end_game(game_id, "b")
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
    assert polled["seats"] == {"a": {"state": "starting"}, "b": {"state": "starting"}}
    assert fake_registry.started == []
    with pytest.raises(SimRefused, match="being started"):
        manager.start(RED, GREEN, seed=2)  # the slot is held while booting
    gate.set()
    _wait(manager, out["match_id"])
    running = manager.status(out["match_id"])
    assert running["status"] == "running"
    assert running["seats"] == {"a": {"state": "thinking"}, "b": {"state": "thinking"}}
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
    assert manager.status(match_id)["status"] == "running"  # one seat is still playing
    fake_registry.states = {"a": dead, "b": "gone"}
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
