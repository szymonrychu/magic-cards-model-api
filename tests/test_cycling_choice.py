"""Typecycling is a search, and a search is one choice over every legal find -
not a walk that picks the first match for the searcher.

**The bug, as reported from a real game.** A seat activated Lorien Revealed's
Islandcycling ("Islandcycling {1} ({1}, Discard this card: Search your
library for an Island card, reveal it, put it into your hand, then
shuffle.)") wanting a particular Island. It never got asked - the engine
searched the library and handed back whatever it found first. The owner's own
words: "Islandcycling (Lorien Revealed) didn't allow me to pick Island,
instead it automatically searched my library for one."

The root cause was the same shape `test_fetchland_search_choice.py` already
closed for an activated search ability's own fetch: typecycling's resolution
(`machines.keywords._search_reveal_to_hand_and_shuffle`) now goes through
`Game.search_library` instead of taking the first match, so it raises the
same `choose_cards` question that method raises for every other search -
CR 701.23a's "look at all cards in that zone... and find a card that matches
the given description", asked inexact (CR 701.23b: a stated-quality search of
a hidden zone is declinable) because CR 400.2 makes the library hidden. This
file is that fix at the API boundary, for the cycling half of the search pool
`test_fetchland_search_choice.py` already proved for the activated-ability
half.

Real board, real cards, nothing mocked: Lorien Revealed and a library holding
a card that plainly isn't an Island (a Mountain), two Islands (so there is a
real choice to make, not a single forced find), and a fetchland (Flooded
Strand, type line "Land" - a land, but not an Island by card type) that a
first-match walk could just as easily have handed back instead of an actual
Island.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def stock_the_cycler(client: TestClient, game_id: str) -> dict[str, Any]:
    """Lorien Revealed in alice's hand, a Forest on her battlefield to pay
    Islandcycling's `{1}`, and a library holding exactly one non-Island (a
    Mountain), two Islands (added in library order, bottom first, so the
    engine's own zone-order candidate list comes back `[island_a, island_b]`
    - `Game.search_library`'s `candidates` is `player.cards_in(zone)` in
    stored order, and `engine_api.put_card_in_zone` appends a `position="bottom"`
    card rather than re-inserting it at the top), and a fetchland (Flooded
    Strand - a land, but not an Island by type, unlike a dual land)."""
    add_card(client, game_id, "Lórien Revealed", "alice", "hand")
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Mountain", "alice", "library", position="bottom")
    island_a = add_card(client, game_id, "Island", "alice", "library", position="bottom")
    island_b = add_card(client, game_id, "Island", "alice", "library", position="bottom")
    add_card(client, game_id, "Flooded Strand", "alice", "library", position="bottom")
    board = state(client, game_id)
    return {
        "cycler": find(board["hands"]["alice"], "Lórien Revealed"),
        "forest": find(board["battlefield"], "Forest"),
        "island_a": island_a["added"]["card_id"],
        "island_b": island_b["added"]["card_id"],
    }


def activate_and_resolve(
    client: TestClient, game_id: str, uid: str, card_id: str, forest_id: str
) -> dict[str, Any]:
    """Activate Islandcycling through the unified `/actions/activate` route
    (the same endpoint an ability from hand shares with a battlefield
    permanent's) and resolve it - the search happens on resolution, so
    `resolve-top` returns the instant it crosses the `choose_cards` question,
    with `uid`'s own view merged in so `pending_decision` carries the
    candidates and their labels."""
    activated = client.post(
        f"/api/games/{game_id}/actions/activate",
        params={"uid": uid},
        json={
            "player": "alice",
            "card_id": card_id,
            "ability_index": 0,
            "mana_sources": [forest_id],
        },
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["allowed"] is True, activated.json()
    resolved = client.post(f"/api/games/{game_id}/resolve-top", params={"uid": uid})
    assert resolved.status_code == 200, resolved.text
    return resolved.json()


def test_the_search_offers_both_islands_by_name_and_holds_the_card_until_answered(
    client, game
) -> None:
    """The reported bug, at the API boundary: activating Islandcycling pauses
    on one `choose_cards` question naming both Islands, the found Island
    lands in hand only once that question is answered, and the answer - not
    zone order - decides which of the two identically-named Islands it is."""
    uids = seats_for(game)
    board = stock_the_cycler(client, game)
    resolved = activate_and_resolve(
        client, game, uids["alice"], board["cycler"]["id"], board["forest"]["id"]
    )
    assert resolved["paused"] is True, resolved
    pending = resolved["pending_decision"]
    assert pending["kind"] == "choose_cards", pending
    assert pending["source"] == "Lórien Revealed", pending
    assert pending["amount"] == 1
    assert pending["exact"] is False, (
        "CR 701.23b makes a stated-quality search of a hidden zone declinable"
    )
    assert pending["cards"] == [board["island_a"], board["island_b"]], (
        "the two library Islands, in zone order, and nothing else - the "
        "Mountain and the fetchland are not Island-typed cards"
    )
    assert pending["card_labels"] == ["Island", "Island"], (
        "the searcher looks at the whole zone (CR 701.23a) and is told by "
        "name what it holds, even when two candidates share a name"
    )
    details_by_id = {detail["id"]: detail for detail in pending["card_details"]}
    assert set(details_by_id) == {board["island_a"], board["island_b"]}, (
        "card_details resolves both candidate ids against the board, since "
        "the library is a hidden zone the deciding seat cannot look up itself"
    )
    assert all(detail["name"] == "Island" for detail in details_by_id.values())

    # Not yet in hand: the search paused on the question, it did not fetch a
    # card first and ask about it after.
    mid_state = state(client, game, uids["alice"])
    assert "Island" not in [c["name"] for c in mid_state["hands"]["alice"]], (
        "the fetch has not happened yet - the choice comes before the find, not after"
    )
    assert mid_state["pending_decision"]["kind"] == "choose_cards", (
        "the state view for the deciding player's own seat carries the same "
        "pending question the resolve-top response did"
    )

    # The opponent's own view of the same board never learns what is in
    # alice's library, or even that there are two Islands to choose between.
    opponent_view = state(client, game, uids["bob"])["pending_decision"]
    assert set(opponent_view) == {"player", "kind", "prompt"}, (
        f"an opponent's view of a decision that is not theirs is {{player, kind, prompt}} "
        f"and nothing that names a card (CR 400.2's hidden library): got {opponent_view}"
    )
    assert "cards" not in opponent_view and "card_labels" not in opponent_view

    # Answering with the second Island - not the first, not zone order -
    # decides which physical card comes back.
    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [board["island_b"]]},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True, answered.json()

    after = state(client, game)
    hand_islands = [c for c in after["hands"]["alice"] if c["name"] == "Island"]
    assert [c["id"] for c in hand_islands] == [board["island_b"]], (
        "the second Island - the one actually named in the answer - is what "
        "came back, not whichever the engine would have found on its own"
    )
    assert after["pending_decision"] is None, "answered - nothing left pending"
    library_names = sorted(card.name for card in store.get(game).player("alice").library)
    assert library_names == ["Flooded Strand", "Island", "Mountain"], (
        "the unchosen Island, the Mountain and the fetchland are all still in "
        "the library, which the search then shuffled"
    )


def test_declining_the_search_leaves_no_island_in_hand_and_clears_the_decision(
    client, game
) -> None:
    """CR 701.23b's decline, for typecycling exactly as for a fetch: answering
    with no cards is a real, single answer - not a card the engine picked for
    you, and not a walk of one refusal per candidate."""
    uids = seats_for(game)
    board = stock_the_cycler(client, game)
    resolved = activate_and_resolve(
        client, game, uids["alice"], board["cycler"]["id"], board["forest"]["id"]
    )
    assert resolved["paused"] is True, resolved
    pending = resolved["pending_decision"]
    assert pending["kind"] == "choose_cards", pending

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": []},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True, answered.json()

    after = state(client, game)
    assert "Island" not in [c["name"] for c in after["hands"]["alice"]], (
        "nothing was found, so nothing entered the hand"
    )
    assert after["pending_decision"] is None, "declined - the decision is cleared, not re-asked"
    library_names = sorted(card.name for card in store.get(game).player("alice").library)
    assert library_names == ["Flooded Strand", "Island", "Island", "Mountain"], (
        "both Islands, the Mountain and the fetchland are still in the "
        "library - the decline found nothing to remove"
    )
