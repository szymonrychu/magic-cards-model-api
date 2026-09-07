"""The running game: attackers, state-based actions, land drops, advancing."""

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def advance_collecting_steps(client: TestClient, game: str, count: int) -> list[str]:
    """`POST .../advance`, `count` genuine step transitions' worth -
    transparently answering any `declare_attackers`/`declare_blockers` pause
    met on the way with "nobody" (CR 508.1/CR 509.1's "if any", always
    legal), the same idiom `test_decisions.py`'s `advance_into_cleanup` uses:
    "declare nobody" is no longer a free, silent default, so a creatureless
    - or simply undeclared - board still pauses there and has to be answered
    before `advance` can complete into the next real step.

    `decide`'s own response never echoes the resumed command's outcome (just
    `accepted`/`log_seq`), so the step landed in is read back off state
    rather than out of that response.
    """
    uids = seats_for(game)
    steps: list[str] = []
    while len(steps) < count:
        response = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
        assert response.status_code == 200, response.text
        body = response.json()
        if body.get("paused"):
            pending = body["pending_decision"]
            assert pending["kind"] in ("declare_attackers", "declare_blockers"), pending
            decided = client.post(
                f"/api/games/{game}/decide",
                params={"uid": uids[pending["player"]]},
                json={"player": pending["player"], "pairs": []},
            )
            decided_body = decided.json()
            assert decided.status_code == 200 and decided_body["accepted"] is True, decided_body
            assert not decided_body.get("paused"), decided_body
            steps.append(state(client, game)["step"])
            continue
        steps.append(body["advanced_to"])
    return steps


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
    advance = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    advance_body = advance.json()
    # `advance` now pauses on CR 508.1's own declare_attackers question rather
    # than completing into the step - the committed board this response shows
    # is still `beginning_of_combat`, the step `advance` was called from (see
    # its own docstring), not the one it is trying to enter.
    assert advance_body["paused"] is True, advance_body
    assert advance_body["pending_decision"]["kind"] == "declare_attackers"

    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": seats_for(game)["alice"]},
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    attack_body = attack.json()
    assert isinstance(attack_body.pop("log_seq"), int)  # the audit entry's own idempotency marker
    # `_answer_declaration`'s success shape is deliberately thin now that it
    # is a thin alias over the pause/resume tape, not its own transport.
    assert attack_body == {"allowed": True}
    board = state(client, game)
    assert board["step"] == "declare_attackers"
    assert len(board["combat"]["attacks"]) == 1
    assert board["combat"]["attacks"][0]["defender"] == "bob"
    assert find(board["battlefield"], "Grizzly Bears")["tapped"] is True


def test_the_opponents_uid_cannot_declare_your_attackers(client, game):
    """`actions/attack` and `actions/declare-blockers` are thin aliases over
    the same paused tape `decide` resumes (`_answer_declaration`'s own
    docstring), and `decide` refuses unless `uid` opens the *deciding*
    player's own seat (CR 608.2d's "the player announces" - nobody else may
    make this choice for them). Before this, `actions/attack` and
    `actions/declare-blockers` enforced nothing at all: any uid, or no uid,
    got the same answer a live seat did - the two doors onto the same tape
    disagreed about who could walk through. This pins that they now agree.
    """
    bears = cast_and_resolve_bears(client, game)
    client.patch(f"/api/games/{game}/cards/{bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    paused = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert paused.json()["pending_decision"]["kind"] == "declare_attackers"

    uids = seats_for(game)

    # bob's own uid does not open alice's declare_attackers decision.
    wrong_seat = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["bob"]},
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    assert wrong_seat.json()["allowed"] is False
    assert "does not open the deciding player's seat" in wrong_seat.json()["reason"]

    # No uid at all is refused the same way - not silently accepted, and not
    # silently attributed to whichever seat the caller happened to claim to
    # be in the request body (this route takes no `player` field at all).
    no_uid = client.post(
        f"/api/games/{game}/actions/attack",
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    assert no_uid.json()["allowed"] is False
    assert "does not open the deciding player's seat" in no_uid.json()["reason"]

    # The decision is still genuinely pending after both refusals - neither
    # touched the tape - and alice's own uid still answers it for real.
    still_pending = state(client, game)["pending_decision"]
    assert still_pending is not None and still_pending["kind"] == "declare_attackers"

    real = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    assert real.json()["allowed"] is True, real.json()


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
    steps = advance_collecting_steps(client, game, 3)
    # No attacker was declared, so the engine skips declare blockers and
    # combat damage entirely: the third advance lands on end of combat.
    assert steps == ["beginning_of_combat", "declare_attackers", "end_of_combat"]


def test_advance_keeps_combat_steps_after_a_declaration(client, game):
    bears = cast_and_resolve_bears(client, game)
    client.patch(f"/api/games/{game}/cards/{bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    into_declare_attackers = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert into_declare_attackers.json()["paused"] is True, into_declare_attackers.json()
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": seats_for(game)["alice"]},
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True
    # With an attacker declared, bob still has to answer CR 509.1's own
    # declare_blockers question - "declares no creatures blocking" is legal
    # but no longer a free default - before the skipped steps happen.
    advanced = advance_collecting_steps(client, game, 3)
    assert advanced == ["declare_blockers", "combat_damage", "end_of_combat"]


def test_the_opponents_uid_cannot_declare_your_blockers(client, game):
    """`actions/declare-blockers`'s own half of `test_the_opponents_uid_
    cannot_declare_your_attackers` above."""
    bears = cast_and_resolve_bears(client, game)
    client.patch(f"/api/games/{game}/cards/{bears}", json={"summoning_sick": False})
    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    uids = seats_for(game)
    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": bears, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True
    paused = client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    assert paused.json()["pending_decision"]["kind"] == "declare_blockers"

    # alice's own uid does not open bob's declare_blockers decision.
    wrong_seat = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["alice"]},
        json={"player": "bob", "blocks": []},
    )
    assert wrong_seat.json()["allowed"] is False
    assert "does not open the deciding player's seat" in wrong_seat.json()["reason"]

    still_pending = state(client, game)["pending_decision"]
    assert still_pending is not None and still_pending["kind"] == "declare_blockers"

    real = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": []},
    )
    assert real.json()["allowed"] is True, real.json()


def test_target_check(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = state(client, game)["hands"]["alice"][0]
    allowed = client.post(
        f"/api/games/{game}/actions/target", json={"source_id": bolt["id"], "target": "bob"}
    )
    assert allowed.json()["allowed"] is True
    # and it checked without touching state
    assert [card["name"] for card in state(client, game)["hands"]["alice"]] == ["Lightning Bolt"]
