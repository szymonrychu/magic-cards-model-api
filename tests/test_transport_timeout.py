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
"""

from __future__ import annotations

import asyncio
import contextlib
import io

import httpx
import pytest
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

    result = seatkit.run_action(seat, perform, hold_priority=False, max_seconds=1.0)

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
