"""The client-side half of the plan-declaration seam: `declare_plan` on
`seatkit.Seat` and on the `mtg` MCP server, and the round trip through both
of them and `records.require_plan`.

`tests/test_plan_declaration.py` covers the backend route and gate directly,
against the raw HTTP surface. This file is what was actually broken on
main before this change: the backend gate (`records.require_plan`) already
refused an agent seat's first mutating action of its own turn until a
`plan_declared` record existed, but neither `seatkit.py` nor `mcp.py` had any
way to call `POST .../plan` at all - so a deployed agent seat could never
clear the gate and take its own first action of a turn, full stop. The test
that matters most here is `test_the_gate_refuses_until_declare_plan_then_allows`
and its MCP-tool twin: a seat that declares a plan can then act, and one that
has not is refused.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import NO_SUCH_SEAT, Seats

PLAN: dict[str, Any] = {
    "board_read": "empty board both sides, I am on the play with a Mountain in hand",
    "my_role": "beatdown - my deck wins before turn 6 or not at all",
    "plan": "land, hold the Bolt for a blocker rather than the face",
    "expecting": "alice plays a tapped land and passes with no play",
    "because": "she kept seven and has cast nothing in two turns",
    "confidence": "medium",
}


def _agent_game(client: TestClient, active: str = "bob") -> dict[str, Any]:
    response = client.post(
        "/api/games",
        json={
            "players": [
                {"username": "bob", "is_agent": True},
                {"username": "alice", "is_agent": False},
            ],
            "active_player": active,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def wired_seats(monkeypatch, game_id: str) -> Seats:
    """`Seats` pointed at the in-process `TestClient` rather than the
    network - the trick every other seatkit test in this repo uses."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id)


def call_tool_raw(server: Any, name: str, arguments: dict[str, Any]) -> str:
    result = asyncio.run(server.call_tool(name, arguments))
    return result.content[0].text


# ---------------------------------------------------------------------------
# Seat.declare_plan
# ---------------------------------------------------------------------------


def test_seat_declare_plan_writes_the_ledger_row(client, monkeypatch) -> None:
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        bob = seats.seat_for(info["seat_uids"]["bob"])
        assert bob is not None
        outcome = bob.declare_plan(**PLAN)
        assert outcome["declared"] is True
        assert outcome["player"] == "bob"

        log = client.get(f"/api/games/{game_id}/log", params={"uid": info["admin_key"]}).json()[
            "log"
        ]
        plans = [e for e in log if e["action"] == "plan_declared"]
        assert len(plans) == 1
        assert plans[0]["detail"]["plan"] == PLAN["plan"]
        assert plans[0]["detail"]["prediction"]["expecting"] == PLAN["expecting"]
        assert plans[0]["detail"]["prediction"]["because"] == PLAN["because"]
        assert plans[0]["detail"]["prediction"]["confidence"] == PLAN["confidence"]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_seat_declare_plan_surfaces_a_refusal_the_normal_way(client, monkeypatch) -> None:
    """A blank field is refused 422 by the route; the seat client must turn
    that into the same `{"allowed": False, "reason": ...}` shape every other
    action's refusal already comes back as (`call_json`'s `_normalise_non_2xx`),
    not FastAPI's bare validation body."""
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        bob = seats.seat_for(info["seat_uids"]["bob"])
        assert bob is not None
        blank = {**PLAN, "plan": "   "}
        outcome = bob.declare_plan(**blank)
        assert outcome["allowed"] is False
        assert "reason" in outcome
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_seat_declare_plan_refuses_a_uid_that_is_not_that_seat(client, monkeypatch) -> None:
    """`declare_plan` always declares for `self.player`/`self.uid`, so the
    only way to exercise the route's own uid check through the client is a
    `Seat` whose `uid` has been swapped out from under it - the server-side
    guarantee that a plan is hidden information nobody else may write into
    a seat's name (`app.declare_plan`'s own docstring)."""
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        bob = seats.seat_for(info["seat_uids"]["bob"])
        assert bob is not None
        bob.uid = info["seat_uids"]["alice"]  # claims to be bob, carries alice's uid
        outcome = bob.declare_plan(**PLAN)
        assert outcome["allowed"] is False
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# the round trip that matters: refused until declared, then allowed
# ---------------------------------------------------------------------------


