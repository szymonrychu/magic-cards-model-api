"""The leak matrix for `known_cards` and the own-only unordered contents blocks.

`serialize.state_view` emits, beside `hands` and never inside it, what each
player knows about hidden cards (`Game.known_to`): cards in another player's
hand, cards in a library with their live position, and sideboard cards. It
also emits, on request only (`?include=contents`), each player's own library
and sideboard as an unordered multiset with no ids and no positions.
`visibility.redact_view` hands each viewer its own entry and nothing else.

Every case here drives the real engine through `TestClient`. The engine owns
what is known; these tests pin that the api neither widens nor narrows it:

- CR 402.3: "A player can't look at the cards in another player's hand but
  may count those cards at any time." A known hand card is the exception a
  reveal or look made, and only for whoever was shown it.
- CR 401.2: "Players can't look at or change the order of cards in a
  library." So the contents blocks carry no order, and only the owner's.
- CR 701.20a / CR 701.20e: a reveal shows a card to all players; a look shows
  it "only to the specified player" - looks are one way.
- CR 701.20d: a library reordering ends a library reveal; CR 701.24a's
  shuffle is one.
- CR 400.7: a card that changes zones is a new object, which ends a record.
- CR 400.11a: sideboard cards are outside the game.
- CR 103.5: a mulligan shuffles the hand back into the library.

Every CR number above was read in full from the Comprehensive Rules text
(effective August 7, 2026) in the session that wrote this file.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, force_turn_position
from magic_cards_model.machines import Step
from magic_cards_model.models import Zone
from magic_cards_model.models.game import Reveal

from magic_cards_model_api import visibility
from magic_cards_model_api.store import store

KNOWN_ZONES = (Zone.HAND, Zone.LIBRARY, Zone.SIDEBOARD)


# -- helpers --------------------------------------------------------------------


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def view(client, game_id: str, uid: str | None, include: str | None = None) -> dict[str, Any]:
    params: dict[str, str] = {}
    if uid is not None:
        params["uid"] = uid
    if include is not None:
        params["include"] = include
    response = client.get(f"/api/games/{game_id}", params=params or None)
    assert response.status_code == 200, response.text
    return response.json()


def admin_view(client, game_id: str, include: str | None = None) -> dict[str, Any]:
    return view(client, game_id, store.admin_key(game_id), include)


def ids_in_block(block: dict[str, Any]) -> set[str]:
    return (
        {card["id"] for card in block["hand"]}
        | {record["card"]["id"] for record in block["library"]}
        | {card["id"] for card in block["sideboard"]}
    )


def names_in_hand(block: dict[str, Any]) -> list[str]:
    return sorted(card["name"] for card in block["hand"])


def expected_known_ids(game_id: str, player: str) -> set[str]:
    """What the engine says `player` knows right now, less what is already
    visible to them: their own hand and their own sideboard."""
    game = store.get(game_id)
    assert game is not None
    kept = set()
    for entry in game.known_to(player):
        if entry.zone not in KNOWN_ZONES:
            continue
        if entry.zone in (Zone.HAND, Zone.SIDEBOARD) and entry.owner == player:
            continue
        kept.add(str(entry.card_id))
    return kept


def assert_matches_engine(client, game_id: str) -> None:
    """Case (b): every seat's `known_cards` entry is exactly the engine's live
    `known_to` for that seat, by id, and the library positions agree."""
    uids = seats_for(game_id)
    game = store.get(game_id)
    assert game is not None
    for player, uid in uids.items():
        entry = view(client, game_id, uid)["known_cards"]
        assert list(entry) == [player], entry
        assert ids_in_block(entry[player]) == expected_known_ids(game_id, player)
        live = {
            str(known.card_id): known.index_from_top
            for known in game.known_to(player)
            if known.zone is Zone.LIBRARY
        }
        for record in entry[player]["library"]:
            assert record["index_from_top"] == live[record["card"]["id"]]


def card_in(game_id: str, name: str, owner: str, zone: Zone):
    game = store.get(game_id)
    assert game is not None
    player = game.player(owner)
    return next(card for card in player.cards_in(zone) if card.name == name)


def battlefield_id(client, game_id: str, name: str, controller: str) -> str:
    return next(
        card["id"]
        for card in admin_view(client, game_id)["battlefield"]
        if card["name"] == name and card["controller"] == controller
    )


def stage_library(client, game_id: str, player: str, top_first: list[str]) -> None:
    """`POST .../cards` puts a library card on top, so add bottom first."""
    for name in reversed(top_first):
        add_card(client, game_id, name, player, "library")


def alice_baubles_bob(client, game_id: str, bob_library: list[str]) -> None:
    """Mishra's Bauble: "Look at the top card of target player's library" -
    a CR 701.20e look, alice's alone."""
    add_card(client, game_id, "Mishra's Bauble", "alice", "battlefield")
    stage_library(client, game_id, "bob", bob_library)
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, "alice")
    activated = client.post(
        f"/api/games/{game_id}/actions/activate",
        json={
            "player": "alice",
            "card_id": battlefield_id(client, game_id, "Mishra's Bauble", "alice"),
            "ability_index": 0,
            "targets": ["bob"],
        },
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["allowed"] is True, activated.json()
    resolved = client.post(f"/api/games/{game_id}/resolve-top")
    assert resolved.status_code == 200, resolved.text


def alice_thoughtseizes_bob(client, game_id: str, bob_hand: list[str], take: str) -> None:
    """Thoughtseize: "Target player reveals their hand. You choose a nonland
    card from it" - a CR 701.20a reveal of the whole hand, to everyone."""
    add_card(client, game_id, "Swamp", "alice", "battlefield")
    add_card(client, game_id, "Thoughtseize", "alice", "hand")
    for name in bob_hand:
        add_card(client, game_id, name, "bob", "hand")
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, "alice")
    board = admin_view(client, game_id)
    thoughtseize = next(card for card in board["hands"]["alice"] if card["name"] == "Thoughtseize")
    cast = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": thoughtseize["id"],
            "targets": ["bob"],
            "mana_sources": [battlefield_id(client, game_id, "Swamp", "alice")],
        },
    )
    assert cast.status_code == 200, cast.text
    assert cast.json()["allowed"] is True, cast.json()
    resolved = client.post(f"/api/games/{game_id}/resolve-top").json()
    uids = seats_for(game_id)
    while resolved.get("paused"):
        pending = resolved["pending_decision"]
        if pending["kind"] == "choose_cards":
            chosen = [card_in(game_id, take, "bob", Zone.HAND)]
            body = {"player": pending["player"], "card_ids": [str(chosen[0].id)]}
        else:
            body = {"player": pending["player"], "accept": True}
        answered = client.post(
            f"/api/games/{game_id}/decide", params={"uid": uids[pending["player"]]}, json=body
        )
        assert answered.status_code == 200, answered.text
        resolved = answered.json()


