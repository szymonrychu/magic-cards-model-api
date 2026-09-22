"""Fiend Artisan ("{X}{B/G}, {T}, Sacrifice another creature: Search your
library for a creature card with mana value X or less, put it onto the
battlefield, then shuffle. Activate only as a sorcery." - oracle text verified
with `resolve_cards` in the session that wrote this file) is the reported gap:
a seat playing a toolbox deck activated the tutor ability meaning to fetch a
real threat, but `cast` takes `--x` and `activate` did not - the ability's own
`{X}` in its cost had no way to be announced through either seat surface, so
every attempt silently ran at X=0 and could only ever fetch Dryad Arbor
(mana value 0), never the bigger creature the seat actually asked for. It
filed this as its own operator error; it was not - `activate` simply had no
`x_value` to carry, and worse, it *succeeded* at X=0 rather than refusing,
which is exactly the silent-degradation failure mode this project exists to
never produce.

Reading `engine_api.py`/`schemas.py`/`game.py` (the engine repo,
`../magic-cards-model`, read-only) found the engine already able to answer:
`Game.begin_activating`/`Game.activate_ability` both take `x_value: int = 0`
and fold it into the ability's own mana cost via `ManaCost.with_x` exactly
the way `Casting.determine_cost` already does for a spell's X - so this is
not an engine gap. The gap was only in the three layers this repo owns:
`ActivateRequest` carried no `x_value` field at all, `engine_api.
activate_ability` never threaded one through even though the engine accepted
it, and neither `seat_cli.py`'s `activate` subcommand nor `mcp.py`'s
`activate` tool had a way to set one - so the field was reachable by nobody,
the same shape of gap `test_activate_sacrifice_cost.py` already proved and
fixed for `sacrificing`/`discarding`/`exiling`.

This file also covers the loud-refusal case the reported gap demanded:
activating an X ability with no `x_value` named must be refused with the
engine's own reason, not silently run at zero - `engine_api._activation_needs_x`
asks the engine's own parsed cost pieces (`ManaCost.parse(...).x_count`,
`LoyaltyCost.variable`), never a guess re-derived from the printed string.

Every card here is a real Scryfall printing loaded through `conftest.add_card`
(Fiend Artisan, Dryad Arbor and Birds of Paradise were all copied in from a
prior session's `.scryfall-cache` capture of the live game that reported this
bug); no mocks in this file's board-building.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def wired_seats(monkeypatch, game_id: str) -> tuple[Any, dict[str, str]]:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id), seats_for(game_id)


def stock_fiend_artisan_board(client: TestClient, game_id: str) -> dict[str, Any]:
    """alice: Fiend Artisan (summoning sickness cleared, so its own `{T}` cost
    is payable), Birds of Paradise as the "sacrifice another creature" fodder,
    and three untapped Forests for `{X}{B/G}`. Her library holds exactly the
    two creatures the tutor's "mana value X or less" clause can ever find:
    Dryad Arbor (mana value 0) and Grizzly Bears (mana value 2) - the reported
    bug's own pair, X=0 finding only the first and X=2 reaching the second."""
    add_card(client, game_id, "Fiend Artisan", "alice", "battlefield")
    add_card(client, game_id, "Birds of Paradise", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Dryad Arbor", "alice", "library")
    add_card(client, game_id, "Grizzly Bears", "alice", "library")
    board = state(client, game_id)
    artisan = find(board["battlefield"], "Fiend Artisan")
    client.patch(f"/api/games/{game_id}/cards/{artisan['id']}", json={"summoning_sick": False})
    board = state(client, game_id)
    return {
        "artisan_id": artisan["id"],
        "birds_id": find(board["battlefield"], "Birds of Paradise")["id"],
        "forests": [c["id"] for c in board["battlefield"] if c["name"] == "Forest"],
    }


def resolve_the_tutor(client: TestClient, game_id: str, uid: str) -> dict[str, Any]:
    # resolve-top is a seat's only while it holds the priority it passes
    # (`game_access._seat_mismatch`): after a tool that auto-passed, that is
    # the other seat, so the call goes out as whoever holds it now.
    # The question itself is read back as `uid`, the seat it is addressed to.
    holder = store.get(game_id).priority_holder
    resolver = next((u for u, name in store.seat_uids(game_id).items() if name == holder), uid)
    resolved = client.post(f"/api/games/{game_id}/resolve-top", params={"uid": resolver})
    assert resolved.status_code == 200, resolved.text
    return {**resolved.json(), "pending_decision": state(client, game_id, uid)["pending_decision"]}


def find_the_search(pending: dict[str, Any], name: str) -> list[str]:
    return [
        card_id
        for card_id, label in zip(pending["cards"], pending["card_labels"], strict=True)
        if label == name
    ]


# -- the reported case, end to end through the HTTP action endpoint --------


def test_fiend_artisan_activated_for_x_above_zero_fetches_a_creature_x_permits(client, game):
    """X=2, announced: the search this raises can find Grizzly Bears (mana
    value 2), not only Dryad Arbor - the exact reported gap, fixed."""
    uids = seats_for(game)
    rig = stock_fiend_artisan_board(client, game)

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": rig["artisan_id"],
            "ability_index": 0,
            "sacrificing": [rig["birds_id"]],
            "x_value": 2,
            "mana_sources": rig["forests"],
        },
    )
    body = response.json()
    assert body["allowed"] is True, body

    mid = state(client, game)
    assert [c["name"] for c in mid["graveyards"]["alice"]] == ["Birds of Paradise"]
    assert mid["stack"][0]["kind"] == "ability"
    assert mid["stack"][0]["name"] == "Fiend Artisan"

    resolved = resolve_the_tutor(client, game, uids["alice"])
    assert resolved.get("paused") is True, resolved
    pending = resolved["pending_decision"]
    assert pending["kind"] == "choose_cards", pending
    bears = find_the_search(pending, "Grizzly Bears")
    assert bears, f"Grizzly Bears (mana value 2) must be a legal find for X=2: {pending}"

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": bears},
    )
    assert answered.status_code == 200, answered.text

    final = state(client, game)
    assert final["stack"] == []
    assert any(c["name"] == "Grizzly Bears" for c in final["battlefield"]), (
        "the bigger creature X=2 permits must actually land, not just be offered"
    )


