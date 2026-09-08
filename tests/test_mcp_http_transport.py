"""2b T11: the http transport, over a real socket - not `server.call_tool`'s
in-process shortcut, and not `TestClient`'s in-process HTTP shortcut either.

`mcp.py --transport http` (the new default) serves the MCP SDK's stateless
streamable-HTTP ASGI app; this is the only test in the suite that drives it
through a real MCP client over a real TCP connection, proving the framing
actually works end to end - `tests/test_mcp_resilience.py`,
`tests/test_mcp_input_required.py` and friends call `server.call_tool(...)`
directly and would not notice a transport-level defect.

The seat/game side still runs through a monkeypatched `httpx.Client` -> the
in-process `TestClient` (this file is not about the api leg, which every
other MCP test already exercises over the same fake); only the mcp server's
own socket is real, the same split `tests/test_transport_timeout.py`'s
`live_server` fixture uses for the api itself.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.client import Client

from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def mcp_http_server(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> Iterator[tuple[str, str]]:
    """A real uvicorn server serving `build_server(seats).streamable_http_app`
    on an ephemeral port, wired to a real game through an in-process
    `TestClient` (see module docstring). Yields (mcp url, alice's seat uid)."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    game_id = created["game_id"]
    uid = {name: u for u, name in store.seat_uids(game_id).items()}["alice"]

    seats = Seats(api="http://testserver", game_id=game_id)
    mcp_server = build_server(seats)
    asgi_app = mcp_server.streamable_http_app(stateless_http=True)

    port = _free_port()
    config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start in time"
    try:
        yield f"http://127.0.0.1:{port}/mcp", uid
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_http_transport_answers_tools_list_and_a_real_tool_call_over_a_socket(
    mcp_http_server: tuple[str, str],
) -> None:
    url, uid = mcp_http_server

    async def drive() -> tuple[list[str], str]:
        async with Client(url) as mcp_client:
            tools = await mcp_client.list_tools()
            names = [tool.name for tool in tools.tools]
            result = await mcp_client.call_tool("my_state", {"player_uid": uid})
            text = result.content[0].text
            return names, text

    names, text = asyncio.run(drive())

    assert "my_state" in names
    assert "cast" in names
    assert '"active_player": "alice"' in text
    assert '"you": "alice"' in text
    assert '"active_player"' in text
