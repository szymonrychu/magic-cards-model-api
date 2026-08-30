"""The running game: attackers, state-based actions, land drops, advancing."""

from conftest import add_card, find, state
from fastapi.testclient import TestClient


def cast_and_resolve_bears(client: TestClient, game: str) -> str:
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    board = state(client, game)
    forest = find(board["battlefield"], "Forest")
    mountain = find(board["battlefield"], "Mountain")
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Grizzly Bears",
            "mana_sources": [{"card_id": mountain["id"]}, {"card_id": forest["id"]}],
        },
    )
    assert cast.json()["allowed"] is True, cast.json()
    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Grizzly Bears"
    return find(state(client, game)["battlefield"], "Grizzly Bears")["id"]


def test_declare_attackers(client, game):
    bears = cast_and_resolve_bears(client, game)

    early = client.post(
        f"/api/games/{game}/actions/attack",
        json={"attackers": [{"card_id": bears, "defender": "bob"}], "dry_run": True},
    )
    assert early.json()["allowed"] is False
    assert "summoning sickness" in early.json()["reason"]

    client.patch(f"/api/games/{game}/cards/{bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert state(client, game)["step"] == "declare_attackers"

    attack = client.post(
        f"/api/games/{game}/actions/attack",
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    attack_body = attack.json()
    assert isinstance(attack_body.pop("log_seq"), int)  # the audit entry's own idempotency marker
    assert attack_body == {"allowed": True, "reason": None, "attackers": 1}
    board = state(client, game)
    assert len(board["combat"]["attacks"]) == 1
    assert board["combat"]["attacks"][0]["defender"] == "bob"
    assert find(board["battlefield"], "Grizzly Bears")["tapped"] is True


def test_state_based_actions_kill_a_lethal_creature(client, game):
    bears = cast_and_resolve_bears(client, game)
    client.patch(
        f"/api/games/{game}/cards/{bears}",
        json={"counters": {"-1/-1": 2, "+1/+1": 1}, "damage": 2},
    )
    result = client.post(f"/api/games/{game}/state-based-actions").json()
    assert result["changed"] == ["Grizzly Bears"]
    board = state(client, game)
    assert [card["name"] for card in board["battlefield"]] == ["Forest", "Mountain"]
    assert [card["name"] for card in board["graveyards"]["alice"]] == ["Grizzly Bears"]


def test_state_based_actions_end_a_lost_player(client, game):
    client.patch(f"/api/games/{game}/players/bob", json={"life": 0})
    client.post(f"/api/games/{game}/state-based-actions")
    board = state(client, game)
    assert board["winner"] == "alice"


def test_play_land_and_land_drop_limit(client, game):
    add_card(client, game, "Mountain", "alice", "hand")
    first = client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "alice", "card": "Mountain"}
    )
    assert first.json()["allowed"] is True
    assert [card["name"] for card in state(client, game)["battlefield"]] == ["Mountain"]

    add_card(client, game, "Mountain", "alice", "hand")
    second = client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "alice", "card": "Mountain"}
    )
    assert second.json()["allowed"] is False
    # The refusal names the numbers since the CR 305.2a comparison landed.
    assert second.json()["reason"] == (
        "alice cannot play a land: 1 of the 1 they may play this turn "
        "already used (CR 305.2a, 305.2b)"
    )


def test_play_land_by_a_name_that_matches_nothing_is_refused_not_raised(client, game):
    """A name that resolves to no land in hand used to raise a bare
    `ValueError` before `action_play_land` ever reached `audit_outcome` - a
    400 shaped `{"detail": "..."}`, not the `{"allowed": False, ...}` every
    other refusal carries. `seatkit._refused` only recognises the latter, so
    that response looked like nothing happened at all to `run_action`, which
    then went on to release priority as if the play had gone through - see
    `test_action_flow.py` for that half. This is the endpoint half: the
    refusal must come back in the ordinary shape, `allowed: False` included.
    """
    add_card(client, game, "Mountain", "alice", "hand")
    response = client.post(
        f"/api/games/{game}/actions/play-land",
        json={"player": "alice", "card": "Not A Real Card"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is False
    assert "no land named Not A Real Card" in body["reason"]
    # Nothing was played and the land drop is untouched.
    assert [card["name"] for card in state(client, game)["battlefield"]] == []


def test_advance_walks_the_turn(client, game):
    client.put(f"/api/games/{game}/step", json={"step": "precombat_main"})
    steps = []
    for _ in range(3):
        response = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
        steps.append(response.json()["advanced_to"])
    # No attacker was declared, so the engine skips declare blockers and
    # combat damage entirely: the third advance lands on end of combat.
    assert steps == ["beginning_of_combat", "declare_attackers", "end_of_combat"]


def test_advance_keeps_combat_steps_after_a_declaration(client, game):
    bears = cast_and_resolve_bears(client, game)
    client.patch(f"/api/games/{game}/cards/{bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})  # declare_attackers step
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True
    advanced = [
        client.post(f"/api/games/{game}/advance", json={"player": "alice"}).json()["advanced_to"]
        for _ in range(3)
    ]
    # With an attack declared, the skipped steps happen.
    assert advanced == ["declare_blockers", "combat_damage", "end_of_combat"]


def test_target_check(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = state(client, game)["hands"]["alice"][0]
    allowed = client.post(
        f"/api/games/{game}/actions/target", json={"source_id": bolt["id"], "target": "bob"}
    )
    assert allowed.json()["allowed"] is True
    # and it checked without touching state
    assert [card["name"] for card in state(client, game)["hands"]["alice"]] == ["Lightning Bolt"]