def test_fiend_artisan_activated_for_x_zero_can_only_find_dryad_arbor(client, game):
    """The other half of the same fetch, at X=0 - Grizzly Bears (mana value 2)
    must NOT be a legal find, so an X actually chosen as 0 is genuinely
    different play from an X that was never announced at all."""
    uids = seats_for(game)
    rig = stock_fiend_artisan_board(client, game)

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": rig["artisan_id"],
            "ability_index": 0,
            "sacrificing": [rig["birds_id"]],
            "x_value": 0,
            "mana_sources": rig["forests"],
        },
    )
    body = response.json()
    assert body["allowed"] is True, body

    resolved = resolve_the_tutor(client, game, uids["alice"])
    pending = resolved["pending_decision"]
    assert pending["kind"] == "choose_cards", pending
    assert find_the_search(pending, "Dryad Arbor"), pending
    assert not find_the_search(pending, "Grizzly Bears"), (
        "mana value 2 is not '0 or less' - X=0 must not be able to reach it"
    )


def test_fiend_artisan_activated_with_no_x_named_is_refused_not_silently_zero(client, game):
    """The loud-refusal case: an ability whose own cost carries an X, given no
    `x_value` at all, must be refused with the engine's own reason - not
    silently run at X=0, which is the exact silent degradation the reported
    seat mistook for its own misplay."""
    rig = stock_fiend_artisan_board(client, game)

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": rig["artisan_id"],
            "ability_index": 0,
            "sacrificing": [rig["birds_id"]],
            "mana_sources": rig["forests"],
        },
    )
    body = response.json()
    assert body["allowed"] is False, body
    assert "x" in body["reason"].lower()

    # Nothing moved: no refused attempt costs the board anything.
    final = state(client, game)
    assert any(c["name"] == "Birds of Paradise" for c in final["battlefield"]), (
        "a refused activation must not have paid its cost"
    )
    assert final["stack"] == []


# -- the seat CLI surface (the literal reported transport) -----------------


