"""The long-poll tools give up at once on a game that is gone, and on a stop.

A lobby release deletes the game (`lobby.reset_game`), after which every read
of it answers 404 - and `Seat.fetch_state` returns that 404's body as if it
were a board, so `block_until_wake` read "nothing new yet" and polled a
deleted game for its whole bound, and `pass_until` did the same. The wait now
answers `GAME_GONE` the first time it reads a 404 or 410.

The other way out is the caller going away: an interrupted MCP call (the
wrapper's ESC makes claude send `notifications/cancelled`) sets the request's
`cancel_requested`, but a sync tool runs on a worker thread the SDK cannot
cancel, so the wait itself has to look (`should_stop`).
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi.testclient import TestClient
from test_action_flow import call_mcp_tool
from test_autopass import set_step, stock_libraries

from magic_cards_model_api import mcp, seat_cli, seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.playscripts import runtime
from magic_cards_model_api.seatkit import Seats, block_until_wake, pass_until
from magic_cards_model_api.store import store


def wired(monkeypatch, game_id: str) -> tuple[Seats, dict[str, str]]:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    uids = {name: uid for uid, name in store.seat_uids(game_id).items()}
    return Seats(api="http://testserver", game_id=game_id), uids


def delete_after(client: TestClient, game_id: str, uid: str, seconds: float) -> list[int]:
    """Delete the game the way `DELETE /api/games/{id}` does, from a thread."""
    statuses: list[int] = []

    def run() -> None:
        time.sleep(seconds)
        statuses.append(client.delete(f"/api/games/{game_id}", params={"uid": uid}).status_code)

    threading.Thread(target=run, daemon=True).start()
    return statuses


def stop_after(seconds: float) -> Any:
    started = time.monotonic()
    return lambda: time.monotonic() - started >= seconds


def test_a_wait_on_a_game_deleted_mid_wait_answers_game_gone_at_once(client, game, monkeypatch):
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    deleted = delete_after(client, game, uids["alice"], 0.2)

    started = time.monotonic()
    result = block_until_wake(bob, before=None, max_seconds=5.0)
    elapsed = time.monotonic() - started

    assert deleted == [200]
    assert result["wake_reason"] == seatkit.GAME_GONE
    assert elapsed < 1.5, f"polled a deleted game for {elapsed:.2f}s"


def test_a_wait_on_a_game_already_gone_does_not_wait(client, game, monkeypatch):
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    client.delete(f"/api/games/{game}", params={"uid": uids["alice"]})

    started = time.monotonic()
    result = block_until_wake(bob, before=None, max_seconds=5.0)

    assert result["wake_reason"] == seatkit.GAME_GONE
    assert time.monotonic() - started < 1.0


def test_pass_until_on_a_game_deleted_mid_wait_stops_with_game_gone(client, game, monkeypatch):
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")
    delete_after(client, game, uids["alice"], 0.2)

    started = time.monotonic()
    result = pass_until(
        alice,
        until_step="postcombat_main",
        whose_turn="mine",
        stop_when_you_can_attack=False,
        max_seconds=5.0,
    )
    elapsed = time.monotonic() - started

    assert result["finished"] is True
    assert result["stopped_because"] == seatkit.GAME_GONE
    assert elapsed < 1.5, f"retried a deleted game for {elapsed:.2f}s"


def test_a_stop_signal_ends_a_wait_with_interrupted(client, game, monkeypatch):
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])

    started = time.monotonic()
    result = block_until_wake(bob, before=None, max_seconds=5.0, should_stop=stop_after(0.2))
    elapsed = time.monotonic() - started

    assert result["wake_reason"] == seatkit.WAIT_INTERRUPTED
    assert elapsed < 1.5


def test_a_stop_signal_ends_pass_until_retries_and_keeps_the_resume_token(
    client, game, monkeypatch
):
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    started = time.monotonic()
    result = pass_until(
        alice,
        until_step="postcombat_main",
        whose_turn="mine",
        stop_when_you_can_attack=False,
        max_seconds=5.0,
        should_stop=stop_after(0.2),
    )
    elapsed = time.monotonic() - started

    assert result["finished"] is False
    assert result["stopped_because"] == seatkit.WAIT_INTERRUPTED
    assert "resume" in result
    assert elapsed < 1.5


def test_the_mcp_wait_tool_answers_game_gone_for_a_game_deleted_mid_wait(client, game, monkeypatch):
    seats, uids = wired(monkeypatch, game)
    server = build_server(seats)
    delete_after(client, game, uids["alice"], 0.5)

    started = time.monotonic()
    answer = call_mcp_tool(
        server, "wait_for_priority", {"player_uid": uids["bob"], "max_seconds": 10}
    )

    assert answer["wake_reason"] == seatkit.GAME_GONE
    assert time.monotonic() - started < 2.5


def test_the_mcp_pass_until_tool_answers_game_gone_for_a_game_deleted_mid_wait(
    client, game, monkeypatch
):
    seats, uids = wired(monkeypatch, game)
    server = build_server(seats)
    stock_libraries(client, game)
    set_step(client, game, "upkeep")
    delete_after(client, game, uids["bob"], 0.5)

    started = time.monotonic()
    answer = call_mcp_tool(
        server,
        "pass_until",
        {
            "player_uid": uids["alice"],
            "step": "postcombat_main",
            "stop_when_you_can_attack": False,
            "max_seconds": 10,
        },
    )

    assert answer["stopped_because"] == seatkit.GAME_GONE
    assert time.monotonic() - started < 2.5


def test_the_cli_wait_answers_game_gone_for_a_game_deleted_mid_wait(
    client, game, monkeypatch, capsys
):
    seats, uids = wired(monkeypatch, game)
    delete_after(client, game, uids["alice"], 0.5)

    started = time.monotonic()
    seat_cli.run(["--uid", uids["bob"], "--game", game, "wait", "--max-seconds", "10"])

    assert json.loads(capsys.readouterr().out)["wake_reason"] == seatkit.GAME_GONE
    assert time.monotonic() - started < 2.5


def test_the_cancel_probe_reads_the_requests_cancellation():
    cancelled = threading.Event()
    probe = mcp.cancel_probe(
        SimpleNamespace(request_context=SimpleNamespace(cancel_requested=cancelled))
    )
    assert probe() is False
    cancelled.set()
    assert probe() is True


def test_the_cancel_probe_never_stops_a_call_outside_a_request():
    class Detached:
        @property
        def request_context(self) -> Any:
            raise ValueError("Context is not available outside of a request")

    assert mcp.cancel_probe(None)() is False
    assert mcp.cancel_probe(Detached())() is False


def test_a_play_script_run_counts_a_stop_signal_as_its_time_cap():
    ctx = runtime._Context(
        seat=SimpleNamespace(player="alice"),
        reasoning="r",
        corpus={},
        inputs={},
        catalog={},
        deadline=time.monotonic() + 60,
        poll_seconds=0.05,
        call_started=time.monotonic(),
        should_stop=lambda: True,
    )
    assert ctx.time_up() is True