# -- (a) the existing boundary holds, and known_cards is a sibling of hands -----


def test_a_revealed_opponent_hand_is_never_put_inside_hands(client, game):
    alice_thoughtseizes_bob(
        client, game, ["Lightning Bolt", "Forest", "Mountain"], "Lightning Bolt"
    )
    uids = seats_for(game)

    as_alice = view(client, game, uids["alice"])

    assert as_alice["hands"]["bob"] == [], "known cards are a sibling block, never inside hands"
    assert "libraries" not in as_alice and "sideboards" not in as_alice
    assert "known_cards" not in as_alice["hands"]
    bob_row = next(p for p in as_alice["players"] if p["username"] == "bob")
    assert bob_row["hand_count"] == 2


# -- (m) a Thoughtseize-shape reveal lists the whole opponent hand ---------------


def test_a_thoughtseize_reveal_lists_the_whole_opponent_hand_for_the_caster(client, game):
    alice_thoughtseizes_bob(
        client, game, ["Lightning Bolt", "Forest", "Mountain"], "Lightning Bolt"
    )
    uids = seats_for(game)

    known = view(client, game, uids["alice"])["known_cards"]["alice"]
    bobs_hand = sorted(card["name"] for card in admin_view(client, game)["hands"]["bob"])

    assert names_in_hand(known) == bobs_hand == ["Forest", "Mountain"]
    assert {card["owner"] for card in known["hand"]} == {"bob"}
    assert known["library"] == [] and known["sideboard"] == []
    # The reveal was public (CR 701.20a), but these are bob's own hand cards,
    # which bob already sees: his own entry drops them.
    assert view(client, game, uids["bob"])["known_cards"] == {
        "bob": {"hand": [], "library": [], "sideboard": []}
    }
    assert_matches_engine(client, game)


