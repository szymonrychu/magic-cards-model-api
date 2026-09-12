from __future__ import annotations

import json

from fastapi.testclient import TestClient

from magic_cards_model_api.store import store


def test_repro_report_is_attributed_to_the_caller_supplied_player(
    client: TestClient, game: str, admin_key: str
) -> None:
    """FINDING 6: /report attributes a ledger row to a caller-supplied
    player."""
    from test_game_access_advisor import advisor_uid

    uid = advisor_uid(game, "alice")
    response = client.post(
        f"/api/games/{game}/report",
        params={"uid": uid},
        json={"player": "bob", "text": "bob is cheating, signed bob"},
    )
    assert response.status_code == 200, response.text
    log = client.get(f"/api/games/{game}/log", params={"uid": admin_key}).json()["log"]
    row = next(e for e in log if e["action"] == "report_problem")
    print("REPORT ROW PLAYER:", row["detail"].get("player"))
    assert row["detail"]["player"] == "alice", (
        "an advisor of alice filed a report attributed to bob: " + json.dumps(row)
    )


def test_repro_board_moved_409_leaks_both_hands(client: TestClient, game: str) -> None:
    """FINDING 5: the new 409 BoardMoved body leaks both players' hands."""
    from conftest import add_card

    add_card(client, game, "Mountain", "alice", "hand")
    add_card(client, game, "Island", "bob", "hand")
    seat = next(u for u, p in store.seat_uids(game).items() if p == "alice")

    response = client.post(
        f"/api/games/{game}/plan",
        json={
            "uid": seat,
            "player": "alice",
            "board_read": "b",
            "my_role": "r",
            "plan": "p",
            "prediction": {"expecting": "a fetchland", "because": "they kept a two-lander"},
            "expected_seq": 1,
        },
    )
    print("STATUS", response.status_code)
    body = response.text
    assert response.status_code == 409, body
    assert "Island" not in body, "bob's hand leaked into alice's 409 body: " + body[:1500]
