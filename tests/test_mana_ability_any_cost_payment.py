"""Any mana ability, whatever its cost, named as a payment source (CR 605.3a).

CR 605.3a: "A player may activate an activated mana ability whenever they have
priority, whenever they are casting a spell or activating an ability that
requires a mana payment, or whenever a rule or effect asks for a mana
payment". A bare `{T}` source is tapped (`CostPayment.tap_for_mana`); any other
cost - Vivi Ornitier's `{0}`, Rakdos Signet's `{1}, {T}`, Lotus Petal's `{T},
Sacrifice this artifact` - used to be refused in the middle of a payment and
had to be activated first. Now the engine activates it in full while paying
(`CostPayment.activate_mana_ability`), refusing in its own words when the
ability's cost cannot be paid.

Every front end reaches it: REST names it as `{card_id, ability_index,
colors}`; the seat CLI's `--sources` and the MCP tools' `mana_sources` name it
as `<card id>:<ability index>[:<colours>]`; the play UI pins such a source with
its ability index (`ui/app.js` `pinStep`).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from conftest import add_card, find, resolve_whole_stack, state
from fastapi.testclient import TestClient
from test_audit_log import entry_of, log_of
from test_decision_kinds import (
    seats_for,
    stage_mystical_dispute_targeting_bears,
    wait_for_pending,
)
from test_front_end_parity import uid_for, wire

from magic_cards_model_api import seat_cli
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats, parse_mana_sources


def battlefield_id(client: TestClient, game: str, name: str) -> str:
    return find(state(client, game)["battlefield"], name)["id"]


def mana_index(client: TestClient, game: str, name: str) -> int:
    card = find(state(client, game)["battlefield"], name)
    return next(ability["index"] for ability in card["abilities"] if ability["is_mana_ability"])


def vivi_with_power(client: TestClient, game: str, power: int) -> str:
    add_card(client, game, "Vivi Ornitier", "alice", "battlefield")
    vivi_id = battlefield_id(client, game, "Vivi Ornitier")
    response = client.patch(
        f"/api/games/{game}/cards/{vivi_id}", json={"counters": {"+1/+1": power}}
    )
    assert response.status_code == 200, response.text
    return vivi_id


def cast(client: TestClient, game: str, card: str, sources: list[Any], **extra: Any) -> dict:
    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": card, "mana_sources": sources, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


def pool(client: TestClient, game: str) -> dict[str, int]:
    player = next(p for p in state(client, game)["players"] if p["username"] == "alice")
    return dict(player["mana_pool"]["floating"])


def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result.content[0].text)


# -- REST -----------------------------------------------------------------------


def test_vivis_zero_cost_ability_pays_for_a_spell(client, game) -> None:
    vivi = vivi_with_power(client, game, 2)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    index = mana_index(client, game, "Vivi Ornitier")

    body = cast(
        client,
        game,
        "Lightning Bolt",
        [{"card_id": vivi, "ability_index": index, "colors": ["R", "U"]}],
        targets=["bob"],
    )

    assert body["allowed"] is True, body
    assert pool(client, game) == {"U": 1}
    assert [entry["card_id"] for entry in body["tapped"]] == [vivi]
    assert entry_of(log_of(client, game), "cast")["detail"]["mana_sources"] == [vivi]


def test_a_dry_run_leaves_vivis_once_each_turn_unused(client, game) -> None:
    vivi = vivi_with_power(client, game, 1)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    index = mana_index(client, game, "Vivi Ornitier")
    source = {"card_id": vivi, "ability_index": index, "colors": ["R"]}

    dry = cast(client, game, "Lightning Bolt", [source], targets=["bob"], dry_run=True)
    assert dry["allowed"] is True, dry

    real = cast(client, game, "Lightning Bolt", [source], targets=["bob"])
    assert real["allowed"] is True, real


def test_the_engine_refuses_an_unpayable_activation_in_its_own_words(client, game) -> None:
    """Vivi already activated this turn: "only once each turn" refuses the
    second activation, and the cast with it; Lightning Bolt stays in hand."""
    vivi = vivi_with_power(client, game, 1)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    index = mana_index(client, game, "Vivi Ornitier")
    first = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": vivi, "ability_index": index, "colors": ["U"]},
    )
    assert first.status_code == 200 and first.json()["allowed"] is True, first.text

    body = cast(
        client,
        game,
        "Lightning Bolt",
        [{"card_id": vivi, "ability_index": index, "colors": ["R"]}],
        targets=["bob"],
    )

    assert body["allowed"] is False, body
    assert "already been activated this turn" in body["reason"], body
    assert find(state(client, game)["hands"]["alice"], "Lightning Bolt")


def test_a_mana_ability_with_a_mana_cost_spends_mana_already_paid_in(client, game) -> None:
    """Rakdos Signet's `{1}, {T}`: the Mountain named before it pays its {1},
    and the Signet's {B}{R} pays Steelclaw Lance."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Rakdos Signet", "alice", "battlefield")
    add_card(client, game, "Steelclaw Lance", "alice", "hand")
    mountain = battlefield_id(client, game, "Mountain")
    signet = battlefield_id(client, game, "Rakdos Signet")
    index = mana_index(client, game, "Rakdos Signet")

    body = cast(
        client,
        game,
        "Steelclaw Lance",
        [{"card_id": mountain, "colors": ["R"]}, {"card_id": signet, "ability_index": index}],
    )

    assert body["allowed"] is True, body
    battlefield = state(client, game)["battlefield"]
    assert find(battlefield, "Rakdos Signet")["tapped"] is True
    assert find(battlefield, "Mountain")["tapped"] is True
    assert pool(client, game) == {}


