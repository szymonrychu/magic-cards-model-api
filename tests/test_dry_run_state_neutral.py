"""A dry run must leave the game byte-identical, not merely "the card came
back to the right zone" - CR 733.1's "entire action is reversed", in full.

The mutation this pins: `Casting.__init__` moves a card onto the stack the
instant it is built (CR 601.2a), whether or not the caller ever means to
finish the cast, and `Casting.cancel()`'s own rewind (`rewind_unfinished`)
puts it back with `Player.add`, which *appends* to the end of the zone's list
rather than reinserting at the index it started from. Invisible with one card
in a hand - `test_dry_run_tapped.py` and `test_affordances.py`'s alternative-
cost tests already pin "the card is still there", each with exactly one
candidate card - and a real corruption with more than one: a hand `[A, B, C]`
probed for `B` came back `[A, C, B]`.

`engine_api.dry_run_copy`/`affordances.alternative_costs` fix this by never
touching the real game at all: a probe runs against a throwaway copy, so the
worst a probe can do is wreck a copy nobody keeps. These tests build a
multi-card hand specifically so a reorder would be visible, and would have
failed before that fix (`cast`'s dry run) and before `alternative_costs` was
pointed at a copy of its own.

`seatkit.available_actions` is `magic-cards-model-api`'s heaviest user of
this path - one dry run per hand card, per graveyard/exile card with a cast
option, and per battlefield ability - so its own multi-candidate run is
covered here too, end to end through the real HTTP surface (`wired_seats`,
the same trick `test_graveyard_cast_options.py` uses). One nuance worth being
honest about: a full sweep that dry-runs *every* hand card once, in the
same left-to-right order the hand is already in, is a queue rotated exactly
once per element - the corrupted `cancel()` moves each probed card to the
back in turn, and after the last one that is, arithmetically, a full cycle
back to the original order, whether or not the underlying bug is fixed.
Checked directly against the unfixed code before writing this file. So the
`available_actions`-level test below is real end-to-end coverage (nothing
else in that path may mutate `game` either), but the two single/partial-probe
tests above it - not a full, order-matched sweep - are what actually pin the
reorder: both were confirmed to fail on the unfixed code before this file
was finished.
"""

from __future__ import annotations

import httpx
from conftest import add_card, card_id, state
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats, available_actions
from magic_cards_model_api.store import store


def affordances(client: TestClient, game: str, card_id_: str, player: str = "alice") -> dict:
    response = client.get(f"/api/games/{game}/affordances/{card_id_}", params={"player": player})
    assert response.status_code == 200, response.text
    return response.json()


def wired_seats(monkeypatch, game_id: str) -> tuple[Seats, dict[str, str]]:
    """`Seats` pointed at the in-process `TestClient` - see
    `test_graveyard_cast_options.py`'s copy of this same trick."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    uids = {name: uid for uid, name in store.seat_uids(game_id).items()}
    return Seats(api="http://testserver", game_id=game_id), uids


def test_dry_run_cast_does_not_reorder_a_multi_card_hand(client, game):
    add_card(client, game, "Giant Growth", "alice", "hand")
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Counterspell", "alice", "hand")
    mountain = card_id(client, game, "Mountain", "alice", "battlefield")
    island = card_id(client, game, "Island", "alice", "battlefield")

    before = [c["name"] for c in state(client, game)["hands"]["alice"]]
    assert before == ["Giant Growth", "Lightning Bolt", "Counterspell"]

    res = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt,
            "targets": ["bob"],
            "mana_sources": [mountain, island],
            "dry_run": True,
        },
    ).json()
    assert res["allowed"] is True, res

    after = [c["name"] for c in state(client, game)["hands"]["alice"]]
    assert after == before


def test_dry_run_cast_leaves_the_whole_board_untouched(client, game):
    """Not just the probed card's own zone: the board as a whole, compared
    key for key, so a future mutation anywhere in the announcement path (a
    counter, a stamp, another card's position) is caught the same way. The
    probed card (`Lightning Bolt`) is the *middle* one of three - not the
    last - so `cancel()`'s append-to-the-end bug is visible in the full-state
    diff rather than accidentally landing back where it started."""
    add_card(client, game, "Giant Growth", "alice", "hand")
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Counterspell", "alice", "hand")
    mountain = card_id(client, game, "Mountain", "alice", "battlefield")
    island = card_id(client, game, "Island", "alice", "battlefield")

    before = state(client, game)
    res = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt,
            "targets": ["bob"],
            "mana_sources": [mountain, island],
            "dry_run": True,
        },
    ).json()
    assert res["allowed"] is True, res
    after = state(client, game)
    assert before == after


def test_asking_for_alternative_costs_does_not_reorder_a_multi_card_hand(client, game):
    add_card(client, game, "Giant Growth", "alice", "hand")
    subtlety = card_id(client, game, "Subtlety", "alice", "hand")
    add_card(client, game, "Counterspell", "alice", "hand")

    before = [c["name"] for c in state(client, game)["hands"]["alice"]]
    assert before == ["Giant Growth", "Subtlety", "Counterspell"]

    for _ in range(3):
        result = affordances(client, game, subtlety)
        assert result["alternative_costs"], result

    after = state(client, game)
    assert [c["name"] for c in after["hands"]["alice"]] == before
    assert after["stack"] == []


def test_available_actions_does_not_reorder_or_mutate_the_board(client, game, monkeypatch):
    """`seatkit.available_actions` is the real caller that turned this
    corruption into a live-service problem: one dry run per hand card, per
    ability. Every candidate here is probed in one call."""
    add_card(client, game, "Giant Growth", "alice", "hand")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Counterspell", "alice", "hand")
    add_card(client, game, "Faithful Mending", "alice", "graveyard")
    for _ in range(3):
        add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Plains", "alice", "battlefield")

    seats, uids = wired_seats(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])

    before = state(client, game, uid=uids["alice"])
    hand_before = [c["name"] for c in before["hands"]["alice"]]

    actions = available_actions(alice, before)
    assert actions["castable"], actions  # sanity: this run actually probed something

    after = state(client, game, uid=uids["alice"])
    assert [c["name"] for c in after["hands"]["alice"]] == hand_before
    assert after == before
