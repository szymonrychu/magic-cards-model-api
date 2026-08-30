"""Seat uids: the credential that claims a player's seat in the single toolset.

A uid is minted when the game is created, handed to whoever dispatches the
players, and is the only way to resolve a seat: an unknown uid is nobody's,
and no state view ever leaks the uids.
"""

from conftest import state


def test_game_creation_mints_one_uid_per_player(client, game):
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    uids = created["seat_uids"]
    assert sorted(uids) == ["alice", "bob"]
    assert len(set(uids.values())) == 2, "each player's uid is distinct"

    board = state(client, created["game_id"])
    assert "seat_uids" not in board, "uids never appear in a state view"


def test_a_uid_resolves_to_its_player_and_only_that_player(client, game):
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    uid = created["seat_uids"]["alice"]

    answer = client.get(f"/api/games/{created['game_id']}/seat", params={"uid": uid})
    assert answer.status_code == 200
    assert answer.json() == {"player": "alice"}

    wrong = client.get(f"/api/games/{created['game_id']}/seat", params={"uid": "not-a-uid"})
    assert wrong.status_code == 404


def test_a_uid_from_one_game_opens_no_seat_in_another(client, game):
    other = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    foreign_uid = other["seat_uids"]["alice"]

    answer = client.get(f"/api/games/{game}/seat", params={"uid": foreign_uid})
    assert answer.status_code == 404, "uids are scoped to the game that minted them"
