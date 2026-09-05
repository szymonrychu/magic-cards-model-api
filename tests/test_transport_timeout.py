"""The read-timeout hazard: a `Seat`'s HTTP client must never give up on a
request before the server could legitimately still be holding it.

A previous wave made every action tool block until the seat next has
something to do (`seatkit.run_action`/`block_until_wake`). That wait loop is
quick polling reads and was never the risk; the real one is upstream of it -
a single mutating call (`cast`/`activate`/`attack`/`pass-priority`/
`resolve-top`/`advance`) can cross a live CR 608.2d question and block
*inside that one request*, in `Game.ask` (`decisions.py`), for up to the
server's own `decision_timeout_seconds`. `Seat`/`Seats` used to build their
`httpx.Client` with a bare, fixed `timeout=30.0` - shorter than that block's
production default (60s) - so a legitimate hold on the server side looked
like a dead server on the client side. The player sees a timeout, the server
already performed the action: a duplicate cast/activation/attack waiting to
happen on any blind retry.

This file: `seatkit.resolve_client_timeout` derives the client's read
timeout from what the server itself reports (`/api/health`'s
`decision_timeout_seconds`) rather than a second, independently-guessed
constant; `Seat.call`/`call_json` turn a genuine, ambiguous timeout into an
explicit "outcome unknown, do not retry blindly" answer instead of raising;
and `audit.record`'s `log_seq`, now carried on every action response, is the
idempotency marker a caller can use to tell whether a retried action created
a second entry.

`test_old_style_short_client_timeout_genuinely_times_out_on_a_real_block` and
`test_seat_with_derived_timeout_survives_the_same_kind_of_block` are the ones
that matter most: they boot a *real* server on a real socket (the only way
this class of bug shows up - see this file's own manual reproduction against
a live server on a non-default port, pasted into the fix's writeup) and prove
the exact split the bug report described - a client still built the old way
(a bare, fixed timeout shorter than the block) genuinely times out, while a
`Seat` whose timeout is derived from the server's own report comfortably
outlasts the identical kind of block and gets the real answer back.

`mcp.py` and `seat_cli.py` never build their own transport - both go through
`Seats`/`Seat` here, so `test_both_transports_report_the_same_unknown_outcome`
checks that shared behaviour once, through both call shapes, rather than
duplicating this whole file for each.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from conftest import add_card, state
from fastapi.testclient import TestClient

from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api import seat_cli as seat_cli_module
from magic_cards_model_api import seatkit
from magic_cards_model_api.decisions import DEFAULT_TIMEOUT_SECONDS
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


# -- resolve_client_timeout: pure, no server needed --------------------------


class _FakeHealth:
    """Stands in for `Seat.http`/`Seats.http` answering `/api/health`."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def get(self, path: str) -> _FakeHealth:
        assert path == "/api/health"
        return self

    def json(self) -> dict:
        return self._payload


class _RaisingHealth:
    def get(self, path: str) -> None:
        raise httpx.ConnectError("connection refused")


def test_resolve_client_timeout_derives_from_the_servers_own_report() -> None:
    """The client timeout is the server's reported block bound plus slack -
    not a second constant sized independently of it, which is exactly how
    the original hazard shipped (30.0 < the server's real 60s default)."""
    timeout = seatkit.resolve_client_timeout(_FakeHealth({"decision_timeout_seconds": 12.0}))
    assert timeout == pytest.approx(12.0 + seatkit.TIMEOUT_MARGIN_SECONDS)
    assert timeout > 12.0, "must comfortably outlast the reported block, not merely equal it"


def test_resolve_client_timeout_scales_with_a_different_reported_bound() -> None:
    """Not hardcoded: a differently-configured server gets a differently
    sized client timeout, derived the same way both times."""
    small = seatkit.resolve_client_timeout(_FakeHealth({"decision_timeout_seconds": 5.0}))
    large = seatkit.resolve_client_timeout(_FakeHealth({"decision_timeout_seconds": 500.0}))
    assert large - small == pytest.approx(495.0)


