"""`play_permissions` and `exile_pile`: the state-view surface over CR 601.3's
dynamic grants (`PlayPermission`, `machines/casting.py`) and CR 406.4's marked
exile piles (suspend, plot, foretell, cascade).

The originally reported defect: a player cast a cascade spell, the trigger
offered "you may cast it without paying its mana cost", the player accepted,
the engine granted a `PlayPermission` - and the seat had no way to see it.
`GET /api/games/{id}` carried no `play_permissions` key at all, so the card
sat in exile until cleanup bottomed it, and the player reported the engine as
broken when the engine had done its part correctly. `serialize._play_
permissions_view`/`_play_permission_view` and the `exile_pile` top-level key
are that discoverability, still exercised below by the mechanics that still
use a standing grant (plot, foretell) or one that lives only for the length
of a `POST .../decide` answer chain.

Battle 8 (B8-01, and the funnel commit right after it) removed the standing
grant from cascade, suspend, rebound, Mosswort Bridge and several other
"cast/play it without paying its mana cost" effects entirely. Accepting any
of their `may` questions now casts (or plays) the card immediately while the
permitting spell or ability is still resolving (`Game.cast_during_resolution`
/ `may_cast_during_resolution`, CR 608.2g) - the engine asks whichever face,
modes, targets and additional costs (kicker, entwine, escalate, ...) the cast
needs as ordinary mid-resolution decisions through the same `POST .../decide`
a seat already answers everything else with, no player ever receives priority
in between, and no `PlayPermission` is ever left lying around to discover:
the card is already a spell on the stack (or a permanent on the battlefield,
for a land) by the time control returns to a seat. `PlayPermission.bottom_
if_unused` (the engine's own cleanup marker for an accepted-but-undriven
cascade grant) has had nothing left to set it since that cleanup sweep was
removed with the standing grant itself - permanently `False` on every
permission this engine still hands out - so `_play_permission_view` and
`PlayPermissionView` drop the field outright rather than carry a key that
can never read anything but the one value: the "cleanup bottomed an unused
cascade/suspend permission" case this file used to test no longer exists at
all, an illegal cast during resolution now being rewound and bottomed
*inside* `cast_during_resolution` itself, before a seat ever sees anything.

Every card here is a real Scryfall printing, resolved through the engine's own
bundled Scryfall corpus (`scryfall.py`'s tier 2, `../magic-cards-model`'s
`data/printings/`) - nothing here needs its own cache file under
tests/fixtures/scryfall/.
"""

from __future__ import annotations

from conftest import add_card, find, force_turn_position, resolve_whole_stack, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step

from magic_cards_model_api.store import store


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


# -- suspend: cast during resolution too (battle 8's funnel commit) ---------


def _drain_two_upkeeps(client: TestClient, game_id: str, player: str) -> None:
    """The first two of Living End's three time counters, through the same
    real-upkeep path `test_special_actions.py` drives - neither one raises
    CR 702.62a's third ability yet, so nothing here pauses."""
    for _ in range(2):
        force_turn_position(game_id, Step.UNTAP, player)
        advanced = client.post(f"/api/games/{game_id}/advance", json={"player": player})
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["advanced_to"] == "upkeep", advanced.json()
        resolve_whole_stack(client, game_id)


