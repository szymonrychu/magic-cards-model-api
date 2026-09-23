"""D16 (battle 1): `decide` never posts a pass the engine must refuse - and,
since HOLD (2026-09-23), never posts a pass at all.

A decision is asked of whoever the resolving object names, not of whoever
holds priority. bob answering a question raised while alice holds priority
used to be followed by `run_action`'s auto-pass on bob's behalf, refused with
"bob does not have priority, alice does" - an audit row that `eval_report`
counts as a refusal and folds into `rules_error_rate`, for a pass no seat ever
chose to make. D16 first limited that release to a seat actually holding
priority; HOLD removed it from `decide` entirely (`seatkit.run_decision`), so a
seat that holds priority after answering keeps it until it passes itself
(CR 117.3d).
"""

from __future__ import annotations

import json

import httpx
from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.store import store


def _uids(game: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game).items()}


def _pass_rows(client: TestClient, game: str, uid: str) -> list[dict]:
    log = client.get(f"/api/games/{game}/log", params={"uid": uid}).json()["log"]
    return [entry for entry in log if entry["action"] == "pass_priority"]


def _refused(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["detail"].get("allowed") is False]


def test_a_decision_answered_without_priority_posts_no_refused_pass(
    client, game, monkeypatch, capsys
):
    """Mind Rot: "Target player discards two cards." bob, the target, chooses
    the discards while alice - the active player - holds priority."""
    uids = _uids(game)
    for _ in range(3):
        add_card(client, game, "Swamp", "alice", "battlefield")
        add_card(client, game, "Mountain", "bob", "hand")
    add_card(client, game, "Mind Rot", "alice", "hand")
    board = state(client, game, uid=uids["alice"])
    mind_rot = find(board["hands"]["alice"], "Mind Rot")["id"]
    swamps = [card["id"] for card in board["battlefield"] if card["name"] == "Swamp"]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": mind_rot, "targets": ["bob"], "mana_sources": swamps},
    )
    assert cast.json()["allowed"] is True, cast.text
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})

    pending = state(client, game, uid=uids["bob"])["pending_decision"]
    assert pending is not None and pending["player"] == "bob", pending
    assert pending["kind"] == "choose_cards", pending
    refused_before = len(_refused(_pass_rows(client, game, uids["bob"])))

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    rc = seat_cli.run(
        ["--uid", uids["bob"], "--game", game, "decide", ",".join(pending["cards"][:2])]
    )

    printed = capsys.readouterr().out
    assert rc == 0, printed
    after = state(client, game, uid=uids["bob"])
    assert len(after["graveyards"]["bob"]) == 2, "the answer itself went through"
    assert after["priority_holder"] == "alice", after["priority_holder"]
    refused = _refused(_pass_rows(client, game, uids["bob"]))
    assert len(refused) == refused_before, refused
    assert "does not have priority" not in printed, printed


def test_a_decision_answered_while_holding_priority_keeps_it(client, game, monkeypatch, capsys):
    """Steam Vents asks alice to pay 2 life as it enters, in her own main
    phase: playing a land is a special action, so CR 116.3 leaves her with
    priority afterward, and answering the question posts no pass for her."""
    uids = _uids(game)
    add_card(client, game, "Steam Vents", "alice", "hand")
    vents = state(client, game, uid=uids["alice"])["hands"]["alice"][0]["id"]
    played = client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "alice", "card_id": vents}
    ).json()
    assert played["paused"] is True, played
    allowed_before = [
        row for row in _pass_rows(client, game, uids["alice"]) if row["detail"].get("allowed")
    ]

    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    rc = seat_cli.run(["--uid", uids["alice"], "--game", game, "decide", "--accept", "yes"])

    printed = capsys.readouterr().out
    assert rc == 0, printed
    rows = _pass_rows(client, game, uids["alice"])
    allowed_after = [row for row in rows if row["detail"].get("allowed")]
    assert len(allowed_after) == len(allowed_before), rows
    assert _refused(rows) == [], rows
    assert state(client, game, uid=uids["alice"])["priority_holder"] == "alice"
    assert json.loads(printed)["held_priority"] is True, printed
