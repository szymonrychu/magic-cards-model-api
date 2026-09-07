"""The live decision provider, for an activated ability's own targets
(CR 601.2c, CR 602.2b).

The engine's `Game.activate_ability` now distinguishes `targets=None`
("caller supplied nothing" - ask, through the ordinary `CHOOSE_TARGETS`
question) from an explicit list, including `[]` ("I choose zero targets",
CR 115.6). `engine_api.activate_ability` used to collapse both shapes into
`req.targets or []` before ever reaching the engine, which reads as an
explicit empty announcement no matter which one the caller meant - so a
seat that simply omitted `targets` silently got a zero-target activation
instead of being asked.

This file is the `test_target_decisions.py` pattern (which proves the same
mechanism for a *triggered* ability's CR 603.3d announcement) reused for an
*activated* ability's CR 601.2c one: Karn, the Great Creator's `+1`
("Until your next turn, up to one target noncreature artifact becomes an
artifact creature with power and toughness each equal to its mana value.")
is the engine's own reported probe for this defect
(`tests/scenarios/test_activated_ability_target_decisions.py` in the engine
repo), reused here end to end through the API and the seat CLI.
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


def stock_karn_board(client: TestClient, game_id: str, *, uid: str | None = None) -> dict[str, Any]:
    """alice: Karn, the Great Creator and a Sol Ring, the engine's own probe
    board for this defect."""
    add_card(client, game_id, "Karn, the Great Creator", "alice", "battlefield")
    add_card(client, game_id, "Sol Ring", "alice", "battlefield")
    board = state(client, game_id, uid=uid)
    return {
        "karn": find(board["battlefield"], "Karn, the Great Creator"),
        "sol_ring": find(board["battlefield"], "Sol Ring"),
    }


def activate_karn_plus_one_in_background(
    game_id: str, board: dict[str, Any], player: str = "alice", targets: list[str] | None = None
) -> threading.Thread:
    """Activating with `targets` omitted blocks inside `Game.ask` once the
    engine discovers nobody announced the "up to one" choice - the same
    shape `test_target_decisions.py.cast_devourer_in_background` uses for
    casting's own blocking `Game.ask` call."""

    def call() -> None:
        stepping_client = TestClient(app)
        body: dict[str, Any] = {
            "player": player,
            "card_id": board["karn"]["id"],
            "ability_index": 0,
        }
        if targets is not None:
            body["targets"] = targets
        stepping_client.post(f"/api/games/{game_id}/actions/activate", json=body)

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


def test_activating_with_no_targets_field_asks_rather_than_silently_choosing_zero(client, game):
    """The reported bug, closed: `targets` omitted entirely from the request
    must reach a live `CHOOSE_TARGETS` decision, not be collapsed into `[]`
    before the engine ever sees it."""
    uids = seats_for(client, game)
    board = stock_karn_board(client, game)

    thread = activate_karn_plus_one_in_background(game, board, targets=None)
    try:
        pending_view = wait_for_pending(client, game, uids["alice"])
        pending = pending_view["pending_decision"]
        assert pending["player"] == "alice"
        assert pending["kind"] == "choose_targets"
        assert pending["target_maximum"] == 1
        candidate_ids = [entry["id"] for entry in pending["targets"]]
        assert board["sol_ring"]["id"] in candidate_ids

        answer = client.post(
            f"/api/games/{game}/decide",
            params={"uid": uids["alice"]},
            json={"player": "alice", "targets": [board["sol_ring"]["id"]]},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["accepted"] is True
    finally:
        thread.join(timeout=10)
    assert not thread.is_alive(), "the activate request never returned"

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "ability from Karn, the Great Creator"

    final = state(client, game)
    sol_ring = find(final["battlefield"], "Sol Ring")
    assert "Creature" in sol_ring["type_line"], "answering with Sol Ring must animate it"
    assert sol_ring["power"] == 1 and sol_ring["toughness"] == 1, "Sol Ring's mana value is 1"


def test_activating_with_an_explicit_empty_list_still_means_zero_targets_and_asks_nothing(
    client, game
):
    """The companion: `targets: []` sent explicitly is a real announcement of
    zero, legal for Karn's "up to one" (CR 115.6), and must not be re-asked -
    the exact behaviour `req.targets or []` used to give *every* omitted
    request by accident, now reachable only when the caller actually means
    it."""
    board = stock_karn_board(client, game)
    karn = board["karn"]

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": karn["id"],
            "ability_index": 0,
            "targets": [],
        },
    )
    body = response.json()
    assert body["allowed"] is True, body

    view = state(client, game)
    assert view.get("pending_decision") is None, "an explicit empty answer is not asked again"

    client.post(f"/api/games/{game}/resolve-top")

    final = state(client, game)
    sol_ring = find(final["battlefield"], "Sol Ring")
    assert "Creature" not in sol_ring["type_line"], "nothing was chosen, so nothing animated"
    karn_after = find(final["battlefield"], "Karn, the Great Creator")
    assert "Creature" not in karn_after["type_line"], "declining must never fall onto Karn"


def test_the_seat_cli_activate_omits_targets_key_and_gets_asked(monkeypatch, capsys) -> None:
    """The whole thing through the seat CLI, as a player would actually do
    it: `seat_cli activate` with no `--target` at all must produce the same
    body-shape (`targets` key absent) that reaches the engine as `None`, and
    the pending question must be answerable through `seat_cli decide
    --targets`."""
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
            board = stock_karn_board(http, game_id, uid=uids["alice"])
            thread = activate_karn_plus_one_in_background(game_id, board, targets=None)
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
                assert pending is not None, "activating never asked alice for a target"
                assert pending["kind"] == "choose_targets"

                rc = seat_cli.run(["--uid", uids["alice"], "--game", game_id, "wait"])
                assert rc == 0
                printed = capsys.readouterr().out
                assert "a decision is waiting for you" in printed

                rc = seat_cli.run(
                    [
                        "--uid",
                        uids["alice"],
                        "--game",
                        game_id,
                        "decide",
                        "",
                        "--targets",
                        board["sol_ring"]["id"],
                        "--hold",
                    ]
                )
                assert rc == 0
            finally:
                thread.join(timeout=10)
            assert not thread.is_alive()

            http.post(f"/api/games/{game_id}/resolve-top")
            final = http.get(f"/api/games/{game_id}", params={"uid": uids["alice"]}).json()
            sol_ring = find(final["battlefield"], "Sol Ring")
            assert "Creature" in sol_ring["type_line"]
        finally:
            http.delete(f"/api/games/{game_id}", params={"key": created["admin_key"]})
    finally:
        http.close()
