"""`GET /api/games/{id}/review` - the post-game read, and the hidden
information it must not become a way around.

Two things are being proved here at once, and they pull in opposite
directions. After a game, showing both seats' plans and reasoning side by
side is the entire point: a prediction is only evidence if a reader can see
that it preceded the outcome, and that means seeing the outcome too.
*During* a game, the same data would hand one seat the other's plan. So the
route refuses outright until the game is over or has been ended, and refuses
anyone who is not the admin key or a seat of that game - the anonymous case
included, which is the one that matters, because `GET /api/games/{id}` and
`GET /api/ui/games` publish every lobby game's id with no credential at all.

The review reads the ledger without `game_or_404`. That call is what
stranded 144 rows of the only substantively played game on the live
deployment: the game had been ended, so the log route 404'd on the game
before it ever read the records that outlived it.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from magic_cards_model_api import audit

PLAN: dict[str, Any] = {
    "board_read": "she is at 4 with no blockers and one card in hand",
    "my_role": "beatdown, and this is my last window",
    "plan": "attack with everything and hold the burn spell for the face",
    "prediction": {
        "expecting": "alice takes the damage rather than chump-blocking",
        "because": "her only creature is the one that unlocks her combo",
        "confidence": "high",
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


def _declare(client: TestClient, info: dict[str, Any], player: str = "bob", **overrides: Any):
    body = {"player": player, "uid": info["seat_uids"][player], **PLAN, **overrides}
    return client.post(f"/api/games/{info['game_id']}/plan", json=body)


def _review(client: TestClient, game_id: str, **params: Any):
    return client.get(f"/api/games/{game_id}/review", params=params)


# ---------------------------------------------------------------------------
# mid-game: nothing gets out
# ---------------------------------------------------------------------------


def test_a_live_games_review_is_refused_to_everyone(client: TestClient) -> None:
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200

        anonymous = _review(client, game_id)
        assert anonymous.status_code in (404, 422), anonymous.text
        assert "board_read" not in anonymous.text

        opponent = _review(client, game_id, uid=info["seat_uids"]["alice"])
        assert opponent.status_code == 409, opponent.text
        assert "board_read" not in opponent.text

        own = _review(client, game_id, uid=info["seat_uids"]["bob"])
        assert own.status_code == 409, own.text

        admin = _review(client, game_id, key=info["admin_key"])
        assert admin.status_code == 409, admin.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_live_games_plan_never_reaches_the_opposing_seat(client: TestClient) -> None:
    """The declaration lands in the same `records` table `GET .../log`
    reads, so the log had to learn about it: a `plan_declared` row is
    readable by the seat that wrote it and by the admin key, and by nobody
    else, live or otherwise."""
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200

        opponent = client.get(
            f"/api/games/{game_id}/log", params={"uid": info["seat_uids"]["alice"]}
        )
        assert opponent.status_code == 200, opponent.text
        assert PLAN["plan"] not in opponent.text
        assert PLAN["prediction"]["expecting"] not in opponent.text
        plans = [e for e in opponent.json()["log"] if e["action"] == audit.PLAN_ACTION]
        assert plans, "the row is still there - it is its content that is withheld"
        assert plans[0]["detail"] == {"player": "bob"}

        anonymous = client.get(f"/api/games/{game_id}/log")
        assert PLAN["plan"] not in anonymous.text

        own = client.get(f"/api/games/{game_id}/log", params={"uid": info["seat_uids"]["bob"]})
        assert PLAN["plan"] in own.text, "the declaring seat still reads its own plan"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_live_games_reasoning_never_reaches_the_opposing_seat(client: TestClient) -> None:
    """Per-action `reasoning` is the seat's own account of why it is doing
    something, and it is written into the same log. An opponent reading it
    live is reading the agent's mind mid-game."""
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        client.post(
            f"/api/games/{game_id}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )
        played = client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": "bob", "card": "Mountain", "reasoning": "I am holding up the Bolt"},
        )
        assert played.status_code == 200, played.text

        opponent = client.get(
            f"/api/games/{game_id}/log", params={"uid": info["seat_uids"]["alice"]}
        )
        assert "holding up the Bolt" not in opponent.text
        lands = [e for e in opponent.json()["log"] if e["action"] == "play_land"]
        assert lands and lands[-1]["reasoning"] is None

        own = client.get(f"/api/games/{game_id}/log", params={"uid": info["seat_uids"]["bob"]})
        assert "holding up the Bolt" in own.text

        admin = client.get(f"/api/games/{game_id}/log", params={"uid": info["admin_key"]})
        assert "holding up the Bolt" in admin.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# after the game: the pairing
# ---------------------------------------------------------------------------