# -- (n) a reveal before a pending choice is known while the choice is pending ----


def test_a_thoughtseize_reveal_is_known_while_its_discard_choice_is_pending(client, game):
    """CR 701.20a: a revealed card "remains revealed for as long as necessary
    to complete the parts of the effect that card is relevant to". Thoughtseize
    reveals the hand before alice chooses from it, so the hand is known while
    the choice is pending - not only once it has been answered (battle 1, D13).
    A paused command keeps no half-applied game, so the view reads the paused
    attempt's records; they still reach only the seat entitled to them."""
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Thoughtseize", "alice", "hand")
    for name in ("Lightning Bolt", "Forest", "Mountain"):
        add_card(client, game, name, "bob", "hand")
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    board = admin_view(client, game)
    thoughtseize = next(card for card in board["hands"]["alice"] if card["name"] == "Thoughtseize")
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": thoughtseize["id"],
            "targets": ["bob"],
            "mana_sources": [battlefield_id(client, game, "Swamp", "alice")],
        },
    )
    assert cast.status_code == 200, cast.text
    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved.get("paused"), resolved
    assert resolved["pending_decision"]["kind"] == "choose_cards"
    uids = seats_for(game)
    bobs_hand = {card["id"]: card["name"] for card in admin_view(client, game)["hands"]["bob"]}

    as_alice = view(client, game, uids["alice"])
    assert as_alice["pending_decision"]["kind"] == "choose_cards"
    known = as_alice["known_cards"]["alice"]
    assert {card["id"] for card in known["hand"]} == set(bobs_hand)
    assert names_in_hand(known) == ["Forest", "Lightning Bolt", "Mountain"]
    assert {card["owner"] for card in known["hand"]} == {"bob"}
    assert known["library"] == [] and known["sideboard"] == []
    assert as_alice["hands"]["bob"] == []

    assert view(client, game, uids["bob"])["known_cards"] == {
        "bob": {"hand": [], "library": [], "sideboard": []}
    }
    for nobody in (None, "garbage"):
        seen = view(client, game, nobody)
        assert seen["known_cards"] == {}
        assert not [card_id for card_id in bobs_hand if card_id in str(seen)]

    bolt = next(card_id for card_id, name in bobs_hand.items() if name == "Lightning Bolt")
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [bolt]},
    )
    assert answered.status_code == 200, answered.text
    assert not answered.json().get("paused"), answered.json()
    after = view(client, game, uids["alice"])["known_cards"]["alice"]
    assert names_in_hand(after) == ["Forest", "Mountain"]
    assert_matches_engine(client, game)


def _cast_a_library_look(client, game, spell: str, land: str, library: list[str]) -> dict:
    """Cast `spell` for `land` and resolve it to its pending look-then-put
    question, with alice's library staged top first."""
    add_card(client, game, land, "alice", "battlefield")
    add_card(client, game, spell, "alice", "hand")
    stage_library(client, game, "alice", library)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    card = next(c for c in admin_view(client, game)["hands"]["alice"] if c["name"] == spell)
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": card["id"],
            "mana_sources": [battlefield_id(client, game, land, "alice")],
        },
    )
    assert cast.status_code == 200, cast.text
    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved.get("paused"), resolved
    return resolved["pending_decision"]


def _library_names(block: dict[str, Any]) -> list[tuple[int, str, str]]:
    return [
        (record["index_from_top"], record["card"]["name"], record["source"])
        for record in block["library"]
    ]


