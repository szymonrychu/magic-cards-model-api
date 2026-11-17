"""Fixtures shared by the Claude-match tests: decks, fake registry, manager, ledger builders."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
RED = (FIXTURES / "red_burn.txt").read_text()
GREEN = (FIXTURES / "green_stompy.txt").read_text()
OPUS_LOW = {"model": "claude-opus-5-5", "effort": "low"}


def snap(
    life: tuple[int, int] = (20, 20),
    hand: tuple[int, int] = (5, 5),
    board: tuple[int, int] = (3, 3),
    stack: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """A snapshot in the shape `audit.record` stores."""
    return {
        "life": {"a": life[0], "b": life[1]},
        "hands": {"a": [f"h{i}" for i in range(hand[0])], "b": [f"h{i}" for i in range(hand[1])]},
        "battlefield": [f"a: Land {i}" for i in range(board[0])]
        + [f"b: Land {i}" for i in range(board[1])],
        "stack": list(stack),
    }


def row(
    seq: int,
    action: str,
    *,
    turn: int | None = 3,
    step: str | None = "precombat_main",
    player: str | None = "a",
    reasoning: str | None = None,
    allowed: bool = True,
    snapshot: dict[str, Any] | None = None,
    active_player: str | None = "a",
    phase: str | None = "main",
    **detail: Any,
) -> dict[str, Any]:
    """One `records` row as `store.records` returns it."""
    body: dict[str, Any] = dict(detail)
    if player is not None:
        body["player"] = player
    if not allowed:
        body["allowed"] = False
    return {
        "seq": seq,
        "created_at": "2026-11-16T00:00:00+00:00",
        "payload": {
            "ts": "2026-11-16T00:00:00+00:00",
            "action": action,
            "detail": body,
            "turn": turn,
            "step": step,
            "phase": phase,
            "snapshot": snapshot if snapshot is not None else snap(),
            "active_player": active_player,
            "priority_holder": player,
            "reasoning": reasoning,
        },
    }


def ledger(turns: int, per_turn: int, *, start_seq: int = 1) -> list[dict[str, Any]]:
    """`turns` turns of `per_turn` distinct non-pass rows each, alternating seats."""
    rows: list[dict[str, Any]] = []
    seq = start_seq
    for turn in range(1, turns + 1):
        for i in range(per_turn):
            seat = "a" if i % 2 == 0 else "b"
            rows.append(
                row(
                    seq,
                    "cast",
                    turn=turn,
                    player=seat,
                    card=f"Spell {turn}-{i}",
                    reasoning=f"reason {turn}-{i}",
                )
            )
            seq += 1
    return rows


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


def _manager(clock=None, wall=None, **env):
    from datetime import UTC, datetime

    from magic_cards_model_api.sim.claude import ClaudeMatchManager
    from magic_cards_model_api.sim.config import SimLimits

    limits = SimLimits.from_env({k: str(v) for k, v in env.items()})
    return ClaudeMatchManager(
        limits, clock=clock or Clock(), wall=wall or (lambda: datetime.now(UTC))
    )


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
