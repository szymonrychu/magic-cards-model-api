"""Kicker, and the engine's other optional additional costs, through the api.

Battle 2, B2-06. CR 702.33a: "'Kicker [cost]' means 'You may pay an additional
[cost] as you cast this spell.'" The engine has always charged it
(`Casting.pay_kicker`, and its siblings `pay_replicate`, `pay_offspring`,
`pay_blight`, `pay_gift`), and CR 702.33d is what "if it was kicked" reads
afterwards. The api offered no way to say "kick it": Sowing Mycospawn's only
row was `cast {3}{G}`, and `CastRequest` had no field for the choice, so the
exile-a-land half of that card and every kicker card in the pool could never
be chosen by a seat.

CR 601.2b is where the choice is announced: "If the spell has alternative or
additional costs that will be paid as it's being cast such as buyback or
kicker costs ..., the player announces their intentions to pay any or all of
those costs." Real printings, real boards, the real API.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from conftest import add_card, find, life_of, state
from fastapi.testclient import TestClient
from test_legal_actions_endpoint import legal_actions

from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api import seat_cli, seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def mountains(client, game: str, count: int) -> list[str]:
    for _ in range(count):
        add_card(client, game, "Mountain", "alice", "battlefield")
    return [card["id"] for card in state(client, game)["battlefield"] if card["name"] == "Mountain"]


def cast_row(client, game: str, name: str) -> dict:
    card = find(state(client, game)["hands"]["alice"], name)
    (row,) = [
        row
        for row in legal_actions(client, game)
        if row["kind"] == "cast" and row["card_id"] == card["id"]
    ]
    return row


def cast(client, game: str, card_id: str, **body) -> dict:
    response = client.post(
        f"/api/games/{game}/actions/cast", json={"player": "alice", "card_id": card_id, **body}
    )
    assert response.status_code == 200, response.text
    return response.json()


# -- the row ------------------------------------------------------------------


def test_burst_lightning_row_names_its_kicker_cost(client, game):
    mountains(client, game, 5)
    add_card(client, game, "Burst Lightning", "alice", "hand")

    (kicker,) = cast_row(client, game, "Burst Lightning")["optional_costs"]
    assert kicker["keyword"] == "Kicker"
    assert kicker["rule"] == "CR 702.33a"
    assert kicker["cost"] == "{4}"
    assert kicker["repeatable"] is False
    assert kicker["affordable"] == "yes", kicker


def test_an_unaffordable_kicker_is_said_so_on_the_row(client, game):
    mountains(client, game, 1)
    add_card(client, game, "Burst Lightning", "alice", "hand")

    row = cast_row(client, game, "Burst Lightning")
    assert row["affordable"] == "yes"
    (kicker,) = row["optional_costs"]
    assert kicker["affordable"] == "no"
    assert "{4}{R}" in kicker["unaffordable_reason"]


def test_sowing_mycospawn_row_names_its_kicker_cost(client, game):
    """The reported card: `Kicker {1}{C}`."""
    add_card(client, game, "Sowing Mycospawn", "alice", "hand")
    (kicker,) = cast_row(client, game, "Sowing Mycospawn")["optional_costs"]
    assert (kicker["keyword"], kicker["cost"]) == ("Kicker", "{1}{C}")


def test_a_card_with_no_optional_cost_carries_no_optional_costs(client, game):
    mountains(client, game, 1)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    assert "optional_costs" not in cast_row(client, game, "Lightning Bolt")


# -- the cast -----------------------------------------------------------------


def test_kicked_burst_lightning_deals_four(client, game):
    """CR 702.33d: announcing the kicker makes the spell kicked, and Burst
    Lightning's own "If this spell was kicked, it deals 4 damage instead"
    reads that."""
    sources = mountains(client, game, 5)
    add_card(client, game, "Burst Lightning", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Burst Lightning")["id"]

    body = cast(
        client,
        game,
        bolt,
        targets=["bob"],
        mana_sources=sources,
        optional_costs=[{"keyword": "Kicker"}],
    )
    assert body["allowed"] is True, body
    assert body["cost"] == "{4}{R}"
    client.post(f"/api/games/{game}/resolve-top")
    assert life_of(state(client, game), "bob") == 16


def test_unkicked_burst_lightning_deals_two(client, game):
    sources = mountains(client, game, 5)
    add_card(client, game, "Burst Lightning", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Burst Lightning")["id"]

    body = cast(client, game, bolt, targets=["bob"], mana_sources=sources)
    assert body["allowed"] is True, body
    assert body["cost"] == "{R}"
    client.post(f"/api/games/{game}/resolve-top")
    assert life_of(state(client, game), "bob") == 18


def test_an_unaffordable_kick_is_refused_in_the_engines_words(client, game):
    sources = mountains(client, game, 1)
    add_card(client, game, "Burst Lightning", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Burst Lightning")["id"]

    body = cast(
        client,
        game,
        bolt,
        targets=["bob"],
        mana_sources=sources,
        optional_costs=[{"keyword": "Kicker"}],
    )
    assert body["allowed"] is False
    assert body["reason"] == "no combination of the available mana sources can pay {4}{R}"
    board = state(client, game)
    assert board["stack"] == []
    assert [card["name"] for card in board["hands"]["alice"]] == ["Burst Lightning"]


def test_kicking_a_card_without_kicker_is_refused(client, game):
    sources = mountains(client, game, 5)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")["id"]

    body = cast(
        client,
        game,
        bolt,
        targets=["bob"],
        mana_sources=sources,
        optional_costs=[{"keyword": "Kicker"}],
    )
    assert body["allowed"] is False
    assert body["reason"] == "Lightning Bolt has no kicker ability (CR 702.33a)"


def test_plain_kicker_paid_twice_is_refused(client, game):
    sources = mountains(client, game, 9)
    add_card(client, game, "Burst Lightning", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Burst Lightning")["id"]

    body = cast(
        client,
        game,
        bolt,
        targets=["bob"],
        mana_sources=sources,
        optional_costs=[{"keyword": "Kicker", "times": 2}],
        dry_run=True,
    )
    assert body["allowed"] is False
    assert "can only be paid once" in body["reason"]


def test_a_keyword_that_is_no_optional_additional_cost_is_refused(client, game):
    sources = mountains(client, game, 5)
    add_card(client, game, "Burst Lightning", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Burst Lightning")["id"]

    body = cast(
        client,
        game,
        bolt,
        targets=["bob"],
        mana_sources=sources,
        optional_costs=[{"keyword": "Flashback"}],
        dry_run=True,
    )
    assert body["allowed"] is False
    assert "Flashback" in body["reason"]
    for keyword in ("Kicker", "Entwine", "Escalate", "Harmonize"):
        assert keyword in body["reason"]


# -- entwine, escalate and harmonize --------------------------------------------


def lands(client, game: str, name: str, count: int) -> list[str]:
    for _ in range(count):
        add_card(client, game, name, "alice", "battlefield")
    return [card["id"] for card in state(client, game)["battlefield"] if card["name"] == name]


def offer(row: dict, keyword: str) -> dict:
    (found,) = [entry for entry in row.get("optional_costs") or [] if entry["keyword"] == keyword]
    return found


def test_tooth_and_nail_row_offers_entwine(client, game):
    lands(client, game, "Forest", 9)
    add_card(client, game, "Tooth and Nail", "alice", "hand")

    entwine = offer(cast_row(client, game, "Tooth and Nail"), "Entwine")
    assert (entwine["rule"], entwine["cost"], entwine["repeatable"]) == ("CR 702.42a", "{2}", False)
    assert entwine["affordable"] == "yes", entwine


def test_entwined_tooth_and_nail_pays_the_entwine_cost_on_top(client, game):
    """CR 702.42a: "You may choose all modes of this spell instead of just the
    number specified. If you do, you pay an additional [cost].\""""
    sources = lands(client, game, "Forest", 9)
    add_card(client, game, "Tooth and Nail", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Tooth and Nail")["id"]

    body = cast(
        client,
        game,
        card,
        modes=[1, 2],
        mana_sources=sources,
        optional_costs=[{"keyword": "Entwine"}],
        dry_run=True,
    )
    assert body["allowed"] is True, body
    assert body["cost"] == "{7}{G}{G}"


def test_both_tooth_and_nail_modes_without_entwine_are_refused(client, game):
    sources = lands(client, game, "Forest", 9)
    add_card(client, game, "Tooth and Nail", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Tooth and Nail")["id"]

    body = cast(client, game, card, modes=[1, 2], mana_sources=sources, dry_run=True)
    assert body["allowed"] is False
    assert "too many modes" in body["reason"]


def test_an_unaffordable_entwine_is_refused(client, game):
    sources = lands(client, game, "Forest", 8)
    add_card(client, game, "Tooth and Nail", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Tooth and Nail")["id"]

    body = cast(
        client,
        game,
        card,
        modes=[1, 2],
        mana_sources=sources,
        optional_costs=[{"keyword": "Entwine"}],
    )
    assert body["allowed"] is False
    assert body["reason"] == "no combination of the available mana sources can pay {7}{G}{G}"


def test_entwining_a_card_without_entwine_is_refused(client, game):
    sources = lands(client, game, "Plains", 5)
    add_card(client, game, "Blessed Alliance", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Blessed Alliance")["id"]

    body = cast(
        client,
        game,
        card,
        modes=[1, 2, 3],
        targets=["alice"],
        mana_sources=sources,
        optional_costs=[{"keyword": "Entwine"}],
        dry_run=True,
    )
    assert body["allowed"] is False
    assert "no entwine cost" in body["reason"]


def test_blessed_alliance_row_offers_escalate(client, game):
    lands(client, game, "Plains", 4)
    add_card(client, game, "Blessed Alliance", "alice", "hand")

    escalate = offer(cast_row(client, game, "Blessed Alliance"), "Escalate")
    assert (escalate["rule"], escalate["cost"]) == ("CR 702.120a", "{2}")
    assert escalate["affordable"] == "yes", escalate


def test_a_second_blessed_alliance_mode_pays_escalate(client, game):
    """CR 702.120a: "For each mode you choose beyond the first as you cast this
    spell, you pay an additional [cost]." Owed by choosing the mode, so it is
    charged whether or not the request names it."""
    sources = lands(client, game, "Plains", 4)
    add_card(client, game, "Blessed Alliance", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Blessed Alliance")["id"]

    body = cast(client, game, card, modes=[1, 2], targets=["alice"], mana_sources=sources)
    assert body["allowed"] is True, body
    assert body["cost"] == "{3}{W}"
    client.post(f"/api/games/{game}/resolve-top")
    assert life_of(state(client, game), "alice") == 24


def test_an_unaffordable_escalate_is_refused(client, game):
    sources = lands(client, game, "Plains", 3)
    add_card(client, game, "Blessed Alliance", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Blessed Alliance")["id"]

    body = cast(
        client,
        game,
        card,
        modes=[1, 2],
        targets=["alice"],
        mana_sources=sources,
        optional_costs=[{"keyword": "Escalate"}],
    )
    assert body["allowed"] is False
    assert body["reason"] == "no combination of the available mana sources can pay {3}{W}"


def test_collective_brutality_escalates_by_discarding_the_named_card(client, game):
    sources = lands(client, game, "Swamp", 2)
    add_card(client, game, "Collective Brutality", "alice", "hand")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    hand = state(client, game)["hands"]["alice"]
    card = find(hand, "Collective Brutality")["id"]
    bolt = find(hand, "Lightning Bolt")["id"]

    body = cast(
        client,
        game,
        card,
        modes=[1, 3],
        targets=["bob", "bob"],
        mana_sources=sources,
        optional_costs=[{"keyword": "Escalate", "choosing": [bolt]}],
    )
    assert body["allowed"] is True, body
    board = state(client, game)
    assert find(board["graveyards"]["alice"], "Lightning Bolt")["id"] == bolt
    client.post(f"/api/games/{game}/resolve-top")
    assert life_of(state(client, game), "bob") == 18


def test_roamers_routine_harmonize_row_offers_the_creature_tap(client, game):
    lands(client, game, "Forest", 5)
    add_card(client, game, "Roamer's Routine", "alice", "graveyard")
    card = find(state(client, game)["graveyards"]["alice"], "Roamer's Routine")["id"]

    (row,) = [
        row
        for row in legal_actions(client, game)
        if row["kind"] == "cast" and row["card_id"] == card and row["keyword"] == "Harmonize"
    ]
    harmonize = offer(row, "Harmonize")
    assert harmonize["rule"] == "CR 702.180b"
    assert harmonize["repeatable"] is False


def test_harmonize_taps_the_named_creature_to_reduce_the_cost(client, game):
    """CR 702.180a: "its total cost is reduced by an amount of generic mana
    equal to the tapped creature's power"; CR 702.180b: "You choose which
    creature to tap as you choose to pay a spell's harmonize cost"."""
    sources = lands(client, game, "Forest", 3)
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Roamer's Routine", "alice", "graveyard")
    board = state(client, game)
    bears = find(board["battlefield"], "Grizzly Bears")["id"]
    card = find(board["graveyards"]["alice"], "Roamer's Routine")["id"]

    body = cast(
        client,
        game,
        card,
        keyword="Harmonize",
        mana_sources=sources,
        optional_costs=[{"keyword": "Harmonize", "choosing": [bears]}],
    )
    assert body["allowed"] is True, body
    assert body["cost"] == "{2}{G}"
    assert find(state(client, game)["battlefield"], "Grizzly Bears")["tapped"] is True


