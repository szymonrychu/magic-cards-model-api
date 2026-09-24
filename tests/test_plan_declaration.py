"""The plan-declaration seam, and the gate that makes it get called.

The deliverable this exists for is a post-game review that convinces a
sceptic, which means it has to be *evidence*: a prediction is worth nothing
unless it demonstrably preceded the outcome. `POST .../plan` is where an
agent says, before it acts, what it reads on the board, what its own role in
the matchup is, what it intends to do, and what it expects the opponent to
do and why. `records.require_plan` is what stops that from being a facility
nobody uses: an agent seat's first mutating action of its own turn is
refused until the declaration for that turn exists in the ledger.

The gate sits on the hot action path, so how it reads the ledger matters as
much as what it decides - see `test_the_gate_reads_a_bounded_window...`,
which is the one test here about cost rather than about rules.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api import audit, records
from magic_cards_model_api.repository import RECENT_RECORDS_WINDOW

PLAN: dict[str, Any] = {
    "board_read": "empty board both sides, I am on the play with a Mountain in hand",
    "my_role": "beatdown - my deck wins before turn 6 or not at all",
    "plan": "land, hold the Bolt for a blocker rather than the face",
    "prediction": {
        "expecting": "alice plays a tapped land and passes with no play",
        "because": "she kept seven and has cast nothing in two turns",
        "confidence": "medium",
    },
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


def _uid(info: dict[str, Any], player: str) -> str:
    return info["seat_uids"][player]


def _declare(client: TestClient, info: dict[str, Any], player: str = "bob", **overrides: Any):
    body = {"player": player, "uid": _uid(info, player), **PLAN, **overrides}
    return client.post(f"/api/games/{info['game_id']}/plan", json=body)


def _play_land(client: TestClient, info: dict[str, Any], player: str = "bob"):
    client.post(
        f"/api/games/{info['game_id']}/cards",
        json={"card": "Mountain", "player": player, "zone": "hand"},
    )
    return client.post(
        f"/api/games/{info['game_id']}/actions/play-land",
        json={"player": player, "card": "Mountain", "reasoning": "I need red mana"},
    )


def _log(client: TestClient, info: dict[str, Any], uid: str | None = None) -> list[dict[str, Any]]:
    params = {"uid": uid or info["admin_key"]}
    response = client.get(f"/api/games/{info['game_id']}/log", params=params)
    assert response.status_code == 200, response.text
    return response.json()["log"]


# ---------------------------------------------------------------------------
# the declaration itself
# ---------------------------------------------------------------------------


def test_declaring_a_plan_writes_one_record(client: TestClient) -> None:
    info = _agent_game(client)
    try:
        response = _declare(client, info)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["declared"] is True

        plans = [e for e in _log(client, info) if e["action"] == audit.PLAN_ACTION]
        assert len(plans) == 1
        detail = plans[0]["detail"]
        assert detail["player"] == "bob"
        assert detail["plan"] == PLAN["plan"]
        assert detail["prediction"]["expecting"] == PLAN["prediction"]["expecting"]
        assert detail["prediction"]["because"] == PLAN["prediction"]["because"]
        assert detail["prediction"]["confidence"] == "medium"
        assert plans[0]["seq"] == body["log_seq"]
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_declaring_a_plan_mutates_no_game(client: TestClient) -> None:
    """It calls `audit.record` and nothing else - no `store.replace`, no CAS,
    no version bump. A declaration is a statement about the game, not a move
    in it, and a route that quietly committed one would make every plan an
    action the opponent's client has to notice."""
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        before = client.get(f"/api/games/{game_id}", params={"uid": info["admin_key"]}).json()
        assert _declare(client, info).status_code == 200
        after = client.get(f"/api/games/{game_id}", params={"uid": info["admin_key"]}).json()

        assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_the_plan_route_refuses_a_uid_that_is_not_that_seat(client: TestClient) -> None:
    """A plan is hidden information about how a seat intends to play; nobody
    but that seat may write one into its name."""
    info = _agent_game(client)
    try:
        wrong = _declare(client, info, player="bob", uid=_uid(info, "alice"))
        assert wrong.status_code == 404, wrong.text

        nonsense = _declare(client, info, player="bob", uid="not-a-uid")
        assert nonsense.status_code == 404, nonsense.text
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_an_empty_declaration_is_refused(client: TestClient) -> None:
    """Small enough that an agent will fill it in honestly is the design;
    blank is not filling it in."""
    info = _agent_game(client)
    try:
        blank = _declare(client, info, plan="   ")
        assert blank.status_code == 422, blank.text
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_an_agent_seats_first_action_of_its_own_turn_needs_a_plan(client: TestClient) -> None:
    info = _agent_game(client, active="bob")
    try:
        refused = _play_land(client, info)
        assert refused.status_code == 422, refused.text
        detail = refused.json()["detail"]
        assert detail[0]["loc"] == ["body", "plan"]
        assert "bob" in detail[0]["msg"]

        assert [e for e in _log(client, info) if e["action"] == "play_land"] == [], (
            "the action must be refused before the engine ever sees it"
        )
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_the_same_action_lands_once_the_plan_is_declared(client: TestClient) -> None:
    info = _agent_game(client, active="bob")
    try:
        assert _declare(client, info).status_code == 200
        played = _play_land(client, info)
        assert played.status_code == 200, played.text
        assert played.json()["allowed"] is True
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_the_plan_covers_the_whole_turn_not_only_the_first_action(client: TestClient) -> None:
    """One declaration per turn, not one per action - the gate refuses the
    first move of a turn nobody has explained, and then gets out of the way."""
    info = _agent_game(client, active="bob")
    try:
        assert _declare(client, info).status_code == 200
        assert _play_land(client, info).status_code == 200
        second = client.post(
            f"/api/games/{info['game_id']}/actions/pass-priority", json={"player": "bob"}
        )
        assert second.status_code == 200, second.text

        plans = [e for e in _log(client, info) if e["action"] == audit.PLAN_ACTION]
        assert len(plans) == 1
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_a_human_seat_never_owes_a_plan(client: TestClient) -> None:
    info = _agent_game(client, active="alice")
    try:
        played = _play_land(client, info, player="alice")
        assert played.status_code == 200, played.text
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_an_agent_seat_owes_no_plan_on_the_opponents_turn(client: TestClient) -> None:
    """The gate is about the turn a seat is *taking*. Responding on somebody
    else's turn - a flash spell, a pass - is not a turn to have a plan for,
    and demanding one would refuse an instant-speed answer outright."""
    info = _agent_game(client, active="alice")
    try:
        passed = client.post(
            f"/api/games/{info['game_id']}/actions/pass-priority", json={"player": "bob"}
        )
        assert passed.status_code == 200, passed.text
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_a_conceding_seat_never_owes_a_plan(client: TestClient) -> None:
    """CR 104.3a: a player may concede the game at any time. A gate that can
    refuse a concession is a gate that can trap a seat in a game."""
    info = _agent_game(client, active="bob")
    try:
        conceded = client.post(
            f"/api/games/{info['game_id']}/actions/concede",
            json={"player": "bob", "reasoning": "this is unwinnable"},
        )
        assert conceded.status_code == 200, conceded.text
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# the cost of the gate
# ---------------------------------------------------------------------------


