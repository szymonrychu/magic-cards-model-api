"""The log's full-information snapshots and the player problem reports.

Every entry carries a snapshot - board, both hands, stack with targets - and a
player can file a problem report straight into the log.
"""

from conftest import add_card, find, state


def log_of(client, game_id):
    response = client.get(f"/api/games/{game_id}/log")
    assert response.status_code == 200, response.text
    return response.json()["log"]


def test_every_entry_carries_a_full_snapshot(client, game):
    """The log is the debugger's eye: nothing is hidden in it. Even the
    opponent's hand is in the snapshot - this is the post-mortem view, not the
    per-player view."""
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    add_card(client, game, "Forest", "alice", "battlefield")

    log = log_of(client, game)

    for entry in log:
        assert entry["snapshot"] is not None, f"entry {entry['seq']} has no snapshot"
    hands = log[-1]["snapshot"]["hands"]
    assert hands["alice"] == ["Grizzly Bears"], "both players' hands are in the log"
    board = log[-1]["snapshot"]["battlefield"]
    assert board == ["alice: Forest"], "the board is in the log, with controller"


def test_the_snapshot_shows_the_stack_and_its_targets(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    bolt = find(board["hands"]["alice"], "Lightning Bolt")
    mountain = find(board["battlefield"], "Mountain")

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "targets": ["bob"],
            "mana_sources": [mountain["id"]],
        },
    )
    assert cast.json()["allowed"] is True, cast.text

    log = log_of(client, game)
    entry = next(e for e in log if e["action"] == "cast" and e["detail"]["allowed"])
    stack = entry["snapshot"]["stack"]
    assert stack == [
        {
            "name": "Lightning Bolt",
            "kind": "spell",
            "controller": "alice",
            "targets": ["bob"],
            "target_refs": [{"kind": "player", "id": "bob", "name": "bob"}],
        }
    ], "the stack and each object's targets are in the snapshot"


def test_a_problem_report_lands_in_the_log(client, game):
    response = client.post(
        f"/api/games/{game}/report",
        json={"player": "alice", "text": "the sky is the wrong colour"},
    )
    assert response.json() == {"recorded": True}

    entry = log_of(client, game)[-1]
    assert entry["action"] == "report_problem"
    assert entry["detail"] == {"player": "alice", "text": "the sky is the wrong colour"}
    assert entry["snapshot"] is not None, "the report carries the state it complains about"


def test_a_problem_report_from_an_unknown_player_is_refused(client, game):
    response = client.post(
        f"/api/games/{game}/report", json={"player": "carol", "text": "who am I"}
    )
    assert response.status_code == 404