def test_a_scry_look_is_known_while_the_scry_is_pending(client, game):
    """CR 701.22a: "look at the top N cards of your library, then put" - the
    look comes before the choice, so Opt's top card is known to alice (a look,
    hers alone, CR 701.20e) while the scry is pending (battle 1, D13). Once
    answered, the record is the one against the order she made - the Bolt on
    the bottom, so Opt's draw does not take it."""
    pending = _cast_a_library_look(client, game, "Opt", "Island", ["Lightning Bolt", "Forest"])
    assert pending["kind"] == "scry"
    uids = seats_for(game)
    top = card_in(game, "Lightning Bolt", "alice", Zone.LIBRARY)

    pending_view = view(client, game, uids["alice"])
    assert _library_names(pending_view["known_cards"]["alice"]) == [(0, "Lightning Bolt", "Opt")]
    assert str(top.id) in pending_view["pending_decision"]["cards"]
    assert view(client, game, uids["bob"])["known_cards"] == {
        "bob": {"hand": [], "library": [], "sideboard": []}
    }
    for nobody in (None, "garbage"):
        assert str(top.id) not in str(view(client, game, nobody)["known_cards"])

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "moved": [str(top.id)]},
    )
    assert answered.status_code == 200, answered.text
    assert not answered.json().get("paused"), answered.json()
    names = [
        record["card"]["name"]
        for record in view(client, game, uids["alice"])["known_cards"]["alice"]["library"]
    ]
    assert "Lightning Bolt" in names
    assert_matches_engine(client, game)


def test_a_surveil_look_is_known_while_the_surveil_is_pending(client, game):
    """CR 701.25a: "look at the top N cards of your library, then put any
    number of them into your graveyard" - Otherworldly Gaze's three cards are
    known to alice, where they sit, while the surveil is pending (battle 1,
    D13's surveil case), and to nobody else. The one she keeps stays known on
    top; the ones she bins are public in the graveyard (CR 400.2) and leave
    no record."""
    pending = _cast_a_library_look(
        client, game, "Otherworldly Gaze", "Island", ["Lightning Bolt", "Forest", "Mountain"]
    )
    assert pending["kind"] == "surveil"
    uids = seats_for(game)

    as_alice = view(client, game, uids["alice"])
    assert _library_names(as_alice["known_cards"]["alice"]) == [
        (0, "Lightning Bolt", "Otherworldly Gaze"),
        (1, "Forest", "Otherworldly Gaze"),
        (2, "Mountain", "Otherworldly Gaze"),
    ]
    assert view(client, game, uids["bob"])["known_cards"] == {
        "bob": {"hand": [], "library": [], "sideboard": []}
    }
    library_ids = {
        str(card_in(game, name, "alice", Zone.LIBRARY).id)
        for name in ("Lightning Bolt", "Forest", "Mountain")
    }
    for nobody in (None, "garbage"):
        seen = str(view(client, game, nobody)["known_cards"])
        assert not [card_id for card_id in library_ids if card_id in seen]

    forest = card_in(game, "Forest", "alice", Zone.LIBRARY)
    mountain = card_in(game, "Mountain", "alice", Zone.LIBRARY)
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "moved": [str(forest.id), str(mountain.id)]},
    )
    assert answered.status_code == 200, answered.text
    assert not answered.json().get("paused"), answered.json()
    after = view(client, game, uids["alice"])["known_cards"]["alice"]
    assert _library_names(after) == [(0, "Lightning Bolt", "Otherworldly Gaze")]
    assert_matches_engine(client, game)


# -- (d) looks are one way ------------------------------------------------------


def test_a_bauble_look_shows_alice_the_card_and_tells_bob_nothing(client, game):
    alice_baubles_bob(client, game, ["Lightning Bolt", "Island"])
    uids = seats_for(game)

    alice_known = view(client, game, uids["alice"])["known_cards"]["alice"]
    (record,) = alice_known["library"]
    assert record["index_from_top"] == 0
    assert record["source"] == "Mishra's Bauble"
    assert record["card"]["name"] == "Lightning Bolt"
    assert record["card"]["owner"] == "bob"
    assert record["card"]["id"] == str(card_in(game, "Lightning Bolt", "bob", Zone.LIBRARY).id)

    bob_view = view(client, game, uids["bob"])
    assert bob_view["known_cards"] == {"bob": {"hand": [], "library": [], "sideboard": []}}
    assert "Lightning Bolt" not in str(bob_view["known_cards"])
    assert_matches_engine(client, game)