class _CountingStore:
    """Just enough `GameRepository` for `records.require_plan`, counting how
    much of the ledger it actually touches. `records()` - the full load -
    raises outright: a gate that ever calls it gets slower with every turn
    of the game, which is the failure this whole shape exists to avoid."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.rows_read = 0
        self.limits: list[int] = []

    def ai_players(self, game_id: str) -> set[str]:
        return {"bob"}

    def records(self, game_id: str) -> list[dict[str, Any]]:  # pragma: no cover - must not run
        raise AssertionError("require_plan must never load the whole ledger")

    def recent_records(self, game_id: str, limit: int) -> list[dict[str, Any]]:
        self.limits.append(limit)
        window = self._rows[-limit:] if limit > 0 else []
        self.rows_read += len(window)
        return list(window)


def _ledger(size: int) -> list[dict[str, Any]]:
    return [
        {"seq": n + 1, "payload": {"action": "pass_priority", "detail": {"player": "alice"}}}
        for n in range(size)
    ]


@pytest.mark.parametrize("size", [10, 500, 5_000, 50_000])
def test_the_gate_reads_a_bounded_window_however_long_the_ledger(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, size: int
) -> None:
    """The one test here about cost. `rows_read` must stay at the window,
    not track the ledger - a 50,000-row game must cost the gate exactly what
    a 500-row game costs it."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        game = _live_game(client, info)
        counting = _CountingStore(_ledger(size))
        monkeypatch.setattr("magic_cards_model_api.store.store", counting, raising=False)

        with pytest.raises(records.MissingPlan):
            records.require_plan(game_id, "bob", game=game)

        assert counting.rows_read == min(size, RECENT_RECORDS_WINDOW)
        assert counting.limits == [RECENT_RECORDS_WINDOW]
    finally:
        # Put the real store back before the cleanup call: `DELETE
        # /api/games/{id}` goes through the singleton too, and the counting
        # stand-in implements only what the gate itself needs.
        monkeypatch.undo()
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def _live_game(client: TestClient, info: dict[str, Any]):
    """The real engine `Game` behind this fixture's id, read before the
    counting store is swapped in."""
    from magic_cards_model_api.store import store

    game = store.get(info["game_id"])
    assert game is not None
    return game


# ---------------------------------------------------------------------------
# F3: a cheaper declaration - optional board_read, carried my_role, reuse
# ---------------------------------------------------------------------------

#: A sentinel so a test can ask for a field to be left off the body entirely,
#: which `dict.update`/`**overrides` cannot express on its own (an override
#: can only replace a key `PLAN` already carries, never remove one).
_OMIT = object()