# -- the seat CLI and the MCP tools ----------------------------------------------


def test_the_source_token_grammar() -> None:
    assert parse_mana_sources("a1, b2:0:URR ,c3:1") == [
        "a1",
        {"card_id": "b2", "ability_index": 0, "colors": ["U", "R", "R"]},
        {"card_id": "c3", "ability_index": 1},
    ]
    assert parse_mana_sources('["a1", "b2:0:R"]') == [
        "a1",
        {"card_id": "b2", "ability_index": 0, "colors": ["R"]},
    ]


def test_the_cli_names_vivis_ability_as_a_source(client, game, monkeypatch, capsys) -> None:
    wire(monkeypatch)
    vivi = vivi_with_power(client, game, 1)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    index = mana_index(client, game, "Vivi Ornitier")

    argv = [
        "--uid",
        uid_for(game, "alice"),
        "--game",
        game,
        "cast",
        "Lightning Bolt",
        "--target",
        "bob",
        "--sources",
        f"{vivi}:{index}:R",
        "--hold",
    ]
    assert seat_cli.run(argv) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"]["allowed"] is True, printed
    assert pool(client, game) == {}


def test_the_mcp_cast_names_vivis_ability_as_a_source(client, game, monkeypatch) -> None:
    wire(monkeypatch)
    vivi = vivi_with_power(client, game, 2)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    index = mana_index(client, game, "Vivi Ornitier")
    server = build_server(Seats(api="http://testserver", game_id=game))

    answer = call_tool(
        server,
        "cast",
        {
            "player_uid": uid_for(game, "alice"),
            "card": "Lightning Bolt",
            "targets": "bob",
            "mana_sources": f"{vivi}:{index}:RU",
            "hold_priority": True,
        },
    )

    assert answer["outcome"]["allowed"] is True, answer
    assert pool(client, game) == {"U": 1}


# -- CR 608.2g: a pay_mana decision ("unless its controller pays {3}") --------


def _vivi_against_mystical_dispute(client, game) -> tuple[dict, str, int]:
    vivi = vivi_with_power(client, game, 3)
    index = mana_index(client, game, "Vivi Ornitier")
    uids = seats_for(client, game)
    stage_mystical_dispute_targeting_bears(client, game, {})
    # Both pass in succession (CR 117.4): Mystical Dispute resolves and asks
    # alice the "unless its controller pays {3}" question - the real route,
    # whose resumed command surfaces an engine refusal of the answer.
    for player in ("bob", "alice"):
        passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": player})
        assert passed.status_code == 200, passed.text
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "pay_mana"
    assert vivi not in pending["mana_sources"]
    return uids, vivi, index


def test_a_pay_mana_decision_is_paid_with_vivis_zero_cost_ability(client, game) -> None:
    uids, vivi, index = _vivi_against_mystical_dispute(client, game)

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "accept": True,
            "mana_sources": [{"card_id": vivi, "ability_index": index, "colors": ["U", "R", "G"]}],
        },
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is False, answer.json()

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "accept": True,
            "mana_sources": [{"card_id": vivi, "ability_index": index, "colors": ["U", "R", "R"]}],
        },
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["accepted"] is True, answer.json()
    resolve_whole_stack(client, game)

    final = state(client, game)
    assert find(final["battlefield"], "Grizzly Bears")["zone"] == "battlefield", "paid"
    assert pool(client, game) == {}


def test_the_mcp_decide_names_vivis_ability_for_a_pay_mana_decision(
    client, game, monkeypatch
) -> None:
    wire(monkeypatch)
    uids, vivi, index = _vivi_against_mystical_dispute(client, game)
    server = build_server(Seats(api="http://testserver", game_id=game))

    answer = call_tool(
        server,
        "decide",
        {
            "player_uid": uid_for(game, "alice"),
            "accept": True,
            "mana_sources": f"{vivi}:{index}:URR",
        },
    )

    assert answer["outcome"]["accepted"] is True, answer
