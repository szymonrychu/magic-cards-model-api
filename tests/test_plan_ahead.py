"""Planning ahead of your own turn, and the upkeep/draw pass exemption.

Live game 2026-09-14: an agent seat's pass in its own upkeep was refused by
`records.require_plan` - the gate as it stood only ever accepted a plan
declared for the turn already under way, so an agent that wanted to think
ahead had no way to. It planned holding priority in its own upkeep instead,
and the human on the other side of the table waited on it for real.

Two independent fixes, both here:

- `POST .../plan` takes `for_turn: "current" | "next"`. `"next"`, called
  during the opponent's turn, writes the plan row against this seat's own
  *next* turn - computed by asking the engine's own `TurnMachine.
  begin_next_turn` (CR 500.1, 800.4, and CR 500.7's extra turns) rather than
  re-deriving turn order here. `records.require_plan`'s own matching logic
  (`turns_taken` equality) is untouched - a plan-ahead row satisfies it the
  same way a same-turn one always has, once that turn arrives.
- `pass_priority` is exempt from `require_plan` outright when the step is
  upkeep or draw - the two steps almost nothing happens in, and the one an
  agent with nothing to say should be able to pass through without writing
  anything first. Every other step, and every other action shape, stays
  gated exactly as before.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import add_card, force_turn_position
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step

from magic_cards_model_api import audit, records, seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.store import store

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
    add_card(client, info["game_id"], "Mountain", player, "hand")
    return client.post(
        f"/api/games/{info['game_id']}/actions/play-land",
        json={"player": player, "card": "Mountain", "reasoning": "I need red mana"},
    )


def _pass(client: TestClient, info: dict[str, Any], player: str):
    return client.post(
        f"/api/games/{info['game_id']}/actions/pass-priority", json={"player": player}
    )


def _log(client: TestClient, info: dict[str, Any], uid: str | None = None) -> list[dict[str, Any]]:
    params = {"uid": uid or info["admin_key"]}
    response = client.get(f"/api/games/{info['game_id']}/log", params=params)
    assert response.status_code == 200, response.text
    return response.json()["log"]


def _plan_rows(client: TestClient, info: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in _log(client, info) if e["action"] == audit.PLAN_ACTION]


def _state(client: TestClient, info: dict[str, Any]) -> dict[str, Any]:
    response = client.get(f"/api/games/{info['game_id']}", params={"uid": info["admin_key"]})
    assert response.status_code == 200, response.text
    return response.json()


def _stock_libraries(client: TestClient, info: dict[str, Any], count: int = 4) -> None:
    """Enough library cards that driving a real turn or two doesn't decree a
    loss by decking out (CR 104.3c) before the test gets to what it is
    actually about."""
    for player in ("alice", "bob"):
        for _ in range(count):
            add_card(client, info["game_id"], "Mountain", player, "library")


def _settle_pending_decision(
    client: TestClient, info: dict[str, Any], pending: dict[str, Any]
) -> None:
    """Answer an empty-board `declare_attackers`/`declare_blockers` pause
    with "nobody" (CR 508.1a/509.1a's own "if any") so a bare pass-priority
    loop can drive a whole turn on an otherwise empty board."""
    game_id = info["game_id"]
    decider = pending["player"]
    uid = _uid(info, decider)
    kind = pending["kind"]
    if kind == "declare_attackers":
        response = client.post(
            f"/api/games/{game_id}/actions/attack", json={"attackers": []}, params={"uid": uid}
        )
    elif kind == "declare_blockers":
        response = client.post(
            f"/api/games/{game_id}/actions/declare-blockers",
            json={"player": decider, "blocks": []},
            params={"uid": uid},
        )
    else:
        raise AssertionError(f"unexpected pending decision on an empty board: {pending!r}")
    assert response.status_code == 200, response.text


def _priority_pass_rounds(client: TestClient, info: dict[str, Any], cap: int = 300) -> int:
    """Pass priority for whoever currently holds it, over and over - settling
    the empty-board combat decisions along the way - until the turn hands
    over to the other seat. Returns how many real `pass-priority` calls it
    took - the "realistic turn of rows" item 8 asks for."""
    game_id = info["game_id"]
    starting = store.get(game_id).turn.turns_taken
    count = 0
    for _ in range(cap):
        st = _state(client, info)
        pending = st.get("pending_decision")
        if pending is not None:
            _settle_pending_decision(client, info, pending)
            continue
        holder = st.get("priority_holder")
        assert holder is not None, f"nobody holds priority at {st.get('step')}"
        response = _pass(client, info, holder)
        assert response.status_code == 200, response.text
        count += 1
        if store.get(game_id).turn.turns_taken != starting:
            return count
    raise AssertionError("the turn never handed over")


def _advance_to_own_precombat_main(client: TestClient, info: dict[str, Any], player: str) -> None:
    """Pass priority for whoever holds it - settling empty-board combat
    decisions along the way - until `player`'s own precombat main. Used to
    prove a plan-ahead row survives real, non-exempt gated actions, not only
    the upkeep/draw pass exemption."""
    game_id = info["game_id"]
    for _ in range(300):
        game = store.get(game_id)
        if game.turn.active_player == player and game.turn.step is Step.PRECOMBAT_MAIN:
            return
        st = _state(client, info)
        pending = st.get("pending_decision")
        if pending is not None:
            _settle_pending_decision(client, info, pending)
            continue
        holder = st.get("priority_holder")
        assert holder is not None, f"nobody holds priority at {st.get('step')}"
        response = _pass(client, info, holder)
        assert response.status_code == 200, response.text
    raise AssertionError(f"never reached {player}'s precombat main")


# ---------------------------------------------------------------------------
# declaring ahead: the computed target turn
# ---------------------------------------------------------------------------


def test_for_turn_defaults_to_current(client: TestClient) -> None:
    info = _agent_game(client, active="bob")
    try:
        response = _declare(client, info)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["for_turn"] == "current"
        detail = _plan_rows(client, info)[0]["detail"]
        assert detail["for_turn"] == "current"
    finally:
        client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})


def test_for_turn_next_during_the_opponents_turn_targets_the_seats_own_next_turn(
    client: TestClient,
) -> None:
    info = _agent_game(client, active="alice")
    game_id = info["game_id"]
    try:
        game = store.get(game_id)
        assert game is not None and game.turn is not None
        declared_at = game.turn.turns_taken

        response = _declare(client, info, player="bob", for_turn="next")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["for_turn"] == "next"

        rows = _plan_rows(client, info)
        assert len(rows) == 1
        detail = rows[0]["detail"]
        assert detail["for_turn"] == "next"
        assert detail["declared_turns_taken"] == declared_at
        # Two players, nobody holds an extra turn: bob's next turn is the
        # very next one taken by anybody.
        assert detail["turns_taken"] == declared_at + 1
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_for_turn_next_skips_a_queued_extra_turn(client: TestClient) -> None:
    """CR 500.7: an extra turn is inserted directly after the one under way.
    Bob's real next turn is the one after alice's extra turn, not the one
    right after her current turn."""
    info = _agent_game(client, active="alice")
    game_id = info["game_id"]
    try:
        game = store.get(game_id)
        assert game is not None and game.turn is not None
        game.turn.grant_extra_turn("alice")
        declared_at = game.turn.turns_taken

        response = _declare(client, info, player="bob", for_turn="next")
        assert response.status_code == 200, response.text

        detail = _plan_rows(client, info)[0]["detail"]
        assert detail["turns_taken"] == declared_at + 2
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_extra_turn_added_after_declaring_breaks_the_match(client: TestClient) -> None:
    """The numbers no longer line up once an extra turn is queued after the
    plan-ahead declaration - the gate refuses exactly as it would for any
    other mismatched `turns_taken`, 422 naming `plan`."""
    info = _agent_game(client, active="alice")
    game_id = info["game_id"]
    try:
        response = _declare(client, info, player="bob", for_turn="next")
        assert response.status_code == 200, response.text

        game = store.get(game_id)
        assert game is not None and game.turn is not None
        game.turn.grant_extra_turn("alice")  # queued AFTER the declaration above
        game.turn.begin_next_turn()  # alice's extra turn
        game.turn.begin_next_turn()  # now bob's real turn, one turns_taken later than declared
        assert game.turn.active_player == "bob"

        refused = _play_land(client, info, player="bob")
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"][0]["loc"] == ["body", "plan"]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_for_turn_next_during_its_own_turn_is_refused(client: TestClient) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        response = _declare(client, info, player="bob", for_turn="next")
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "own_turn_in_progress"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_for_turn_next_during_the_pregame_is_not_refused_and_targets_the_seats_first_own_turn(
    client: TestClient,
) -> None:
    """CR 103's mulligan/opening-hand windows: no turn has begun yet, so
    `next_own_turn`'s "already under way" check (`turn.step not in
    _PREGAME_STEPS and turn.active_player == player`) must never fire there,
    whatever the pregame's own nominal active seat is - `begin_first_turn`
    stands in for it instead, and bob (seat 0, so the one whose mulligan-
    active seat will become CR 103.8's first turn) declaring `for_turn:
    "next"` while the game still rests in `mulligan` is answered 200, not
    409, with that first turn as the target."""
    response = client.post(
        "/api/games",
        json={
            "players": [
                {"username": "bob", "is_agent": True},
                {"username": "alice", "is_agent": False},
            ],
            "active_player": "bob",
            "step": "mulligan",
        },
    )
    assert response.status_code == 200, response.text
    info = response.json()
    game_id = info["game_id"]
    try:
        game = store.get(game_id)
        assert game is not None and game.turn is not None
        assert game.turn.step is Step.MULLIGAN

        declared = _declare(client, info, player="bob", for_turn="next")
        assert declared.status_code == 200, declared.text
        body = declared.json()
        assert body["for_turn"] == "next"
        assert body["turn_number"] == 1

        detail = _plan_rows(client, info)[0]["detail"]
        assert detail["for_turn"] == "next"
        assert detail["turns_taken"] == 1
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# a plan-ahead row actually clears the gate, on a real turn
# ---------------------------------------------------------------------------


def test_a_plan_ahead_row_clears_the_gate_for_the_seats_own_turn(client: TestClient) -> None:
    """The whole point: declared during the opponent's turn, the row still
    matches once that turn actually becomes the seat's own - main-phase
    play, not only the pass exemption - and it survives a realistic number
    of ledger rows in between (item 8: the bounded window must not exclude
    it)."""
    info = _agent_game(client, active="alice")
    game_id = info["game_id"]
    try:
        _stock_libraries(client, info)
        declared = _declare(client, info, player="bob", for_turn="next")
        assert declared.status_code == 200, declared.text

        rounds = _priority_pass_rounds(client, info)
        assert rounds < 100, f"unexpectedly long turn ({rounds} passes) - window assumption at risk"

        game = store.get(game_id)
        assert game.turn.active_player == "bob"

        _advance_to_own_precombat_main(client, info, "bob")
        played = _play_land(client, info, player="bob")
        assert played.status_code == 200, played.text
        assert played.json()["allowed"] is True

        # Still exactly the one plan row - no fresh declaration was needed.
        assert len(_plan_rows(client, info)) == 1
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_plan_ahead_row_also_clears_the_decide_gate(client: TestClient) -> None:
    """`decide`'s own call site (`records.require_plan(game_id, engine_
    decision.player, game=game)`) is exercised directly here - the same
    function `action_pass_priority` and every casting route call, just
    without needing to stand up a real pending decision to reach it."""
    info = _agent_game(client, active="alice")
    game_id = info["game_id"]
    try:
        _stock_libraries(client, info)
        assert _declare(client, info, player="bob", for_turn="next").status_code == 200
        _priority_pass_rounds(client, info)
        game = store.get(game_id)
        assert game.turn.active_player == "bob"

        records.require_plan(game_id, "bob", game=game)  # must not raise
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# the upkeep/draw pass exemption
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", [Step.UPKEEP, Step.DRAW])
def test_pass_priority_in_the_beginning_phase_is_exempt(client: TestClient, step: Step) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        force_turn_position(game_id, step, "bob")
        passed = _pass(client, info, "bob")
        assert passed.status_code == 200, passed.text
        assert passed.json()["allowed"] is True
        assert _plan_rows(client, info) == []
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


@pytest.mark.parametrize("step", [Step.UPKEEP, Step.DRAW])
def test_pass_priority_in_the_beginning_phase_with_a_stack_object_still_needs_a_plan(
    client: TestClient, step: Step
) -> None:
    """The exemption is for "I have nothing to do" - a trigger or spell
    sitting on the stack in upkeep or draw (a landfall trigger, an upkeep
    cost) is a real decision the same way a main-phase pass is, and stays
    gated even in the two otherwise-exempt steps."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        force_turn_position(game_id, step, "bob")
        add_card(client, game_id, "Lightning Bolt", "bob", "stack")
        refused = _pass(client, info, "bob")
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"][0]["loc"] == ["body", "plan"]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


@pytest.mark.parametrize(
    "step",
    [
        Step.PRECOMBAT_MAIN,
        Step.DECLARE_ATTACKERS,
        Step.POSTCOMBAT_MAIN,
        Step.END_STEP,
    ],
)
def test_pass_priority_outside_the_beginning_phase_still_needs_a_plan(
    client: TestClient, step: Step
) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        force_turn_position(game_id, step, "bob")
        refused = _pass(client, info, "bob")
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"][0]["loc"] == ["body", "plan"]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_cast_in_upkeep_still_needs_a_plan(client: TestClient) -> None:
    """The exemption is `pass_priority`-shaped only - casting an instant at
    the same step a bare pass would be waved through stays gated."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        force_turn_position(game_id, Step.UPKEEP, "bob")
        add_card(client, game_id, "Lightning Bolt", "bob", "hand")
        refused = client.post(
            f"/api/games/{game_id}/actions/cast",
            json={"player": "bob", "card": "Lightning Bolt", "reasoning": "burn a blocker"},
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"][0]["loc"] == ["body", "plan"]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_decide_in_upkeep_still_needs_a_plan(client: TestClient) -> None:
    """White-box, at the gate `decide`'s own call site exercises
    (`pass_shaped` defaults to `False`) - the exemption never reaches it."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        force_turn_position(game_id, Step.UPKEEP, "bob")
        game = store.get(game_id)
        with pytest.raises(records.MissingPlan):
            records.require_plan(game_id, "bob", game=game)
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_human_seats_upkeep_pass_needed_no_exemption_anyway(client: TestClient) -> None:
    info = _agent_game(client, active="alice")
    game_id = info["game_id"]
    try:
        force_turn_position(game_id, Step.UPKEEP, "alice")
        passed = _pass(client, info, "alice")
        assert passed.status_code == 200, passed.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# `seat_cli plan`
# ---------------------------------------------------------------------------


def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every `httpx.Client` `seat_cli` builds at the in-process app -
    the same trick `test_front_end_parity.wire` and `test_seat_cli_script.py`
    already use."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))


def _cli_plan(game_id: str, uid: str, *extra: str, for_turn: str | None = None) -> list[str]:
    argv = [
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
        PLAN["prediction"]["expecting"],
        "--because",
        PLAN["prediction"]["because"],
        *extra,
    ]
    if for_turn is not None:
        argv += ["--for-turn", for_turn]
    return argv


def test_seat_cli_plan_declares_for_the_current_turn_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        _wire(monkeypatch)
        rc = seat_cli.run(_cli_plan(game_id, _uid(info, "bob")))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["declared"] is True
        assert out["for_turn"] == "current"
        assert len(_plan_rows(client, info)) == 1
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_seat_cli_plan_for_turn_next(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    info = _agent_game(client, active="alice")
    game_id = info["game_id"]
    try:
        _wire(monkeypatch)
        rc = seat_cli.run(_cli_plan(game_id, _uid(info, "bob"), for_turn="next"))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["declared"] is True
        assert out["for_turn"] == "next"
        assert _plan_rows(client, info)[0]["detail"]["for_turn"] == "next"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_seat_cli_plan_for_turn_next_during_its_own_turn_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        _wire(monkeypatch)
        rc = seat_cli.run(_cli_plan(game_id, _uid(info, "bob"), for_turn="next"))
        assert rc == 0  # the CLI itself does not fail the process - it prints the refusal
        out = json.loads(capsys.readouterr().out)
        assert out["allowed"] is False
        assert out.get("code") == "own_turn_in_progress"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