def test_a_look_at_your_own_library_is_kept_since_contents_give_no_order(client, game):
    add_card(client, game, "Mishra's Bauble", "alice", "battlefield")
    stage_library(client, game, "alice", ["Lightning Bolt", "Forest"])
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    activated = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": battlefield_id(client, game, "Mishra's Bauble", "alice"),
            "ability_index": 0,
            "targets": ["alice"],
        },
    )
    assert activated.json()["allowed"] is True, activated.json()
    client.post(f"/api/games/{game}/resolve-top")
    uids = seats_for(game)

    (record,) = view(client, game, uids["alice"])["known_cards"]["alice"]["library"]

    assert (record["index_from_top"], record["card"]["name"]) == (0, "Lightning Bolt")
    assert_matches_engine(client, game)


# -- (e) expiry -------------------------------------------------------------------


def test_a_fetch_shuffle_ends_the_look(client, game):
    alice_baubles_bob(client, game, ["Lightning Bolt", "Plains", "Island"])
    uids = seats_for(game)
    assert view(client, game, uids["alice"])["known_cards"]["alice"]["library"]

    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
    add_card(client, game, "Flooded Strand", "bob", "battlefield")
    activated = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "bob",
            "card_id": battlefield_id(client, game, "Flooded Strand", "bob"),
            "ability_index": 0,
        },
    )
    assert activated.json()["allowed"] is True, activated.json()
    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    while resolved.get("paused"):
        pending = resolved["pending_decision"]
        if pending["kind"] == "choose_cards":
            plains = card_in(game, "Plains", "bob", Zone.LIBRARY)
            body = {"player": "bob", "card_ids": [str(plains.id)]}
        else:
            body = {"player": "bob", "accept": False}
        resolved = client.post(
            f"/api/games/{game}/decide", params={"uid": uids["bob"]}, json=body
        ).json()
    assert store.get(game).player("bob").library_epoch > 0

    assert view(client, game, uids["alice"])["known_cards"]["alice"]["library"] == []
    assert_matches_engine(client, game)


def test_a_discard_ends_a_revealed_hand_card(client, game):
    alice_thoughtseizes_bob(
        client, game, ["Lightning Bolt", "Forest", "Mountain"], "Lightning Bolt"
    )
    uids = seats_for(game)
    with store.locked(game) as live:
        live.discard_cards("bob", [card_in(game, "Forest", "bob", Zone.HAND)])

    known = view(client, game, uids["alice"])["known_cards"]["alice"]

    assert names_in_hand(known) == ["Mountain"]
    assert_matches_engine(client, game)


def test_a_revealed_battlefield_card_bounced_to_hand_is_not_known(client, game):
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    bears_id = battlefield_id(client, game, "Grizzly Bears", "bob")
    with store.locked(game) as live:
        live.reveal(next(card for card in live.battlefield if str(card.id) == bears_id))
    moved = client.post(
        f"/api/games/{game}/cards/{bears_id}/move", json={"to_zone": "hand", "player": "bob"}
    )
    assert moved.status_code == 200, moved.text
    uids = seats_for(game)

    known = view(client, game, uids["alice"])["known_cards"]["alice"]

    assert known["hand"] == [], "CR 400.7: the card in hand is a new object nobody was shown"
    assert_matches_engine(client, game)


# -- (j) known-draw memory ----------------------------------------------------------


def test_a_looked_at_card_bob_draws_stays_known_to_alice_until_it_leaves_his_hand(client, game):
    alice_baubles_bob(client, game, ["Lightning Bolt", "Island"])
    uids = seats_for(game)
    with store.locked(game) as live:
        live.draw_cards("bob", 1)

    alice_known = view(client, game, uids["alice"])["known_cards"]["alice"]
    (card,) = alice_known["hand"]
    assert (card["name"], card["owner"], card["zone"]) == ("Lightning Bolt", "bob", "hand")
    assert alice_known["library"] == []
    assert view(client, game, uids["bob"])["known_cards"]["bob"]["hand"] == []
    assert view(client, game, uids["bob"])["hands"]["alice"] == []
    assert_matches_engine(client, game)

    with store.locked(game) as live:
        live.discard_cards("bob", [card_in(game, "Lightning Bolt", "bob", Zone.HAND)])

    assert view(client, game, uids["alice"])["known_cards"]["alice"]["hand"] == []
    assert_matches_engine(client, game)


