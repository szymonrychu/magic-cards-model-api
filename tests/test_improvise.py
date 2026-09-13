"""Improvise, reachable end to end (battle 6, B6-05).

CR 702.126a: "Improvise is a static ability that functions while the spell
with improvise is on the stack. 'Improvise' means 'For each generic mana in
this spell's total cost, you may tap an untapped artifact you control rather
than pay that mana.'" CR 702.126b: it "isn't an additional or alternative cost
and applies only after the total cost of the spell with improvise is
determined". Both read in full with `mise run rules:find 702.126` in the
session that wrote this file.

The engine has implemented it all along (`Game.cast(..., improvise=...)`,
`machines/objects_as_costs.improvise`), but the cast route refused an
`improvise` field as an extra input, the seat CLI had no flag and the MCP cast
tool no parameter, so affinity's Kappa Cannoneers and Metallic Rebukes could
only ever be cast off lands. The action row said `affordable: unknown`,
"improvise ... is not evaluated by this affordability check".

Every card is a real Scryfall printing, every board is played through the
real routes. Whir of Invention is the off-pool improvise card: no deck in
`tests/fixtures/guides` plays it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from magic_cards_model.machines.objects_as_costs import improvise_offer
from test_front_end_parity import uid_for, wire

from magic_cards_model_api import seat_cli
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store

#: Artifacts with no mana ability of their own, so an improvise tap is the
#: only thing they can pay with.
ARTIFACTS = ("Ornithopter", "Memnite", "Welding Jar")


def battlefield_ids(board: dict[str, Any], *names: str) -> list[str]:
    return [card["id"] for card in board["battlefield"] if card["name"] in names]


def stage(
    client: TestClient, game: str, spell: str, *, artifacts: int, islands: int
) -> tuple[str, list[str], list[str]]:
    """`spell` in alice's hand, `islands` Islands and `artifacts` untapped
    artifacts on her battlefield; returns (spell id, island ids, artifact ids)."""
    add_card(client, game, spell, "alice", "hand")
    for _ in range(islands):
        add_card(client, game, "Island", "alice", "battlefield")
    for index in range(artifacts):
        add_card(client, game, ARTIFACTS[index % len(ARTIFACTS)], "alice", "battlefield")
    board = state(client, game)
    return (
        find(board["hands"]["alice"], spell)["id"],
        battlefield_ids(board, "Island"),
        battlefield_ids(board, *ARTIFACTS),
    )


def cast(client: TestClient, game: str, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/games/{game}/actions/cast", json={"player": "alice", **body})
    assert response.status_code == 200, response.text
    return response.json()


def engine_refusal(game: str, card_id: str) -> str:
    """The engine's own reason for refusing `card_id` as an improvise tap -
    `ObjectOffer.why_not`, the predicate `objects_as_costs.improvise` raises
    with - read off the committed game."""
    engine_game = store.get(game)
    assert engine_game is not None
    card = engine_game.find_by_id(UUID(card_id))
    reason = improvise_offer().why_not(card, "alice", engine_game)
    assert reason is not None, "the pick under test is not actually illegal"
    return reason


# -- the cast route --------------------------------------------------------


def test_kappa_cannoneer_is_cast_with_five_artifacts_and_one_island(client, game):
    """{5}{U}: five artifacts cover the five generic symbols, the Island the
    {U}. The reported board had seven potential payers and was told no
    combination could pay."""
    kappa, islands, artifacts = stage(client, game, "Kappa Cannoneer", artifacts=5, islands=1)

    body = cast(client, game, card_id=kappa, mana_sources=islands, improvise=artifacts)

    assert body["allowed"] is True, body
    # CR 702.126b: improvise never changes the total cost.
    assert body["cost"] == "{5}{U}"
    after = state(client, game)
    assert all(find_id(after, artifact)["tapped"] for artifact in artifacts)
    assert [item["name"] for item in after["stack"]] == ["Kappa Cannoneer"]


def test_an_improvise_dry_run_answers_without_tapping_anything(client, game):
    kappa, islands, artifacts = stage(client, game, "Kappa Cannoneer", artifacts=5, islands=1)

    body = cast(
        client, game, card_id=kappa, mana_sources=islands, improvise=artifacts, dry_run=True
    )

    assert body["allowed"] is True, body
    after = state(client, game)
    assert not any(find_id(after, artifact)["tapped"] for artifact in artifacts)
    assert after["stack"] == []


def test_whir_of_invention_improvises_its_x(client, game):
    """The off-pool card: {X}{U}{U}{U} at X=2 is two generic symbols, so two
    artifacts and three Islands pay it."""
    whir, islands, artifacts = stage(client, game, "Whir of Invention", artifacts=2, islands=3)

    body = cast(client, game, card_id=whir, x_value=2, mana_sources=islands, improvise=artifacts)

    assert body["allowed"] is True, body
    assert body["cost"] == "{2}{U}{U}{U}"
    assert [item["name"] for item in state(client, game)["stack"]] == ["Whir of Invention"]


def test_a_tapped_artifact_is_refused_with_the_engines_reason(client, game):
    """CR 702.126a taps an *untapped* artifact."""
    kappa, islands, artifacts = stage(client, game, "Kappa Cannoneer", artifacts=4, islands=1)
    add_card(client, game, "Ornithopter", "alice", "battlefield", tapped=True)
    tapped = next(
        card["id"]
        for card in state(client, game)["battlefield"]
        if card["name"] == "Ornithopter" and card["tapped"]
    )
    reason = engine_refusal(game, tapped)

    body = cast(client, game, card_id=kappa, mana_sources=islands, improvise=[*artifacts, tapped])

    assert body["allowed"] is False, body
    assert reason in body["reason"]
    after = state(client, game)
    assert find(after["hands"]["alice"], "Kappa Cannoneer")
    assert not any(find_id(after, artifact)["tapped"] for artifact in artifacts)


def test_a_creature_that_is_not_an_artifact_is_refused_with_the_engines_reason(client, game):
    """An artifact improvises; a Grizzly Bears does not (CR 702.126a)."""
    kappa, islands, artifacts = stage(client, game, "Kappa Cannoneer", artifacts=4, islands=1)
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    bears = find(state(client, game)["battlefield"], "Grizzly Bears")["id"]
    reason = engine_refusal(game, bears)

    body = cast(client, game, card_id=kappa, mana_sources=islands, improvise=[*artifacts, bears])

    assert body["allowed"] is False, body
    assert reason in body["reason"]
    assert find(state(client, game)["hands"]["alice"], "Kappa Cannoneer")


def test_a_spell_without_improvise_refuses_an_improvise_list(client, game):
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Ornithopter", "alice", "battlefield")
    board = state(client, game)
    bears = find(board["hands"]["alice"], "Grizzly Bears")["id"]

    body = cast(
        client,
        game,
        card_id=bears,
        mana_sources=battlefield_ids(board, "Forest"),
        improvise=battlefield_ids(board, "Ornithopter"),
    )

    assert body["allowed"] is False, body
    assert "does not have improvise" in body["reason"]


# -- discovery: affordances and the action row ---------------------------------


def test_affordances_offer_the_untapped_artifacts_you_control(client, game):
    kappa, _, artifacts = stage(client, game, "Kappa Cannoneer", artifacts=2, islands=0)
    add_card(client, game, "Memnite", "alice", "battlefield", tapped=True)
    add_card(client, game, "Ornithopter", "bob", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")

    response = client.get(f"/api/games/{game}/affordances/{kappa}", params={"player": "alice"})

    assert response.status_code == 200, response.text
    offer = response.json().get("object_costs", {}).get("improvise")
    assert offer is not None, response.json()
    assert offer["rule"] == "CR 702.126a"
    assert {candidate["id"] for candidate in offer["candidates"]} == set(artifacts)


def paying_sources(client: TestClient, game: str, improvising: list[str]) -> list[str]:
    """alice's untapped mana sources that a row's `improvise` does not tap -
    the raw route pays only from the sources it is handed."""
    return [
        card["id"]
        for card in state(client, game)["battlefield"]
        if card["name"] in ("Island", "Mind Stone")
        and not card["tapped"]
        and card["id"] not in improvising
    ]


def cast_row(client: TestClient, game: str, card_id: str) -> dict[str, Any]:
    response = client.get(
        f"/api/games/{game}/legal-actions",
        params={"player": "alice", "uid": uid_for(game, "alice")},
    )
    assert response.status_code == 200, response.text
    rows = [
        row
        for row in response.json()["actions"]
        if row["kind"] == "cast" and row.get("card_id") == card_id and not row.get("keyword")
    ]
    assert rows, response.json()["actions"]
    return rows[0]


def test_the_row_says_castable_when_lands_and_artifacts_pay(client, game):
    kappa, _, artifacts = stage(client, game, "Kappa Cannoneer", artifacts=5, islands=1)

    row = cast_row(client, game, kappa)

    assert row["affordable"] == "yes", row
    assert not row.get("unaffordable_reason")
    # The artifacts to name back as `CastRequest.improvise`.
    assert sorted(row["improvise"]) == sorted(artifacts)
    body = cast(
        client,
        game,
        card_id=kappa,
        improvise=row["improvise"],
        mana_sources=paying_sources(client, game, row["improvise"]),
    )
    assert body["allowed"] is True, body


def test_the_row_says_no_when_lands_and_artifacts_fall_short(client, game):
    kappa, _, _ = stage(client, game, "Kappa Cannoneer", artifacts=3, islands=1)

    row = cast_row(client, game, kappa)

    assert row["affordable"] == "no", row
    assert "not evaluated" not in (row.get("unaffordable_reason") or "")
    assert "improvise" not in row


def test_a_mana_artifact_pays_once_never_twice(client, game):
    """Mind Stone taps for {C} or improvises one generic, not both: Island,
    Mind Stone and three Ornithopters are five payers for Kappa Cannoneer's
    six symbols, and a fourth Ornithopter makes six."""
    kappa, _, _ = stage(client, game, "Kappa Cannoneer", artifacts=0, islands=1)
    add_card(client, game, "Mind Stone", "alice", "battlefield")
    for _ in range(3):
        add_card(client, game, "Ornithopter", "alice", "battlefield")
    assert cast_row(client, game, kappa)["affordable"] == "no"

    add_card(client, game, "Ornithopter", "alice", "battlefield")
    row = cast_row(client, game, kappa)
    assert row["affordable"] == "yes", row
    body = cast(
        client,
        game,
        card_id=kappa,
        improvise=row["improvise"],
        mana_sources=paying_sources(client, game, row["improvise"]),
    )
    assert body["allowed"] is True, body


# -- the two front ends ---------------------------------------------------


def test_the_seat_cli_casts_with_improvise(client, game, monkeypatch, capsys):
    wire(monkeypatch)
    kappa, islands, artifacts = stage(client, game, "Kappa Cannoneer", artifacts=5, islands=1)
    uid = uid_for(game, "alice")

    rc = seat_cli.run(
        [
            "--uid",
            uid,
            "--game",
            game,
            "cast",
            kappa,
            "--sources",
            ",".join(islands),
            "--improvise",
            ",".join(artifacts),
        ]
    )

    printed = capsys.readouterr().out
    assert rc == 0, printed
    assert [item["name"] for item in state(client, game)["stack"]] == ["Kappa Cannoneer"], printed


def test_the_mcp_cast_tool_casts_with_improvise(client, game, monkeypatch):
    wire(monkeypatch)
    kappa, islands, artifacts = stage(client, game, "Kappa Cannoneer", artifacts=5, islands=1)
    uid = uid_for(game, "alice")
    server = build_server(Seats(api="http://testserver", game_id=game))

    answer = asyncio.run(
        server.call_tool(
            "cast",
            {
                "player_uid": uid,
                "card": kappa,
                "mana_sources": ",".join(islands),
                "improvise": ",".join(artifacts),
            },
        )
    )

    text = answer.content[0].text
    assert json.loads(text)["outcome"]["allowed"] is True, text
    assert [item["name"] for item in state(client, game)["stack"]] == ["Kappa Cannoneer"]


def find_id(board: dict[str, Any], card_id: str) -> dict[str, Any]:
    return next(card for card in board["battlefield"] if card["id"] == card_id)
