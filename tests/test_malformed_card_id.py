"""D15 (battle 1): a malformed or truncated card id is refused in words that
name the bad id and the format a card id has.

`seat.sh alice activate 5609c7e2 0` answered "Input should be a valid UUID,
invalid length: expected length 32 for simple format, found 8" - pydantic's
own text, which names neither the id nor where the full one is. Every card id
a seat can send reaches the API one of three ways, and each now gives the same
typed refusal:

- a request body or path field typed `UUID` (pydantic, `RequestValidationError`);
- a `decide` answer's ids, parsed in `decisions.py`;
- a seat CLI / MCP `card` argument that looks like an id fragment, which is
  sent as `card_id` instead of being looked up as a card name.

Every other validation error keeps its existing shape.
"""

from __future__ import annotations

import uuid

import httpx
from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_baseline_seat import battlefield_creature, into_declare_attackers
from test_decisions import advance_into_cleanup

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import card_reference
from magic_cards_model_api.store import store

SHORT = "5609c7e2"
FORMAT = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
PYDANTIC = "Input should be a valid UUID"


def _uids(game: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game).items()}


def _assert_typed(reason: str, bad: str = SHORT) -> None:
    assert f"'{bad}'" in reason, reason
    assert FORMAT in reason, reason
    assert PYDANTIC not in reason, reason
    assert "badly formed" not in reason, reason


def test_a_short_body_card_id_is_a_typed_refusal(client, game):
    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": SHORT, "ability_index": 0},
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["allowed"] is False, body
    _assert_typed(body["reason"])
    assert body["detail"] == [
        {"loc": ["body", "card_id"], "msg": body["reason"], "type": "malformed_card_id"}
    ], body


def test_a_bad_id_in_a_union_list_is_one_entry_not_two(client, game):
    """`mana_sources` is `list[UUID | ManaSource]`: pydantic reports the bad
    string once per union branch, and only the UUID branch is the seat's
    meaning."""
    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": str(uuid.uuid4()), "mana_sources": ["abc"]},
    )

    assert response.status_code == 422, response.text
    body = response.json()
    _assert_typed(body["reason"], "abc")
    assert [entry["loc"] for entry in body["detail"]] == [["body", "mana_sources", 0]], body


def test_a_short_path_card_id_is_a_typed_refusal(client, game):
    response = client.post(f"/api/games/{game}/cards/{SHORT}/tap", json={"tapped": True})

    assert response.status_code == 422, response.text
    _assert_typed(response.json()["reason"])


def test_every_other_validation_error_keeps_its_shape(client, game):
    response = client.post(
        f"/api/games/{game}/actions/activate", json={"player": "alice", "ability_index": 0}
    )

    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [{"loc": ["body", "card_id"], "msg": "Field required", "type": "missing"}]
    }


def test_a_short_id_in_a_decide_answer_is_a_typed_refusal(client, game):
    for _ in range(8):
        add_card(client, game, "Mountain", "alice", "hand")
    assert advance_into_cleanup(game) is not None
    uid = _uids(game)["alice"]

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uid},
        json={"player": "alice", "card_ids": [SHORT]},
    ).json()

    assert answer["accepted"] is False, answer
    _assert_typed(answer["reason"])


def test_card_reference_sends_an_id_fragment_as_an_id():
    full = str(uuid.uuid4())
    assert card_reference(full) == {"card_id": full}
    assert card_reference(f" {SHORT} ") == {"card_id": SHORT}
    assert card_reference(full[:23]) == {"card_id": full[:23]}
    assert card_reference("Lightning Bolt") == {"card": "Lightning Bolt"}
    assert card_reference("Witch-Blessed Meadow") == {"card": "Witch-Blessed Meadow"}
    assert card_reference("Fade") == {"card": "Fade"}


def _cli(uid: str, game: str, *argv: str, monkeypatch, capsys) -> str:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seat_cli.run(["--uid", uid, "--game", game, *argv])
    return capsys.readouterr().out


def test_the_seat_cli_activate_with_a_short_id_names_it(client, game, monkeypatch, capsys):
    printed = _cli(
        _uids(game)["alice"], game, "activate", SHORT, "0", monkeypatch=monkeypatch, capsys=capsys
    )
    _assert_typed(printed)


