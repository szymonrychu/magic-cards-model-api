"""Naming the objects a CR 603.3b question is about, instead of hex.

CR 603.3b: "first, each player, in APNAP order, puts each triggered ability
they control on the stack in any order they choose". The engine asks it, and
this API can answer it (`decisions.ANSWERABLE_KINDS`, `app.py`'s `decide`) -
but `Decision.cards` for that kind holds `AbilityOnStack` ids, not card ids.
CR 113.1c: "an ability can be an activated or triggered ability on the stack.
This kind of ability is an object", and CR 603.3 puts a triggered ability
there "as an object that's not a card". So `visibility._card_lookup`, which
walked hands, libraries, sideboards, graveyards and the battlefield and
nothing else, could never match one: `card_details` came back empty for this
kind always, and every client fell back to printing eight hex digits.

Two halves are fixed here, and they are separable on purpose:

- **the payload.** `_card_lookup` walks the stack too now. CR 400.2 makes that
  no widening at all - "public zones are zones in which all players can see the
  cards' faces ... graveyard, battlefield, stack, exile, ante, and command are
  public zones" - and `serialize._stack_view`'s ability entry already carries
  the source's printed name and the ability's own text, which is what tells two
  triggers of the *same* permanent apart. Every surface that prints the state
  view gets this for free: the browser, `mcp.my_state`, `seat_cli state`.
- **the clients.** `ui/app.js` names each option instead of slicing the id, and
  falls back to the position in `pending.cards` - never a raw id - for an
  object it cannot find.

The one case still not named by the payload is CR 603.3b's own first batch,
and the reason is the rule's own sequence: the question is asked one step
*before* CR 603.3's "its controller puts it on the stack" happens, so those
abilities are on the stack in no sense yet and no zone holds them. That case
is `test_order_triggers_card_details_names_each_ability_source` below, an
`xfail` with the engine change it needs written out.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from conftest import add_card, state
from fastapi.testclient import TestClient
from magic_cards_model.machines.decisions import Decision, DecisionKind
from test_decision_kinds import (
    advance_to_upkeep_in_background,
    seats_for,
    set_short_timeout,
    stage_two_simultaneous_upkeep_triggers,
    wait_for_pending,
)

from magic_cards_model_api import seat_cli
from magic_cards_model_api.decisions import PendingDecision
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store

APP_JS = Path(__file__).resolve().parents[1] / "ui" / "app.js"

#: Delver of Secrets' printed upkeep trigger, as `serialize._stack_view` would
#: carry it - both copies read exactly this, which is the point.
UPKEEP_LOOK = "At the beginning of your upkeep, look at the top card of your library."


# -- a real ability, really on the stack ------------------------------------


def stage_an_ability_on_the_stack(client: TestClient, game_id: str) -> str:
    """Alice activates Mind Stone's second ability for real, and it sits on
    the stack unresolved. Returns its `AbilityOnStack` id.

    A real printing from the recorded Scryfall fixtures, activated through the
    ordinary `actions/activate` endpoint and paid for with a real Island - no
    card json is written here and no object is fabricated. "{1}, {T}, Sacrifice
    this artifact: Draw a card" is not a mana ability (CR 605.1a), so unlike
    Mind Stone's own "{T}: Add {C}" it really does use the stack (CR 602.2a).
    """
    add_card(client, game_id, "Mind Stone", "alice", "battlefield")
    add_card(client, game_id, "Island", "alice", "battlefield")
    board = state(client, game_id)["battlefield"]
    (stone,) = [card for card in board if card["name"] == "Mind Stone"]
    islands = [card["id"] for card in board if card["name"] == "Island"]
    response = client.post(
        f"/api/games/{game_id}/actions/activate",
        json={
            "player": "alice",
            "card_id": stone["id"],
            "ability_index": 1,
            "mana_sources": islands,
        },
    )
    assert response.json()["allowed"] is True, response.text
    return response.json()["ability_id"]


def ask_to_order(game_id: str, player: str, ability_ids: list[str]) -> None:
    """Put a CR 603.3b question about `ability_ids` on this game's mailbox.

    The `Decision` is the engine's own type and every id in it is a real
    `AbilityOnStack.id` from the real stack - what is skipped is only the
    *timing* of the engine's own ask, which cannot be reached with an ability
    already on the stack (see this module's docstring), and which the
    end-to-end test below drives for real instead. Nothing about a card is
    invented here.
    """
    decision = Decision(
        kind=DecisionKind.ORDER_TRIGGERS,
        player=player,
        prompt="order your triggered abilities on the stack",
        cards=tuple(UUID(ability_id) for ability_id in ability_ids),
    )
    mailbox = store.mailbox(game_id)
    assert mailbox is not None
    mailbox.publish(PendingDecision.from_engine(decision), decision)


# -- the payload a human's browser and an agent both read -------------------


def test_the_payload_names_an_ability_the_decision_is_about(client, game):
    """The API half. A decision naming an `AbilityOnStack` id resolves that id
    to its source's printed name and the ability's own text, in
    `card_details`, exactly the way a scry's cards already resolve - so no
    client has to know what a hex string means."""
    uids = seats_for(client, game)
    ability_id = stage_an_ability_on_the_stack(client, game)
    ask_to_order(game, "alice", [ability_id])

    pending = state(client, game, uid=uids["alice"])["pending_decision"]
    details = {entry["id"]: entry for entry in pending["card_details"]}
    assert ability_id in details, f"the ability id was not resolved: {pending}"
    assert details[ability_id]["name"] == "Mind Stone"
    assert details[ability_id]["text"] == "{1}, {T}, Sacrifice this artifact: Draw a card."
    assert details[ability_id]["kind"] == "ability"


def test_the_opponent_is_told_nothing_the_decision_names(client, game):
    """The stack being public (CR 400.2) does not make somebody else's pending
    decision public: CR 101.4a's "clearly indicate which ... they chose" makes
    only the *fact* of a choice public, and `redact_pending_decision` still
    strips this whole payload down to `{player, kind, prompt}` for anyone but
    the deciding seat. Walking the stack must not have widened that."""
    uids = seats_for(client, game)
    ability_id = stage_an_ability_on_the_stack(client, game)
    ask_to_order(game, "alice", [ability_id])

    pending = state(client, game, uid=uids["bob"])["pending_decision"]
    assert set(pending) == {"player", "kind", "prompt"}
    assert "card_details" not in pending


# -- what an agent reads through MCP and the CLI ----------------------------


def wired_seats(monkeypatch, game_id: str) -> Seats:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id)


def test_an_agent_sees_the_source_named_through_mcp(client, game, monkeypatch):
    """`mcp.my_state` prints the seat's whole filtered state view, so the same
    `card_details` the browser reads is what an agent seat reads."""
    uids = seats_for(client, game)
    ability_id = stage_an_ability_on_the_stack(client, game)
    ask_to_order(game, "alice", [ability_id])

    server = build_server(wired_seats(monkeypatch, game))
    result = asyncio.run(server.call_tool("my_state", {"player_uid": uids["alice"]}))
    seen = json.loads(result.content[0].text)["pending_decision"]
    named = {entry["id"]: entry["name"] for entry in seen["card_details"]}
    assert named == {ability_id: "Mind Stone"}


def test_an_agent_sees_the_source_named_through_the_seat_cli(client, game, monkeypatch, capsys):
    """`seat_cli state` prints the same view verbatim."""
    uids = seats_for(client, game)
    ability_id = stage_an_ability_on_the_stack(client, game)
    ask_to_order(game, "alice", [ability_id])

    wired_seats(monkeypatch, game)
    seat_cli.run(["--uid", uids["alice"], "--game", game, "state"])
    printed = json.loads(capsys.readouterr().out)["pending_decision"]
    named = {entry["id"]: entry["name"] for entry in printed["card_details"]}
    assert named == {ability_id: "Mind Stone"}


# -- what a human sees in the browser ---------------------------------------


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def trigger_order_options(pending: dict[str, Any]) -> list[dict[str, str]]:
    """`ui/app.js`'s own `triggerOrderOptions`, run under Node against the
    exact file the browser is served - never a Python re-description of it.
    Same trick as `tests/test_ui_autopilot.py`."""
    script = f"""
    const {{ triggerOrderOptions }} = require({json.dumps(str(APP_JS))});
    process.stdout.write(JSON.stringify(triggerOrderOptions({json.dumps(pending)})));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


@needs_node
def test_the_ui_names_the_source_of_each_trigger_it_offers():
    """Two triggers of two different permanents, in the order they triggered.
    Each button reads as a sentence a player can act on."""
    options = trigger_order_options(
        {
            "cards": [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
            "card_details": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "kind": "ability",
                    "name": "Delver of Secrets",
                    "text": UPKEEP_LOOK,
                },
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "kind": "ability",
                    "name": "Mind Stone",
                    "text": "{1}, {T}, Sacrifice this artifact: Draw a card.",
                },
            ],
        }
    )
    labels = [option["label"] for option in options]
    assert "Delver of Secrets" in labels[0]
    assert "Mind Stone" in labels[1]
    assert "look at the top card" in labels[0]
    # the ids never reach the label - that was the whole bug
    assert not any("11111111" in label for label in labels)


