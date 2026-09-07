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
- **the clients.** `ui/decisions.js` names each option instead of slicing the id, and
  falls back to the position in `pending.cards` - never a raw id - for an
  object it cannot find.

The one case still not named by `card_details` is CR 603.3b's own first
batch, and the reason is the rule's own sequence: the question is asked one
step *before* CR 603.3's "its controller puts it on the stack" happens, so
those abilities are on the stack in no sense yet and no zone holds them
(`visibility.redact_pending_decision`'s own docstring says so, and
`test_order_triggers_card_details_names_each_ability_source` below is the
`xfail` with the engine change it needs written out). `PendingDecision.
card_labels` - the engine's own naming, stamped directly onto the `Decision`
rather than resolved against a zone - is what actually carries the names for
this kind today, and is what every test below reads.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from conftest import add_card, state
from fastapi.testclient import TestClient
from ui_node import requires_node, run_module

from magic_cards_model_api import seat_cli
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store

#: Delver of Secrets' printed upkeep trigger, as `serialize._stack_view` would
#: carry it - both copies read exactly this, which is the point.
UPKEEP_LOOK = "At the beginning of your upkeep, look at the top card of your library."


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


# -- a real CR 603.3b question, from a real board ----------------------------


def stage_order_triggers_decision(client: TestClient, game_id: str, uid: str) -> dict[str, Any]:
    """A real `order_triggers` question, from a real board: two Delver of
    Secrets on alice's battlefield and one real untap-to-upkeep `advance`,
    which fires both "at the beginning of your upkeep" abilities in the same
    batch (CR 603.3b's own premise, "multiple abilities have triggered since
    the last time a player received priority") - the same construction
    `test_decision_validator_drift.py`'s `board` fixture uses. No card is
    invented and no id is hand-assigned; `advance` now pauses on the question
    and returns it immediately rather than blocking a thread for an answer to
    land on (`test_decisions.py`'s module docstring), so this reads it
    straight off that response.
    """
    add_card(client, game_id, "Delver of Secrets", "alice", "battlefield")
    add_card(client, game_id, "Delver of Secrets", "alice", "battlefield")
    client.put(f"/api/games/{game_id}/step", json={"step": "untap", "player": "alice"})
    advanced = client.post(
        f"/api/games/{game_id}/advance", params={"uid": uid}, json={"player": "alice"}
    )
    body = advanced.json()
    assert body.get("paused") is True, body
    pending = body["pending_decision"]
    assert pending["kind"] == "order_triggers", pending
    return pending


# -- the payload a human's browser and an agent both read -------------------


def test_the_payload_names_an_ability_the_decision_is_about(client, game):
    """The API half. `card_details` never resolves CR 603.3b's own first
    batch (see this module's own docstring, and the `xfail` at the bottom) -
    but `Decision.card_labels`, the engine's own naming stamped directly onto
    the `Decision` rather than resolved against a zone, does: one label per
    `pending.cards` entry, positionally aligned, naming the source and the
    ability's own text - so no client has to know what a hex string means."""
    uids = seats_for(client, game)
    pending = stage_order_triggers_decision(client, game, uids["alice"])

    assert "card_details" not in pending, (
        "these abilities are in no zone yet (see this module's own docstring) "
        "- card_labels, not card_details, is what actually names them today"
    )
    assert len(pending["cards"]) == 2
    assert len(pending["card_labels"]) == 2
    for label in pending["card_labels"]:
        assert label.startswith("Delver of Secrets"), label
        assert UPKEEP_LOOK in label


def test_the_opponent_is_told_nothing_the_decision_names(client, game):
    """The stack being public (CR 400.2) does not make somebody else's pending
    decision public: CR 101.4a's "clearly indicate which ... they chose" makes
    only the *fact* of a choice public, and `redact_pending_decision` still
    strips this whole payload down to `{player, kind, prompt}` for anyone but
    the deciding seat - `card_labels` included."""
    uids = seats_for(client, game)
    stage_order_triggers_decision(client, game, uids["alice"])

    pending = state(client, game, uid=uids["bob"])["pending_decision"]
    assert set(pending) == {"player", "kind", "prompt"}
    assert "card_details" not in pending
    assert "card_labels" not in pending


# -- what an agent reads through MCP and the CLI ----------------------------


def wired_seats(monkeypatch, game_id: str) -> Seats:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id)


def test_an_agent_sees_the_source_named_through_mcp(client, game, monkeypatch):
    """`mcp.my_state` prints the seat's whole filtered state view, so the same
    `card_labels` the browser reads is what an agent seat reads."""
    uids = seats_for(client, game)
    pending = stage_order_triggers_decision(client, game, uids["alice"])

    server = build_server(wired_seats(monkeypatch, game))
    result = asyncio.run(server.call_tool("my_state", {"player_uid": uids["alice"]}))
    seen = json.loads(result.content[0].text)["pending_decision"]
    assert seen["cards"] == pending["cards"]
    assert len(seen["card_labels"]) == 2
    assert all(label.startswith("Delver of Secrets") for label in seen["card_labels"])


def test_an_agent_sees_the_source_named_through_the_seat_cli(client, game, monkeypatch, capsys):
    """`seat_cli state` prints the same view verbatim."""
    uids = seats_for(client, game)
    stage_order_triggers_decision(client, game, uids["alice"])

    wired_seats(monkeypatch, game)
    seat_cli.run(["--uid", uids["alice"], "--game", game, "state"])
    printed = json.loads(capsys.readouterr().out)["pending_decision"]
    assert len(printed["card_labels"]) == 2
    assert all(label.startswith("Delver of Secrets") for label in printed["card_labels"])


# -- what a human sees in the browser ---------------------------------------


needs_node = requires_node


def trigger_order_options(pending: dict[str, Any]) -> list[dict[str, str]]:
    """`ui/decisions.js`'s own `triggerOrderOptions`, run under Node against
    the exact file the browser is served - never a Python re-description of
    it. Same trick as `tests/test_ui_actions.py`."""
    return run_module("decisions.js", "m.triggerOrderOptions(p)", p=pending)


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
        "needs an engine change to `card_details` specifically, not to naming "
        "in general - `Decision.card_labels` (`PendingDecision.card_labels`) "
        "already carries the (source name, ability text) pairs "
        "`machines/decisions.ask_trigger_order` builds them from, and every "
        "test above this one reads that field. `card_details` is a different "
        "mechanism (`visibility._card_lookup`, resolved against a zone) and "
        "stays genuinely empty for this kind: CR 603.3b is asked one step "
        "before CR 603.3's 'its controller puts it on the stack', so the "
        "abilities it names are in no zone the API can read at ask time - "
        "`Game._put_pending_triggers_on_stack` holds them in a local list "
        "until then. Until `_card_lookup` (or some other source) can resolve "
        "an id that is not yet in any zone, `card_details` cannot carry them."
    ),
)
def test_order_triggers_card_details_names_each_ability_source(client, game):
    """The board CR 603.3b's premise needs - two real Delver of Secrets, one
    controller - advanced untap to upkeep so the engine raises the question of
    its own accord. `card_details` specifically must be told what they are -
    `card_labels` already is (see every test above)."""
    uids = seats_for(client, game)
    pending = stage_order_triggers_decision(client, game, uids["alice"])
    assert pending["kind"] == "order_triggers"
    named = [entry["name"] for entry in pending.get("card_details", [])]
    assert named == ["Delver of Secrets", "Delver of Secrets"]