def test_suspends_free_cast_is_a_may_decision_answered_through_decide(client, game):
    """CR 702.62a's third ability: once the last time counter comes off,
    `Game.finish_suspend` casts Living End immediately while that ability is
    still resolving (`Game.may_cast_during_resolution`, CR 608.2g) - the same
    funnel cascade's hit goes through, not a standing `PlayPermission` a seat
    discovers and drives later with its own `actions/cast`."""
    add_card(client, game, "Living End", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Swamp", "alice", "battlefield")
    swamps = [c["id"] for c in state(client, game)["battlefield"]]
    client.post(
        f"/api/games/{game}/actions/suspend",
        json={"player": "alice", "card": "Living End", "mana_sources": swamps},
    )
    _drain_two_upkeeps(client, game, "alice")
    uids = seats_for(client, game)

    # Third upkeep: the counter-removal trigger resolves first (2 -> 1 -> a
    # second ability triggers off the last counter coming off), then that
    # second ability is what pauses on the third ability's own "may".
    force_turn_position(game, Step.UNTAP, "alice")
    advanced = client.post(f"/api/games/{game}/advance", json={"player": "alice"}).json()
    assert advanced["advanced_to"] == "upkeep", advanced

    first = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]}).json()
    assert first.get("paused") is not True, first
    assert first["resolved"] == "ability from Living End"
    living_end = find(state(client, game)["exile"], "Living End")
    assert living_end["counters"].get("time", 0) == 0

    second = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]}).json()
    assert second.get("paused") is True, "the third ability pauses on its own may (CR 608.2g)"
    pending = second["pending_decision"]
    assert pending["kind"] == "may"
    assert pending["player"] == "alice"
    assert pending["source"] == "Living End"

    # Nobody else may answer alice's question.
    wrong_seat = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["bob"]},
        json={"player": "alice", "accept": True},
    )
    assert wrong_seat.json()["accepted"] is False

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["accepted"] is True
    assert body.get("paused") is not True, body

    mid = state(client, game)
    assert mid["exile"] == [], "Living End left exile the instant it was cast"
    assert mid["play_permissions"]["alice"] == []
    assert [entry["name"] for entry in mid["stack"]] == ["Living End"]

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Living End"
    final = state(client, game)
    assert final["exile"] == []
    assert [c["name"] for c in final["graveyards"]["alice"]] == ["Living End"]


def test_declining_suspends_free_cast_leaves_it_in_exile(client, game):
    """ "If you don't, it remains exiled" - Living End with no time counters
    left, still sitting there, once the seat says no."""
    add_card(client, game, "Living End", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Swamp", "alice", "battlefield")
    swamps = [c["id"] for c in state(client, game)["battlefield"]]
    client.post(
        f"/api/games/{game}/actions/suspend",
        json={"player": "alice", "card": "Living End", "mana_sources": swamps},
    )
    _drain_two_upkeeps(client, game, "alice")
    uids = seats_for(client, game)

    force_turn_position(game, Step.UNTAP, "alice")
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]})
    client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]})

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": False},
    )
    assert answer.json()["accepted"] is True

    final = state(client, game)
    living_end = find(final["exile"], "Living End")
    assert living_end["counters"].get("time", 0) == 0
    assert final["stack"] == []
    assert final["play_permissions"]["alice"] == []


# -- exile_pile: the marked piles pulled out of the whole exile list --------


def test_exile_pile_surfaces_suspended_foretold_and_plotted_cards(client, game):
    add_card(client, game, "Living End", "alice", "hand")
    add_card(client, game, "Lupine Harbingers", "alice", "hand")
    add_card(client, game, "Slickshot Show-Off", "alice", "hand")
    for _ in range(4):
        add_card(client, game, "Swamp", "alice", "battlefield")
    for _ in range(8):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    board = state(client, game)
    swamps = [c["id"] for c in board["battlefield"] if c["name"] == "Swamp"]
    forests = [c["id"] for c in board["battlefield"] if c["name"] == "Forest"]
    mountains = [c["id"] for c in board["battlefield"] if c["name"] == "Mountain"]

    client.post(
        f"/api/games/{game}/actions/suspend",
        json={"player": "alice", "card": "Living End", "mana_sources": swamps},
    )
    client.post(
        f"/api/games/{game}/actions/foretell",
        json={"player": "alice", "card": "Lupine Harbingers", "mana_sources": forests[:2]},
    )
    client.post(
        f"/api/games/{game}/actions/plot",
        json={"player": "alice", "card": "Slickshot Show-Off", "mana_sources": mountains},
    )

    uids = seats_for(client, game)
    admin_pile = {card["name"]: card for card in state(client, game)["exile_pile"]}
    assert set(admin_pile) == {"Living End", "Lupine Harbingers", "Slickshot Show-Off"}
    assert admin_pile["Living End"]["exile_pile"]["marker"] == "suspended"
    assert admin_pile["Living End"]["counters"] == {"time": 3}
    assert admin_pile["Lupine Harbingers"]["exile_pile"]["marker"] == "foretold"
    assert admin_pile["Lupine Harbingers"]["face_up"] is False
    assert admin_pile["Slickshot Show-Off"]["exile_pile"]["marker"] == "plotted"

    # exile_pile is exactly the marked subset of exile, no more, no less.
    exile_names = {card["name"] for card in state(client, game)["exile"]}
    assert exile_names == set(admin_pile)

    # A foretold card's identity stays hidden from the opponent (CR 406.3)
    # even inside exile_pile - the same redaction `exile` already gets.
    as_bob = {c["name"]: c for c in state(client, game, uid=uids["bob"])["exile_pile"]}
    harbingers_id = admin_pile["Lupine Harbingers"]["id"]
    hidden = next(c for c in as_bob.values() if c["id"] == harbingers_id)
    assert hidden["name"] == "a face-down card"
    assert hidden["exile_pile"]["marker"] == "foretold"  # the pile fact stays public


