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


# -- what the seat and the human are SHOWN --------------------------------------
#
# The reported defect a second time (2026-09-20): "Sowing Mycospawn, cast with
# kicker didn't work. I simply wasn't presented with option to pay the kicker."
# The row above already named the cost; the browser never read that row before
# casting, and neither the row nor the hand card said anything about a printed
# optional cost the engine cannot charge. `Action.additional_cost_options` is
# the engine's own single enumeration of CR 601.2b's optional additional costs
# (`machines/casting.optional_additional_costs`) - these pin it reaching both
# surfaces. Proved on cards that are NOT the reported one: Mold Shambler
# (`Kicker {1}{G}`), Consign to Memory (`Replicate {1}`, repeatable) and March
# of Otherworldly Light (a printed "you may ..." sentence with no keyword,
# which the engine reports `understood: false`).


def hand_card(client, game: str, name: str) -> dict:
    return find(state(client, game)["hands"]["alice"], name)


def test_the_engines_own_enumeration_reaches_the_cast_row(client, game):
    add_card(client, game, "Mold Shambler", "alice", "hand")
    row = cast_row(client, game, "Mold Shambler")
    (option,) = row["additional_cost_options"]
    assert option == {
        "keyword": "kicker",
        "cost": "{1}{G}",
        "rule": "CR 702.33a",
        "repeatable": False,
        "understood": True,
    }


def test_a_hand_card_says_what_it_may_be_cast_with(client, game):
    """`cast_hints` is what the browser reads when the player picks the card
    up, before any legal-actions row or affordance is fetched - so it is
    where a kicker has to be visible for the option to be offered at all."""
    add_card(client, game, "Mold Shambler", "alice", "hand")
    card = hand_card(client, game, "Mold Shambler")
    assert card["cast"]["additional_costs"] == [
        {
            "keyword": "Kicker",
            "cost": "{1}{G}",
            "rule": "CR 702.33a",
            "repeatable": False,
            "understood": True,
        }
    ]


def test_a_repeatable_optional_cost_says_so_to_the_human(client, game):
    """CR 702.56a's replicate is announced "any number of times", so the hand
    card carries a count to choose rather than a yes/no."""
    add_card(client, game, "Consign to Memory", "alice", "hand")
    (replicate,) = hand_card(client, game, "Consign to Memory")["cast"]["additional_costs"]
    assert (replicate["keyword"], replicate["cost"], replicate["repeatable"]) == (
        "Replicate",
        "{1}",
        True,
    )