@needs_node
def test_the_ui_tells_two_triggers_of_the_same_source_apart():
    """Two copies of one card is the normal case for CR 603.3b, and both read
    the same printed text, so the name alone can never distinguish them. The
    position in `pending.cards` - which is the order they triggered in, and
    what the answer is a permutation of - is carried on every label."""
    options = trigger_order_options(
        {
            "cards": [
                "aaaaaaaa-0000-0000-0000-000000000000",
                "bbbbbbbb-0000-0000-0000-000000000000",
            ],
            "card_details": [
                {
                    "id": "aaaaaaaa-0000-0000-0000-000000000000",
                    "kind": "ability",
                    "name": "Delver of Secrets",
                    "text": UPKEEP_LOOK,
                },
                {
                    "id": "bbbbbbbb-0000-0000-0000-000000000000",
                    "kind": "ability",
                    "name": "Delver of Secrets",
                    "text": UPKEEP_LOOK,
                },
            ],
        }
    )
    labels = [option["label"] for option in options]
    assert labels[0] != labels[1]
    assert all("Delver of Secrets" in label for label in labels)
    assert labels[0].startswith("#1") and labels[1].startswith("#2")


@needs_node
def test_the_ui_never_shows_a_bare_id_when_it_cannot_resolve_one():
    """The state CR 603.3b's first batch is genuinely in (see the module
    docstring): nothing to resolve the ids against. The fallback is the
    position in `pending.cards`, which is exactly what
    `machines/decisions.ask_trigger_order`'s prompt numbers against - so the
    heading and the buttons line up - and never eight hex digits, which no
    player and no agent can act on."""
    options = trigger_order_options(
        {
            "cards": [
                "deadbeef-0000-0000-0000-000000000000",
                "feedface-0000-0000-0000-000000000000",
            ],
            "card_details": [],
        }
    )
    labels = [option["label"] for option in options]
    assert labels == ["trigger #1", "trigger #2"]
    assert not any("deadbeef" in label for label in labels)


