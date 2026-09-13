"""`ASSIGN_COMBAT_DAMAGE` (CR 703.4k, 510.1c, 702.19b) through the real routes:
no fake engine, no scripted answer - a real double block behind a real
trampler, played through `actions/attack`, `actions/declare-blockers` and
`decide` exactly the way a live seat would.

Every card here is a real Scryfall printing already cached in
`tests/fixtures/scryfall` (`conftest.py`'s `MCM_CACHE_DIR` - the network is
never touched): Lupine Harbingers (4/4, trample and haste, so it can attack
the turn it enters and needs no summoning-sickness patch), blocked by
Grizzly Bears (2/2) and Elvish Mystic (1/1) - CR 510.1c's real trigger for
this decision is two-or-more blockers, no trample excess required, but this
board gives both at once: 2 (Bears) + 1 (Mystic) = 3 lethal, one short of
the attacker's 4 power, so the excess only reaches bob with trample (CR
702.19b). Their combined 2 + 1 = 3 power hits back at Harbingers, short of
its own 4 toughness, so it lives to show the marked damage.
"""

from __future__ import annotations

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def _boot_double_block(client: TestClient, game: str) -> dict[str, str]:
    """Alice's Lupine Harbingers (4/4 trample, haste) attacking, blocked by
    Bob's Grizzly Bears (2/2) and Elvish Mystic (1/1) - paused right at the
    `assign_combat_damage` question, CR 510.1c's own trigger (two blockers)
    doubled up with CR 702.19b's (trample beyond their combined lethal: 4
    power vs. 2+1). Returns the three cards' ids."""
    harbingers = add_card(client, game, "Lupine Harbingers", "alice", "battlefield")["added"][
        "card_id"
    ]
    bears = add_card(client, game, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    mystic = add_card(client, game, "Elvish Mystic", "bob", "battlefield")["added"]["card_id"]
    uids = seats_for(game)

    client.put(f"/api/games/{game}/step", json={"step": "beginning_of_combat"})
    declare_attackers = client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    assert declare_attackers.json()["pending_decision"]["kind"] == "declare_attackers"

    attack = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": harbingers, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True, attack.json()

    declare_blockers = client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    assert declare_blockers.json()["pending_decision"]["kind"] == "declare_blockers"

    blocks = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={
            "player": "bob",
            "blocks": [
                {"blocker": bears, "blocked": harbingers},
                {"blocker": mystic, "blocked": harbingers},
            ],
        },
    )
    blocks_body = blocks.json()
    assert blocks_body.get("allowed", blocks_body.get("accepted")) is True, blocks_body

    paused = client.post(
        f"/api/games/{game}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    body = paused.json()
    assert body["paused"] is True, body
    assert body["pending_decision"]["kind"] == "assign_combat_damage", body
    return {"harbingers": harbingers, "bears": bears, "mystic": mystic}


def test_double_block_with_trample_pauses_with_the_right_shape(
    client: TestClient, game: str
) -> None:
    ids = _boot_double_block(client, game)
    uids = seats_for(game)

    pending = state(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "assign_combat_damage"
    assert pending["player"] == "alice"
    assert pending["amount"] == 4
    assert pending["trample"] is True
    assert pending["cards"][0] == ids["harbingers"]
    assert set(pending["cards"][1:]) == {ids["bears"], ids["mystic"]}

    targets_by_id = {t["id"]: t for t in pending["targets"]}
    assert targets_by_id[ids["bears"]]["kind"] == "card"
    assert targets_by_id[ids["mystic"]]["kind"] == "card"
    assert targets_by_id["bob"]["kind"] == "player"

    lethal_by_id = dict(
        zip((t["id"] for t in pending["targets"]), pending["lethal_damage"], strict=True)
    )
    assert lethal_by_id[ids["bears"]] == 2  # Grizzly Bears is a 2/2
    assert lethal_by_id[ids["mystic"]] == 1  # Elvish Mystic is a 1/1
    assert lethal_by_id["bob"] == 0

    # The decision names the call that answers it, `damage` field included.
    assert pending["answer_field"] == "damage"
    assert "damage=" in pending["answer_with"]


def test_an_illegal_assignment_is_refused_and_the_decision_stays_pending(
    client: TestClient, game: str
) -> None:
    """CR 702.19b: trampling past a blocker that has not been assigned its
    own lethal damage is refused - the excess (1) reaching bob while the
    Bears are short of their own lethal (2). A second attempt then shows
    CR 510.1a's own floor: the amounts must sum to exactly the attacker's
    power, not merely to no more than it."""
    ids = _boot_double_block(client, game)
    uids = seats_for(game)

    illegal = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "damage": [
                {"target": ids["bears"], "amount": 1},
                {"target": ids["mystic"], "amount": 1},
                {"target": "bob", "amount": 2},
            ],
        },
    )
    body = illegal.json()
    assert body["accepted"] is False, body
    assert "702.19b" in body["reason"], body

    still_pending = state(client, game, uids["alice"])["pending_decision"]
    assert still_pending is not None and still_pending["kind"] == "assign_combat_damage"

    bad_total = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "damage": [
                {"target": ids["bears"], "amount": 2},
                {"target": ids["mystic"], "amount": 1},
            ],
        },
    )
    bad_body = bad_total.json()
    assert bad_body["accepted"] is False, bad_body
    assert "510.1a" in bad_body["reason"], bad_body


def test_the_legal_assignment_lands_on_the_board(client: TestClient, game: str) -> None:
    """Bears get their 2, Mystic its 1, and the 1 excess tramples to bob (CR
    702.19b) - state-based actions then destroy both lethally damaged
    blockers (CR 704.5g), bob's life falls by exactly the trampled 1 (not the
    whole 4 power), and Harbingers survives with the 2+1=3 damage the two
    blockers dealt back marked on it (CR 510.2, short of its own toughness)."""
    ids = _boot_double_block(client, game)
    uids = seats_for(game)

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "damage": [
                {"target": ids["bears"], "amount": 2},
                {"target": ids["mystic"], "amount": 1},
                {"target": "bob", "amount": 1},
            ],
        },
    )
    body = answered.json()
    assert body["accepted"] is True, body

    board = state(client, game)
    bob_life = next(p["life"] for p in board["players"] if p["username"] == "bob")
    assert bob_life == 19, board

    battlefield_names = [c["name"] for c in board["battlefield"]]
    assert "Grizzly Bears" not in battlefield_names, battlefield_names
    assert "Elvish Mystic" not in battlefield_names, battlefield_names
    harbingers_card = find(board["battlefield"], "Lupine Harbingers")
    assert harbingers_card["damage"] == 3  # 2 (Bears) + 1 (Mystic) marked back