def test_a_goblin_guide_reveal_then_a_draw_is_known_to_both_but_listed_only_for_the_other(
    client, game
):
    add_card(client, game, "Goblin Guide", "alice", "battlefield")
    stage_library(client, game, "bob", ["Lightning Bolt", "Forest"])
    with store.locked(game) as live:
        live.reveal(live.player("bob").library[0], source="Goblin Guide")
        live.draw_cards("bob", 1)
    uids = seats_for(game)

    assert names_in_hand(view(client, game, uids["alice"])["known_cards"]["alice"]) == [
        "Lightning Bolt"
    ]
    assert view(client, game, uids["bob"])["known_cards"]["bob"]["hand"] == []
    assert_matches_engine(client, game)


def _booted(client, alice_deck: str, bob_deck: str, seed: int = 7) -> dict[str, Any]:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": alice_deck, "ai": False},
                {"username": "bob", "decklist_text": bob_deck, "ai": False},
            ],
            "seed": seed,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


MIRROR_DECK = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"


def test_a_carried_card_is_gone_after_its_owner_mulligans(client):
    info = _booted(client, MIRROR_DECK, MIRROR_DECK)
    game_id, uids = info["game_id"], info["seat_uids"]
    try:
        with store.locked(game_id) as live:
            top = live.player("bob").library[0]
            live.look(top, "alice")
            live.draw_cards("bob", 1)
        known = view(client, game_id, uids["alice"])["known_cards"]["alice"]
        assert [card["id"] for card in known["hand"]] == [str(top.id)]
        assert_matches_engine(client, game_id)

        for player, take in (("alice", False), ("bob", True)):
            declared = client.post(
                f"/api/games/{game_id}/pregame/mulligan",
                params={"uid": uids[player]},
                json={"player": player, "take": take},
            )
            assert declared.status_code == 200, declared.text

        assert store.get(game_id).player("bob").mulligans_taken == 1
        assert view(client, game_id, uids["alice"])["known_cards"]["alice"]["hand"] == []
        assert_matches_engine(client, game_id)
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# -- (c) the mirror match ----------------------------------------------------------


def test_a_mirror_match_never_crosses_same_named_cards_between_seats(client):
    info = _booted(client, MIRROR_DECK, MIRROR_DECK)
    game_id, uids = info["game_id"], info["seat_uids"]
    try:
        with store.locked(game_id) as live:
            alice, bob = live.player("alice"), live.player("bob")
            bobs_bolt = next(card for card in bob.library if card.name == "Lightning Bolt")
            live.look(bobs_bolt, "alice")
            alices_bolt_in_hand = next(
                (card for card in alice.hand if card.name != "Mountain"), alice.hand[0]
            )
            live.reveal(alices_bolt_in_hand)
            alices_own_ids = {str(card.id) for zone in alice.zones.values() for card in zone}
            bobs_own_ids = {str(card.id) for zone in bob.zones.values() for card in zone}

        as_alice = view(client, game_id, uids["alice"])["known_cards"]
        as_bob = view(client, game_id, uids["bob"])["known_cards"]

        assert list(as_alice) == ["alice"] and list(as_bob) == ["bob"]
        assert ids_in_block(as_alice["alice"]) <= bobs_own_ids
        assert not ids_in_block(as_alice["alice"]) & alices_own_ids
        assert ids_in_block(as_bob["bob"]) == {str(alices_bolt_in_hand.id)}
        assert not ids_in_block(as_bob["bob"]) & bobs_own_ids
        for block, owner in ((as_alice["alice"], "bob"), (as_bob["bob"], "alice")):
            for card in [*block["hand"], *block["sideboard"]]:
                assert card["owner"] == owner
            for record in block["library"]:
                assert record["card"]["owner"] == owner
        assert_matches_engine(client, game_id)
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# -- (f) unknown or missing viewers data fails closed --------------------------------


