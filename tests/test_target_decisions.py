"""The live decision provider, for a triggered ability's targets (CR 603.3d).

`test_decisions.py` covers the cleanup discard (`choose_cards`); this file is
the same mechanism for `choose_targets`, added so that a triggered ability
requiring a target - Devourer of Destiny's "When you cast this spell, exile
target permanent that's one or more colors." is the reported case - can
actually be answered by a seat sitting outside the engine, rather than always
going on the stack with no target because nobody was ever asked.

Casting Devourer of Destiny is itself the blocking call here (`Game.put_on_stack`
puts the cast trigger on the stack synchronously, which is where
`Game.ask` blocks), the same shape `test_decisions.py`'s `advance_into_cleanup`
uses for the turn-based action that blocks on the discard.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def stock_devourer_board(
    client: TestClient, game_id: str, *, uid: str | None = None
) -> dict[str, Any]:
    """alice: a Sol Ring and five Mountains, enough to cast Devourer of
    Destiny ({5}{C}{C}); bob: a Grizzly Bears to be the offered target.

    `uid` is alice's own seat uid when the caller has one already (a game
    built by hand rather than the `game` fixture has no admin key stashed for
    `state()`'s default to fall back on, and an unauthenticated view redacts
    her hand to nothing).
    """
    add_card(client, game_id, "Sol Ring", "alice", "battlefield")
    for _ in range(5):
        add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Devourer of Destiny", "alice", "hand")
    add_card(client, game_id, "Grizzly Bears", "bob", "battlefield")
    board = state(client, game_id, uid=uid)
    return {
        "sol_ring": find(board["battlefield"], "Sol Ring"),
        "mountains": [c for c in board["battlefield"] if c["name"] == "Mountain"],
        "devourer": board["hands"]["alice"][0],
        "bears": find(board["battlefield"], "Grizzly Bears"),
    }


def cast_devourer_in_background(
    game_id: str, board: dict[str, Any], player: str = "alice"
) -> threading.Thread:
    """The cast call blocks inside `Game.ask` once the cast trigger is put on
    the stack, so it is run on a background thread - exactly the shape
    `test_decisions.py.advance_into_cleanup` uses for cleanup's discard."""
    sources = [board["sol_ring"]["id"]] + [m["id"] for m in board["mountains"]]

    def call() -> None:
        stepping_client = TestClient(app)
        stepping_client.post(
            f"/api/games/{game_id}/actions/cast",
            json={
                "player": player,
                "card_id": board["devourer"]["id"],
                "mana_sources": sources,
            },
        )

    thread = threading.Thread(target=call)
    thread.start()
    return thread


def wait_for_pending(client: TestClient, game_id: str, uid: str, tries: int = 300) -> dict:
    for _ in range(tries):
        view = state(client, game_id, uid=uid)
        if view.get("pending_decision") is not None:
            return view
        time.sleep(0.01)
    raise AssertionError("no pending decision showed up in time")


def test_a_seat_sees_and_answers_its_pending_target_decision(client, game):
    uids = seats_for(client, game)
    board = stock_devourer_board(client, game)

    thread = cast_devourer_in_background(game, board)
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        pending = pending_view["pending_decision"]
        assert pending["player"] == "alice"
        assert pending["kind"] == "choose_targets"
        assert pending["target_maximum"] == 1
        candidate_ids = [entry["id"] for entry in pending["targets"]]
        assert board["bears"]["id"] in candidate_ids

        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "targets": [board["bears"]["id"]]},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive(), "the casting request never returned"

    client.post(f"/api/games/{game}/resolve-top")  # the trigger, above its own spell
    resolved = client.post(f"/api/games/{game}/resolve-top").json()  # the spell
    assert resolved["resolved"] == "Devourer of Destiny"

    final = state(client, game)
    exiled = [card["name"] for card in final["exile"]]
    assert "Grizzly Bears" in exiled, "the chosen target was actually exiled"


def test_the_opponent_cannot_see_the_target_candidates(client, game):
    """Security test: bob may see that alice is choosing a target and why,
    but never the candidate identities, and he may not answer for her - the
    same guarantee `test_decisions.py` proves for the cleanup discard, now for
    `choose_targets`. `visibility.redact_pending_decision` does this generically
    (it is not `choose_cards`-specific), so this is the proof that generic
    stripping actually covers the new kind too."""
    uids = seats_for(client, game)
    board = stock_devourer_board(client, game)

    thread = cast_devourer_in_background(game, board)
    try:
        wait_for_pending(client, game, uids["alice"])
        bobs_view = state(client, game, uid=uids["bob"])
        pending = bobs_view["pending_decision"]
        assert pending is not None, "bob should still see that a decision is pending"
        assert pending["player"] == "alice"
        assert pending["kind"] == "choose_targets"
        assert "prompt" in pending
        assert "targets" not in pending, "bob must never see the candidate target identities"
        assert "target_maximum" not in pending

        alices_view = state(client, game, uid=uids["alice"])
        alices_pending = alices_view["pending_decision"]
        assert alices_pending["targets"], "alice, the deciding player, does see her candidates"

        refused = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["bob"]},
            json={"player": "bob", "targets": [board["bears"]["id"]]},
        )
        assert refused.status_code == 200
        assert refused.json()["accepted"] is False

        still_pending = state(client, game, uid=uids["alice"])
        assert still_pending.get("pending_decision") is not None, "bob's attempt changed nothing"

        # Let the real player answer so the thread can finish.
        client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "targets": [board["bears"]["id"]]},
        )
    finally:
        thread.join(timeout=10)