# -- cascade: cast during resolution (B8-01, CR 608.2g) ----------------------


def _lands(client: TestClient, game_id: str) -> dict[str, list[str]]:
    board = state(client, game_id)
    by_name: dict[str, list[str]] = {}
    for card in board["battlefield"]:
        by_name.setdefault(card["name"], []).append(card["id"])
    return by_name


def test_cascade_into_living_end_casts_it_during_resolution(client, game):
    """Violent Outburst into Living End, end to end through the real API:
    accepting the cascade trigger's "you may cast it" (`may`, through the
    ordinary `decide` route) now casts Living End immediately while the
    trigger is still resolving (CR 608.2g) - no separate `actions/cast`
    afterwards, no lingering `play_permissions` entry to drive, and Living
    End resolves *before* Violent Outburst itself, since it is pushed onto
    the stack above it.

    Board: alice has a Grizzly Bears in her graveyard (Living End returns it)
    and casts Violent Outburst; bob has a Grizzly Bears on the battlefield
    with none in his own graveyard (Living End's "sacrifice all creatures you
    control" then returns nothing for him - CR 608.2g's evaluation is
    per-player, and Living End's own text sacrifices before returning)."""
    add_card(client, game, "Violent Outburst", "alice", "hand")
    add_card(client, game, "Living End", "alice", "library", position="top")
    add_card(client, game, "Grizzly Bears", "alice", "graveyard")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    uids = seats_for(client, game)
    lands = _lands(client, game)
    outburst = state(client, game)["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": outburst["id"],
            "mana_sources": [lands["Mountain"][0], lands["Forest"][0], lands["Forest"][1]],
        },
    )
    assert cast.status_code == 200, cast.text
    assert cast.json()["allowed"] is True, cast.json()

    # The cascade trigger sits above Violent Outburst - resolving it is where
    # CR 608.2d's "you may cast it" question is asked.
    top = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]})
    assert top.status_code == 200, top.text
    top_body = top.json()
    assert top_body.get("paused") is True, (
        "expected the cascade trigger to pause on its may question"
    )
    pending = top_body["pending_decision"]
    assert pending["kind"] == "may"
    assert pending["source"] == "Violent Outburst"

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["accepted"] is True
    # Living End needs no face, mode or target - nothing further pauses, and
    # the answer that accepted the cascade is also the one that cast it.
    assert body.get("paused") is not True, body

    mid = state(client, game)
    assert mid["exile"] == [], "Living End left exile the instant it was cast"
    assert mid["play_permissions"]["alice"] == [], (
        "no permission is ever left lying around for a cast made during resolution"
    )
    assert [entry["name"] for entry in mid["stack"]] == ["Living End", "Violent Outburst"], (
        "Living End is pushed onto the stack above the spell that cascaded (CR 608.2g); "
        "the stack view is top-first"
    )

    resolved_living_end = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved_living_end["resolved"] == "Living End"

    after_living_end = state(client, game)
    assert find(after_living_end["battlefield"], "Grizzly Bears")["controller"] == "alice", (
        "alice's Bears were exiled from her graveyard and returned to the battlefield"
    )
    assert all(
        card["name"] != "Grizzly Bears" or card["controller"] != "bob"
        for card in after_living_end["battlefield"]
    ), "bob had no Bears in his graveyard to get back, so his were sacrificed and stayed dead"
    assert find(after_living_end["graveyards"]["bob"], "Grizzly Bears")["zone"] == "graveyard"

    resolved_outburst = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved_outburst["resolved"] == "Violent Outburst"

    final = state(client, game)
    bears = find(final["battlefield"], "Grizzly Bears")
    assert (bears["power"], bears["toughness"]) == (3, 2), (
        "Violent Outburst's own +1/+0 applies to the Bears Living End just returned"
    )