def _declare_omitting(
    client: TestClient, info: dict[str, Any], *omit: str, player: str = "bob", **overrides: Any
):
    body = {"player": player, "uid": _uid(info, player), **PLAN, **overrides}
    for key in omit:
        body.pop(key, None)
    return client.post(f"/api/games/{info['game_id']}/plan", json=body)


def _declared_detail(client: TestClient, info: dict[str, Any]) -> dict[str, Any]:
    plans = [e for e in _log(client, info) if e["action"] == audit.PLAN_ACTION]
    return plans[-1]["detail"]


def test_board_read_omitted_stores_an_auto_compact_board(client: TestClient) -> None:
    """(1) `board_read` becomes optional. Left off, the row carries
    `board_read_auto`: `decision_dsl.compact_board` of this seat's own
    redacted view at declaration time - objective, and cheaper than prose."""
    info = _agent_game(client)
    try:
        response = _declare_omitting(client, info, "board_read")
        assert response.status_code == 200, response.text

        detail = _declared_detail(client, info)
        assert not detail.get("board_read")
        auto = detail.get("board_read_auto")
        assert isinstance(auto, dict), detail
        assert auto["players"]["bob"]["life"] == 20
        assert auto["players"]["alice"]["life"] == 20
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_my_role_is_required_on_a_first_declaration(client: TestClient) -> None:
    """(2) `my_role` is optional only *after* this seat's first declaration
    this game. Omitted on the very first one, it is refused 422 naming
    `my_role`."""
    info = _agent_game(client)
    try:
        response = _declare_omitting(client, info, "my_role")
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail[0]["loc"] == ["body", "my_role"]
        assert "bob" in detail[0]["msg"]

        assert [e for e in _log(client, info) if e["action"] == audit.PLAN_ACTION] == []
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_my_role_omitted_after_a_first_declaration_is_carried(client: TestClient) -> None:
    info = _agent_game(client, active="bob")
    try:
        first = _declare_omitting(client, info)
        assert first.status_code == 200, first.text

        second = _declare_omitting(client, info, "my_role")
        assert second.status_code == 200, second.text

        detail = _declared_detail(client, info)
        assert detail["my_role"] == PLAN["my_role"]
        assert detail["my_role_carried"] is True
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_reuse_previous_copies_plan_and_prediction_forward(client: TestClient) -> None:
    """(3) `reuse_previous: true` plus a one-line `still_because` copies
    `plan`/`prediction` from the seat's most recent declaration into a new
    row for this turn, marked `reused_from_seq`."""
    info = _agent_game(client, active="bob")
    try:
        first = _declare_omitting(client, info)
        assert first.status_code == 200, first.text
        first_seq = first.json()["log_seq"]

        second = client.post(
            f"/api/games/{info['game_id']}/plan",
            json={
                "player": "bob",
                "uid": _uid(info, "bob"),
                "reuse_previous": True,
                "still_because": "board has not changed since last turn",
            },
        )
        assert second.status_code == 200, second.text

        detail = _declared_detail(client, info)
        assert detail["plan"] == PLAN["plan"]
        assert detail["prediction"]["expecting"] == PLAN["prediction"]["expecting"]
        assert detail["reused_from_seq"] == first_seq
        assert detail["still_because"] == "board has not changed since last turn"
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_reuse_previous_with_no_earlier_declaration_is_refused(client: TestClient) -> None:
    info = _agent_game(client)
    try:
        response = client.post(
            f"/api/games/{info['game_id']}/plan",
            json={
                "player": "bob",
                "uid": _uid(info, "bob"),
                "reuse_previous": True,
                "still_because": "nothing has changed",
            },
        )
        assert response.status_code == 422, response.text
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_reuse_previous_without_still_because_is_refused(client: TestClient) -> None:
    info = _agent_game(client)
    try:
        assert _declare_omitting(client, info).status_code == 200

        response = client.post(
            f"/api/games/{info['game_id']}/plan",
            json={"player": "bob", "uid": _uid(info, "bob"), "reuse_previous": True},
        )
        assert response.status_code == 422, response.text
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_a_fresh_declaration_still_requires_plan_and_prediction(client: TestClient) -> None:
    """(4) `plan` and `prediction` stay required on a fresh (non-reuse)
    declaration."""
    info = _agent_game(client)
    try:
        no_plan = _declare_omitting(client, info, "plan")
        assert no_plan.status_code == 422, no_plan.text

        no_prediction = _declare_omitting(client, info, "prediction")
        assert no_prediction.status_code == 422, no_prediction.text
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_an_existing_full_five_field_declaration_is_unchanged(client: TestClient) -> None:
    """Backward compatibility: a full declaration with every field named
    behaves exactly as it always has - no auto board, no carried role, no
    reuse marker."""
    info = _agent_game(client)
    try:
        response = _declare(client, info)
        assert response.status_code == 200, response.text

        detail = _declared_detail(client, info)
        assert detail["board_read"] == PLAN["board_read"]
        assert not detail.get("board_read_auto")
        assert detail["my_role"] == PLAN["my_role"]
        assert not detail.get("my_role_carried")
        assert detail["plan"] == PLAN["plan"]
        assert not detail.get("reused_from_seq")
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})