def test_giving_no_target_for_a_mandatory_target_is_refused(client, game):
    """ "exile target permanent" is a required target on Devourer's own text
    (CR 601.2c, CR 603.3d) - a legal target exists (bob's Grizzly Bears), so
    declining to name one is not "nobody answered", it is an illegal answer.

    `decisions.validate_answer` refuses the empty `targets: []` synchronously,
    in this request, using the same `target_minimum` check `Game.ask` itself
    runs (see
    `test_an_engine_refused_answer_is_refused_synchronously_not_recorded_allowed`
    in `test_decisions.py` for the general regression this is one instance
    of) - this used to be accepted and silently resolved the ability doing
    nothing, which is the gap that test closed. The resolving thread stays
    parked on the same decision, so naming the legal target afterwards still
    lets it resolve and actually exile the target."""
    uids = seats_for(client, game)
    board = stock_devourer_board(client, game)

    thread = cast_devourer_in_background(game, board)
    try:
        wait_for_pending(client, game, uids["alice"])
        empty = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "targets": []},
        )
        assert empty.status_code == 200
        body = empty.json()
        assert body["accepted"] is False
        assert "at least 1 target(s) must be chosen" in body["reason"]
        assert thread.is_alive(), "a refused answer must not have unblocked the resolving thread"

        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "targets": [board["bears"]["id"]]},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive(), "the casting request never returned"

    client.post(f"/api/games/{game}/resolve-top")  # the trigger, above its own spell
    resolved = client.post(f"/api/games/{game}/resolve-top").json()  # the spell
    assert resolved["resolved"] == "Devourer of Destiny"

    final = state(client, game)
    assert "Grizzly Bears" in [c["name"] for c in final["exile"]], "the named target was exiled"


def test_the_seat_cli_flow_answers_a_target_decision(monkeypatch, capsys) -> None:
    """The whole thing through the seat CLI, as a player would actually do it."""
    import httpx

    from magic_cards_model_api import seat_cli

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))

    http = TestClient(app)
    try:
        created = http.post(
            "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
        ).json()
        game_id = created["game_id"]
        uids = created["seat_uids"]
        try:
            board = stock_devourer_board(http, game_id, uid=uids["alice"])
            thread = cast_devourer_in_background(game_id, board)
            try:
                deadline = time.monotonic() + 5
                pending = None
                while time.monotonic() < deadline:
                    pending = http.get(
                        f"/api/games/{game_id}", params={"uid": uids["alice"]}
                    ).json()["pending_decision"]
                    if pending is not None:
                        break
                    time.sleep(0.01)
                assert pending is not None, "casting never asked alice for a target"
                assert pending["kind"] == "choose_targets"

                rc = seat_cli.run(
                    ["--uid", uids["alice"], "--game", game_id, "wait", "--max-seconds", "3"]
                )
                assert rc == 0
                printed = capsys.readouterr().out
                assert "a decision is waiting for you" in printed

                # `--hold`: see test_decisions.py's matching comment - `decide`
                # blocks by default now, and there is no opponent thread here
                # to wake it.
                rc = seat_cli.run(
                    [
                        "--uid",
                        uids["alice"],
                        "--game",
                        game_id,
                        "decide",
                        "",
                        "--targets",
                        board["bears"]["id"],
                        "--hold",
                    ]
                )
                assert rc == 0
            finally:
                thread.join(timeout=10)
            assert not thread.is_alive()

            http.post(f"/api/games/{game_id}/resolve-top")  # the trigger
            resolved = http.post(f"/api/games/{game_id}/resolve-top").json()  # the spell
            assert resolved["resolved"] == "Devourer of Destiny"
            final = http.get(f"/api/games/{game_id}", params={"uid": uids["alice"]}).json()
            assert "Grizzly Bears" in [c["name"] for c in final["exile"]]
        finally:
            http.delete(f"/api/games/{game_id}", params={"key": created["admin_key"]})
    finally:
        http.close()