def test_cascade_hit_with_modes_and_targets_announced_during_resolution(client, game):
    """Bloodbraid Elf into Kolaghan's Command: CR 601.2b/c inside CR 608.2g -
    the modal spell's two modes, then each mode's own target, are asked as
    ordinary mid-resolution decisions chained off the same `decide` call that
    accepted the cascade, through the same `choose_a_mode`/`choose_targets`
    kinds any other modal spell already uses."""
    add_card(client, game, "Bloodbraid Elf", "alice", "hand")
    add_card(client, game, "Kolaghan's Command", "alice", "library", position="top")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    island = add_card(client, game, "Island", "bob", "hand")["added"]["card_id"]
    spellbomb = add_card(client, game, "Aether Spellbomb", "bob", "battlefield")["added"]["card_id"]
    uids = seats_for(client, game)
    lands = _lands(client, game)
    elf = state(client, game)["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": elf["id"],
            "mana_sources": [
                lands["Forest"][0],
                lands["Mountain"][0],
                lands["Forest"][1],
                lands["Mountain"][1],
            ],
        },
    )
    assert cast.status_code == 200, cast.text
    assert cast.json()["allowed"] is True, cast.json()

    top = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]})
    assert top.status_code == 200, top.text
    pending = top.json()["pending_decision"]
    assert pending["kind"] == "may"
    assert pending["source"] == "Bloodbraid Elf"

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["accepted"] is True and body.get("paused") is True
    pending = body["pending_decision"]
    assert pending["kind"] == "choose_a_mode"
    discard_mode = next(mode for mode in pending["options"] if "discards a card" in mode)

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": discard_mode},
    )
    assert answer.json()["accepted"] is True
    pending = answer.json()["pending_decision"]
    assert pending["kind"] == "choose_a_mode"
    destroy_mode = next(mode for mode in pending["options"] if "Destroy target artifact" in mode)

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": destroy_mode},
    )
    assert answer.json()["accepted"] is True
    pending = answer.json()["pending_decision"]
    assert pending["kind"] == "choose_targets"

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "targets": ["bob"]},
    )
    assert answer.json()["accepted"] is True
    pending = answer.json()["pending_decision"]
    assert pending["kind"] == "choose_targets"

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "targets": [spellbomb]},
    )
    body = answer.json()
    assert body["accepted"] is True
    assert body.get("paused") is not True, body

    mid = state(client, game)
    assert [entry["name"] for entry in mid["stack"]] == ["Kolaghan's Command", "Bloodbraid Elf"]
    assert mid["exile"] == []
    assert mid["play_permissions"]["alice"] == []

    # The priority holder resolves it: resolve-top is a seat's only while it
    # holds the priority it passes (`game_access._seat_mismatch`).
    holder = client.get(f"/api/games/{game}").json()["priority_holder"]
    resolved_command = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids[holder]})
    resolved_body = resolved_command.json()
    assert resolved_body.get("paused") is True, (
        "bob has one card, but Kolaghan's Command still asks which to discard"
    )
    discard_pending = resolved_body["pending_decision"]
    assert discard_pending["kind"] == "choose_cards"
    discard = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["bob"]},
        json={"player": "bob", "card_ids": [island]},
    )
    assert discard.json()["accepted"] is True

    after = state(client, game)
    assert after["hands"]["bob"] == [], "bob discarded his one card"
    assert all(card["name"] != "Aether Spellbomb" for card in after["battlefield"])
    assert find(after["graveyards"]["bob"], "Aether Spellbomb")["zone"] == "graveyard"