def test_harmonize_without_a_creature_pays_the_whole_cost(client, game):
    sources = lands(client, game, "Forest", 3)
    add_card(client, game, "Roamer's Routine", "alice", "graveyard")
    card = find(state(client, game)["graveyards"]["alice"], "Roamer's Routine")["id"]

    body = cast(client, game, card, keyword="Harmonize", mana_sources=sources)
    assert body["allowed"] is False
    assert body["reason"] == "no combination of the available mana sources can pay {4}{G}"


def test_harmonize_named_on_a_plain_cast_is_refused(client, game):
    sources = lands(client, game, "Forest", 3)
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Roamer's Routine", "alice", "hand")
    board = state(client, game)
    bears = find(board["battlefield"], "Grizzly Bears")["id"]
    card = find(board["hands"]["alice"], "Roamer's Routine")["id"]

    body = cast(
        client,
        game,
        card,
        mana_sources=sources,
        optional_costs=[{"keyword": "Harmonize", "choosing": [bears]}],
        dry_run=True,
    )
    assert body["allowed"] is False
    assert "not being cast using harmonize" in body["reason"]


# -- both front ends ------------------------------------------------------------


def test_both_front_ends_parse_optional_costs_with_the_one_parser():
    assert seat_cli.parse_optional_costs is seatkit.parse_optional_costs
    assert mcp_module.parse_optional_costs is seatkit.parse_optional_costs
    card = "5609c7e2-0000-4000-8000-000000000001"
    assert seatkit.parse_optional_costs(f"Kicker, Multikicker:2, Gift:bob, Blight:{card}") == [
        {"keyword": "Kicker"},
        {"keyword": "Multikicker", "times": 2},
        {"keyword": "Gift", "opponent": "bob"},
        {"keyword": "Blight", "choosing": [card]},
    ]
    assert seatkit.parse_optional_costs("") == []