def test_a_card_with_no_optional_cost_carries_no_additional_costs(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    assert "additional_costs" not in hand_card(client, game, "Lightning Bolt")["cast"]


def test_an_optional_cost_the_engine_cannot_charge_is_shown_not_swallowed(client, game):
    """March of Otherworldly Light prints its optional additional cost as a
    sentence the engine cannot charge. Failing closed out loud: the card says
    the option exists and that announcing it is not available here, rather
    than showing nothing at all."""
    add_card(client, game, "March of Otherworldly Light", "alice", "hand")
    (option,) = hand_card(client, game, "March of Otherworldly Light")["cast"]["additional_costs"]
    assert option["understood"] is False
    assert option["keyword"] is None
    assert option["cost"] == "you may exile any number of white cards from your hand"
    assert option["rule"] == "CR 118.8b"


def test_a_row_says_which_optional_costs_it_cannot_charge(client, game):
    add_card(client, game, "March of Otherworldly Light", "alice", "hand")
    row = cast_row(client, game, "March of Otherworldly Light")
    (option,) = row["optional_costs"]
    assert option["understood"] is False
    assert option["keyword"] is None
    assert option["affordable"] == "unknown"
    assert "cannot" in option["unaffordable_reason"]


def test_a_row_the_engine_can_charge_says_so_too(client, game):
    add_card(client, game, "Mold Shambler", "alice", "hand")
    (kicker,) = cast_row(client, game, "Mold Shambler")["optional_costs"]
    assert kicker["keyword"] == "Kicker"
    assert kicker["understood"] is True


def test_a_repeatable_cost_is_paid_the_named_number_of_times(client, game):
    """CR 702.56a: replicate is paid "any number of times", so the count has
    to survive the whole way to the engine - a boolean anywhere in the chain
    would cap it at one."""
    sources = lands(client, game, "Island", 4)
    add_card(client, game, "Consign to Memory", "alice", "hand")
    add_card(client, game, "Sol Ring", "bob", "stack")
    spell = hand_card(client, game, "Consign to Memory")["id"]
    sol = find(state(client, game)["stack"], "Sol Ring")["id"]

    body = cast(
        client,
        game,
        spell,
        targets=[sol],
        mana_sources=sources,
        optional_costs=[{"keyword": "Replicate", "times": 3}],
    )
    assert body["allowed"] is True, body
    # {U} plus three replicate {1}s - times=1 would read {1}{U}, which is
    # what a boolean anywhere in the chain would have produced.
    assert body["cost"] == "{3}{U}"


# -- "pay N life or pay {M}" ----------------------------------------------------
#
# CR 601.2b: "If the spell has alternative or additional costs that will be
# paid as it's being cast ..., the player announces their intentions to pay
# any or all of those costs." Redirect Lightning's "pay 5 life or pay {2}" is
# one such choice: the life half has no object to name, so it is announced by
# keyword - the engine's own `Pay life` handle - or the mana half is paid.


def bolt_bob_then_hold_redirect(client, game: str) -> tuple[list[str], str, str]:
    """Alice bolts Bob with Redirect Lightning in hand and one Mountain left:
    enough for Redirect's {R}, not for its {2}."""
    sources = mountains(client, game, 2)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Redirect Lightning", "alice", "hand")
    bolt = hand_card(client, game, "Lightning Bolt")["id"]
    body = cast(client, game, bolt, targets=["bob"], mana_sources=sources[:1])
    assert body["allowed"] is True, body
    redirect = hand_card(client, game, "Redirect Lightning")["id"]
    return sources[1:], redirect, bolt


def test_redirect_lightnings_row_offers_the_life_half_by_name(client, game):
    bolt_bob_then_hold_redirect(client, game)
    row = cast_row(client, game, "Redirect Lightning")
    life = offer(row, "Pay life")
    assert life["cost"] == "pay 5 life or pay {2}"
    assert life["rule"] == "CR 601.2b"
    assert life["understood"] is True


def test_announcing_the_life_half_pays_five_life_instead_of_two_mana(client, game):
    sources, redirect, bolt = bolt_bob_then_hold_redirect(client, game)

    body = cast(
        client,
        game,
        redirect,
        targets=[bolt],
        mana_sources=sources,
        optional_costs=[{"keyword": "Pay life"}],
    )

    assert body["allowed"] is True, body
    board = state(client, game)
    assert [card["name"] for card in board["stack"]] == ["Redirect Lightning", "Lightning Bolt"]
    assert life_of(board, "alice") == 15


def test_unannounced_the_mana_half_is_charged(client, game):
    sources, redirect, bolt = bolt_bob_then_hold_redirect(client, game)

    body = cast(client, game, redirect, targets=[bolt], mana_sources=sources, dry_run=True)

    assert body["allowed"] is False, body
    assert "{2}{R}" in body["reason"]


def test_the_life_half_named_on_a_card_without_one_is_refused(client, game):
    sources = mountains(client, game, 1)
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = hand_card(client, game, "Lightning Bolt")["id"]

    body = cast(
        client,
        game,
        bolt,
        targets=["bob"],
        mana_sources=sources,
        optional_costs=[{"keyword": "Pay life"}],
        dry_run=True,
    )

    assert body["allowed"] is False
    assert body["reason"] == (
        "Lightning Bolt has no 'pay N life or pay {M}' additional cost to pay with life (CR 601.2b)"
    )


# -- two kicker costs, bargain, collect evidence and a revealed card -----------------
#
# CR 702.33b: "Kicker [cost 1] and/or [cost 2]" is two kicker costs, each
# announced on its own, and CR 702.33f's "if it was kicked with its [A] kicker"
# reads which. CR 702.166a: bargain is "you may sacrifice an artifact,
# enchantment, or token", and paying it makes the spell bargained (CR 702.166b).
# CR 701.59a: "you may collect evidence N" exiles graveyard cards of total mana
# value N or greater. Orator of Ojutai's "you may reveal a Dragon card from your
# hand" is an optional additional cost too (CR 118.8b). Each is a CR 601.2b
# announcement a seat has to be able to make.


def _record(game: str, card_id: str):
    from uuid import UUID

    engine = store.get(game)
    return engine.cast_record(engine.find_by_id(UUID(card_id)))


def _spell_on_stack(client, game: str, name: str) -> str:
    return find(state(client, game)["stack"], name)["id"]


def test_thornscape_battlemage_row_offers_each_kicker_cost(client, game):
    lands(client, game, "Mountain", 2)
    lands(client, game, "Plains", 2)
    add_card(client, game, "Thornscape Battlemage", "alice", "hand")
    row = cast_row(client, game, "Thornscape Battlemage")
    kickers = sorted(
        entry["cost"] for entry in row["optional_costs"] if entry["keyword"] == "Kicker"
    )
    assert kickers == ["{R}", "{W}"]


def test_kicked_with_one_named_kicker_cost(client, game):
    sources = lands(client, game, "Forest", 3) + lands(client, game, "Plains", 1)
    add_card(client, game, "Thornscape Battlemage", "alice", "hand")
    card = hand_card(client, game, "Thornscape Battlemage")["id"]
    body = cast(
        client,
        game,
        card,
        mana_sources=sources,
        optional_costs=[{"keyword": "Kicker", "cost": "{W}"}],
    )
    assert body["allowed"] is True, body
    record = _record(game, _spell_on_stack(client, game, "Thornscape Battlemage"))
    assert record.kicker_costs_paid == ["{W}"]
    assert record.kicked == 1


def test_kicked_with_both_kicker_costs_is_kicked_twice(client, game):
    sources = (
        lands(client, game, "Forest", 3)
        + lands(client, game, "Plains", 1)
        + lands(client, game, "Mountain", 1)
    )
    add_card(client, game, "Thornscape Battlemage", "alice", "hand")
    card = hand_card(client, game, "Thornscape Battlemage")["id"]
    body = cast(
        client,
        game,
        card,
        mana_sources=sources,
        optional_costs=[{"keyword": "Kicker", "cost": "{R}"}, {"keyword": "Kicker", "cost": "{W}"}],
    )
    assert body["allowed"] is True, body
    record = _record(game, _spell_on_stack(client, game, "Thornscape Battlemage"))
    assert sorted(record.kicker_costs_paid) == ["{R}", "{W}"]
    assert record.kicked == 2


def test_troublemaker_ouphe_row_offers_bargain(client, game):
    add_card(client, game, "Troublemaker Ouphe", "alice", "hand")
    bargain = offer(cast_row(client, game, "Troublemaker Ouphe"), "Bargain")
    assert bargain["rule"] == "CR 702.166a"
    assert bargain["understood"] is True


def test_bargained_by_sacrificing_the_named_artifact(client, game):
    sources = lands(client, game, "Forest", 2)
    add_card(client, game, "Ornithopter", "alice", "battlefield")
    add_card(client, game, "Troublemaker Ouphe", "alice", "hand")
    thopter = find(state(client, game)["battlefield"], "Ornithopter")["id"]
    card = hand_card(client, game, "Troublemaker Ouphe")["id"]
    body = cast(
        client,
        game,
        card,
        mana_sources=sources,
        optional_costs=[{"keyword": "Bargain", "choosing": [thopter]}],
    )
    assert body["allowed"] is True, body
    board = state(client, game)
    assert "Ornithopter" in [c["name"] for c in board["graveyards"]["alice"]]
    assert _record(game, _spell_on_stack(client, game, "Troublemaker Ouphe")).bargained


def test_bargain_names_exactly_one_permanent(client, game):
    sources = lands(client, game, "Forest", 2)
    add_card(client, game, "Troublemaker Ouphe", "alice", "hand")
    card = hand_card(client, game, "Troublemaker Ouphe")["id"]
    body = cast(
        client,
        game,
        card,
        mana_sources=sources,
        optional_costs=[{"keyword": "Bargain"}],
        dry_run=True,
    )
    assert body["allowed"] is False
    assert "CR 702.166a" in body["reason"]


def test_evidence_collected_exiles_graveyard_cards(client, game):
    sources = lands(client, game, "Forest", 2)
    add_card(client, game, "Craw Wurm", "alice", "graveyard")
    add_card(client, game, "Vitu-Ghazi Inspector", "alice", "hand")
    card = hand_card(client, game, "Vitu-Ghazi Inspector")["id"]
    assert offer(cast_row(client, game, "Vitu-Ghazi Inspector"), "Collect evidence")
    body = cast(
        client,
        game,
        card,
        mana_sources=sources,
        optional_costs=[{"keyword": "Collect evidence"}],
    )
    assert body["allowed"] is True, body
    assert "Craw Wurm" in [c["name"] for c in state(client, game)["exile"]]
    record = _record(game, _spell_on_stack(client, game, "Vitu-Ghazi Inspector"))
    assert record.evidence_collected


def test_revealing_the_named_dragon_card(client, game):
    sources = lands(client, game, "Plains", 2)
    add_card(client, game, "Siege Dragon", "alice", "hand")
    add_card(client, game, "Orator of Ojutai", "alice", "hand")
    dragon = hand_card(client, game, "Siege Dragon")["id"]
    card = hand_card(client, game, "Orator of Ojutai")["id"]
    assert offer(cast_row(client, game, "Orator of Ojutai"), "Reveal")
    body = cast(
        client,
        game,
        card,
        mana_sources=sources,
        optional_costs=[{"keyword": "Reveal", "choosing": [dragon]}],
    )
    assert body["allowed"] is True, body
    record = _record(game, _spell_on_stack(client, game, "Orator of Ojutai"))
    assert record.revealed_for_cost


def test_the_one_parser_reads_a_kicker_cost_and_a_two_word_keyword():
    assert seatkit.parse_optional_costs("Kicker:{W}, Collect evidence") == [
        {"keyword": "Kicker", "cost": "{W}"},
        {"keyword": "Collect evidence"},
    ]


# -- a chosen creature type, beheld (Celestial Reunion) -------------------------
#
# CR 118.8b + CR 701.4a: "As an additional cost to cast this spell, you may
# choose a creature type and behold two creatures of that type." The type is
# the seat's own choice (CR 607.2d's "the chosen type" reads it back at
# resolution), so the announcement carries it alongside the beheld creatures.


def test_celestial_reunion_row_offers_its_behold(client, game):
    add_card(client, game, "Celestial Reunion", "alice", "hand")
    behold = offer(cast_row(client, game, "Celestial Reunion"), "Behold")
    assert behold["rule"] == "CR 701.4a"
    assert behold["understood"] is True


def test_beholding_two_goblins_of_the_named_type(client, game):
    sources = lands(client, game, "Forest", 2)
    add_card(client, game, "Goblin Guide", "alice", "battlefield")
    add_card(client, game, "Goblin Guide", "alice", "battlefield")
    goblins = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Goblin Guide"]
    add_card(client, game, "Celestial Reunion", "alice", "hand")
    card = hand_card(client, game, "Celestial Reunion")["id"]
    body = cast(
        client,
        game,
        card,
        x_value=1,
        mana_sources=sources,
        optional_costs=[{"keyword": "Behold", "creature_type": "Goblin", "choosing": goblins}],
    )
    assert body["allowed"] is True, body
    record = _record(game, _spell_on_stack(client, game, "Celestial Reunion"))
    assert record.behold_chosen_type_paid
    assert record.chosen_creature_type == "goblin"


def test_behold_needs_the_creature_type_named(client, game):
    sources = lands(client, game, "Forest", 2)
    add_card(client, game, "Goblin Guide", "alice", "battlefield")
    add_card(client, game, "Goblin Guide", "alice", "battlefield")
    goblins = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Goblin Guide"]
    add_card(client, game, "Celestial Reunion", "alice", "hand")
    card = hand_card(client, game, "Celestial Reunion")["id"]
    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": card,
            "x_value": 1,
            "mana_sources": sources,
            "optional_costs": [{"keyword": "Behold", "choosing": goblins}],
            "dry_run": True,
        },
    )
    body = response.json()
    assert body.get("allowed") is not True, body
    assert "creature_type" in json.dumps(body), body