def test_records_with_an_unknown_viewer_or_missing_data_are_known_to_nobody(client, game):
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    bolt = card_in(game, "Lightning Bolt", "bob", Zone.HAND)
    with store.locked(game) as live:
        common = {
            "card_id": bolt.id,
            "card_name": bolt.name,
            "revealed_by": "bob",
            "zone": Zone.HAND,
        }
        live.reveals.append(
            Reveal(**common, viewers=["mallory"], zone_change_stamp=bolt.zone_change_stamp, turn=0)
        )
        live.reveals.append(Reveal(**common, viewers=["alice"], turn=0))
        live.reveals.append(
            Reveal(**common, viewers=["alice"], zone_change_stamp=bolt.zone_change_stamp)
        )
    uids = seats_for(game)

    for player in ("alice", "bob"):
        assert view(client, game, uids[player])["known_cards"][player]["hand"] == []
    assert admin_view(client, game)["known_cards"]["alice"]["hand"] == []
    assert_matches_engine(client, game)


def test_redact_view_drops_malformed_known_cards_data_fail_closed():
    card = {"id": "c1", "name": "Lightning Bolt", "zone": "hand", "owner": "bob"}
    raw = {
        "hands": {"alice": [], "bob": []},
        "players": [],
        "known_cards": {
            "alice": {
                "hand": [card, "not-a-card", None],
                "library": [
                    {"index_from_top": 0, "card": card},
                    {"card": card},
                    {"index_from_top": "0", "card": card},
                    {"index_from_top": 1},
                    {"index_from_top": 2, "source": ["not", "a", "name"], "card": card},
                    {"index_from_top": 3, "source": "Mishra's Bauble", "card": card},
                ],
            },
            "bob": {"hand": [card], "library": [], "sideboard": []},
            None: {"hand": [card], "library": [], "sideboard": []},
        },
    }

    as_alice = visibility.redact_view(raw, "alice")["known_cards"]
    assert as_alice == {
        "alice": {
            "hand": [card],
            "library": [
                {"index_from_top": 0, "source": None, "card": card},
                {"index_from_top": 2, "source": None, "card": card},
                {"index_from_top": 3, "source": "Mishra's Bauble", "card": card},
            ],
            "sideboard": [],
        }
    }

    raw_missing = {**raw, "known_cards": {"alice": None, "bob": "garbage"}}
    assert visibility.redact_view(raw_missing, "alice")["known_cards"] == {}
    assert visibility.redact_view(raw_missing, "bob")["known_cards"] == {}
    assert visibility.redact_view({**raw, "known_cards": None}, "alice")["known_cards"] == {}


# -- (g) the advisor, (h) the opponent and the bystander ---------------------------


def test_an_advisor_sees_exactly_what_its_human_sees(client, game):
    from magic_cards_model_api.repository import Advisor

    alice_baubles_bob(client, game, ["Lightning Bolt", "Island"])
    add_card(client, game, "Forest", "alice", "sideboard")
    store.register_advisors(game, {"buddy-uid": Advisor(player="alice")})
    uids = seats_for(game)

    for include in (None, "contents"):
        as_alice = client.get(
            f"/api/games/{game}",
            params={"uid": uids["alice"], **({"include": include} if include else {})},
        )
        as_buddy = client.get(
            f"/api/games/{game}",
            params={"uid": "buddy-uid", **({"include": include} if include else {})},
        )
        assert as_buddy.status_code == 200, as_buddy.text
        assert as_buddy.content == as_alice.content
    assert list(as_buddy.json()["known_cards"]) == ["alice"]


@pytest.mark.parametrize("who", ["bob", None, "garbage"])
def test_the_opponent_and_a_bystander_see_nothing_of_alices_knowledge(client, game, who):
    alice_baubles_bob(client, game, ["Lightning Bolt", "Island"])
    add_card(client, game, "Forest", "alice", "library")
    add_card(client, game, "Plains", "alice", "sideboard")
    uid = seats_for(game)[who] if who == "bob" else who

    for include in (None, "contents"):
        seen = view(client, game, uid, include)
        assert "alice" not in seen["known_cards"]
        assert "Lightning Bolt" not in str(seen["known_cards"])
        if include:
            assert "alice" not in seen["library_contents"]
            assert "alice" not in seen["sideboard_contents"]
    if who != "bob":
        assert view(client, game, uid)["known_cards"] == {}


