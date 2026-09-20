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
from playscript_testkit import FETCH_SHOCK, SLUG
from test_unloadable_games import engine_refusal
from test_unloadable_games import unloadable as unloadable  # noqa: PLC0414 - a fixture, re-exported
from ui_node import requires_node, run_module

from magic_cards_model_api import audit, review
from magic_cards_model_api.repository import Advisor, ArchivedGame
from magic_cards_model_api.store import store

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


# ---------------------------------------------------------------------------
# a plan-ahead row is attributed to its TARGET turn, not the turn it was
# written in (live game 2026-09-14: a 3-player game landed bob's plan-ahead
# declaration on carol's turn instead of his own)
# ---------------------------------------------------------------------------


def _agent_game_3p(client: TestClient, active: str = "carol") -> dict[str, Any]:
    response = client.post(
        "/api/games",
        json={
            "players": [
                {"username": "alice", "is_agent": False},
                {"username": "bob", "is_agent": True},
                {"username": "carol", "is_agent": False},
            ],
            "active_player": active,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_a_plan_ahead_declaration_is_attributed_to_its_target_turn_not_the_turn_it_was_declared_in(
    client: TestClient,
) -> None:
    """The exact repro: 3 players (alice, bob[agent], carol), carol active at
    `turn_number` 1. Bob declares `for_turn: "next"` while carol's turn is
    still under way - his own next turn is `turn_number` 2 (alice's turn,
    seated between carol's and bob's, shares it: `TurnMachine.turn_number`
    counts rounds of the table, not player-turns, so more than one player's
    turn can carry the same number). The ledger row's own top-level `turn`
    stays 1 - the turn it was honestly written in - but `_declarations` must
    report `turn: 2`, the turn the declaration is ABOUT, or the review page
    hangs bob's plan on carol's turn instead of his own."""
    info = _agent_game_3p(client, active="carol")
    game_id = info["game_id"]
    try:
        game = store.get(game_id)
        assert game is not None and game.turn is not None
        assert game.turn.active_player == "carol"
        assert game.turn.turn_number == 1

        declared = client.post(
            f"/api/games/{game_id}/plan",
            json={"player": "bob", "uid": info["seat_uids"]["bob"], "for_turn": "next", **PLAN},
        )
        assert declared.status_code == 200, declared.text
        assert declared.json()["turn_number"] == 2

        entries = audit.entries(game_id)
        plan_rows = [e for e in entries if e["action"] == audit.PLAN_ACTION]
        assert len(plan_rows) == 1
        row = plan_rows[0]
        assert row["turn"] == 1, "the row is honestly stamped with when it was written"
        assert row["detail"]["turns_taken"] == 3

        declarations = review._declarations(entries)
        assert len(declarations) == 1
        assert declarations[0]["turn"] == 2, "attributed to bob's own target turn, not carol's"
        assert declarations[0]["turns_taken"] == 3
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_two_player_plan_ahead_declaration_still_groups_correctly(client: TestClient) -> None:
    """The divergence is not a 3+ player-only thing: `_agent_game`'s bob is
    seat 0, and `TurnMachine.turn_number` only increments when the wrap
    lands back on seat 0 (`begin_next_turn`) - so while alice (seat 1) is
    active at `turn_number` 1, bob's own next turn already crosses that wrap
    and is `turn_number` 2. The row is still written during alice's turn
    (chronological `turn` 1); `_declarations` must report the target, 2."""
    info = _agent_game(client, active="alice")
    game_id = info["game_id"]
    try:
        game = store.get(game_id)
        assert game is not None and game.turn is not None
        assert game.turn.turn_number == 1

        declared = _declare(client, info, player="bob", for_turn="next")
        assert declared.status_code == 200, declared.text
        assert declared.json()["turn_number"] == 2

        entries = audit.entries(game_id)
        plan_rows = [e for e in entries if e["action"] == audit.PLAN_ACTION]
        assert plan_rows[0]["turn"] == 1, "still honestly stamped with when it was written"

        declarations = review._declarations(entries)
        assert len(declarations) == 1
        assert declarations[0]["turn"] == 2, "attributed to bob's own target turn"
        assert declarations[0]["turns_taken"] == 2
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_current_turn_declaration_still_reports_its_own_turn(client: TestClient) -> None:
    """`for_turn: "current"` (the default) is unaffected by the fix - the
    row's target IS the turn it was written in, so `_declarations` reports
    exactly what it always has."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        game = store.get(game_id)
        assert game is not None and game.turn is not None
        assert game.turn.turn_number == 1

        assert _declare(client, info, player="bob").status_code == 200

        entries = audit.entries(game_id)
        declarations = review._declarations(entries)
        assert len(declarations) == 1
        assert declarations[0]["turn"] == 1
        assert declarations[0]["turns_taken"] == 1
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


# ---------------------------------------------------------------------------
# the reflection: written after the result, and only then
# ---------------------------------------------------------------------------


def _reflect(client: TestClient, info: dict[str, Any], player: str = "bob", **overrides: Any):
    body = {
        "player": player,
        "uid": info["seat_uids"][player],
        "text": "the Bolt on turn one was the whole game - next time it comes down first",
        **overrides,
    }
    return client.post(f"/api/games/{info['game_id']}/reflection", json=body)


def test_a_reflection_is_refused_while_the_game_is_still_live(client: TestClient) -> None:
    """Written after the result is known, and the route enforces it - the
    same post-game gate `game_review` enforces on reading."""
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        response = _reflect(client, info)
        assert response.status_code == 409, response.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_reflection_requires_the_seats_own_uid(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        wrong_seat = _reflect(client, info, player="bob", uid=info["seat_uids"]["alice"])
        assert wrong_seat.status_code == 404, wrong_seat.text
        stranger = _reflect(client, info, player="bob", uid="not-a-uid")
        assert stranger.status_code == 404, stranger.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_finished_games_reflection_is_recorded_and_reaches_the_review(
    client: TestClient,
) -> None:
    """The whole point: `POST .../reflection` writes a `game_reflection` row
    (`audit.REFLECTION_ACTION`), which `GET .../review` hands back inside
    `entries` exactly like every other row - the same shape
    `ui/review_model.js`'s `reflectionOf` already scans the ledger for."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        filed = _reflect(client, info)
        assert filed.status_code == 200, filed.text
        body = filed.json()
        assert body["recorded"] is True
        assert body["player"] == "bob"
        assert isinstance(body["log_seq"], int)

        review_body = _review(client, game_id, key=info["admin_key"]).json()
        reflections = [e for e in review_body["entries"] if e["action"] == audit.REFLECTION_ACTION]
        assert len(reflections) == 1
        entry = reflections[0]
        assert entry["detail"]["player"] == "bob"
        assert entry["detail"]["text"] == (
            "the Bolt on turn one was the whole game - next time it comes down first"
        )
        # Never mistaken for something recorded live: a separate action kind
        # from `plan_declared`, and not paired into any declaration's window.
        assert entry["action"] != audit.PLAN_ACTION
        assert all(entry["seq"] != d["seq"] for d in review_body["declarations"])
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_reflection_can_be_filed_after_the_game_is_ended(client: TestClient) -> None:
    """The driver posts a game's reflection after `turn_done` confirms a
    winner - which can land after the lobby has already ended (soft-deleted)
    that game, so this is authorised the same way `game_review` is: against
    `store.archived`, not the live game."""
    info = _played_out(client)
    game_id = info["game_id"]
    ended = client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
    assert ended.status_code == 200, ended.text

    filed = _reflect(client, info)
    assert filed.status_code == 200, filed.text

    review_body = _review(client, game_id, key=info["admin_key"]).json()
    actions = [e["action"] for e in review_body["entries"]]
    assert audit.REFLECTION_ACTION in actions


def test_a_reflection_for_a_game_that_never_existed_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/api/games/no-such-game/reflection",
        json={"player": "bob", "uid": "whatever", "text": "notes"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# the AI review: a separate, Opus-generated critique, gated the same way
# ---------------------------------------------------------------------------


def _ai_review(client: TestClient, info: dict[str, Any], player: str = "bob", **overrides: Any):
    body = {
        "player": player,
        "uid": info["seat_uids"][player],
        "text": "bob's mainboard removal came a turn late twice - the reflection already"
        " named the first instance, the ledger confirms the second on turn 6",
        **overrides,
    }
    return client.post(f"/api/games/{info['game_id']}/ai-review", json=body)


def test_an_ai_review_is_refused_while_the_game_is_still_live(client: TestClient) -> None:
    """Same post-game gate as a reflection - `declare_ai_review` mirrors
    `declare_reflection` exactly."""
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        response = _ai_review(client, info)
        assert response.status_code == 409, response.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_ai_review_requires_the_seats_own_uid(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        wrong_seat = _ai_review(client, info, player="bob", uid=info["seat_uids"]["alice"])
        assert wrong_seat.status_code == 404, wrong_seat.text
        stranger = _ai_review(client, info, player="bob", uid="not-a-uid")
        assert stranger.status_code == 404, stranger.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_ai_review_is_refused_for_a_player_who_is_not_an_ai_seat(client: TestClient) -> None:
    """Forged reviews: `alice` is a human seat in `_played_out` (only `bob`
    is registered `is_agent`/AI) - her own uid genuinely opens her own seat,
    so the uid-owns-seat check alone would let her file a review "of"
    herself or of `bob` under her own credential. Only an AI seat's driver
    may ever generate one."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        as_herself = _ai_review(client, info, player="alice", uid=info["seat_uids"]["alice"])
        assert as_herself.status_code == 404, as_herself.text
        review_body = _review(client, game_id, key=info["admin_key"]).json()
        assert all(e["action"] != audit.AI_REVIEW_ACTION for e in review_body["entries"])
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_finished_games_ai_review_is_recorded_and_reaches_the_review(
    client: TestClient,
) -> None:
    """`POST .../ai-review` writes an `ai_review` row (`audit.AI_REVIEW_ACTION`),
    which `GET .../review` hands back inside `entries` exactly like every
    other row - a separate action from `game_reflection`, never mistaken for
    the seat's own reflection."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        filed = _ai_review(client, info)
        assert filed.status_code == 200, filed.text
        body = filed.json()
        assert body["recorded"] is True
        assert body["player"] == "bob"
        assert isinstance(body["log_seq"], int)

        review_body = _review(client, game_id, key=info["admin_key"]).json()
        reviews = [e for e in review_body["entries"] if e["action"] == audit.AI_REVIEW_ACTION]
        assert len(reviews) == 1
        entry = reviews[0]
        assert entry["detail"]["player"] == "bob"
        assert "mainboard removal" in entry["detail"]["text"]
        assert entry["action"] != audit.REFLECTION_ACTION
        assert entry["action"] != audit.PLAN_ACTION
        assert all(entry["seq"] != d["seq"] for d in review_body["declarations"])
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_ai_review_can_be_filed_after_the_game_is_ended(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    ended = client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
    assert ended.status_code == 200, ended.text

    filed = _ai_review(client, info)
    assert filed.status_code == 200, filed.text

    review_body = _review(client, game_id, key=info["admin_key"]).json()
    actions = [e["action"] for e in review_body["entries"]]
    assert audit.AI_REVIEW_ACTION in actions


def test_an_ai_review_for_a_game_that_never_existed_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/api/games/no-such-game/ai-review",
        json={"player": "bob", "uid": "whatever", "text": "notes"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# T35: an AI review that could not be produced, recorded durably
# ---------------------------------------------------------------------------


def _ai_review_failure(
    client: TestClient, info: dict[str, Any], player: str = "bob", **overrides: Any
):
    body = {
        "player": player,
        "uid": info["seat_uids"][player],
        "stage": "generate",
        "reason": "the wrapper answered 502",
        "invoked": True,
        **overrides,
    }
    return client.post(f"/api/games/{info['game_id']}/ai-review-failure", json=body)


def test_an_ai_review_failure_is_recorded_and_reaches_the_review(client: TestClient) -> None:
    """The signal is a row of the same ledger everything else in this system
    is durable in - `audit.AI_REVIEW_FAILED_ACTION`, never `ai_review`, so a
    reader never mistakes "no review" for a review."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        filed = _ai_review_failure(client, info)
        assert filed.status_code == 200, filed.text
        assert filed.json()["recorded"] is True

        review_body = _review(client, game_id, key=info["admin_key"]).json()
        rows = [
            e for e in review_body["entries"] if e["action"] == audit.AI_REVIEW_FAILED_ACTION
        ]
        assert len(rows) == 1
        assert rows[0]["detail"]["stage"] == "generate"
        assert rows[0]["detail"]["invoked"] is True
        assert "502" in rows[0]["detail"]["reason"]
        assert all(e["action"] != audit.AI_REVIEW_ACTION for e in review_body["entries"])
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_the_review_body_surfaces_ai_review_failures_without_reading_the_ledger(
    client: TestClient,
) -> None:
    """Clause (c): readable through an existing surface. An operator reads
    `GET .../review` and sees WHY there is no review, without grepping pod
    logs or walking `entries` by hand."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        _ai_review_failure(client, info, stage="review_record", reason="500", invoked=False)
        body = _review(client, game_id, key=info["admin_key"]).json()
        assert body["ai_review_failures"] == [
            {
                "seq": body["ai_review_failures"][0]["seq"],
                "ts": body["ai_review_failures"][0]["ts"],
                "player": "bob",
                "stage": "review_record",
                "reason": "500",
                "invoked": False,
            }
        ]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_game_with_no_review_failure_carries_an_empty_list(client: TestClient) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        assert _review(client, game_id, key=info["admin_key"]).json()["ai_review_failures"] == []
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_ai_review_failure_is_gated_exactly_like_an_ai_review(client: TestClient) -> None:
    live = _agent_game(client)
    try:
        assert _ai_review_failure(client, live).status_code == 409
    finally:
        client.delete(f"/api/games/{live['game_id']}", params={"key": live["admin_key"]})

    info = _played_out(client)
    game_id = info["game_id"]
    try:
        wrong_seat = _ai_review_failure(
            client, info, player="bob", uid=info["seat_uids"]["alice"]
        )
        assert wrong_seat.status_code == 404, wrong_seat.text
        human = _ai_review_failure(client, info, player="alice", uid=info["seat_uids"]["alice"])
        assert human.status_code == 404, human.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_ai_review_failure_refuses_a_stage_outside_the_vocabulary(client: TestClient) -> None:
    """The stage is a closed vocabulary, so `ai_review_failures` can be read
    as data rather than as free text a driver invented."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        bad = _ai_review_failure(client, info, stage="whatever")
        assert bad.status_code == 422, bad.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_the_review_failure_action_is_not_loud(client: TestClient) -> None:
    """A judgement made deliberately, not inherited: a driver-side
    infrastructure failure is not a seat filing an engine bug, and
    `LOUD_ACTIONS`'s banner names a seat as the actor. The durable row plus
    the driver's own warning line carry it instead."""
    assert audit.AI_REVIEW_FAILED_ACTION not in audit.LOUD_ACTIONS


# ---------------------------------------------------------------------------
# T36: the body this route answers with is the one the review PAGE reads
# ---------------------------------------------------------------------------
#
# Every assertion above is about the row being in the ledger. None of them
# says the page can find it, and that is the half the live deployment
# actually lost: a key or a shape that drifted on either side of this seam
# renders no `section.ai-review` at all - not an empty one, none - which is
# indistinguishable from "no review was ever filed". So these run
# `ui/review_model.js`'s own `aiReviewsOf` (the function `ui/render/review.
# js` builds each section from) against the real body, under Node.


@requires_node
def test_the_review_page_reads_a_filed_review_out_of_this_bodys_own_shape(
    client: TestClient,
) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        assert _ai_review(client, info).status_code == 200
        body = _review(client, game_id, key=info["admin_key"]).json()
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})

    found = run_module("review_model.js", "m.aiReviewsOf(rev)", rev=body)
    assert len(found) == 1, found
    assert found[0]["player"] == "bob"
    assert "mainboard removal" in found[0]["text"]
    # The seat's own words are a different row and a different reader: this
    # game has no reflection, and the page must not borrow the AI's.
    assert run_module("review_model.js", "m.reflectionOf(rev)", rev=body) is None


@requires_node
def test_a_review_that_failed_and_one_that_was_filed_are_different_bodies(
    client: TestClient,
) -> None:
    """The two outcomes T35 exists to tell apart, judged by what a reader
    actually gets: the failed game's body makes `aiReviewsOf` return nothing
    and says why in `ai_review_failures`; the filed game's does the
    opposite. Neither is the silent "no rows at all" that a pod-killed game
    leaves behind (`UNREACHABLE_TERMINAL_PATH`)."""
    failed = _played_out(client)
    filed = _played_out(client)
    try:
        assert _ai_review_failure(client, failed).status_code == 200
        assert _ai_review(client, filed).status_code == 200
        failed_body = _review(client, failed["game_id"], key=failed["admin_key"]).json()
        filed_body = _review(client, filed["game_id"], key=filed["admin_key"]).json()
    finally:
        for info in (failed, filed):
            client.delete(f"/api/games/{info['game_id']}", params={"key": info["admin_key"]})

    assert run_module("review_model.js", "m.aiReviewsOf(rev)", rev=failed_body) == []
    assert [row["stage"] for row in failed_body["ai_review_failures"]] == ["generate"]
    assert failed_body["ai_review_failures"][0]["invoked"] is True

    assert len(run_module("review_model.js", "m.aiReviewsOf(rev)", rev=filed_body)) == 1
    assert filed_body["ai_review_failures"] == []


# ---------------------------------------------------------------------------
# the snapshot this build cannot decode: the review is rebuilt from the ledger
# ---------------------------------------------------------------------------
#
# Production, verbatim: a game played to the end at `schema_version=3`, a
# build that decodes 5, and `GET .../review` answering 409 with the engine's
# refusal. The refusal is right and stays - what was wrong is that 191
# actions of recorded reasoning became unreachable because of it. Every row
# of that ledger is a self-contained JSON frame written by `audit.record`,
# and no part of reading one needs the engine to decode anything, so the
# review is rebuilt from the ledger and says, in the payload, that it was.
#
# The refusal these tests inject is the real engine's own message for a real
# schema-3 payload (`test_unloadable_games.engine_refusal`), raised through
# the same seam a stale postgres row raises it through. The repository half -
# `archived()` on a genuinely stale stored row, live and soft-deleted - is
# pinned in `test_unloadable_games.py` against real postgres.


def test_an_undecodable_finished_games_review_is_rebuilt_from_the_ledger(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        unloadable[game_id] = engine_refusal()

        response = _review(client, game_id, key=info["admin_key"])
        assert response.status_code == 200, response.text
        body = response.json()

        # Said out loud, so no reader mistakes this for a board that was read
        # off a live final state.
        assert body["reconstructed"] is True
        assert body["unloadable"] == engine_refusal()

        # The outcome comes from the `game_over` row - the ledger's own
        # record of CR 104.1, written when it happened - never guessed.
        assert body["is_over"] is True
        assert body["winner"] == "bob"
        assert body["drawn"] is False
        assert body["unestablished"] == []
        assert sorted(body["players"]) == ["alice", "bob"]

        # And the whole point: the reasoning is all still there.
        assert len(body["declarations"]) == 1
        declaration = body["declarations"][0]
        assert declaration["player"] == "bob"
        assert declaration["plan"] == PLAN["plan"]
        assert declaration["prediction"]["expecting"] == PLAN["prediction"]["expecting"]
        assert "play_land" in [entry["action"] for entry in declaration["did"]]
        assert declaration["did"][-1]["reasoning"] == "land first, then the attack"
        assert "concede" in [entry["action"] for entry in declaration["opponent_did"]]

        actions = [entry["action"] for entry in body["entries"]]
        assert actions[0] == "create_game"
        assert audit.GAME_OVER_ACTION in actions
        assert all(entry["snapshot"] is not None for entry in body["entries"])
        ending = [e for e in body["entries"] if e["action"] == audit.GAME_OVER_ACTION][-1]
        assert body["ended_seq"] == ending["seq"]
    finally:
        unloadable.pop(game_id, None)
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_decodable_games_review_is_not_marked_reconstructed(client: TestClient) -> None:
    """The marker means something only if it is absent from the ordinary
    read - which still comes off the final state the engine holds."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        assert body["reconstructed"] is False
        assert body["unloadable"] is None
        assert body["unestablished"] == []
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_undecodable_game_with_no_recorded_ending_says_so_rather_than_guessing(
    client: TestClient,
) -> None:
    """A game ended from the lobby before it finished, whose board this build
    then could not decode: `deleted_at` is what makes it readable, and the
    ledger has no `game_over` row, so whether anyone won is not something
    this review can establish. It says so rather than inventing "the game is
    over" out of the absence of evidence.

    Against `review.build_review` itself, with the real ledger of a real
    game and an `ArchivedGame` in exactly the shape `PostgresRepository.
    archived` hands back for a stale, soft-deleted row (pinned against real
    postgres in `test_unloadable_games.py`). `InMemoryRepository` holds live
    `Game` objects and never decodes a stored snapshot, so its soft-delete
    branch cannot produce this state at all - and faking it by patching the
    engine's own version constant would be testing the fake."""
    info = _agent_game(client)
    game_id = info["game_id"]
    assert _declare(client, info).status_code == 200
    ended = client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
    assert ended.status_code == 200, ended.text

    entries = audit.entries(game_id)
    assert entries and all(entry["action"] != audit.GAME_OVER_ACTION for entry in entries)
    archived = ArchivedGame(
        game_id=game_id,
        game=None,
        admin_key=info["admin_key"],
        seat_uids={uid: player for player, uid in info["seat_uids"].items()},
        created_at="2026-09-09T00:00:00+00:00",
        deleted_at="2026-09-09T00:01:00+00:00",
        unloadable=engine_refusal(),
    )

    body = review.build_review(archived, entries)
    assert body["reconstructed"] is True
    assert body["unloadable"] == engine_refusal()
    assert body["ended_at"] is not None
    assert body["is_over"] is None
    assert body["winner"] is None
    assert body["drawn"] is None
    assert sorted(body["unestablished"]) == ["drawn", "is_over", "winner"]
    assert body["ended_seq"] is None
    assert sorted(body["players"]) == ["alice", "bob"]
    assert body["declarations"][0]["plan"] == PLAN["plan"]


def test_an_undecodable_live_games_review_is_still_refused(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    """The over-or-ended gate does not soften because the board cannot be
    read. With no `game_over` row and no `deleted_at`, nothing establishes
    that this game is finished - so the 409 stands, for the seat that wrote
    the plan as much as for anyone else."""
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        unloadable[game_id] = engine_refusal()

        for params in (
            {"key": info["admin_key"]},
            {"uid": info["seat_uids"]["bob"]},
            {"uid": info["seat_uids"]["alice"]},
        ):
            refused = _review(client, game_id, **params)
            assert refused.status_code == 409, refused.text
            assert "board_read" not in refused.text
            assert PLAN["plan"] not in refused.text
    finally:
        unloadable.pop(game_id, None)
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_an_undecodable_finished_games_review_is_still_refused_to_a_stranger(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    """The credential gate is unchanged too: it reads the archived row's own
    `admin_key`/`seat_uids` columns, neither of which ever needed the
    snapshot."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        unloadable[game_id] = engine_refusal()
        assert _review(client, game_id).status_code in (404, 422)
        assert _review(client, game_id, uid="not-a-uid").status_code == 404
        assert _review(client, game_id, key="not-the-admin-key").status_code == 404
        assert _review(client, game_id, uid=info["seat_uids"]["alice"]).status_code == 200
    finally:
        unloadable.pop(game_id, None)
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_reflection_on_an_undecodable_finished_game_is_still_recorded(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    """`declare_reflection` reads the same archived row and enforces the same
    post-game gate, so it has to survive the same refusal - and a seat filing
    its write-up must not get a 500 for it."""
    info = _played_out(client)
    game_id = info["game_id"]
    try:
        unloadable[game_id] = engine_refusal()
        filed = _reflect(client, info)
        assert filed.status_code == 200, filed.text
        assert filed.json()["recorded"] is True
    finally:
        unloadable.pop(game_id, None)
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_reflection_on_an_undecodable_live_game_is_still_refused(
    client: TestClient, unloadable: dict[str, str]
) -> None:
    info = _agent_game(client)
    game_id = info["game_id"]
    try:
        unloadable[game_id] = engine_refusal()
        refused = _reflect(client, info)
        assert refused.status_code == 409, refused.text
    finally:
        unloadable.pop(game_id, None)


# ---------------------------------------------------------------------------
# the mtg-buddy advisor's own stream: what was proposed, and what came of it
#
# B2 (concurrent) owns `/advice` and `/advice/{seq}/approve` - the routes
# that write `audit.ADVICE_ACTION`/`ADVICE_APPROVED_ACTION` rows. Neither
# exists in this tree yet, so every row below is written directly through
# `audit.record`, exactly as the two routes are contracted to write them:
# an `advice` row's detail carries `player`, `method`, `path`, `body`,
# `summary`, `based_on_seq`, `validated` (with `reasoning` passed as
# `audit.record`'s own dedicated keyword - the same convention every action
# route already uses, so it lands as the entry's top-level `reasoning` rather
# than a second copy inside `detail`), and an `advice_approved` row's detail
# carries `advice_seq`. Neither row mutates the game - the real attempt, when
# there is one, is its own ordinary action row with its own `allowed`.
# ---------------------------------------------------------------------------


def _card_id(client: TestClient, game_id: str, card: str, player: str) -> str:
    added = client.post(
        f"/api/games/{game_id}/cards", json={"card": card, "player": player, "zone": "hand"}
    )
    assert added.status_code == 200, added.text
    return added.json()["added"]["card_id"]


def _advise(
    client: TestClient,
    game_id: str,
    *,
    player: str = "bob",
    path_suffix: str = "actions/play-land",
    body: dict[str, Any] | None = None,
    summary: str = "play a land",
    reasoning: str = "this keeps the curve on track",
    validated: bool = True,
) -> int:
    return audit.record(
        game_id,
        audit.ADVICE_ACTION,
        reasoning=reasoning,
        player=player,
        method="POST",
        path=f"/api/games/{game_id}/{path_suffix}",
        body=body or {},
        summary=summary,
        based_on_seq=audit.last_seq(game_id),
        validated=validated,
    )


def _approve(client: TestClient, game_id: str, advice_seq: int) -> int:
    return audit.record(game_id, audit.ADVICE_APPROVED_ACTION, advice_seq=advice_seq)


def _advice_row(body: dict[str, Any], seq: int) -> dict[str, Any]:
    rows = [row for row in body["advice"] if row["seq"] == seq]
    assert len(rows) == 1, body["advice"]
    return rows[0]


def test_advice_approved_and_followed_is_shown_as_such(client: TestClient) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        card_id = _card_id(client, game_id, "Forest", "bob")

        advice_seq = _advise(client, game_id, body={"card_id": card_id}, summary="play the Forest")
        approved_seq = _approve(client, game_id, advice_seq)

        played = client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": "bob", "card_id": card_id, "reasoning": "taking the advice"},
        )
        assert played.status_code == 200, played.text
        assert played.json()["allowed"] is True, played.text

        conceded = client.post(
            f"/api/games/{game_id}/actions/concede",
            json={"player": "alice", "reasoning": "no way back"},
        )
        assert conceded.status_code == 200, conceded.text

        body = _review(client, game_id, key=info["admin_key"]).json()
        row = _advice_row(body, advice_seq)
        assert row["player"] == "bob"
        assert row["summary"] == "play the Forest"
        assert row["reasoning"] == "this keeps the curve on track"
        assert row["approved"] is True
        assert row["approved_seq"] == approved_seq
        assert row["did"] is not None
        assert row["did"]["action"] == "play_land"
        assert row["followed"] is True
        assert row["refused"] == [], "nothing was refused in this window"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_advice_approved_but_then_refused_is_not_the_same_as_ignored(
    client: TestClient,
) -> None:
    """The engine's refusal is never `did` - a refusal is a failed attempt,
    not "what happened" - so this reads as `approved` with `did: None`, and
    that combination is what distinguishes it from a piece of advice nobody
    ever approved at all (`test_advice_that_was_ignored_and_nothing_followed`
    below), which is `did: None` too but with `approved: False`.

    The refusal is not simply dropped, though: it is the single most
    instructive line a retrospective can contain, since this engine's whole
    reason for existing is saying out loud why a refused action is illegal -
    so it is carried in `refused`, reason verbatim."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        forest = _card_id(client, game_id, "Forest", "bob")
        mountain = _card_id(client, game_id, "Mountain", "bob")

        # bob's own land drop for the turn, spent before any advice exists.
        first = client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": "bob", "card_id": forest, "reasoning": "land for the turn"},
        )
        assert first.status_code == 200 and first.json()["allowed"] is True, first.text

        advice_seq = _advise(
            client,
            game_id,
            body={"card_id": mountain},
            summary="play the Mountain",
            reasoning="a second land would be great, if it were legal",
            validated=False,
        )
        approved_seq = _approve(client, game_id, advice_seq)

        refused = client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": "bob", "card_id": mountain, "reasoning": "taking the advice anyway"},
        )
        assert refused.status_code == 200, refused.text
        assert refused.json()["allowed"] is False, refused.text
        refusal_reason = refused.json()["reason"]
        assert refusal_reason

        conceded = client.post(
            f"/api/games/{game_id}/actions/concede",
            json={"player": "alice", "reasoning": "no way back"},
        )
        assert conceded.status_code == 200, conceded.text

        body = _review(client, game_id, key=info["admin_key"]).json()
        row = _advice_row(body, advice_seq)
        assert row["approved"] is True
        assert row["approved_seq"] == approved_seq
        assert row["did"] is None
        assert row["followed"] is False
        assert len(row["refused"]) == 1
        refused_row = row["refused"][0]
        assert refused_row["action"] == "play_land"
        assert refused_row["player"] == "bob"
        assert refused_row["detail"]["reason"] == refusal_reason
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_advice_that_was_ignored_in_favour_of_something_else(client: TestClient) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        card_id = _card_id(client, game_id, "Forest", "bob")

        advice_seq = _advise(client, game_id, body={"card_id": card_id}, summary="play the Forest")
        # no advice_approved row: this proposal is simply never approved.

        passed = client.post(
            f"/api/games/{game_id}/actions/pass-priority",
            json={"player": "bob", "reasoning": "holding everything up instead"},
        )
        assert passed.status_code == 200, passed.text

        conceded = client.post(
            f"/api/games/{game_id}/actions/concede",
            json={"player": "alice", "reasoning": "no way back"},
        )
        assert conceded.status_code == 200, conceded.text

        body = _review(client, game_id, key=info["admin_key"]).json()
        row = _advice_row(body, advice_seq)
        assert row["approved"] is False
        assert row["approved_seq"] is None
        assert row["did"] is not None
        assert row["did"]["action"] == "pass_priority"
        assert row["followed"] is False
        assert row["refused"] == []
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_advice_that_was_ignored_and_nothing_followed(client: TestClient) -> None:
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        card_id = _card_id(client, game_id, "Forest", "bob")

        advice_seq = _advise(client, game_id, body={"card_id": card_id}, summary="play the Forest")
        # bob never acts again - alice ends it.

        conceded = client.post(
            f"/api/games/{game_id}/actions/concede",
            json={"player": "alice", "reasoning": "no way back"},
        )
        assert conceded.status_code == 200, conceded.text

        body = _review(client, game_id, key=info["admin_key"]).json()
        row = _advice_row(body, advice_seq)
        assert row["approved"] is False
        assert row["approved_seq"] is None
        assert row["did"] is None
        assert row["followed"] is False
        assert row["refused"] == []
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_advice_can_be_refused_more_than_once_before_something_finally_works(
    client: TestClient,
) -> None:
    """`refused` is never assumed to hold at most one row - a seat retrying a
    refused attempt is real, and every one of those attempts belongs in the
    retrospective, not just the last."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        forest = _card_id(client, game_id, "Forest", "bob")
        mountain = _card_id(client, game_id, "Mountain", "bob")

        first = client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": "bob", "card_id": forest, "reasoning": "land for the turn"},
        )
        assert first.status_code == 200 and first.json()["allowed"] is True, first.text

        advice_seq = _advise(
            client, game_id, body={"card_id": mountain}, summary="play the Mountain"
        )

        refused_once = client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": "bob", "card_id": mountain, "reasoning": "trying anyway"},
        )
        assert refused_once.status_code == 200 and refused_once.json()["allowed"] is False

        refused_twice = client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": "bob", "card_id": mountain, "reasoning": "trying again"},
        )
        assert refused_twice.status_code == 200 and refused_twice.json()["allowed"] is False

        conceded = client.post(
            f"/api/games/{game_id}/actions/concede",
            json={"player": "alice", "reasoning": "no way back"},
        )
        assert conceded.status_code == 200, conceded.text

        body = _review(client, game_id, key=info["admin_key"]).json()
        row = _advice_row(body, advice_seq)
        assert row["did"] is None
        assert len(row["refused"]) == 2
        assert [r["seq"] for r in row["refused"]] == sorted(r["seq"] for r in row["refused"])
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_advice_rows_do_not_disturb_the_declaration_windows(client: TestClient) -> None:
    """`_declarations` marks a window's start at `PLAN_ACTION` rows only - an
    `advice` row existing beside one must not read as a second declaration
    and must not split a window in two. The advice row still shows up inside
    the window's own `did`/`opponent_did`, exactly as any other row by that
    seat already does - it is simply never treated as a new marker."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        client.post(
            f"/api/games/{game_id}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )
        client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": "bob", "card": "Mountain", "reasoning": "land first, then the attack"},
        )
        advice_seq = _advise(client, game_id, summary="attack next")
        conceded = client.post(
            f"/api/games/{game_id}/actions/concede",
            json={"player": "alice", "reasoning": "the race is lost"},
        )
        assert conceded.status_code == 200, conceded.text

        body = _review(client, game_id, key=info["admin_key"]).json()
        assert len(body["declarations"]) == 1, (
            "one plan_declared row is one declaration, whatever else sits beside it"
        )
        declaration = body["declarations"][0]
        did_actions = [entry["action"] for entry in declaration["did"]]
        assert "play_land" in did_actions
        assert audit.ADVICE_ACTION in did_actions, (
            "the advice row is still a row bob made in this window - it is not "
            "hidden, only not a second marker"
        )
        opponent_did = [entry["action"] for entry in declaration["opponent_did"]]
        assert "concede" in opponent_did

        assert len(body["advice"]) == 1
        assert body["advice"][0]["seq"] == advice_seq
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_the_opponent_reads_the_same_advice_the_human_seat_does(client: TestClient) -> None:
    """`game_review` has no per-seat redaction of any kind post-game - it
    hands `build_review`'s result to whichever seat or admin key asks,
    unfiltered, exactly as it already does for `declarations` (`test_a_seat_
    may_read_the_finished_games_review` above proves the same thing for a
    plan). This is a decision on the record, not a side effect: an advice
    row's `summary`, `reasoning` and `body` can name cards that were in the
    human seat's own hand at the time, and once the game is over, the
    opponent reads them exactly as the advised seat does."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200
        card_id = _card_id(client, game_id, "Forest", "bob")
        advice_seq = _advise(
            client,
            game_id,
            body={"card_id": card_id},
            summary="play the Forest",
            reasoning="this keeps the curve on track",
        )
        conceded = client.post(
            f"/api/games/{game_id}/actions/concede",
            json={"player": "alice", "reasoning": "no way back"},
        )
        assert conceded.status_code == 200, conceded.text

        for player in ("bob", "alice"):
            response = _review(client, game_id, uid=info["seat_uids"][player])
            assert response.status_code == 200, response.text
            row = _advice_row(response.json(), advice_seq)
            assert row["summary"] == "play the Forest"
            assert row["reasoning"] == "this keeps the curve on track"
            assert row["body"] == {"card_id": card_id}
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ---------------------------------------------------------------------------
# play-script run rows: not moves, and never the opponent's to read
# ---------------------------------------------------------------------------


def _script_run(client: TestClient, game_id: str, uid: str, phase: str, **over: Any):
    body: dict[str, Any] = {
        "phase": phase,
        "run_id": "run-1",
        "seat": "bob",
        "role": "advisor",
        "advise": True,
        "script_id": FETCH_SHOCK["id"],
        "guides_revision": "0123abcd",
        "opposing_archetype_asserted": "dimir-frog",
        "turn": 1,
        "step": "precombat_main",
        "invoked_via": "mcp",
        **({"exit": "completed", "refusal_text": "a secret refusal"} if phase == "finish" else {}),
        **over,
    }
    return client.post(f"/api/games/{game_id}/script-runs", params={"uid": uid}, json=body)


def _advised_run_then_the_real_move(client: TestClient) -> tuple[dict[str, Any], int]:
    """bob is advised to play a Forest, the buddy advising bob reports a
    script run for bob, then bob plays the Forest and alice concedes."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    store.register_advisors(game_id, {"bobs-advisor": Advisor(player="bob")})
    registered = client.put(
        f"/api/games/{game_id}/seats/bobs-advisor/scripts",
        params={"uid": "bobs-advisor"},
        json={"deck_slug": SLUG, "guides_revision": "0123abcd", "scripts": [FETCH_SHOCK]},
    )
    assert registered.status_code == 200, registered.text
    assert _declare(client, info).status_code == 200
    card_id = _card_id(client, game_id, "Forest", "bob")
    advice_seq = _advise(client, game_id, body={"card_id": card_id}, summary="play the Forest")
    for phase in ("start", "finish"):
        reported = _script_run(client, game_id, "bobs-advisor", phase)
        assert reported.status_code == 200, reported.text
    played = client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "bob", "card_id": card_id, "reasoning": "taking the advice"},
    )
    assert played.status_code == 200, played.text
    assert played.json()["allowed"] is True, played.text
    conceded = client.post(
        f"/api/games/{game_id}/actions/concede",
        json={"player": "alice", "reasoning": "no way back"},
    )
    assert conceded.status_code == 200, conceded.text
    return info, advice_seq


def test_a_script_run_row_is_never_the_move_advice_was_followed_by(client: TestClient) -> None:
    info, advice_seq = _advised_run_then_the_real_move(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        row = _advice_row(body, advice_seq)
        assert row["did"] is not None
        assert row["did"]["action"] == "play_land", row["did"]
        assert row["followed"] is True
        assert row["refused"] == []
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_script_run_row_is_not_a_declared_seats_move(client: TestClient) -> None:
    info, _ = _advised_run_then_the_real_move(client)
    game_id = info["game_id"]
    try:
        body = _review(client, game_id, key=info["admin_key"]).json()
        declaration = body["declarations"][0]
        for side in ("did", "opponent_did"):
            actions = [entry["action"] for entry in declaration[side]]
            assert not set(actions) & set(audit.SCRIPT_RUN_ACTIONS), (side, actions)
        assert "play_land" in [entry["action"] for entry in declaration["did"]]
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_the_opponents_review_omits_the_script_run_rows(client: TestClient) -> None:
    """Post-game the review shows both seats' plans and advice, but a script
    run is not part of that: it never reaches the opponent, live or after."""
    info, _ = _advised_run_then_the_real_move(client)
    game_id = info["game_id"]
    try:
        opponent = _review(client, game_id, uid=info["seat_uids"]["alice"])
        assert opponent.status_code == 200, opponent.text
        actions = [entry["action"] for entry in opponent.json()["entries"]]
        assert not set(actions) & set(audit.SCRIPT_RUN_ACTIONS), actions
        assert "dimir-frog" not in opponent.text
        assert "a secret refusal" not in opponent.text
        assert "play_land" in actions, "only the script rows are dropped"

        for params in ({"uid": info["seat_uids"]["bob"]}, {"key": info["admin_key"]}):
            own = _review(client, game_id, **params)
            assert own.status_code == 200, own.text
            kept = [
                e["action"]
                for e in own.json()["entries"]
                if e["action"] in audit.SCRIPT_RUN_ACTIONS
            ]
            assert kept == [audit.SCRIPT_RUN_STARTED_ACTION, audit.SCRIPT_RUN_ACTION], params
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
