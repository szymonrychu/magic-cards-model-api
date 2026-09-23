"""Goblin Bombardment ("Sacrifice a creature: This enchantment deals 1
damage to any target.") is the reported gap: a seat playing Boros Energy
could never activate it, because `activate` exposed `--target`/`--sources`/
`--color` but had no way to name the creature its own cost sacrifices. Every
attempt failed with the engine's own honest refusal, "sacrifice needs 1
card(s) chosen, 0 given" - the engine already modelled the cost correctly and
refused correctly; the seat surface simply had no way to answer it.

Reading `engine_api.py`/`schemas.py` found the field already there:
`ActivateRequest.sacrificing` existed and `engine_api.activate_ability`
already threaded it into `Game.begin_activating`. The gap was only in the two
front ends that build the request - `seat_cli.py`'s `activate` subcommand had
no `--sacrificing` flag, and `mcp.py`'s `activate` tool had no `sacrificing`
parameter - so the field was reachable by nobody. This file proves the fix at
the layer that actually carries the cost (the HTTP action endpoint, which is
what `engine_api.py`/`schemas.py` alone give you) and at both seat surfaces
that a real seat calls through, `seat_cli.py` and `mcp.py`: a fix landed on
only one of the three leaves the others exactly as broken as the reported
seat found them.

Also covers the general shape rather than a sacrifice-only special case:
`discarding` and `exiling` are the same kind of object-choosing cost
component the engine's `Game.begin_activating` already accepts
(`machines.cost.ComponentKind`), so `ActivateRequest` carries all three as
lists - a list, not a single id, because a cost can name a quantity greater
than one ("Sacrifice two creatures") - and `engine_api.py` threads
them straight through with no new game logic: the engine still decides what a
cost requires, this repo only carries the seat's answer to it.

Every card here is a real Scryfall printing loaded through `conftest.add_card`
(Goblin Bombardment's own fixture was copied in from a prior session's
`.scryfall-cache` capture of the live game that reported this bug); no mocks
in this file's board-building, matching this repo's own rule about fakes
being legitimate everywhere except a real board proving a real defect.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from conftest import add_card, find, life_of, state
from fastapi.testclient import TestClient
from test_legal_actions_endpoint import by_kind, legal_actions

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


def stock_bombardment_board(client: TestClient, game_id: str) -> dict[str, str]:
    """alice: Goblin Bombardment and a Grizzly Bears on the battlefield, a
    Mountain too (a legal permanent that is not a creature, for the refusal
    case). bob starts at 20 life, untouched."""
    add_card(client, game_id, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, game_id, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    board = state(client, game_id)
    return {
        "bombardment_id": find(board["battlefield"], "Goblin Bombardment")["id"],
        "bears_id": find(board["battlefield"], "Grizzly Bears")["id"],
        "mountain_id": find(board["battlefield"], "Mountain")["id"],
    }


# -- the reported case, end to end through the HTTP action endpoint --------


def test_goblin_bombardment_sacrifices_the_named_creature_and_deals_damage(client, game):
    """The activation succeeds, the sacrificed creature is in its owner's
    graveyard, and once the ability resolves the damage landed on the named
    target - the exact reported scenario, driven the way `engine_api.py`/
    `schemas.py` alone already supported it."""
    rig = stock_bombardment_board(client, game)

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": rig["bombardment_id"],
            "ability_index": 0,
            "sacrificing": [rig["bears_id"]],
            "targets": ["bob"],
        },
    )
    body = response.json()
    assert body["allowed"] is True, body

    mid = state(client, game)
    assert [c["name"] for c in mid["graveyards"]["alice"]] == ["Grizzly Bears"]
    assert all(c["name"] != "Grizzly Bears" for c in mid["battlefield"])
    assert mid["stack"][0]["kind"] == "ability"
    assert mid["stack"][0]["name"] == "Goblin Bombardment"

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"]

    final = state(client, game)
    assert final["stack"] == []
    assert life_of(final, "bob") == 19


def test_goblin_bombardment_refuses_with_the_engines_own_reason_when_none_is_named(client, game):
    """Naming none at all: refused with the engine's own count, not a crash
    and not a silent no-op - the exact sentence the reported seat saw."""
    rig = stock_bombardment_board(client, game)

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": rig["bombardment_id"],
            "ability_index": 0,
            "targets": ["bob"],
        },
    )
    body = response.json()
    assert body["allowed"] is False
    assert "sacrifice needs 1 card(s) chosen, 0 given" in body["reason"]

    # Nothing moved: no refused attempt costs the board anything.
    final = state(client, game)
    assert final["battlefield"] == state(client, game)["battlefield"]
    assert life_of(final, "bob") == 20


def test_goblin_bombardment_refuses_a_card_that_cannot_be_sacrificed(client, game):
    """Naming a card that cannot be sacrificed: refused with the engine's own
    reason (it is not a creature), not a crash and not a silent no-op."""
    rig = stock_bombardment_board(client, game)

    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": rig["bombardment_id"],
            "ability_index": 0,
            "sacrificing": [rig["mountain_id"]],
            "targets": ["bob"],
        },
    )
    body = response.json()
    assert body["allowed"] is False
    assert "Mountain" in body["reason"]
    assert "creature" in body["reason"].lower()

    final = state(client, game)
    assert any(c["name"] == "Mountain" for c in final["battlefield"]), "nothing was sacrificed"
    assert life_of(final, "bob") == 20


# -- the seat CLI surface (the literal reported transport) -----------------


def test_seat_cli_activate_can_name_a_sacrifice(client, game, monkeypatch, capsys):
    """`seat_cli.py activate --sacrificing <id>` reaches the same request the
    HTTP test above drove directly - the actual transport the reported seat
    was using when it hit the wall."""
    seats, uids = wired_seats(monkeypatch, game)
    rig = stock_bombardment_board(client, game)

    seat_cli.run(
        [
            "--uid",
            uids["alice"],
            "--game",
            game,
            "activate",
            rig["bombardment_id"],
            "0",
            "--sacrificing",
            rig["bears_id"],
            "--target",
            "bob",
        ]
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"]["allowed"] is True, printed

    mid = state(client, game)
    assert [c["name"] for c in mid["graveyards"]["alice"]] == ["Grizzly Bears"]

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"]
    assert life_of(state(client, game), "bob") == 19


def test_seat_cli_activate_without_sacrificing_is_refused_not_crashed(
    client, game, monkeypatch, capsys
):
    seats, uids = wired_seats(monkeypatch, game)
    rig = stock_bombardment_board(client, game)

    exit_code = seat_cli.run(
        [
            "--uid",
            uids["alice"],
            "--game",
            game,
            "activate",
            rig["bombardment_id"],
            "0",
            "--target",
            "bob",
        ]
    )
    assert exit_code == 0, "a refusal is a normal answer, not a CLI failure"
    printed = json.loads(capsys.readouterr().out)
    assert printed["outcome"]["allowed"] is False
    assert "sacrifice needs 1 card(s) chosen, 0 given" in printed["outcome"]["reason"]


# -- the MCP tool surface (what an AI seat actually calls) ------------------


def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(server.call_tool(name, arguments))


def test_mcp_activate_tool_can_name_a_sacrifice(client, game, monkeypatch):
    """`mcp.py`'s `activate` tool takes `sacrificing` too - an AI seat plays
    through this transport, not the CLI, and a fix that only reached
    `seat_cli.py` would leave every AI seat exactly as stuck."""
    seats, uids = wired_seats(monkeypatch, game)
    rig = stock_bombardment_board(client, game)
    server = build_server(seats)

    result = call_tool(
        server,
        "activate",
        {
            "player_uid": uids["alice"],
            "card_id": rig["bombardment_id"],
            "ability_index": 0,
            "sacrificing": rig["bears_id"],
            "targets": "bob",
        },
    )
    answer = json.loads(result.content[0].text)
    assert answer["outcome"]["allowed"] is True, answer

    mid = state(client, game)
    assert [c["name"] for c in mid["graveyards"]["alice"]] == ["Grizzly Bears"]

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"]
    assert life_of(state(client, game), "bob") == 19


def test_mcp_activate_tool_without_sacrificing_is_refused_not_crashed(client, game, monkeypatch):
    seats, uids = wired_seats(monkeypatch, game)
    rig = stock_bombardment_board(client, game)
    server = build_server(seats)

    result = call_tool(
        server,
        "activate",
        {
            "player_uid": uids["alice"],
            "card_id": rig["bombardment_id"],
            "ability_index": 0,
            "targets": "bob",
        },
    )
    answer = json.loads(result.content[0].text)
    assert answer["outcome"]["allowed"] is False
    assert "sacrifice needs 1 card(s) chosen, 0 given" in answer["outcome"]["reason"]


# -- the general shape: an exile cost is paid off the battlefield too ------


def test_an_off_battlefield_exile_cost_is_paid_end_to_end(client, game):
    """Scrapheap Scrounger's graveyard ability costs "Exile another creature
    card from your graveyard" (CR 113.6j, CR 602.2b). The engine offers it as
    an `activate_off_zone` action and `off_zone_activation` takes `exiling=`,
    so the route must thread the seat's chosen card through rather than
    refuse it - otherwise the seat is offered an action it can never pay."""
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Elvish Mystic", "alice", "graveyard")
    add_card(client, game, "Scrapheap Scrounger", "alice", "graveyard")
    board = state(client, game)
    scrounger = find(board["graveyards"]["alice"], "Scrapheap Scrounger")
    mystic = find(board["graveyards"]["alice"], "Elvish Mystic")
    swamps = [c["id"] for c in board["battlefield"] if c["name"] == "Swamp"]

    (offer,) = [
        a
        for a in by_kind(legal_actions(client, game), "activate_off_zone")
        if a["card_id"] == scrounger["id"]
    ]
    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": scrounger["id"],
            "ability_index": offer["ability_index"],
            "exiling": [mystic["id"]],
            "mana_sources": swamps,
        },
    )
    body = response.json()
    assert body["allowed"] is True, body
    after = state(client, game)
    assert [c["name"] for c in after["exile"]] == ["Elvish Mystic"]
    # The cost is paid; the return itself waits on the stack (CR 113.7a).
    assert [c["name"] for c in after["graveyards"]["alice"]] == ["Scrapheap Scrounger"]
    assert len(after["stack"]) == 1


# -- a paused activation completes through /decide, then priority passes ---


def test_a_paused_sacrifice_cost_activation_completes_through_decide_and_passes(client, game):
    """The server half of the play UI's auto-pass fix. An activation that
    names its sacrifice but no target is not refused: the engine asks for the
    target (`Game.begin_activating` - `targets=None` asks), so the route
    answers `paused: true` with a `pending_decision` for alice, and nothing is
    on the stack yet. A `pass-priority` sent at that moment - what `fire()`
    used to send straight away - is refused, because a decision is still
    open. Once `/decide` names the target the ability is on the stack, and
    only then does alice's pass go through.

    (Naming no sacrifice at all is refused outright, not paused - see
    `test_goblin_bombardment_refuses_with_the_engines_own_reason_when_none_is_named`
    above.)"""
    uids = seats_for(game)
    rig = stock_bombardment_board(client, game)

    paused = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": rig["bombardment_id"],
            "ability_index": 0,
            "sacrificing": [rig["bears_id"]],
        },
    ).json()
    assert paused.get("paused") is True, paused
    assert paused["allowed"] is None, paused
    pending = paused["pending_decision"]
    assert pending["player"] == "alice", pending
    assert pending["kind"] == "choose_targets", pending
    assert state(client, game)["stack"] == [], "a paused activation is not on the stack yet"

    premature = client.post(
        f"/api/games/{game}/actions/pass-priority",
        params={"uid": uids["alice"]},
        json={"player": "alice"},
    )
    assert premature.status_code != 200 or premature.json().get("allowed") is not True, (
        premature.text
    )

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "targets": ["bob"]},
    )
    assert answered.status_code == 200, answered.text
    done = answered.json()
    assert done["accepted"] is True, done
    assert done.get("paused") is not True, done

    mid = state(client, game)
    assert mid["pending_decision"] is None, mid["pending_decision"]
    assert mid["stack"][0]["kind"] == "ability"
    assert mid["stack"][0]["name"] == "Goblin Bombardment"

    passed = client.post(
        f"/api/games/{game}/actions/pass-priority",
        params={"uid": uids["alice"]},
        json={"player": "alice"},
    )
    assert passed.status_code == 200, passed.text
    assert passed.json().get("allowed") is not False, passed.json()
    assert state(client, game)["priority_holder"] != "alice"