# -- (i) the log's snapshot placeholders are untouched ---------------------------


def test_redact_snapshot_placeholders_are_unchanged(client, game):
    alice_thoughtseizes_bob(
        client, game, ["Lightning Bolt", "Forest", "Mountain"], "Lightning Bolt"
    )
    uids = seats_for(game)

    log = client.get(f"/api/games/{game}/log", params={"uid": uids["alice"]}).json()["log"]
    snapshot = log[-1]["snapshot"]

    assert snapshot["hands"]["bob"] == ["<hidden>", "<hidden>"]
    assert "known_cards" not in snapshot
    assert "library_contents" not in snapshot
    assert visibility.redact_snapshot({"hands": {"alice": ["A"], "bob": ["B", "C"]}}, "alice") == {
        "hands": {"alice": ["A"], "bob": ["<hidden>", "<hidden>"]}
    }


# -- (k) contents reach only their owner -------------------------------------------


def test_contents_reach_only_their_owner_and_the_admin_sees_both(client, game):
    stage_library(client, game, "alice", ["Lightning Bolt", "Mountain", "Lightning Bolt"])
    stage_library(client, game, "bob", ["Forest"])
    add_card(client, game, "Abrade", "alice", "sideboard")
    add_card(client, game, "Shock", "bob", "sideboard")
    uids = seats_for(game)

    plain = view(client, game, uids["alice"])
    assert "library_contents" not in plain and "sideboard_contents" not in plain

    as_alice = view(client, game, uids["alice"], "contents")
    assert list(as_alice["library_contents"]) == ["alice"]
    assert [(e["card"]["name"], e["count"]) for e in as_alice["library_contents"]["alice"]] == [
        ("Lightning Bolt", 2),
        ("Mountain", 1),
    ]
    assert [(e["card"]["name"], e["count"]) for e in as_alice["sideboard_contents"]["alice"]] == [
        ("Abrade", 1)
    ]

    as_bob = view(client, game, uids["bob"], "contents")
    assert list(as_bob["library_contents"]) == ["bob"]
    assert list(as_bob["sideboard_contents"]) == ["bob"]

    for nobody in (None, "garbage"):
        seen = view(client, game, nobody, "contents")
        assert seen["library_contents"] == {} and seen["sideboard_contents"] == {}

    as_admin = admin_view(client, game, "contents")
    assert set(as_admin["library_contents"]) == {"alice", "bob"}
    assert set(as_admin["sideboard_contents"]) == {"alice", "bob"}


def test_an_unknown_include_is_refused_by_name(client, game):
    uids = seats_for(game)
    refused = client.get(f"/api/games/{game}", params={"uid": uids["alice"], "include": "library"})
    assert refused.status_code == 422, refused.text
    assert "library" in refused.text and "contents" in refused.text


# -- (l) order independence ----------------------------------------------------------


def _keys_anywhere(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys_anywhere(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys_anywhere(item)}
    return set()


def test_library_contents_are_byte_identical_across_a_shuffle(client, game):
    import json

    stage_library(
        client,
        game,
        "alice",
        ["Lightning Bolt", "Mountain", "Grizzly Bears", "Mountain", "Lightning Bolt", "Forest"],
    )
    uids = seats_for(game)

    def contents_bytes() -> str:
        return json.dumps(view(client, game, uids["alice"], "contents")["library_contents"])

    before = contents_bytes()
    order_before = [card.id for card in store.get(game).player("alice").library]
    with store.locked(game) as live:
        live.shuffle_library("alice")
    order_after = [card.id for card in store.get(game).player("alice").library]
    assert order_before != order_after, "the seeded shuffle must actually reorder for this test"

    assert contents_bytes() == before
    entries = json.loads(before)["alice"]
    assert not {"id", "index_from_top", "position", "index"} & _keys_anywhere(entries)
    assert sum(entry["count"] for entry in entries) == 6
