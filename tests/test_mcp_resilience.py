"""The MCP server must outlive the API restarting underneath it.

A playtest loop reboots the API once per game (a fresh `uv run python -m
magic_cards_model_api.serve`); the MCP process is spawned once for the whole
session and never reloads (see `mcp.py`'s module docstring). Three things used
to break when the API restarted mid-session, found by actually reproducing it
rather than reading the code:

- `mcp.py`'s `main()` used to call a fatal, boot-time-only protocol check
  (`check_protocol_version`, since removed) that raised `SystemExit` the
  moment the API was unreachable - including at the MCP process's own boot,
  before it ever served a tool.
- an unreachable API mid-session used to propagate an unhandled `httpx`
  exception straight out of a tool call.
- `Seats.game_id()` cached the first game id it ever discovered forever, so
  a restarted API booting a brand new game (brand new id, brand new seat
  uids - `serve.py` never reuses either) left every subsequent call
  permanently pointed at a game that no longer existed, indistinguishable
  from the MCP process itself being dead - the actual, observed "tools
  disconnect and stay gone" symptom.

This file pins down the fix: `seatkit.api_trouble` (a pure check, tested
directly, never raising), `mcp._guarded` (every tool call goes through it),
and `Seats` re-discovering the live game rather than caching it.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
from fastapi.testclient import TestClient

from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def make_game(client: TestClient) -> tuple[str, dict[str, str]]:
    """A fresh game plus its username -> uid map, the same shape `serve.py`
    hands to a real playtest dispatcher."""
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    game_id = created["game_id"]
    uids = {name: uid for uid, name in store.seat_uids(game_id).items()}
    return game_id, uids


def unpinned_seats(monkeypatch) -> Seats:
    """A `Seats` wired to the in-process TestClient, discovering the live
    game rather than being pinned to one via `--game` - how a real playtest
    MCP process runs, since nothing hands it a game id up front."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=None)


def call_tool_raw(server: Any, name: str, arguments: dict[str, Any]) -> str:
    """The tool's raw text answer, unparsed - the answers under test here are
    plain error strings, not the usual JSON blob every other tool returns."""
    result = asyncio.run(server.call_tool(name, arguments))
    return result.content[0].text


# -- seatkit.api_trouble: the check itself, direct and pure -----------------


class _FakeHttp:
    """Stands in for `Seat.http`/`Seats.http`: whatever `/api/health` would
    have answered, without a real server."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def get(self, path: str, timeout: float | None = None) -> Any:
        assert path == "/api/health"
        return self

    def json(self) -> dict[str, Any]:
        return self._payload


class _RaisingHttp:
    """Stands in for an `http` whose transport cannot reach the API at all."""

    def get(self, path: str, timeout: float | None = None) -> Any:
        raise httpx.ConnectError("Connection refused")


def test_api_trouble_none_when_versions_match() -> None:
    http = _FakeHttp({"status": "ok", "protocol_version": seatkit.PROTOCOL_VERSION})
    assert seatkit.api_trouble(http, "http://example") is None


def test_api_trouble_names_both_versions_on_mismatch() -> None:
    http = _FakeHttp({"status": "ok", "protocol_version": "seatkit-999"})
    trouble = seatkit.api_trouble(http, "http://example")
    assert trouble is not None
    assert "seatkit-999" in trouble
    assert seatkit.PROTOCOL_VERSION in trouble
    # the message must not itself be mistaken for a crash - it is a message
    assert isinstance(trouble, str)


def test_api_trouble_reports_unreachable_without_raising() -> None:
    trouble = seatkit.api_trouble(_RaisingHttp(), "http://127.0.0.1:1")
    assert trouble is not None
    assert "not reachable" in trouble
    assert "http://127.0.0.1:1" in trouble


# -- mcp.py: a tool call never crashes the process ---------------------------


def test_guarded_tool_returns_clear_error_when_api_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id="does-not-matter")
    seats.http = _RaisingHttp()  # the API is down before the call even starts
    server = build_server(seats)

    text = call_tool_raw(server, "battlefield_state", {"player_uid": "whoever"})

    assert "not reachable" in text
    assert "http://testserver" in text


def test_guarded_tool_survives_httpx_error_mid_call(monkeypatch) -> None:
    """The pre-call health probe can pass and the API can still drop away a
    moment later, inside the tool's own request - `_guarded` must catch that
    too, not just the up-front check."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=None)

    def boom(self: Seats, uid: str) -> None:
        raise httpx.ReadTimeout("dropped mid-request")

    monkeypatch.setattr(Seats, "seat_for", boom)
    server = build_server(seats)

    text = call_tool_raw(server, "battlefield_state", {"player_uid": "whoever"})

    assert "unreachable mid-call" in text
    assert "http://testserver" in text


def test_main_does_not_exit_when_api_unreachable_at_boot(monkeypatch, capsys) -> None:
    """The old `check_protocol_version` raised `SystemExit` here; `main()`
    must now start the server regardless and only warn. Pinned to
    `--transport stdio` deliberately: that branch is what's under test here
    (`FakeServer.run`), not the http transport's own boot path - see
    `tests/test_mcp_transport_flag.py` for the (now default) http one."""
    monkeypatch.setattr(sys, "argv", ["mcp", "--api", "http://127.0.0.1:1", "--transport", "stdio"])
    started: dict[str, Any] = {}

    class FakeServer:
        def run(self, transport: str) -> None:
            started["transport"] = transport

    monkeypatch.setattr(mcp_module, "build_server", lambda seats: FakeServer())

    mcp_module.main()  # must not raise SystemExit

    assert started["transport"] == "stdio"
    assert "not reachable" in capsys.readouterr().err


# -- Seats: the live game is re-discovered, not cached forever --------------


def test_seats_picks_up_new_game_after_restart_and_drops_stale_uid(client, monkeypatch) -> None:
    seats = unpinned_seats(monkeypatch)

    game1, uids1 = make_game(client)
    assert seats.game_id() == game1
    seat = seats.seat_for(uids1["alice"])
    assert seat is not None
    assert seat.player == "alice"

    # The API restarting: the old game is gone, a brand new one takes its
    # place with a brand new id and brand new uids - never reused, exactly
    # what a fresh `serve.py` boot does.
    client.delete(f"/api/games/{game1}", params={"key": store.admin_key(game1)})
    game2, uids2 = make_game(client)
    assert game2 != game1
    assert uids2["alice"] != uids1["alice"]

    assert seats.game_id() == game2, "must not still be pointed at the dead game"
    assert seats.seat_for(uids2["alice"]) is not None
    assert seats.seat_for(uids1["alice"]) is None, (
        "a uid from the previous game must not silently keep working "
        "against the new one just because it was cached"
    )


def test_pinned_game_id_never_switches(client, monkeypatch) -> None:
    """`--game <id>` pins this process to one game on purpose; that must
    keep working even though auto-discovery now re-checks every call."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    game1, _ = make_game(client)
    seats = Seats(api="http://testserver", game_id=game1)

    make_game(client)  # a second, unrelated game becomes "the live one" too

    assert seats.game_id() == game1
