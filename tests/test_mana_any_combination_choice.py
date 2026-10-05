"""A seat choosing the split of "Add N mana in any combination of <colours>"
by hand (CR 106.1a, CR 605.3b).

The engine reads the phrase family - a printed palette ("{U} and/or {R}") as
well as "colors" - as N mana whose colours are each chosen; this is the api
half: the board view says how many colours to name (`mana_choice_count`) out
of which (`mana_options`), and both a direct activation (`colors` on the
activate request) and a named mana source (`ManaSource.colors`) take the
split.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from conftest import add_card, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines.turn import Step

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def _vivi_with_power(client, game, power):
    add_card(client, game, "Vivi Ornitier", "alice", "battlefield")
    vivi = find(state(client, game)["battlefield"], "Vivi Ornitier")
    response = client.patch(
        f"/api/games/{game}/cards/{vivi['id']}", json={"counters": {"+1/+1": power}}
    )
    assert response.status_code == 200, response.text
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return find(state(client, game)["battlefield"], "Vivi Ornitier")


def _mana_ability(card):
    (ability,) = [ability for ability in card["abilities"] if ability["is_mana_ability"]]
    return ability


def _pool(client, game):
    return next(p for p in state(client, game)["players"] if p["username"] == "alice")["mana_pool"]


def test_the_board_view_names_the_palette_and_one_colour_per_mana(client, game):
    """Vivi Ornitier at power 3: "Add X mana in any combination of {U}
    and/or {R}" offers blue and red, three colours to name."""
    vivi = _vivi_with_power(client, game, 3)
    ability = _mana_ability(vivi)

    assert sorted(ability["mana_options"]) == ["R", "U"], ability
    assert ability["mana_choice_count"] == 3, ability


def test_an_activation_takes_the_split_one_colour_per_mana(client, game):
    """`colors` on the activate request is the split: one blue, two red."""
    vivi = _vivi_with_power(client, game, 3)
    ability = _mana_ability(vivi)

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": vivi["id"],
            "ability_index": ability["index"],
            "colors": ["U", "R", "R"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True, response.json()
    assert _pool(client, game)["floating"] == {"U": 1, "R": 2}


def test_a_split_outside_the_palette_is_refused_in_the_engines_words(client, game):
    """Green is not among "{U} and/or {R}": refused, nothing added."""
    vivi = _vivi_with_power(client, game, 2)
    ability = _mana_ability(vivi)

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": vivi["id"],
            "ability_index": ability["index"],
            "colors": ["U", "G"],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is False, body
    assert "{G}" in body["reason"], body
    assert _pool(client, game)["floating"] == {}


def test_color_and_colors_together_are_refused(client, game):
    """One choice, one field: naming both is ambiguous and refused."""
    vivi = _vivi_with_power(client, game, 2)
    ability = _mana_ability(vivi)

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": vivi["id"],
            "ability_index": ability["index"],
            "color": "U",
            "colors": ["U", "R"],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is False, response.json()


def test_a_named_source_pays_with_a_counted_split(client, game):
    """Axebane Guardian beside a Fog Bank adds two mana in any combination;
    named with `ManaSource.colors` as {R}{W}, it alone pays Lightning Helix."""
    add_card(client, game, "Axebane Guardian", "alice", "battlefield")
    add_card(client, game, "Fog Bank", "alice", "battlefield")
    add_card(client, game, "Lightning Helix", "alice", "hand")
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    guardian = find(state(client, game)["battlefield"], "Axebane Guardian")
    response = client.patch(
        f"/api/games/{game}/cards/{guardian['id']}", json={"summoning_sick": False}
    )
    assert response.status_code == 200, response.text
    ability = _mana_ability(find(state(client, game)["battlefield"], "Axebane Guardian"))
    assert ability["mana_choice_count"] == 2, ability

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Lightning Helix",
            "targets": ["bob"],
            "mana_sources": [
                {"card_id": guardian["id"], "colors": ["R", "W"], "ability_index": ability["index"]}
            ],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True, response.json()


# -- the MCP tool surface (what an AI seat actually calls) ------------------


def test_mcp_activate_tool_takes_the_split(client, game, monkeypatch):
    """An AI seat names the split through `activate`'s `color`, one letter
    per mana, comma separated - the HTTP endpoint's `colors`."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game)
    uids = seats_for(game)
    vivi = _vivi_with_power(client, game, 2)
    ability = _mana_ability(vivi)
    server = build_server(seats)

    result = asyncio.run(
        server.call_tool(
            "activate",
            {
                "player_uid": uids["alice"],
                "card_id": vivi["id"],
                "ability_index": ability["index"],
                "color": "U,R",
            },
        )
    )

    answer = json.loads(result.content[0].text)
    assert answer["outcome"]["allowed"] is True, answer
    assert _pool(client, game)["floating"] == {"U": 1, "R": 1}


def test_seat_cli_activate_takes_the_split(client, game, monkeypatch, capsys):
    """The seat CLI's `activate --colors U,R` is the same split."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    uids = seats_for(game)
    vivi = _vivi_with_power(client, game, 2)
    ability = _mana_ability(vivi)

    exit_code = seat_cli.run(
        [
            "--uid",
            uids["alice"],
            "--game",
            game,
            "activate",
            vivi["id"],
            str(ability["index"]),
            "--colors",
            "U,R",
        ]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"]["allowed"] is True, printed
    assert _pool(client, game)["floating"] == {"U": 1, "R": 1}
