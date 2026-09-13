"""Mana sources a seat names are the only ones a request carries.

**The defect (battle 5, B5-05).** `seat_cli cast` and `activate` sent every
untapped source as `mana_sources` whether or not `--sources` was given, even
for a cost with no mana in it. The engine tapped only what the cost needed, but
the audit row recorded the request: `activate <Flooded Strand> 0` logged
`mana_sources: [Sacred Foundry, Plains, Dalkovan Encampment]` with all three
still untapped, and an evoked Solitude logged two lands that paid nothing
(CR 702.74a's evoke cost has no mana in it). A reviewer reading the ledger saw
mana paid that was not.

So a request names only the sources the seat named, and naming none leaves the
choice to the engine's automatic payment (`CostPayment.auto_tap`, which taps
only what the cost needs - CR 601.2g, CR 118.3c) over that player's untapped
permanents. A log row names the permanents that actually paid, never the ones
merely offered. Both front ends, one rule (`test_front_end_parity.py`). Every
CR number here was looked up with `mise run rules:find` in the session that
wrote this file.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from conftest import add_card, state
from fastapi.testclient import TestClient
from test_audit_log import entry_of, log_of
from test_front_end_parity import uid_for, wire

from magic_cards_model_api import seat_cli
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats


def cli(game_id: str, *argv: str) -> list[str]:
    return ["--uid", uid_for(game_id, "alice"), "--game", game_id, *argv]


def ids_of(client: TestClient, game_id: str, name: str) -> list[str]:
    return [card["id"] for card in state(client, game_id)["battlefield"] if card["name"] == name]


def tapped(client: TestClient, game_id: str, name: str) -> list[str]:
    return [
        card["id"]
        for card in state(client, game_id)["battlefield"]
        if card["name"] == name and card["tapped"]
    ]


def two_mountains_and_a_bolt(client: TestClient, game_id: str) -> list[str]:
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Lightning Bolt", "alice", "hand")
    return ids_of(client, game_id, "Mountain")


def a_fetchland_among_lands(client: TestClient, game_id: str) -> str:
    for name in ("Plains", "Mountain", "Island"):
        add_card(client, game_id, name, "alice", "battlefield")
    add_card(client, game_id, "Plains", "alice", "library")
    add_card(client, game_id, "Flooded Strand", "alice", "battlefield")
    return ids_of(client, game_id, "Flooded Strand")[0]


def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = asyncio.run(server.call_tool(name, arguments))
    return json.loads(result.content[0].text)


# -- the seat CLI --------------------------------------------------------------


@pytest.mark.parametrize("sources", [None, ""], ids=["omitted", "empty"])
def test_cli_cast_without_sources_pays_automatically_and_logs_the_payer(
    client, game, monkeypatch, capsys, sources
) -> None:
    wire(monkeypatch)
    mountains = two_mountains_and_a_bolt(client, game)
    argv = ["cast", "Lightning Bolt", "--target", "bob", "--hold"]
    if sources is not None:
        argv += ["--sources", sources]

    assert seat_cli.run(cli(game, *argv)) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"]["allowed"] is True, printed

    paid = tapped(client, game, "Mountain")
    assert len(paid) == 1 and paid[0] in mountains, "CR 118.3c: only what the cost needs"
    row = entry_of(log_of(client, game), "cast")
    assert row["detail"]["mana_sources"] == paid, row


def test_cli_cast_names_only_the_sources_given(client, game, monkeypatch, capsys) -> None:
    wire(monkeypatch)
    mountains = two_mountains_and_a_bolt(client, game)

    argv = ["cast", "Lightning Bolt", "--target", "bob", "--sources", mountains[1], "--hold"]
    assert seat_cli.run(cli(game, *argv)) == 0
    assert json.loads(capsys.readouterr().out)["outcome"]["allowed"] is True

    assert tapped(client, game, "Mountain") == [mountains[1]]
    assert entry_of(log_of(client, game), "cast")["detail"]["mana_sources"] == [mountains[1]]


def test_cli_activate_with_no_mana_in_the_cost_logs_no_sources(
    client, game, monkeypatch, capsys
) -> None:
    """Flooded Strand's "{T}, Pay 1 life, Sacrifice this land" has no mana in
    it: nothing is tapped for mana and the row names no source."""
    wire(monkeypatch)
    strand = a_fetchland_among_lands(client, game)

    assert seat_cli.run(cli(game, "activate", strand, "0", "--hold")) == 0
    assert json.loads(capsys.readouterr().out)["outcome"]["allowed"] is True

    assert all(not card["tapped"] for card in state(client, game)["battlefield"])
    assert entry_of(log_of(client, game), "activate")["detail"]["mana_sources"] == []


def test_cli_help_states_what_sources_means(capsys) -> None:
    for command in ("cast", "activate"):
        with pytest.raises(SystemExit):
            seat_cli.run(["--uid", "u", "--game", "g", command, "--help"])
        text = " ".join(capsys.readouterr().out.split())
        assert "automatic payment" in text, text
        assert "only the sources" in text, text


# -- the MCP tools, the same rule ----------------------------------------------


def test_mcp_cast_without_sources_pays_automatically_and_logs_the_payer(
    client, game, monkeypatch
) -> None:
    wire(monkeypatch)
    two_mountains_and_a_bolt(client, game)
    server = build_server(Seats(api="http://testserver", game_id=game))

    answer = call_tool(
        server,
        "cast",
        {
            "player_uid": uid_for(game, "alice"),
            "card": "Lightning Bolt",
            "targets": "bob",
            "hold_priority": True,
        },
    )
    assert answer["outcome"]["allowed"] is True, answer

    paid = tapped(client, game, "Mountain")
    assert len(paid) == 1
    assert entry_of(log_of(client, game), "cast")["detail"]["mana_sources"] == paid


def test_mcp_activate_with_no_mana_in_the_cost_logs_no_sources(client, game, monkeypatch) -> None:
    wire(monkeypatch)
    strand = a_fetchland_among_lands(client, game)
    server = build_server(Seats(api="http://testserver", game_id=game))

    answer = call_tool(
        server,
        "activate",
        {
            "player_uid": uid_for(game, "alice"),
            "card_id": strand,
            "ability_index": 0,
            "hold_priority": True,
        },
    )
    assert answer["outcome"]["allowed"] is True, answer
    assert entry_of(log_of(client, game), "activate")["detail"]["mana_sources"] == []


# -- the route the front ends now lean on ---------------------------------------


def test_a_request_naming_no_sources_is_paid_from_untapped_permanents(client, game) -> None:
    mountains = two_mountains_and_a_bolt(client, game)

    dry = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": "Lightning Bolt", "targets": ["bob"], "dry_run": True},
    ).json()
    assert dry["allowed"] is True, dry
    assert [entry["card_id"] for entry in dry["tapped"]] == [mountains[0]] or [
        entry["card_id"] for entry in dry["tapped"]
    ] == [mountains[1]], dry

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": "Lightning Bolt", "targets": ["bob"]},
    ).json()
    assert cast["allowed"] is True, cast
    assert len(tapped(client, game, "Mountain")) == 1