def _played_out(client: TestClient) -> dict[str, Any]:
    """A finished game with one declaration, one action by the declaring
    seat, and one by the opponent afterwards."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    assert _declare(client, info).status_code == 200
    client.post(
        f"/api/games/{game_id}/cards", json={"card": "Mountain", "player": "bob", "zone": "hand"}
    )
    client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "bob", "card": "Mountain", "reasoning": "land first, then the attack"},
    )
    conceded = client.post(
        f"/api/games/{game_id}/actions/concede",
        json={"player": "alice", "reasoning": "the race is lost"},
    )
    assert conceded.status_code == 200, conceded.text
    return info


def test_a_finished_games_review_pairs_the_prediction_with_the_outcome(
    client: TestClient,
) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        response = _review(client, game_id, key=info["admin_key"])
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["game_id"] == game_id
        assert body["is_over"] is True
        assert body["winner"] == "bob"
        assert body["drawn"] is False
        assert sorted(body["players"]) == ["alice", "bob"]

        assert len(body["declarations"]) == 1
        declaration = body["declarations"][0]
        assert declaration["player"] == "bob"
        assert declaration["plan"] == PLAN["plan"]
        assert declaration["prediction"]["expecting"] == PLAN["prediction"]["expecting"]
        assert declaration["prediction"]["because"] == PLAN["prediction"]["because"]
        assert declaration["prediction"]["confidence"] == "high"

        did = [entry["action"] for entry in declaration["did"]]
        assert "play_land" in did
        assert declaration["did"][-1]["reasoning"] == "land first, then the attack"

        opponent_did = [entry["action"] for entry in declaration["opponent_did"]]
        assert "concede" in opponent_did, opponent_did
        assert declaration["opponent_did"][-1]["player"] == "alice"

        # No grade, no score, no verdict: the raw pairing is what a sceptic
        # cannot argue with, and a number is what they would argue with.
        assert "score" not in declaration
        assert "correct" not in declaration
        assert "accuracy" not in body
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_the_review_carries_the_whole_ledger_with_its_snapshots(client: TestClient) -> None:
    """Every `records` row already carries a full-information snapshot
    (`audit.record` writes `serialize.snapshot` on all of them), so the
    review needs no replay of its own - it hands the frames over."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()

        actions = [entry["action"] for entry in body["entries"]]
        assert actions[0] == "create_game"
        assert audit.PLAN_ACTION in actions
        assert "game_over" in actions
        assert all(entry["snapshot"] is not None for entry in body["entries"])
        # The paired copies stay light: the frames live in `entries` once.
        assert "snapshot" not in body["declarations"][0]["did"][0]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_seat_may_read_the_finished_games_review(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        for player in ("bob", "alice"):
            response = _review(client, game_id, uid=info["seat_uids"][player])
            assert response.status_code == 200, response.text
            assert response.json()["declarations"][0]["plan"] == PLAN["plan"], (
                "after the game, both sides are shown - that is the whole point"
            )
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_finished_games_review_is_still_refused_to_a_stranger(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        assert _review(client, game_id).status_code in (404, 422)
        assert _review(client, game_id, uid="not-a-uid").status_code == 404
        assert _review(client, game_id, key="not-the-admin-key").status_code == 404
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# after the game is ENDED: the 144 stranded rows
# ---------------------------------------------------------------------------


def test_an_ended_games_review_still_reads(client: TestClient) -> None:
    """`DELETE /api/games/{id}` is a soft delete now, so the row, its final
    state and its credentials are all still there - and the review reads the
    ledger without `game_or_404`, which is the call that stranded the live
    deployment's only real game."""
    info = _agent_game(client)
    game_id = info["game_id"]
    assert _declare(client, info).status_code == 200

    ended = client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
    assert ended.status_code == 200, ended.text
    assert client.get(f"/api/games/{game_id}").status_code == 404, "and it is gone as a game"

    response = _review(client, game_id, key=info["admin_key"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ended_at"] is not None
    assert body["declarations"][0]["plan"] == PLAN["plan"]
    assert [entry["action"] for entry in body["entries"]][-1] == "delete_game"


def test_an_ended_lobby_games_review_still_reads(client: TestClient) -> None:
    """The lobby's End button goes through `lobby.reset_game`, which writes
    its own `game_reset` row and *then* deletes - the transcript ends with
    the reset, and now so does the board it describes."""
    boot = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {
                    "username": "bob",
                    "decklist_text": "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt",
                    "ai": True,
                },
                {
                    "username": "alice",
                    "decklist_text": "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt",
                    "ai": False,
                },
            ],
            "seed": 7,
        },
    )
    assert boot.status_code == 200, boot.text
    info = boot.json()
    game_id = info["game_id"]

    ended = client.delete(f"/api/lobby/games/{game_id}", params={"key": info["admin_key"]})
    assert ended.status_code == 200, ended.text

    response = _review(client, game_id, key=info["admin_key"])
    assert response.status_code == 200, response.text
    assert [entry["action"] for entry in response.json()["entries"]][-1] == "game_reset"


def test_a_review_of_a_game_that_never_existed_is_a_404(client: TestClient) -> None:
    assert _review(client, "no-such-game", key="anything").status_code == 404
