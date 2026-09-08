"""Defect 2 (2026-09-08): a 4xx refusal reported to the seat as success.

`records.require_reasoning` raises `MissingReasoning` before any
`audit.record` call, so a 422 leaves no log row at all. On the seat side
`Seat.call_json` used to return `response.json()` regardless of HTTP status.
The 422 body is FastAPI's validation shape - `{"detail": [{"loc", "msg",
"type"}]}` - which carries no `allowed`, no `paused`, no `unknown_outcome`, so
`seatkit._refused`/`_paused`/`_unknown_outcome` all missed it and
`run_action` fell through to its **success** path. Measured against a real
agent seat before this fix:

    OUTCOME : {"detail": [{"loc": ["body","reasoning"], "msg": "...", "type": "missing"}]}
    held    : False
    wake    : nothing new yet - call again to check

A refused action, reported as completed.

The fix makes `Seat.call_json` status-aware for every non-2xx response, not
only 422/`reasoning`: a 4xx (client-side - the request itself was rejected,
nothing ran server-side) normalises into the same `allowed: False` shape an
engine-level refusal already carries, with `reason` pulled from whatever the
body's `detail` holds (a plain string everywhere in this api except the two
pydantic-validation-shaped handlers, whose `msg` text - the actual, useful
sentence - is what this surfaces). A body that already carries its own
`allowed` (`DecisionInProgressError`'s 409) is left alone. A 5xx is left
genuinely ambiguous, the same `unknown_outcome` shape an ambiguous transport
timeout already gets, since a server error may or may not have mutated
anything before it failed.

Every test here drives a genuine 422 through the real HTTP app and the real
`Seat.call_json`/`seatkit.run_action` - never a hand-built dict standing in
for one.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi.testclient import TestClient

from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def _agent_game(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/games",
        json={
            "players": [
                {"username": "bob", "is_agent": True},
                {"username": "alice", "is_agent": False},
            ],
            "active_player": "bob",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _seat(monkeypatch, game_id: str, player: str) -> seatkit.Seat:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game_id)
    seat = seats.seat_for(seats_for(game_id)[player])
    assert seat is not None
    return seat


def _bob_seat(monkeypatch, client: TestClient, info: dict[str, Any]) -> seatkit.Seat:
    return _seat(monkeypatch, info["game_id"], "bob")


# -- the reproduction, at the transport layer --------------------------------


def test_call_json_never_reports_a_422_refusal_as_a_successful_outcome(monkeypatch, client) -> None:
    info = _agent_game(client)
    seat = _bob_seat(monkeypatch, client, info)

    # Direct HTTP proof this really is the 422 the docstring describes.
    raw = client.post(f"/api/games/{info['game_id']}/actions/pass-priority", json={"player": "bob"})
    assert raw.status_code == 422, raw.text
    assert raw.json()["detail"][0]["type"] == "missing"

    outcome = seat.call_json(
        "POST", f"/api/games/{info['game_id']}/actions/pass-priority", {"player": "bob"}
    )

    assert outcome["allowed"] is False, outcome
    assert "reasoning" in outcome["reason"], outcome
    assert "agent seat" in outcome["reason"], outcome
    # never silently mistaken for the ambiguous-timeout shape either - the
    # request definitely landed and was definitely refused
    assert outcome.get("unknown_outcome") is not True, outcome


def test_run_action_reports_the_422_as_a_refusal_not_a_completed_action(
    monkeypatch, client
) -> None:
    info = _agent_game(client)
    seat = _bob_seat(monkeypatch, client, info)

    def perform() -> Any:
        return seat.call_json(
            "POST", f"/api/games/{info['game_id']}/actions/pass-priority", {"player": "bob"}
        )

    result = seatkit.run_action(seat, perform, hold_priority=False)

    assert result["held_priority"] is True, result
    assert "refused" in result["wake_reason"].lower(), result
    assert result["outcome"]["allowed"] is False, result
    # `run_action` never chased this with an auto-pass - the refused action
    # left this seat exactly where it was, still its move
    assert "pass_outcome" not in result, result


# -- the same status-blind hole, generalised past 422/`reasoning` -----------


def test_call_json_normalises_an_ordinary_400_the_same_way(client, monkeypatch, game) -> None:
    """A plain `ValueError` (an unknown zone, say) is a 400 with a bare
    string `detail`, not the pydantic-validation list shape - the fix must
    read both, not special-case one. `POST .../cards` is board setup
    (ungated, no `reasoning` needed), so this reaches the 400 with nothing
    else in the way."""
    seat = _seat(monkeypatch, game, "alice")

    raw = client.post(
        f"/api/games/{game}/cards",
        json={"card": "Mountain", "player": "alice", "zone": "not-a-real-zone"},
    )
    assert raw.status_code == 400, raw.text
    assert isinstance(raw.json()["detail"], str)

    outcome = seat.call_json(
        "POST",
        f"/api/games/{game}/cards",
        {"card": "Mountain", "player": "alice", "zone": "not-a-real-zone"},
    )
    assert outcome["allowed"] is False, outcome
    assert outcome["reason"], outcome
    assert outcome.get("unknown_outcome") is not True, outcome


def test_call_json_treats_a_5xx_as_genuinely_unknown_not_a_refusal(
    monkeypatch, client, game
) -> None:
    """A 5xx is not the same claim as a 4xx: the request may have half-landed
    server-side, so it gets the same honest `unknown_outcome` shape an
    ambiguous transport timeout already does - never an ordinary refusal,
    which would tell a caller "nothing changed" when nobody actually knows."""
    seat = _seat(monkeypatch, game, "alice")

    class _FakeResponse:
        status_code = 500
        text = "internal server error"

        def json(self) -> Any:
            return {"detail": "internal server error"}

    monkeypatch.setattr(seat.http, "request", lambda *a, **k: _FakeResponse())

    outcome = seat.call_json("POST", f"/api/games/{game}/advance", {"player": "alice"})

    assert outcome["allowed"] is False, outcome
    assert outcome["unknown_outcome"] is True, outcome