def test_cascade_hit_with_no_legal_target_is_not_cast_and_goes_to_the_bottom(client, game):
    """CR 601.2c makes casting Fatal Push illegal with no creature anywhere to
    target - `Game.can_cast_during_resolution` catches this up front, so the
    `may` itself is offered with `affirmative_is_legal: False` (CR 608.2d,
    "the player can't choose an option that's illegal or impossible") and an
    `accept: True` answer is refused outright rather than accepted and then
    discovered to do nothing. Declining is the only legal answer, and Fatal
    Push is bottomed instead (CR 702.85a's "weren't cast")."""
    add_card(client, game, "Bloodbraid Elf", "alice", "hand")
    add_card(client, game, "Fatal Push", "alice", "library", position="top")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    uids = seats_for(client, game)
    lands = _lands(client, game)
    elf = state(client, game)["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": elf["id"],
            "mana_sources": [
                lands["Forest"][0],
                lands["Mountain"][0],
                lands["Forest"][1],
                lands["Mountain"][1],
            ],
        },
    )
    assert cast.status_code == 200, cast.text
    assert cast.json()["allowed"] is True, cast.json()

    top = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]})
    pending = top.json()["pending_decision"]
    assert pending["kind"] == "may"
    assert pending["affirmative_is_legal"] is False

    illegal_accept = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    assert illegal_accept.json()["accepted"] is False

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": False},
    )
    body = answer.json()
    assert body["accepted"] is True
    assert body.get("paused") is not True, body

    final = state(client, game)
    assert [entry["name"] for entry in final["stack"]] == ["Bloodbraid Elf"]
    assert final["exile"] == []
    push = find(final["libraries"]["alice"], "Fatal Push")
    assert push["zone"] == "library"


def test_a_kicker_spell_off_a_cascade_pays_the_kicker_through_decide(client, game):
    """Bloodbraid Elf into a kicked Burst Lightning: CR 601.2b's optional
    additional cost is offered as its own `may` decision (`Game.
    cast_during_resolution`, the same funnel a mandatory cost or a mode uses),
    chained off the cascade's own accept - paying it taps four more untapped
    sources with no separate `pay_mana` step (auto-tap), and the kicked spell
    deals 4 rather than Burst Lightning's unkicked 2."""
    add_card(client, game, "Bloodbraid Elf", "alice", "hand")
    add_card(client, game, "Burst Lightning", "alice", "library", position="top")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    for _ in range(4):
        add_card(client, game, "Mountain", "alice", "battlefield")
    uids = seats_for(client, game)
    lands = _lands(client, game)
    elf = state(client, game)["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": elf["id"],
            "mana_sources": [
                lands["Forest"][0],
                lands["Mountain"][0],
                lands["Forest"][1],
                lands["Mountain"][1],
            ],
        },
    )
    assert cast.status_code == 200, cast.text
    assert cast.json()["allowed"] is True, cast.json()

    top = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]})
    pending = top.json()["pending_decision"]
    assert pending["kind"] == "may"
    assert pending["source"] == "Bloodbraid Elf"

    accept_cascade = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    body = accept_cascade.json()
    assert body["accepted"] is True and body.get("paused") is True
    pending = body["pending_decision"]
    assert pending["kind"] == "may"
    assert pending["source"] == "Burst Lightning"

    pay_kicker = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    body = pay_kicker.json()
    assert body["accepted"] is True and body.get("paused") is True
    pending = body["pending_decision"]
    assert pending["kind"] == "choose_targets"

    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "targets": ["bob"]},
    )
    body = answer.json()
    assert body["accepted"] is True and body.get("paused") is True
    pending = body["pending_decision"]
    assert pending["kind"] == "pay_mana"
    assert pending["prompt"] == "pay {4} to cast Burst Lightning"

    pay = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "accept": True},
    )
    body = pay.json()
    assert body["accepted"] is True
    assert body.get("paused") is not True, body

    mid = state(client, game)
    assert mid["exile"] == []
    assert mid["play_permissions"]["alice"] == []
    assert all(land["tapped"] for land in mid["battlefield"] if land["name"] == "Mountain"), (
        "the kicker's {4} was paid from the untapped Mountains"
    )
    assert [entry["name"] for entry in mid["stack"]] == ["Burst Lightning", "Bloodbraid Elf"]

    resolved_bolt = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved_bolt["resolved"] == "Burst Lightning"
    bob = next(p for p in state(client, game)["players"] if p["username"] == "bob")
    assert bob["life"] == 16, "kicked Burst Lightning deals 4, not the unkicked 2"
