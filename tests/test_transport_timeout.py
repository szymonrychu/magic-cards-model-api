"""The ambiguous-timeout hazard: a `Seat`'s HTTP client must never mistake an
ordinary read/write/pool timeout for a rules refusal, and a caller must never
be tempted to retry one blindly.

T7b's pause-and-resume machinery (`commands.py`) removed the hazard this file
used to be about: a mutating call used to be able to block *inside the
request*, in `Game.ask`, for up to the server's own `decision_timeout_seconds`
- and `Seat`'s HTTP client timeout had to be derived from that bound
(`seatkit.resolve_client_timeout`, since removed) or a legitimate hold read as
a dead server. That hazard cannot happen any more: a question with no
scripted answer now pauses and returns immediately instead of blocking the
request, so there is no long server-side hold for a client timeout to be
shorter than - see `seatkit.CLIENT_TIMEOUT_SECONDS`'s own docstring. The
tests that exercised that specific derivation (`resolve_client_timeout`
scaling with the server's reported bound, a live server genuinely blocking
past a short client timeout) went with it.

What is left, and still real: a plain network read/write/pool timeout -
unrelated to any decision, just ordinary latency or a dropped connection -
must still turn into `seatkit.UNKNOWN_OUTCOME` rather than an ordinary
refusal or a raised exception, because the action may already have gone
through server-side and a caller must never retry blind. `audit.record`'s
`log_seq`, carried on every action response, is the idempotency marker a
caller uses to check that afterward.

**Real-socket coverage did not go with the rest.** Every test above this
point, and every other API test in this repo, drives the app through
`fastapi.testclient.TestClient` - which runs the ASGI app in-process, with no
real transport underneath it at all (no socket, no HTTP framing, no actual
connection). That hides a whole class of bug a live server can hit and an
in-process client cannot, which is exactly why the two tests this file used
to end with booted a real `uvicorn` server on a real socket. Their specific
proof (a client whose timeout is shorter than a server-side block genuinely
times out; one derived from the server's own report does not) is gone with
the block itself - there is nothing left that holds a request open server
side to time a client out against. But "does the pause/resume round trip
actually work over a real socket" is a different, still-live question -
nothing about `TestClient` proves real HTTP framing carries `paused: true`
and `pending_decision` faithfully, or that a second real request against a
real connection resumes it correctly - so `live_server` and
`test_pause_and_resume_round_trip_over_a_real_socket` below restore that
coverage in the new shape, against the current pause/resume transport
instead of the old blocking one.
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
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


# -- Seat/Seats build with the fixed client timeout ---------------------------


def test_seats_and_seat_carry_the_fixed_timeout(monkeypatch, client, game) -> None:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game)
    assert seats.http.timeout.read == pytest.approx(seatkit.CLIENT_TIMEOUT_SECONDS)

    uid = seats_for(game)["alice"]
    seat = seats.seat_for(uid)
    assert seat is not None
    assert seat.http.timeout.read == pytest.approx(seatkit.CLIENT_TIMEOUT_SECONDS)


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

    result = seatkit.run_action(seat, perform, hold_priority=False)

    assert result["wake_reason"] == seatkit.UNKNOWN_OUTCOME
    assert result["held_priority"] is None, "cannot claim to hold or not hold what it cannot see"
    assert result["state"] is not None, "state is still fetched - reads never block, see store.py"


def _selective_timeout(original_request, blocked_path: str):
    """A `.request` replacement that times out only the one call under test
    (a POST to `blocked_path`) and delegates every other call - the `GET`s
    `run_action`/`fetch_state` still need for `before`/state-after - to the
    real bound method, the same split a genuine mid-flight timeout has in
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
        mcp_seat.http,
        "request",
        _selective_timeout(mcp_seat.http.request, "/actions/pass-priority"),
    )
    monkeypatch.setattr(mcp_seats, "seat_for", lambda uid: mcp_seat)
    server = mcp_module.build_server(mcp_seats)

    tool_result = asyncio.run(server.call_tool("pass", {"player_uid": alice_uid}))
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
            cli_seat.http,
            "request",
            _selective_timeout(cli_seat.http.request, "/actions/pass-priority"),
        )
        monkeypatch.setattr(seat_cli_module, "Seats", lambda api, game: cli_seats)
        monkeypatch.setattr(cli_seats, "seat_for", lambda uid: cli_seat)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            seat_cli_module.run(
                ["--uid", alice_uid_2, "--api", "http://testserver", "--game", game2, "pass"]
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


# -- the real thing: a live server, a real socket, a real pause/resume ------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def live_server() -> Iterator[str]:
    """A real uvicorn server on a real (ephemeral, non-8000) port, serving
    the same `app`/`store` this whole test session already imports - the
    only way to exercise real HTTP framing and a real socket-level
    connection, which an in-process `TestClient` (no real transport
    underneath it) cannot."""
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


def test_pause_and_resume_round_trip_over_a_real_socket(live_server: str) -> None:
    """The pause/resume transport (`commands.py`, `POST .../decide`) proven
    over a real socket rather than `TestClient`'s in-process shortcut - the
    same CR 514.1 cleanup discard `test_decisions.py`'s
    `test_a_seat_sees_and_answers_its_pending_decision` proves over
    `TestClient`, driven here instead through real HTTP requests against a
    live `uvicorn` server: real framing of `paused`/`pending_decision` out,
    a real second connection back in to resume it. Nothing here needs a
    server-side block or a tuned timeout any more - pausing returns
    immediately - so this only has to show the round trip survives an actual
    socket, not merely Starlette's in-process request/response shortcut.
    """
    with httpx.Client(base_url=live_server, timeout=10.0) as http:
        created = http.post(
            "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
        ).json()
        game_id = created["game_id"]
        admin_key = created["admin_key"]
        uids = created["seat_uids"]
        try:
            for _ in range(8):  # 7 max hand size + 1 to force the discard
                r = http.post(
                    f"/api/games/{game_id}/cards",
                    params={"key": admin_key},
                    json={"card": "Mountain", "player": "alice", "zone": "hand"},
                )
                assert r.status_code == 200, r.text
            jump = http.put(
                f"/api/games/{game_id}/step",
                params={"key": admin_key},
                json={"step": "end_step", "player": "alice"},
            )
            assert jump.status_code == 200, jump.text
            board = http.get(f"/api/games/{game_id}", params={"uid": admin_key}).json()
            mountains = [card["id"] for card in board["hands"]["alice"]]
            to_discard = mountains[3]  # an arbitrary one, to prove it is a real choice

            paused = http.post(
                f"/api/games/{game_id}/advance",
                params={"uid": admin_key},
                json={"player": "alice"},
            ).json()
            assert paused["paused"] is True, paused
            pending = paused["pending_decision"]
            assert pending["kind"] == "choose_cards"
            assert pending["player"] == "alice"
            assert to_discard in pending["cards"]

            answered = http.post(
                f"/api/games/{game_id}/decide",
                params={"uid": uids["alice"]},
                json={"player": "alice", "card_ids": [to_discard]},
            )
            assert answered.status_code == 200, answered.text
            assert answered.json()["accepted"] is True

            final = http.get(f"/api/games/{game_id}", params={"uid": admin_key}).json()
            assert final["step"] == "cleanup"
            assert len(final["hands"]["alice"]) == 7
            discarded_ids = [card["id"] for card in final["graveyards"]["alice"]]
            assert discarded_ids == [to_discard]
            assert final.get("pending_decision") is None
        finally:
            http.delete(f"/api/games/{game_id}", params={"key": admin_key})