def test_resolve_client_timeout_falls_back_when_the_server_cannot_be_asked() -> None:
    """An unreachable server (or an old build missing the field) still gets a
    safe, generous timeout - this process's own import of the server's
    default - rather than raising or reverting to a bare short constant.
    `api_trouble` is what reports the unreachability loudly; this only needs
    *a* safe number to build a client with."""
    timeout = seatkit.resolve_client_timeout(_RaisingHealth())
    assert timeout == pytest.approx(DEFAULT_TIMEOUT_SECONDS + seatkit.TIMEOUT_MARGIN_SECONDS)


# -- Seat/Seats actually pick the derived timeout up -------------------------


def test_seats_and_seat_carry_the_derived_timeout(monkeypatch, client, game) -> None:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game)
    expected = DEFAULT_TIMEOUT_SECONDS + seatkit.TIMEOUT_MARGIN_SECONDS
    assert seats.http.timeout.read == pytest.approx(expected)

    uid = seats_for(game)["alice"]
    seat = seats.seat_for(uid)
    assert seat is not None
    assert seat.http.timeout.read == pytest.approx(expected)


# -- an ambiguous timeout is reported, never raised or mistaken for refusal --


def test_call_json_turns_a_read_timeout_into_an_explicit_unknown_outcome(
    monkeypatch, client, game
) -> None:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game)
    seat = seats.seat_for(seats_for(game)["alice"])
    assert seat is not None

    def timed_out(*args: object, **kwargs: object) -> None:
        raise httpx.ReadTimeout("simulated: the server may still be holding this request")

    monkeypatch.setattr(seat.http, "request", timed_out)

    outcome = seat.call_json("POST", f"/api/games/{game}/advance", {"player": "alice"})

    assert outcome["allowed"] is False
    assert outcome["unknown_outcome"] is True
    assert "UNKNOWN" in outcome["reason"]
    assert "not" in outcome["reason"].lower() and "retry" in outcome["reason"].lower()