def test_seat_cli_activate_can_name_an_x(client, game, monkeypatch, capsys):
    """`seat_cli.py activate --x 2` reaches the same request the HTTP test
    above drove directly - the actual transport the reported seat was using
    when it hit the wall."""
    seats, uids = wired_seats(monkeypatch, game)
    rig = stock_fiend_artisan_board(client, game)

    seat_cli.run(
        [
            "--uid",
            uids["alice"],
            "--game",
            game,
            "activate",
            rig["artisan_id"],
            "0",
            "--sacrificing",
            rig["birds_id"],
            "--x",
            "2",
            "--sources",
            ",".join(rig["forests"]),
        ]
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"]["allowed"] is True, printed

    resolved = resolve_the_tutor(client, game, uids["alice"])
    pending = resolved["pending_decision"]
    bears = find_the_search(pending, "Grizzly Bears")
    assert bears, pending
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": bears},
    )
    assert answered.status_code == 200, answered.text
    final = state(client, game)
    assert any(c["name"] == "Grizzly Bears" for c in final["battlefield"])


def test_seat_cli_activate_without_x_is_refused_not_crashed(client, game, monkeypatch, capsys):
    seats, uids = wired_seats(monkeypatch, game)
    rig = stock_fiend_artisan_board(client, game)

    exit_code = seat_cli.run(
        [
            "--uid",
            uids["alice"],
            "--game",
            game,
            "activate",
            rig["artisan_id"],
            "0",
            "--sacrificing",
            rig["birds_id"],
            "--sources",
            ",".join(rig["forests"]),
        ]
    )
    assert exit_code == 0, "a refusal is a normal answer, not a CLI failure"
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"]["allowed"] is False
    assert "x" in printed["outcome"]["reason"].lower()


# -- the MCP tool surface (what an AI seat actually calls) ------------------


def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, arguments))


def test_mcp_activate_tool_can_name_an_x(client, game, monkeypatch):
    """`mcp.py`'s `activate` tool takes `x_value` too - an AI seat plays
    through this transport, not the CLI, and a fix that only reached
    `seat_cli.py` would leave every AI seat exactly as stuck (this is the
    exact reported seat's own transport)."""
    seats, uids = wired_seats(monkeypatch, game)
    rig = stock_fiend_artisan_board(client, game)
    server = build_server(seats)

    result = call_tool(
        server,
        "activate",
        {
            "player_uid": uids["alice"],
            "card_id": rig["artisan_id"],
            "ability_index": 0,
            "sacrificing": rig["birds_id"],
            "x_value": 2,
            "mana_sources": ",".join(rig["forests"]),
        },
    )
    answer = json.loads(result.content[0].text)
    assert answer["outcome"]["allowed"] is True, answer

    resolved = resolve_the_tutor(client, game, uids["alice"])
    pending = resolved["pending_decision"]
    bears = find_the_search(pending, "Grizzly Bears")
    assert bears, pending
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": bears},
    )
    assert answered.status_code == 200, answered.text
    final = state(client, game)
    assert any(c["name"] == "Grizzly Bears" for c in final["battlefield"])


def test_mcp_activate_tool_without_x_is_refused_not_crashed(client, game, monkeypatch):
    seats, uids = wired_seats(monkeypatch, game)
    rig = stock_fiend_artisan_board(client, game)
    server = build_server(seats)

    result = call_tool(
        server,
        "activate",
        {
            "player_uid": uids["alice"],
            "card_id": rig["artisan_id"],
            "ability_index": 0,
            "sacrificing": rig["birds_id"],
            "mana_sources": ",".join(rig["forests"]),
        },
    )
    answer = json.loads(result.content[0].text)
    assert answer["outcome"]["allowed"] is False
    assert "x" in answer["outcome"]["reason"].lower()


# -- an ability with no X at all is unaffected ------------------------------


def test_an_ability_with_no_x_is_never_asked_for_one(client, game):
    """Goblin Bombardment's cost is pure sacrifice, no `{X}` anywhere - naming
    no `x_value` for it must keep working exactly as
    `test_activate_sacrifice_cost.py` already proved, unaffected by this
    change."""
    add_card(client, game, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    board = state(client, game)
    bombardment = find(board["battlefield"], "Goblin Bombardment")
    bears = find(board["battlefield"], "Grizzly Bears")

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": bombardment["id"],
            "ability_index": 0,
            "sacrificing": [bears["id"]],
            "targets": ["bob"],
        },
    )
    body = response.json()
    assert body["allowed"] is True, body
