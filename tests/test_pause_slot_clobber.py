"""The pause slot is one seat's half-finished command, not a scratch pad.

**The defect, from a live game.** A seat cracked Flooded Strand, answered the
CR 701.23a search with Steam Vents, and the answer was silently discarded:
Steam Vents stayed in the library and the fetch resolved as a decline. The
audit log has the whole shape of it in three entries, 227 ms apart:

- the seat's `POST .../decide` appended `choose_cards=SteamVents` to the tape
  and the command re-paused, so the slot held
  `(pass_priority, (choose_cards=SteamVents,), pay_life@seq1)`;
- the opponent's seatkit autopass re-posted `POST .../actions/pass-priority`,
  which `app.py`'s `_run_command` ran *fresh*, from the committed snapshot,
  with no tape - so it re-paused on the search and wrote
  `(pass_priority, (), choose_cards@seq0)` straight over the slot;
- the seat's decline (`card_ids: []`, which for CR 701.23b is a real answer
  and not a missing one) was then read against the *clobbered* slot,
  dispatched on `choose_cards`, and completed the resolution as a decline.

The transport half of the fix is that a fresh command never overwrites a live
pause. The rules half is that it should never have been able to run at all:
CR 608.2g - "No other spells can normally be cast and no other abilities can
normally be activated during resolution" - and CR 117.3b - "The active player
receives priority after a spell or ability (other than a mana ability)
resolves" - between them mean nobody has priority mid-resolution, so an
opponent's pass in that window is not a race to be tolerated, it is an
illegal action to be refused in the engine's own words.

Everything here plays a real board with real Scryfall printings out of
`tests/fixtures/scryfall`, through the real HTTP routes, in the real order the
audit log recorded. Every CR number was verified against the Comprehensive
Rules text (effective August 7, 2026) in the session that wrote this file.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, life_of, state
from fastapi.testclient import TestClient

from magic_cards_model_api.store import store


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def crack_the_strand(client: TestClient, game_id: str) -> dict[str, Any]:
    """Flooded Strand activated, both seats passed, the ability resolving and
    paused on its search - `pass-priority`'s own paused response.

    The library holds exactly the two cards the fetch's "Island or Plains
    card" clause can find a legal answer among: Steam Vents ("Land - Mountain
    Island", so an Island card by type even though it is neither basic nor
    blue) and a basic Island. Cracking it through two passes rather than
    `POST .../resolve-top` is deliberate - `pass_priority` is the command the
    live game was mid-way through, and CR 117.4 makes resolving the stack's
    top what the second pass in succession does.
    """
    add_card(client, game_id, "Flooded Strand", "alice", "battlefield")
    add_card(client, game_id, "Steam Vents", "alice", "library")
    add_card(client, game_id, "Island", "alice", "library")
    board = state(client, game_id)
    strand = find(board["battlefield"], "Flooded Strand")["id"]
    activated = client.post(
        f"/api/games/{game_id}/actions/activate",
        json={"player": "alice", "card_id": strand, "ability_index": 0},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["allowed"] is True, activated.json()
    first = client.post(f"/api/games/{game_id}/actions/pass-priority", json={"player": "alice"})
    assert first.status_code == 200, first.text
    second = client.post(f"/api/games/{game_id}/actions/pass-priority", json={"player": "bob"})
    assert second.status_code == 200, second.text
    body = second.json()
    assert body.get("paused") is True, body
    assert body["pending_decision"]["kind"] == "choose_cards", body
    return body


def steam_vents_in(pending: dict[str, Any]) -> str:
    """The candidate id the search's own labels name Steam Vents. CR 400.2
    makes the library hidden, so `card_labels` is the only naming a seat has.
    """
    named = [
        card_id
        for card_id, label in zip(pending["cards"], pending["card_labels"], strict=True)
        if label == "Steam Vents"
    ]
    assert named, pending
    return named[0]


def test_an_opponents_pass_does_not_discard_the_answered_search(client, game) -> None:
    """The reproduction, seat for seat and call for call.

    Alice answers the search with Steam Vents; bob's autopass fires in the
    227 ms window before she answers the shockland; alice then declines the
    pay-life exactly the way `seat_cli.py`'s `--decline` does, with
    `card_ids: []`. Steam Vents must be on the battlefield tapped
    (CR 614.1c's replacement, since the payment was declined), the library
    one card smaller, and alice's life exactly the 1 the fetch's own cost
    took and not a point more.
    """
    uids = seats_for(game)
    paused = crack_the_strand(client, game)
    vents = steam_vents_in(paused["pending_decision"])

    answered = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": [vents]},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["accepted"] is True, answered.json()
    assert answered.json()["pending_decision"]["kind"] == "pay_life", answered.json()

    # The clobber: seatkit's autopass re-posts pass-priority on `paused: true`.
    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})

    standing = state(client, game, uid=uids["alice"])["pending_decision"]
    assert standing is not None and standing["kind"] == "pay_life", (
        "the opponent's pass must not have rewound the slot to the search: "
        f"{standing}"
    )

    declined = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        # Byte-identical to what `seat_cli.py`'s `--decline` posts, and to an
        # omitted `card_ids` - which is exactly why the answer can never be
        # routed by "whichever field the client filled".
        json={"player": "alice", "accept": False, "card_ids": []},
    )
    assert declined.status_code == 200, declined.text
    assert declined.json()["accepted"] is True, declined.json()

    after = state(client, game)
    assert find(after["battlefield"], "Steam Vents")["tapped"] is True, (
        "the shockland's payment was declined, so CR 614.1c brought it in tapped"
    )
    library = [card.name for card in store.get(game).player("alice").library]
    assert library == ["Island"], f"the fetch took Steam Vents out of the library: {library}"
    assert life_of(after, "alice") == 19, (
        "1 life for the fetch's own cost, and none for the shockland whose payment "
        "was declined"
    )