def test_run_action_never_reports_an_unknown_outcome_as_an_ordinary_refusal(
    monkeypatch, client, game
) -> None:
    """Both `unknown_outcome` and an ordinary refusal carry `allowed: False`
    - `run_action` must still tell them apart, since "refused - nothing
    changed, still your move" is simply false about a call whose real
    outcome nobody knows yet."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game)
    seat = seats.seat_for(seats_for(game)["alice"])
    assert seat is not None

    def perform() -> dict:
        return {
            "allowed": False,
            "reason": "boom (simulated timeout)",
            "unknown_outcome": True,
        }

    result = seatkit.run_action(seat, perform, hold_priority=False, max_seconds=1.0)

    assert result["wake_reason"] == seatkit.UNKNOWN_OUTCOME
    assert result["held_priority"] is None, "cannot claim to hold or not hold what it cannot see"
    assert result["state"] is not None, "state is still fetched - reads never block, see store.py"


def _selective_timeout(original_request, blocked_path: str):
    """A `.request` replacement that times out only the one call under test
    (a POST to `blocked_path`) and delegates every other call - the `GET`s
    `run_action`/`fetch_state` still need for `before`/state-after - to the
    real bound method, the same split a genuine mid-block timeout has in
    production (the action's own request never answers; reads beside it do).
    """

    def wrapper(method: str, path: str, **kwargs: object):
        if method == "POST" and blocked_path in path:
            raise httpx.ReadTimeout("simulated: the server may still be holding this request")
        return original_request(method, path, **kwargs)

    return wrapper


def test_both_transports_report_the_same_unknown_outcome(monkeypatch, client, game) -> None:
    """`mcp.py` and `seat_cli.py` are two different call shapes over the same
    `Seats`/`Seat` transport (`seatkit.py`'s module docstring) - this checks
    that shared behaviour once through each, rather than trusting that
    "shares a module" actually means "behaves the same" without ever
    driving both.
    """
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    alice_uid = seats_for(game)["alice"]

    # -- the mcp tool path --
    mcp_seats = Seats(api="http://testserver", game_id=game)
    mcp_seat = mcp_seats.seat_for(alice_uid)
    assert mcp_seat is not None
    monkeypatch.setattr(
        mcp_seat.http, "request", _selective_timeout(mcp_seat.http.request, "/advance")
    )
    monkeypatch.setattr(mcp_seats, "seat_for", lambda uid: mcp_seat)
    server = mcp_module.build_server(mcp_seats)

    tool_result = asyncio.run(server.call_tool("advance", {"player_uid": alice_uid}))
    tool_text = tool_result.content[0].text
    assert "UNKNOWN" in tool_text
    assert "do not" in tool_text.lower() or "do NOT" in tool_text

    # -- the seat_cli path, a fresh game so the two transports do not step on
    # each other's turn/priority state --
    game2 = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()["game_id"]
    try:
        alice_uid_2 = seats_for(game2)["alice"]
        cli_seats = Seats(api="http://testserver", game_id=game2)
        cli_seat = cli_seats.seat_for(alice_uid_2)
        assert cli_seat is not None
        monkeypatch.setattr(
            cli_seat.http, "request", _selective_timeout(cli_seat.http.request, "/advance")
        )
        monkeypatch.setattr(seat_cli_module, "Seats", lambda api, game: cli_seats)
        monkeypatch.setattr(cli_seats, "seat_for", lambda uid: cli_seat)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            seat_cli_module.run(
                ["--uid", alice_uid_2, "--api", "http://testserver", "--game", game2, "advance"]
            )
        cli_text = buf.getvalue()
        assert "UNKNOWN" in cli_text
        assert "do not" in cli_text.lower() or "do NOT" in cli_text
    finally:
        client.delete(f"/api/games/{game2}")


# -- log_seq: a caller can tell a retry apart from the original --------------


def test_log_seq_lets_a_caller_detect_a_repeated_action(client, game) -> None:
    add_card(client, game, "Mountain", "alice", "battlefield")
    mountain = next(c for c in state(client, game)["battlefield"] if c["name"] == "Mountain")

    first = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": mountain["id"]},
    ).json()
    assert first["allowed"] is True
    seq1 = first["log_seq"]
    assert isinstance(seq1, int)

    log = client.get(f"/api/games/{game}/log").json()["log"]
    assert log[-1]["seq"] == seq1
    assert log[-1]["action"] == "activate"

    # A caller unsure whether its first call landed (a timeout, say) and
    # that retries the same call sees a strictly later log_seq - a second,
    # real entry, not silently indistinguishable from the first.
    add_card(client, game, "Mountain", "alice", "battlefield")
    second_mountain = next(
        c for c in state(client, game)["battlefield"] if c["id"] != mountain["id"]
    )
    second = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": second_mountain["id"]},
    ).json()
    assert second["log_seq"] > seq1


# -- the real thing: a live server, a real socket, a real block -------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def live_server() -> Iterator[str]:
    """A real uvicorn server on a real (ephemeral, non-8000) port, serving
    the same `app`/`store` this whole test session already imports - the
    only way to exercise a genuine socket-level read timeout, which an
    in-process `TestClient` (no real transport underneath it) cannot."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not start in time"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _new_game_over_http(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=10.0) as boot:
        created = boot.post(
            "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
        ).json()
    return created["game_id"]


def _setup_pending_discard(live_server: str, block_seconds: float) -> str:
    """A fresh game, over the real socket, one call away from the discard
    decision blocking for `block_seconds`: alice's hand stuffed past the
    maximum hand size, the marker jumped to `end_step` so the very next
    `advance()` is the one that enters cleanup (CR 514.1, 701.9b) and asks
    it. `provider.timeout_seconds` is the same white-box override
    `test_decisions.py` uses, shrunk from the real 60s default so the block
    is a genuine, real-seconds socket wait without making this test slow.
    """
    game_id = _new_game_over_http(live_server)
    provider = store.get(game_id).decisions.provider
    assert provider is not None
    provider.timeout_seconds = block_seconds
    # All three of the provider's bounds, not just `timeout_seconds`: what
    # these tests need is a server-side block of a known length, and how long
    # the provider actually blocks depends on which bound applies (see
    # `DecisionMailbox.delivery_bound`). Nobody polls this game, so the seat is
    # unproven and it is `presence_seconds` that decides - pinning only
    # `timeout_seconds` would leave the block's real length set by a module
    # default these tests never mention.
    provider.delivery_seconds = block_seconds
    provider.presence_seconds = block_seconds
    with httpx.Client(base_url=live_server, timeout=10.0) as boot:
        for _ in range(9):  # 7 max hand size + a couple to force the discard
            r = boot.post(
                f"/api/games/{game_id}/cards",
                json={"card": "Mountain", "player": "alice", "zone": "hand"},
            )
            assert r.status_code == 200, r.text
        jump = boot.put(f"/api/games/{game_id}/step", json={"step": "end_step", "player": "alice"})
        assert jump.status_code == 200, jump.text
    return game_id


def test_old_style_short_client_timeout_genuinely_times_out_on_a_real_block(
    live_server: str,
) -> None:
    """Proves the hazard is real, not merely theoretical: a client built the
    old way - a bare, fixed timeout shorter than a legitimate server-side
    block - genuinely gives up mid-block over a real socket, exactly the
    "read-timed-out even though the action had gone through" symptom the
    bug report described. This is the failure mode `resolve_client_timeout`
    exists to prevent (see the next test for the fixed side of this split).
    """
    block_seconds = 3.0
    game_id = _setup_pending_discard(live_server, block_seconds)
    try:
        old_style_client = httpx.Client(base_url=live_server, timeout=block_seconds - 1.5)
        with old_style_client, pytest.raises(httpx.ReadTimeout):
            old_style_client.post(f"/api/games/{game_id}/advance", json={"player": "alice"})
    finally:
        # The abandoned request is still running server-side (that is the
        # whole point) - give it time to finish on its own before the game
        # is torn down out from under it.
        time.sleep(block_seconds + 1.0)
        with httpx.Client(base_url=live_server, timeout=10.0) as cleanup_client:
            cleanup_client.delete(f"/api/games/{game_id}", params={"key": store.admin_key(game_id)})


def test_seat_with_derived_timeout_survives_the_same_kind_of_block(live_server: str) -> None:
    """The fixed side: a `Seat` built through `Seats` derives its timeout
    from the server's own default `decision_timeout_seconds` (not this
    game's shrunk white-box override, which is not visible over the API) and
    so comfortably outlasts a block far shorter than that default - the
    call blocks for the real duration and then returns the real answer,
    never a timeout.
    """
    block_seconds = 3.0
    game_id = _setup_pending_discard(live_server, block_seconds)
    try:
        alice_uid = next(u for u, name in store.seat_uids(game_id).items() if name == "alice")
        seats = Seats(api=live_server, game_id=game_id)
        seat = seats.seat_for(alice_uid)
        assert seat is not None
        assert seat.http.timeout.read > block_seconds, (
            "the derived timeout must comfortably exceed this block for the "
            "call below to prove anything"
        )

        started = time.monotonic()
        result = seat.call_json("POST", f"/api/games/{game_id}/advance", {"player": "alice"})
        elapsed = time.monotonic() - started

        assert elapsed >= block_seconds * 0.8, "the call returned suspiciously fast - not blocked"
        assert result.get("unknown_outcome") is not True, (
            f"the derived-timeout client should not have timed out: {result}"
        )
        assert result["advanced_to"] == "cleanup"
        assert isinstance(result["log_seq"], int)
    finally:
        with httpx.Client(base_url=live_server, timeout=10.0) as cleanup_client:
            cleanup_client.delete(f"/api/games/{game_id}", params={"key": store.admin_key(game_id)})
