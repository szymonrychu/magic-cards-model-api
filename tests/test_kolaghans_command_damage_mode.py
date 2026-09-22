"""Kolaghan's Command's own damage mode, end to end through the real API.

Engine commit 4ce9ae1: "the damage-to-target reader admits a possessive
apostrophe in a card's own name (Kolaghan's Command's damage mode read as
nothing)." The mode's printed text is self-referential - "Kolaghan's Command
deals 2 damage to any target" - rather than "this spell" or "it", and the
reader that turns a mode's own body into a damage effect choked on the
card's own possessive apostrophe, so choosing this mode was legal (CR 700.2a
never refused it) but resolved doing nothing at all: a defect that would
never show up as a refusal, only as a spell that fizzled its own words.

This proves the mode renders in `affordances`, is choosable through the
ordinary `cast` route (no cascade, no keyword - the plain modal path any
other charm already uses) alongside a second mode (CR 700.2 "choose two"),
and actually deals its 2 damage once resolved.
"""

from __future__ import annotations

from conftest import add_card, card_id, state

from magic_cards_model_api.store import store

KOMMAND = "Kolaghan's Command"


def test_the_damage_mode_renders_and_deals_its_damage(client, game):
    command = card_id(client, game, KOMMAND, "alice", "hand")
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    island = add_card(client, game, "Island", "bob", "hand")["added"]["card_id"]
    uids = {name: uid for uid, name in store.seat_uids(game).items()}
    lands = [
        card["id"]
        for card in state(client, game)["battlefield"]
        if card["name"] in ("Swamp", "Mountain", "Forest")
    ]

    aff = client.get(
        f"/api/games/{game}/affordances/{command}",
        params={"player": "alice", "modes": "2,4"},
    ).json()
    phrases = " ".join(
        entry.get("mode_text") or entry.get("phrase") or "" for entry in aff.get("requirements", [])
    ).lower()
    assert "damage" in phrases, aff

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": command,
            # Mode 2 ("target player discards a card") and mode 4 ("deals 2
            # damage to any target") each name their own target, bob for
            # both - two different instances of the word "target" (CR
            # 115.3), so the repeat is legal.
            "targets": ["bob", "bob"],
            "modes": [2, 4],
            "mana_sources": lands,
        },
    )
    assert cast.status_code == 200, cast.text
    assert cast.json()["allowed"] is True, cast.json()

    # alice holds priority after her cast: resolve-top is a seat's only while it
    # holds the priority it passes (`game_access._seat_mismatch`).
    resolved = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]}).json()
    assert resolved.get("paused") is True, resolved
    discard = client.post(
        f"/api/games/{game}/decide",
        params={"uid": uids["bob"]},
        json={"player": "bob", "card_ids": [island]},
    )
    assert discard.json()["accepted"] is True

    final = state(client, game)
    bob = next(p for p in final["players"] if p["username"] == "bob")
    assert bob["life"] == 18, "the damage mode - previously a silent no-op - actually dealt 2"
    assert final["hands"]["bob"] == [], "the discard mode still fired alongside it"