def test_the_seat_cli_cast_with_a_short_id_names_it(client, game, monkeypatch, capsys):
    printed = _cli(
        _uids(game)["alice"], game, "cast", SHORT, monkeypatch=monkeypatch, capsys=capsys
    )
    _assert_typed(printed)


def test_the_seat_cli_decide_with_a_short_id_names_it(client, game, monkeypatch, capsys):
    for _ in range(8):
        add_card(client, game, "Mountain", "alice", "hand")
    assert advance_into_cleanup(game) is not None

    printed = _cli(
        _uids(game)["alice"],
        game,
        "decide",
        SHORT,
        "--hold",
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    _assert_typed(printed)


# -- a short id where a target or a defender goes --------------------------
#
# A target or an attack's defender is "a player name or a card id". A token
# that is neither a seat's name nor a UUID is refused naming both halves, so a
# truncated id still hears the full form rather than "not a candidate player".


def _bolt_in_hand(client, game) -> tuple[str, str]:
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    return board["hands"]["alice"][0]["id"], find(board["battlefield"], "Mountain")["id"]


def test_a_short_id_as_a_cast_target_is_a_typed_refusal(client, game):
    bolt, mountain = _bolt_in_hand(client, game)

    body = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": bolt, "targets": [SHORT], "mana_sources": [mountain]},
    ).json()

    assert body["allowed"] is False, body
    _assert_typed(body["reason"])


def test_a_short_id_as_an_activate_target_is_a_typed_refusal(client, game):
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    karn = find(state(client, game)["battlefield"], "Karn, the Great Creator")["id"]

    body = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": karn, "ability_index": 0, "targets": [SHORT]},
    ).json()

    assert body["allowed"] is False, body
    _assert_typed(body["reason"])


def test_the_seat_cli_cast_with_a_short_target_names_it(client, game, monkeypatch, capsys):
    bolt, _ = _bolt_in_hand(client, game)

    printed = _cli(
        _uids(game)["alice"],
        game,
        "cast",
        bolt,
        "--target",
        SHORT,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    _assert_typed(printed)


def test_a_short_id_in_a_decide_targets_answer_is_a_typed_refusal(client, game):
    add_card(client, game, "Karn, the Great Creator", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "alice", "battlefield")
    karn = find(state(client, game)["battlefield"], "Karn, the Great Creator")["id"]
    paused = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": karn, "ability_index": 0},
    ).json()
    assert paused.get("paused") is True, paused
    assert paused["pending_decision"]["kind"] == "choose_targets", paused

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": _uids(game)["alice"]},
        json={"player": "alice", "targets": [SHORT]},
    ).json()

    assert answer["accepted"] is False, answer
    _assert_typed(answer["reason"])


def test_a_short_defender_id_in_a_decide_pairs_answer_is_a_typed_refusal(client, game):
    attacker = battlefield_creature(client, game, "Savannah Lions", "alice")
    into_declare_attackers(client, game, "alice")

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": _uids(game)["alice"]},
        json={"player": "alice", "pairs": [{"creature": attacker, "defender": SHORT}]},
    ).json()

    assert answer["accepted"] is False, answer
    _assert_typed(answer["reason"])


def test_a_short_defender_id_in_an_attack_is_a_typed_refusal(client, game):
    attacker = battlefield_creature(client, game, "Savannah Lions", "alice")
    into_declare_attackers(client, game, "alice")

    body = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": _uids(game)["alice"]},
        json={"attackers": [{"card_id": attacker, "defender": SHORT}]},
    ).json()

    assert body["allowed"] is False, body
    _assert_typed(body["reason"])


def test_a_misspelt_player_defender_still_names_both_forms(client, game):
    """A token that is neither a seat's name nor an id might have meant
    either; the refusal says both, so a name typo is not misread as an id."""
    attacker = battlefield_creature(client, game, "Savannah Lions", "alice")
    into_declare_attackers(client, game, "alice")

    body = client.post(
        f"/api/games/{game}/actions/attack",
        params={"uid": _uids(game)["alice"]},
        json={"attackers": [{"card_id": attacker, "defender": "bbo"}]},
    ).json()

    assert body["allowed"] is False, body
    assert "player name" in body["reason"], body
    _assert_typed(body["reason"], "bbo")