@needs_node
def test_the_ui_still_sends_bottom_of_the_stack_first():
    """`submitTriggerOrder` reverses the player's *resolution* order into the
    stack order CR 603.3b's answer is given in - the last id put on the stack
    is the first to resolve. Labelling the options must not have disturbed
    that; the ids come back untouched and in the offered order."""
    ids = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
    options = trigger_order_options({"cards": ids, "card_details": []})
    assert [option["id"] for option in options] == ids


# -- the engine's own ask, end to end ---------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "needs an engine change. CR 603.3b is asked one step before CR 603.3's "
        "'its controller puts it on the stack', so the abilities it names are "
        "in no zone the API can read - `Game._put_pending_triggers_on_stack` "
        "holds them in a local list. `machines/decisions.ask_trigger_order` "
        "already has the `(id, source_name)` pairs and spends them on the "
        "prompt text; until a `Decision` field carries them, no payload can "
        "resolve these ids."
    ),
)
def test_order_triggers_card_details_names_each_ability_source(client, game):
    """The board CR 603.3b's premise needs - two real Delver of Secrets, one
    controller - advanced untap to upkeep so the engine raises the question of
    its own accord. The seat asked to order them must be told what they are."""
    uids = seats_for(client, game)
    stage_two_simultaneous_upkeep_triggers(client, game)
    set_short_timeout(game, 5.0)

    thread = advance_to_upkeep_in_background(game)
    try:
        pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
        assert pending["kind"] == "order_triggers"
        named = [entry["name"] for entry in pending.get("card_details", [])]
        assert named == ["Delver of Secrets", "Delver of Secrets"]
    finally:
        answer = state(client, game, uid=uids["alice"])["pending_decision"]
        if answer is not None:
            client.post(
                f"/api/games/{game}/decide",
                params={"uid": uids["alice"]},
                json={"player": "alice", "order": answer["cards"]},
            )
        thread.join(timeout=10)
