"""The two "this card lives under that card" relationships, on the wire.

The engine models both and the state view carried neither, so the play UI had
no honest way to draw an Aura tucked under the creature it enchants or an
imprinted card under the artifact that exiled it:

- `CardState.attachment` - what an Aura or an Equipment is attached to, a
  card *or* a player - now `attached_to` on a battlefield card's view, in the
  same `{kind, id, name}` shape every other reference on this wire uses;
- `ExileAnnotation.linked_permanent_id` - the permanent a card in exile was
  exiled *with* (imprint, hideaway) - now `linked_permanent_id` inside the
  card's existing `exile_pile` block.

The Aura case is a real board played through the real API. The exile link is
asserted against a real `Game` out of the store with a real annotation on it:
no card in `tests/fixtures/scryfall` imprints or hides away, and the point
under test is the serializer, not the effect that sets the field.
"""

from __future__ import annotations

import time

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from magic_cards_model.models import Zone
from magic_cards_model.models.card_state import ExileAnnotation, ExileMarker

from magic_cards_model_api import serialize
from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def wait_for_pending(client: TestClient, game_id: str, uid: str, tries: int = 300) -> dict:
    for _ in range(tries):
        view = state(client, game_id, uid=uid)
        if view.get("pending_decision") is not None:
            return view
        time.sleep(0.01)
    raise AssertionError("no pending decision showed up in time")


def test_an_aura_reports_the_permanent_it_is_attached_to(client, game):
    """Utopia Sprawl, cast on one of alice's own Forests and resolved. The
    Aura's own view has to name the Forest, or the board cannot draw it in
    the pile it physically sits in."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Utopia Sprawl", "alice", "hand")
    uids = seats_for(client, game)
    board = state(client, game, uid=uids["alice"])
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]
    sprawl = board["hands"]["alice"][0]

    client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": sprawl["id"],
            "mana_sources": [forests[0]],
            "targets": [forests[1]],
        },
    )
    client.post(f"/api/games/{game}/resolve-top")
    pending = wait_for_pending(client, game, uids["alice"])["pending_decision"]
    assert pending["kind"] == "choose_a_quality"
    client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "R"},
    )

    final = state(client, game)
    attached = find(final["battlefield"], "Utopia Sprawl")["attached_to"]
    assert attached == {"kind": "card", "id": forests[1], "name": "Forest"}


def test_a_permanent_attached_to_nothing_carries_no_attachment_field(client, game):
    """An Equipment enters attached to nothing, and so does every ordinary
    permanent - the key is simply absent rather than a null every client has
    to special-case."""
    add_card(client, game, "Sol Ring", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    board = state(client, game)
    for name in ("Sol Ring", "Grizzly Bears"):
        assert "attached_to" not in find(board["battlefield"], name)


def test_a_card_in_hand_carries_no_attachment_field(client, game):
    """Attachment is a battlefield-only relationship - a card in hand is not
    attached to anything, and the engine clears the field on the way out of
    the battlefield anyway."""
    add_card(client, game, "Utopia Sprawl", "alice", "hand")
    board = state(client, game)
    assert "attached_to" not in board["hands"]["alice"][0]


def test_a_card_exiled_with_a_permanent_names_it(client, game):
    """`ExileAnnotation.linked_permanent_id` reaches the wire inside the
    card's own `exile_pile` block, so the UI can draw the imprinted card
    under the permanent that exiled it instead of loose in exile."""
    add_card(client, game, "Sol Ring", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "exile")
    engine = store.get(game)
    ring = next(c for c in engine.shared_zones[Zone.BATTLEFIELD] if c.name == "Sol Ring")
    bolt = next(c for c in engine.shared_zones[Zone.EXILE] if c.name == "Lightning Bolt")
    bolt.exile_annotation = ExileAnnotation(
        marker=ExileMarker.IMPRINTED, owner="alice", linked_permanent_id=ring.id
    )

    view = serialize.state_view(engine, game)
    pile = find(view["exile"], "Lightning Bolt")["exile_pile"]
    assert pile["marker"] == "imprinted"
    assert pile["linked_permanent_id"] == str(ring.id)


def test_an_exile_pile_with_no_linked_permanent_says_nothing_about_one(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "exile")
    engine = store.get(game)
    bolt = next(c for c in engine.shared_zones[Zone.EXILE] if c.name == "Lightning Bolt")
    bolt.exile_annotation = ExileAnnotation(marker=ExileMarker.FORETOLD, owner="alice")

    view = serialize.state_view(engine, game)
    pile = find(view["exile"], "Lightning Bolt")["exile_pile"]
    assert "linked_permanent_id" not in pile


def test_a_prepared_permanent_reports_prepared(client, game):
    """`CardState.prepared` (CR 722.3a, the Reality Fracture bump) reaches
    the wire as `prepared` on a battlefield card's view - the point under
    test is the serializer, not the prepare-spell effect that sets the
    field, so the flag is set directly the same way the exile link above is."""
    add_card(client, game, "Sol Ring", "alice", "battlefield")
    engine = store.get(game)
    ring = next(c for c in engine.shared_zones[Zone.BATTLEFIELD] if c.name == "Sol Ring")
    ring.prepared = True

    view = serialize.state_view(engine, game)
    assert find(view["battlefield"], "Sol Ring")["prepared"] is True


def test_an_unprepared_permanent_carries_no_prepared_field(client, game):
    add_card(client, game, "Sol Ring", "alice", "battlefield")
    engine = store.get(game)

    view = serialize.state_view(engine, game)
    assert "prepared" not in find(view["battlefield"], "Sol Ring")
