"""2b T11: `mcp.py --transport {stdio,http}`, default `http`.

`--game` stays for pinning one game (stdio's existing shape, and useful for
an http server a caller wants dedicated to a single game); left unset in http
mode it means "resolve every call's game from its own seat uid"
(`Seats(resolve_by_uid=True)`, `tests/test_seats_resolve_by_uid.py`) rather
than stdio's "the one live game" auto-discovery, since one http server can
now sit in front of several live games at once.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api.seatkit import Seats


def test_transport_flag_defaults_to_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["mcp"])
    parser_args = mcp_module._build_parser().parse_args([])
    assert parser_args.transport == "http"


def test_transport_flag_accepts_stdio_and_http(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = mcp_module._build_parser()
    assert parser.parse_args(["--transport", "stdio"]).transport == "stdio"
    assert parser.parse_args(["--transport", "http"]).transport == "http"
    with pytest.raises(SystemExit):
        parser.parse_args(["--transport", "carrier-pigeon"])


def test_main_stdio_transport_still_calls_server_run_stdio(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(sys, "argv", ["mcp", "--api", "http://127.0.0.1:1", "--transport", "stdio"])
    started: dict[str, Any] = {}

    class FakeServer:
        def run(self, transport: str) -> None:
            started["transport"] = transport

    monkeypatch.setattr(mcp_module, "build_server", lambda seats, **_: FakeServer())

    mcp_module.main()

    assert started["transport"] == "stdio"
    assert "not reachable" in capsys.readouterr().err


def test_main_default_transport_serves_the_streamable_http_asgi_app(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(sys, "argv", ["mcp", "--api", "http://127.0.0.1:1"])
    served: dict[str, Any] = {}

    class FakeServer:
        pass

    fake_server = FakeServer()
    monkeypatch.setattr(mcp_module, "build_server", lambda seats, **_: fake_server)

    def fake_serve_http(server: Any, host: str, port: int) -> None:
        served["server"] = server
        served["host"] = host
        served["port"] = port

    monkeypatch.setattr(mcp_module, "_serve_http", fake_serve_http)

    mcp_module.main()

    assert served["server"] is fake_server
    assert served["host"] == "127.0.0.1"
    assert isinstance(served["port"], int)


def test_main_resolves_by_uid_whenever_no_game_is_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class RecordingSeats(Seats):
        def __init__(self, api: str, game_id: str | None, *, resolve_by_uid: bool = False):
            captured["game_id"] = game_id
            captured["resolve_by_uid"] = resolve_by_uid
            super().__init__(api, game_id, resolve_by_uid=resolve_by_uid)

    monkeypatch.setattr(mcp_module, "Seats", RecordingSeats)
    monkeypatch.setattr(mcp_module, "build_server", lambda seats, **_: _NullServer())
    monkeypatch.setattr(mcp_module, "_serve_http", lambda server, host, port: None)

    monkeypatch.setattr(sys, "argv", ["mcp", "--api", "http://127.0.0.1:1"])
    mcp_module.main()
    assert captured["resolve_by_uid"] is True, "http, no --game: must resolve per uid"

    monkeypatch.setattr(sys, "argv", ["mcp", "--api", "http://127.0.0.1:1", "--game", "g1"])
    mcp_module.main()
    assert captured["resolve_by_uid"] is False, "a pinned --game must never be overridden"

    monkeypatch.setattr(sys, "argv", ["mcp", "--api", "http://127.0.0.1:1", "--transport", "stdio"])
    mcp_module.main()
    assert captured["resolve_by_uid"] is True, "stdio, no --game: must resolve per uid too (S1)"


class _NullServer:
    def run(self, transport: str) -> None:
        pass