def test_the_gate_refuses_until_declare_plan_then_allows(client, monkeypatch) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        bob = seats.seat_for(info["seat_uids"]["bob"])
        assert bob is not None
        client.post(
            f"/api/games/{game_id}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )

        refused = bob.call_json(
            "POST",
            f"/api/games/{game_id}/actions/play-land",
            {"player": "bob", "card": "Mountain", "reasoning": "need red mana"},
        )
        assert refused["allowed"] is False
        assert "plan" in refused["reason"].lower()

        declared = bob.declare_plan(**PLAN)
        assert declared["declared"] is True

        played = bob.call_json(
            "POST",
            f"/api/games/{game_id}/actions/play-land",
            {"player": "bob", "card": "Mountain", "reasoning": "need red mana"},
        )
        assert played["allowed"] is True
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# the MCP tool: the same round trip, through `mtg`
# ---------------------------------------------------------------------------


def test_mcp_declare_plan_tool_round_trips(client, monkeypatch) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        server = build_server(seats)

        client.post(
            f"/api/games/{game_id}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )

        refused_text = call_tool_raw(
            server,
            "play_land",
            {
                "player_uid": info["seat_uids"]["bob"],
                "card": "Mountain",
                "reasoning": "need red mana",
            },
        )
        refused = json.loads(refused_text)
        assert refused["outcome"]["allowed"] is False
        assert "plan" in refused["outcome"]["reason"].lower()

        declared_text = call_tool_raw(
            server, "declare_plan", {"player_uid": info["seat_uids"]["bob"], **PLAN}
        )
        declared = json.loads(declared_text)
        assert declared["declared"] is True

        played_text = call_tool_raw(
            server,
            "play_land",
            {
                "player_uid": info["seat_uids"]["bob"],
                "card": "Mountain",
                "reasoning": "need red mana",
            },
        )
        played = json.loads(played_text)
        assert played["outcome"]["allowed"] is True
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_seat_declare_plan_passes_for_turn_next_through(client, monkeypatch) -> None:
    """`for_turn` is the plan-ahead field (`schemas.DeclarePlanIn.for_turn`) -
    `Seat.declare_plan` must carry it through to the body rather than
    silently defaulting every call to `"current"`."""
    info = _agent_game(client, active="alice")
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        bob = seats.seat_for(info["seat_uids"]["bob"])
        assert bob is not None
        outcome = bob.declare_plan(for_turn="next", **PLAN)
        assert outcome["declared"] is True
        assert outcome["for_turn"] == "next"

        log = client.get(f"/api/games/{game_id}/log", params={"uid": info["admin_key"]}).json()[
            "log"
        ]
        [plan] = [e for e in log if e["action"] == "plan_declared"]
        assert plan["detail"]["for_turn"] == "next"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_mcp_declare_plan_tool_passes_for_turn_next_through(client, monkeypatch) -> None:
    info = _agent_game(client, active="alice")
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        server = build_server(seats)

        declared_text = call_tool_raw(
            server,
            "declare_plan",
            {"player_uid": info["seat_uids"]["bob"], "for_turn": "next", **PLAN},
        )
        declared = json.loads(declared_text)
        assert declared["declared"] is True
        assert declared["for_turn"] == "next"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_mcp_declare_plan_refuses_an_unknown_uid(client, monkeypatch) -> None:
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        server = build_server(seats)
        text = call_tool_raw(server, "declare_plan", {"player_uid": "not-a-uid", **PLAN})
        assert text == NO_SUCH_SEAT
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# F3: the cheaper shapes, through both transports
# ---------------------------------------------------------------------------


def test_seat_declare_plan_omits_board_read_when_not_given(client, monkeypatch) -> None:
    """`Seat.declare_plan` must not send a body key for a field the caller
    left at its default `None` - the server tells an omitted `board_read`
    apart from an empty one."""
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        bob = seats.seat_for(info["seat_uids"]["bob"])
        assert bob is not None
        no_board = {k: v for k, v in PLAN.items() if k != "board_read"}
        outcome = bob.declare_plan(**no_board)
        assert outcome["declared"] is True, outcome

        log = client.get(f"/api/games/{game_id}/log", params={"uid": info["admin_key"]}).json()[
            "log"
        ]
        [plan] = [e for e in log if e["action"] == "plan_declared"]
        assert not plan["detail"].get("board_read")
        assert isinstance(plan["detail"].get("board_read_auto"), dict)
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_seat_declare_plan_reuse_previous_round_trips(client, monkeypatch) -> None:
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        bob = seats.seat_for(info["seat_uids"]["bob"])
        assert bob is not None
        first = bob.declare_plan(**PLAN)
        assert first["declared"] is True

        second = bob.declare_plan(reuse_previous=True, still_because="board is unchanged")
        assert second["declared"] is True, second

        log = client.get(f"/api/games/{game_id}/log", params={"uid": info["admin_key"]}).json()[
            "log"
        ]
        plans = [e for e in log if e["action"] == "plan_declared"]
        assert plans[-1]["detail"]["plan"] == PLAN["plan"]
        assert plans[-1]["detail"]["reused_from_seq"] == plans[0]["seq"]
        assert plans[-1]["detail"]["still_because"] == "board is unchanged"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_mcp_declare_plan_tool_accepts_the_short_shape(client, monkeypatch) -> None:
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        seats = wired_seats(monkeypatch, game_id)
        server = build_server(seats)

        first_text = call_tool_raw(
            server, "declare_plan", {"player_uid": info["seat_uids"]["bob"], **PLAN}
        )
        assert json.loads(first_text)["declared"] is True

        second_text = call_tool_raw(
            server,
            "declare_plan",
            {
                "player_uid": info["seat_uids"]["bob"],
                "reuse_previous": True,
                "still_because": "still on the same read",
            },
        )
        second = json.loads(second_text)
        assert second["declared"] is True, second

        log = client.get(f"/api/games/{game_id}/log", params={"uid": info["admin_key"]}).json()[
            "log"
        ]
        plans = [e for e in log if e["action"] == "plan_declared"]
        assert plans[-1]["detail"]["reused_from_seq"] == plans[0]["seq"]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_seat_cli_declare_plan_accepts_reuse_previous(client, monkeypatch, capsys) -> None:
    from magic_cards_model_api import seat_cli

    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        wired_seats(monkeypatch, game_id)
        uid = info["seat_uids"]["bob"]
        code = seat_cli.run(
            [
                "--uid",
                uid,
                "--game",
                game_id,
                "plan",
                "--board-read",
                PLAN["board_read"],
                "--my-role",
                PLAN["my_role"],
                "--plan",
                PLAN["plan"],
                "--expecting",
                PLAN["expecting"],
                "--because",
                PLAN["because"],
            ]
        )
        out = capsys.readouterr().out
        assert code in (0, None), out
        assert json.loads(out)["declared"] is True, out

        code = seat_cli.run(
            [
                "--uid",
                uid,
                "--game",
                game_id,
                "plan",
                "--reuse-previous",
                "--still-because",
                "same read as last turn",
            ]
        )
        out = capsys.readouterr().out
        assert code in (0, None), out
        result = json.loads(out)
        assert result["declared"] is True, out
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
