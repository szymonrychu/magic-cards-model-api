"""The audit snapshot's own copy of the two "this card lives under that
card" relationships - `test_serialize_attachments.py`'s pair, but for
`serialize.snapshot` rather than `serialize.state_view`.

`snapshot` writes one *string* per permanent (`battlefield_line`) and bare
names for exile, so neither `CardState.attachment` nor `ExileAnnotation.
linked_permanent_id` had anywhere to go: a review frame built from a
`records` row's snapshot could never tuck an Aura or Equipment under its
host, or link an imprinted card to the artifact holding it, even though the
live board draws both (`ui/board_layout.js`).

Both relationships now ride along as a small `(attached:N)` suffix, where
`N` is the *index* of the host within this same snapshot's own `battlefield`
list - not the engine's uuid. That is deliberate: a snapshot carries no
card ids at all, `ui/review_model.js`'s `frameState` builds `bf-${index}`
ids straight off each battlefield line's position in the array, and the
Python side and the JS side iterate the exact same list in the exact same
order (`game.shared_zones[Zone.BATTLEFIELD]`) - so the index is the one
identity both sides already agree on for free.

The Aura case is a real board played through the real API, and the point
under test is that the relationship survives all the way into a `records`
row's own snapshot - not only into a snapshot built by hand. The exile link
is asserted against `serialize.snapshot` directly, against a real `Game` out
of the store with a real annotation set on it, the same way the live-view
test does: no card in `tests/fixtures/scryfall` imprints or hides away.
"""

from __future__ import annotations

import time
from uuid import uuid4

from conftest import add_card, state
from fastapi.testclient import TestClient
from magic_cards_model.models import TargetRef, Zone
from magic_cards_model.models.card_state import ExileAnnotation, ExileMarker

from magic_cards_model_api import audit, serialize
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


def _index_of(lines: list[str], prefix: str) -> int:
    matches = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    assert len(matches) == 1, f"expected exactly one of {prefix!r} among {lines}"
    return matches[0]


def test_an_aura_attached_to_a_permanent_survives_into_a_records_rows_snapshot(
    client: TestClient, game
) -> None:
    """Utopia Sprawl, cast on alice's second Forest and resolved - the same
    board `test_serialize_attachments.py`'s live-view test builds. Its own
    line in the *last records row's* snapshot has to name the index its
    host (that same Forest) occupies in that same snapshot's battlefield
    list, or a review frame can never draw the tuck the live board draws."""
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
    decided = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "R"},
    )
    assert decided.status_code == 200, decided.text

    entries = audit.entries(game)
    last = entries[-1]
    lines = last["snapshot"]["battlefield"]
    assert len(lines) == 3, lines

    # Cards enter this list in the order they entered the battlefield -
    # identical on both the state view and the snapshot - so `forests[0]`
    # (tapped to pay for Utopia Sprawl) is index 0 and `forests[1]` (the
    # enchant target, left untapped) is index 1; Utopia Sprawl itself, the
    # last permanent to arrive, is index 2.
    assert lines[0] == "alice: Forest (tapped)"
    assert lines[1] == "alice: Forest"
    assert lines[2] == "alice: Utopia Sprawl (attached:1)"


def test_a_permanent_attached_to_nothing_carries_no_marker(client: TestClient, game) -> None:
    add_card(client, game, "Sol Ring", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    lines = serialize.snapshot(store.get(game))["battlefield"]
    assert "attached:" not in " ".join(lines)


def test_a_card_exiled_with_a_permanent_names_its_battlefield_index(
    client: TestClient, game
) -> None:
    """`ExileAnnotation.linked_permanent_id` (imprint, hideaway) reaches the
    snapshot's `exile` list the same way `attached_to` reaches
    `battlefield`: as `(attached:N)`, `N` the host's index in *this same
    snapshot's* battlefield list."""
    add_card(client, game, "Sol Ring", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "exile")
    engine = store.get(game)
    ring = next(c for c in engine.shared_zones[Zone.BATTLEFIELD] if c.name == "Sol Ring")
    bolt = next(c for c in engine.shared_zones[Zone.EXILE] if c.name == "Lightning Bolt")
    bolt.exile_annotation = ExileAnnotation(
        marker=ExileMarker.IMPRINTED, owner="alice", linked_permanent_id=ring.id
    )

    snap = serialize.snapshot(engine)
    ring_index = _index_of(snap["battlefield"], "alice: Sol Ring")
    assert snap["exile"] == [f"Lightning Bolt (attached:{ring_index})"]


def test_an_exiled_card_with_no_link_is_a_bare_name(client: TestClient, game) -> None:
    add_card(client, game, "Lightning Bolt", "alice", "exile")
    engine = store.get(game)
    bolt = next(c for c in engine.shared_zones[Zone.EXILE] if c.name == "Lightning Bolt")
    bolt.exile_annotation = ExileAnnotation(marker=ExileMarker.FORETOLD, owner="alice")

    snap = serialize.snapshot(engine)
    assert snap["exile"] == ["Lightning Bolt"]


def test_a_link_to_a_permanent_no_longer_on_the_battlefield_is_dropped(
    client: TestClient, game
) -> None:
    """`ExileAnnotation.linked_permanent_id` is never cleared when the host
    leaves the battlefield (see its own docstring) - so this has to be a
    lookup that can find nothing, exactly like `_card_view`'s live
    `attached_to`, rather than a promise that always resolves."""
    add_card(client, game, "Lightning Bolt", "alice", "exile")
    engine = store.get(game)
    bolt = next(c for c in engine.shared_zones[Zone.EXILE] if c.name == "Lightning Bolt")

    bolt.exile_annotation = ExileAnnotation(
        marker=ExileMarker.IMPRINTED, owner="alice", linked_permanent_id=uuid4()
    )

    snap = serialize.snapshot(engine)
    assert snap["exile"] == ["Lightning Bolt"]


def test_an_aura_enchanting_a_player_carries_no_marker(client: TestClient, game) -> None:
    """CR 303.4's other case - an Aura attached to a player rather than a
    card. The live board never tucks it under anything either
    (`board_layout.rootParentId` only follows a `kind: "card"` reference),
    so the snapshot says nothing about it rather than pointing nowhere."""
    add_card(client, game, "Sol Ring", "alice", "battlefield")
    engine = store.get(game)
    ring = next(c for c in engine.shared_zones[Zone.BATTLEFIELD] if c.name == "Sol Ring")
    ring.attachment = TargetRef.to_player("bob")

    lines = serialize.snapshot(engine)["battlefield"]
    assert lines == ["alice: Sol Ring"]