def test_the_seat_cli_kicks(client, game, monkeypatch, capsys):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    mountains(client, game, 5)
    add_card(client, game, "Burst Lightning", "alice", "hand")
    uid = next(uid for uid, who in store.seat_uids(game).items() if who == "alice")
    bolt = find(state(client, game, uid)["hands"]["alice"], "Burst Lightning")["id"]

    code = seat_cli.run(
        ["--uid", uid, "--game", game, "cast", bolt, "--target", "bob"]
        + ["--optional-costs", "Kicker", "--hold"]
    )
    assert code == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["outcome"]["allowed"] is True, answer
    assert answer["outcome"]["cost"] == "{4}{R}"


def test_the_mcp_cast_tool_kicks(client, game, monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    mountains(client, game, 5)
    add_card(client, game, "Burst Lightning", "alice", "hand")
    uid = next(uid for uid, who in store.seat_uids(game).items() if who == "alice")
    bolt = find(state(client, game, uid)["hands"]["alice"], "Burst Lightning")["id"]

    server = build_server(Seats(api="http://testserver", game_id=game))
    answer = asyncio.run(
        server.call_tool(
            "cast",
            {
                "player_uid": uid,
                "card": bolt,
                "targets": "bob",
                "optional_costs": "Kicker",
                "hold_priority": True,
            },
        )
    )
    body = json.loads(answer.content[0].text)
    assert body["outcome"]["allowed"] is True, body
    assert body["outcome"]["cost"] == "{4}{R}"
