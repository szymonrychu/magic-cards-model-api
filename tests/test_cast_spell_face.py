"""Announcing which half of a card is being cast (CR 709.3, CR 715.3).

The defect this file was written for: the engine's own enumeration offers a
split card as **two** `CAST` actions, one per half, each carrying
`Action.spell_face` - and this api could not act on either of them. `CastRequest`
had no face field, and `engine_api._drive_cast` never called
`Casting.cast_as_split_half`, so every cast of Wear // Tear resolved as Wear
whatever the seat had been offered. An agent seat handed "cast Tear" in
`available_actions` followed its instructions and got the front half, or a
refusal it could not diagnose. That is worse than a missing feature: the api
was advertising an action it could not perform.

The property that makes the field usable is the **round trip**: the string a
caller sends in `spell_face` is the same string the engine put in
`Action.spell_face`, so a seat can hand back exactly what it was offered
without transforming it.

Every CR number here was verified in the session that wrote this file, against
the Comprehensive Rules text on disk (`mise run rules:find 709.3`,
`rules:find 715.3` in the engine repo - the file `mise run rules:pull`
downloads). Every card is a real Scryfall printing out of the engine's own
packaged corpus and every board is played through the real API.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from test_mcp_input_required import call_tool, wired

from magic_cards_model_api import baseline_seat
from magic_cards_model_api.mcp import build_server

# -- harness -----------------------------------------------------------------


def lands(client: TestClient, game: str, name: str, count: int) -> None:
    for _ in range(count):
        add_card(client, game, name, "alice", "battlefield")


def cast(client: TestClient, game: str, **body: Any) -> dict[str, Any]:
    """One cast over the raw endpoint, offering every land alice controls -
    the endpoint takes explicit `mana_sources` where the tools offer the whole
    untapped board on the caller's behalf."""
    body.setdefault(
        "mana_sources",
        [
            card["id"]
            for card in state(client, game)["battlefield"]
            if card["controller"] == "alice" and "Land" in card.get("type_line", "")
        ],
    )
    response = client.post(f"/api/games/{game}/actions/cast", json={"player": "alice", **body})
    assert response.status_code == 200, response.text
    return response.json()


def enumerated(client: TestClient, game: str) -> list[dict[str, Any]]:
    """`GET .../legal-actions` for alice, as a seat reads it."""
    from conftest import _ADMIN_KEYS

    response = client.get(
        f"/api/games/{game}/legal-actions",
        params={"player": "alice", "uid": _ADMIN_KEYS[game]},
    )
    assert response.status_code == 200, response.text
    return response.json()["actions"]


def board_names(client: TestClient, game: str) -> list[str]:
    return [card["name"] for card in state(client, game)["battlefield"]]


def wear_and_tear(client: TestClient, game: str) -> None:
    """Wear // Tear in alice's hand, an artifact and an enchantment to aim the
    two halves at, and enough mana for either."""
    add_card(client, game, "Wear // Tear", "alice", "hand")
    lands(client, game, "Sacred Foundry", 4)
    add_card(client, game, "Pithing Needle", "bob", "battlefield")
    add_card(client, game, "Rest in Peace", "bob", "battlefield")


def target(client: TestClient, game: str, name: str) -> str:
    return find(state(client, game)["battlefield"], name)["id"]


# -- the back half, actually cast as the back half ---------------------------


def test_the_back_half_of_a_split_card_is_cast_as_the_back_half(client, game):
    """CR 709.3: "A player chooses which half of a split card they are casting
    before putting it onto the stack." CR 709.3b: "While on the stack, only
    the characteristics of the half being cast exist."

    Tear is {W} and destroys an enchantment; Wear is {1}{R} and destroys an
    artifact. Announcing Tear must charge Tear's own cost, accept Tear's own
    target, and destroy the enchantment - the artifact is not Tear's business.
    """
    wear_and_tear(client, game)

    outcome = cast(
        client,
        game,
        card="Wear // Tear",
        spell_face="Tear",
        targets=[target(client, game, "Rest in Peace")],
        reasoning="the enchantment is the problem, not the artifact",
    )

    assert outcome["allowed"] is True, outcome
    assert outcome["cost"] == "{W}", outcome
    assert [card["name"] for card in state(client, game)["stack"]] == ["Tear"]

    client.post(f"/api/games/{game}/resolve-top")
    names = board_names(client, game)
    assert "Rest in Peace" not in names, names
    assert "Pithing Needle" in names, names


def test_a_cast_that_names_no_face_is_still_the_front_half(client, game):
    """The regression guard for every existing caller: nothing on this api
    sent a face before this change, and absent must keep meaning the front
    half - Wear, {1}{R}, aimed at an artifact."""
    wear_and_tear(client, game)

    outcome = cast(
        client,
        game,
        card="Wear // Tear",
        targets=[target(client, game, "Pithing Needle")],
        reasoning="no face named, so the front half",
    )

    assert outcome["allowed"] is True, outcome
    assert outcome["cost"] == "{1}{R}", outcome
    assert [card["name"] for card in state(client, game)["stack"]] == ["Wear"]

    client.post(f"/api/games/{game}/resolve-top")
    names = board_names(client, game)
    assert "Pithing Needle" not in names, names
    assert "Rest in Peace" in names, names


def test_the_front_half_cannot_take_the_back_halfs_target(client, game):
    """The other side of the same coin, and the proof that the face is what
    drives the targeting profile rather than an accident of which half was
    cheaper: with no face named, an enchantment is not a legal target at all
    (CR 601.2c), because the front half destroys artifacts."""
    wear_and_tear(client, game)

    outcome = cast(
        client,
        game,
        card="Wear // Tear",
        targets=[target(client, game, "Rest in Peace")],
        reasoning="aiming the front half at an enchantment",
    )

    assert outcome["allowed"] is False, outcome
    assert "Rest in Peace" in (outcome["reason"] or ""), outcome
    assert state(client, game)["stack"] == []


# -- the face string is the engine's own, and it round-trips ------------------


def test_the_face_the_enumeration_offers_is_accepted_back_verbatim(client, game):
    """The property that makes the field usable by an agent: take `spell_face`
    straight out of `GET .../legal-actions` and post it back unmodified.

    A seat that has to transform what it was offered before it can send it is
    a seat that will get the transformation wrong.
    """
    wear_and_tear(client, game)

    rows = [row for row in enumerated(client, game) if row["kind"] == "cast"]
    faces = [row["spell_face"] for row in rows]
    assert faces == ["Wear", "Tear"], faces
    back = next(row for row in rows if row["spell_face"] == "Tear")

    outcome = cast(
        client,
        game,
        card_id=back["card_id"],
        spell_face=back["spell_face"],
        targets=[target(client, game, "Rest in Peace")],
        reasoning="posting back exactly the row I was handed",
    )

    assert outcome["allowed"] is True, outcome
    assert outcome["cost"] == back["mana_cost"], (outcome, back["mana_cost"])


def test_a_face_this_card_does_not_have_is_refused_in_the_engines_own_words(client, game):
    """Fail closed and loudly: a face that is not one of the card's halves is
    refused with the engine's own sentence, never silently resolved as the
    front half - which is precisely what a seat could not diagnose before."""
    wear_and_tear(client, game)

    outcome = cast(
        client,
        game,
        card="Wear // Tear",
        spell_face="Naturalize",
        targets=[target(client, game, "Rest in Peace")],
        reasoning="naming a half this card does not print",
    )

    assert outcome["allowed"] is False, outcome
    assert outcome["reason"] == "Wear has no half called Naturalize", outcome
    assert state(client, game)["stack"] == []
    assert find(state(client, game)["hands"]["alice"], "Wear"), "the card never left the hand"


def test_a_card_with_no_halves_refuses_a_face_in_the_engines_own_words(client, game):
    """CR 709 is about split cards, and the engine says so itself when a face
    is named on a card that has none."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    lands(client, game, "Mountain", 2)

    outcome = cast(
        client,
        game,
        card="Lightning Bolt",
        spell_face="Lightning Bolt",
        targets=["bob"],
        reasoning="naming a face on a single-faced card",
    )

    assert outcome["allowed"] is False, outcome
    assert outcome["reason"] == "Lightning Bolt is not a split card (CR 709)", outcome


# -- generality: a split card that is not the one that motivated this ---------


def test_a_different_split_card_announces_its_own_back_half_too(client, game):
    """Repudiate // Replicate, which is not Wear // Tear and is not in the deck
    pool that exposed this: Repudiate ({G/U}{G/U}) counters an ability,
    Replicate ({1}{G}{U}) copies a creature you control. The mechanic is
    CR 709.3's choice, not one card's printing.

    A creature is a legal target for Replicate and could never be one for
    Repudiate, so this pair of assertions is the same face-awareness claim
    made on a second card with a different pair of halves.
    """
    add_card(client, game, "Repudiate // Replicate", "alice", "hand")
    add_card(client, game, "Repudiate // Replicate", "alice", "hand")
    lands(client, game, "Breeding Pool", 5)
    add_card(client, game, "Llanowar Elves", "alice", "battlefield")
    elves = target(client, game, "Llanowar Elves")

    front = cast(
        client,
        game,
        card="Repudiate // Replicate",
        targets=[elves],
        reasoning="the front half cannot target a creature",
    )
    assert front["allowed"] is False, front

    back = cast(
        client,
        game,
        card="Repudiate // Replicate",
        spell_face="Replicate",
        targets=[elves],
        reasoning="copying my own creature",
    )
    assert back["allowed"] is True, back
    assert back["cost"] == "{1}{G}{U}", back
    assert [card["name"] for card in state(client, game)["stack"]] == ["Replicate"]


# -- CR 715.3's Adventure: refused, not guessed at ---------------------------


def test_an_adventure_face_is_refused_rather_than_cast_without_its_targeting(client, game):
    """CR 715.3a: "When casting an adventurer card as an Adventure, only the
    alternative characteristics are evaluated to see if it can be cast."

    The engine does not do that yet. `Casting.cast_as_adventure` charges the
    Adventure's own mana cost and stamps `CardState.face_played`, but
    `Casting.targeting` reads the *whole printing* for every layout except a
    split card - its own docstring says so - so Stomp ("Stomp deals 2 damage
    to any target") is announced with **no** target requirement at all: any
    target named is refused with "too many targets: 1 chosen where the spell
    takes at most 0", and a Stomp cast with none resolves dealing no damage.

    So the api refuses the announcement instead of making it. A refusal a seat
    can read beats a spell that costs mana and does nothing, and the refusal
    says where the gap is rather than guessing at the rule on the engine's
    behalf.
    """
    add_card(client, game, "Bonecrusher Giant", "alice", "hand")
    lands(client, game, "Mountain", 3)

    outcome = cast(
        client,
        game,
        card="Bonecrusher Giant // Stomp",
        spell_face="Stomp",
        reasoning="casting the Adventure half",
    )

    assert outcome["allowed"] is False, outcome
    reason = outcome["reason"] or ""
    assert "Stomp" in reason and "CR 715.3a" in reason, outcome
    assert state(client, game)["stack"] == []
    assert find(state(client, game)["hands"]["alice"], "Bonecrusher Giant")


def test_the_creature_half_of_an_adventurer_card_is_unaffected(client, game):
    """Naming no face is the creature's own plain cast (CR 715.3's other
    branch), and it is exactly as castable as it ever was."""
    add_card(client, game, "Bonecrusher Giant", "alice", "hand")
    lands(client, game, "Mountain", 3)

    outcome = cast(
        client, game, card="Bonecrusher Giant // Stomp", reasoning="casting the creature"
    )

    assert outcome["allowed"] is True, outcome
    assert outcome["cost"] == "{2}{R}", outcome
    assert [card["name"] for card in state(client, game)["stack"]] == ["Bonecrusher Giant"]


def test_a_second_adventurer_card_is_refused_the_same_way(client, game):
    """Generality again: Brazen Borrower // Petty Theft, a different adventurer
    card with a different Adventure, refused for the mechanic rather than for
    the card."""
    add_card(client, game, "Brazen Borrower", "alice", "hand")
    lands(client, game, "Island", 3)

    outcome = cast(
        client,
        game,
        card="Brazen Borrower // Petty Theft",
        spell_face="Petty Theft",
        reasoning="casting the Adventure half",
    )

    assert outcome["allowed"] is False, outcome
    assert "Petty Theft" in (outcome["reason"] or ""), outcome
    assert state(client, game)["stack"] == []


# -- the surfaces a seat actually calls --------------------------------------


def test_the_cast_tool_announces_a_split_half(client, game, monkeypatch):
    """The seat's own path, end to end: `mcp__mtg__cast` with `spell_face`.
    Without this parameter the enumeration's face rows are unreachable from
    the one surface an agent seat calls."""
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)
    wear_and_tear(client, game)

    result = call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": "Wear // Tear",
            "spell_face": "Tear",
            "targets": target(client, game, "Rest in Peace"),
            "hold_priority": True,
            "reasoning": "the enchantment is the problem",
        },
    )

    content = result if isinstance(result, str) else result.content[0].text
    payload = json.loads(content)
    assert payload["outcome"]["allowed"] is True, payload
    assert [card["name"] for card in state(client, game)["stack"]] == ["Tear"]


def test_the_cast_tools_own_contract_names_the_face_parameter():
    """`contract/tools.json` is the schema a seat's client reads
    (`scripts/export_tools.py`). A parameter that is not in it is a parameter
    no seat can send, however well documented it is server-side."""
    contract = json.loads(
        (Path(__file__).resolve().parents[1] / "contract" / "tools.json").read_text()
    )
    cast_tool = next(tool for tool in contract["tools"] if tool["name"] == "cast")
    names = {parameter["name"] for parameter in cast_tool["parameters"]}
    assert "spell_face" in names, sorted(names)


def test_the_published_openapi_carries_the_face_on_cast_request():
    """`openapi.json` is this repo's committed HTTP contract
    (`mise run openapi:dump`), and `CastRequest` is the body of the one
    endpoint this whole file is about."""
    spec = json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text())
    properties = spec["components"]["schemas"]["CastRequest"]["properties"]
    assert "spell_face" in properties, sorted(properties)


def test_the_seat_cli_can_announce_a_face():
    """The other front end onto one seat (`seat_cli.py`). Where the two
    disagree, one of them is wrong - see `test_front_end_parity.py`."""
    from magic_cards_model_api import seat_cli

    sub = next(
        action for action in seat_cli.build_parser()._actions if getattr(action, "choices", None)
    )
    cast_parser = sub.choices["cast"]
    assert any(entry.dest == "spell_face" for entry in cast_parser._actions), [
        entry.dest for entry in cast_parser._actions
    ]


# -- the baseline seat's own choice ------------------------------------------


def test_the_baseline_still_announces_no_face_even_though_the_api_would_take_one(client, game):
    """`baseline_seat._plain_cast` skips a `spell_face` row, and keeps doing
    so after this change - deliberately, and no longer because the api cannot
    act on one.

    The policy is "a printed cast from hand and nothing more exotic": it
    announces no alternative cost, no keyword, no mode, no X and no splice,
    because announcing one badly is a refusal the scorecard would report as a
    rules error the agent never made. A chosen face is an announcement of
    exactly that kind (CR 709.3, CR 715.3), so it stays out for the same
    reason the other five do. Two live facts back that up: the engine's
    `Action.target_slots` still reports the FRONT half's phrase on both of a
    split card's rows, so the row a policy would reason from misdescribes what
    the back half needs; and an Adventure face is refused outright (CR 715.3a).

    The second half of this test is what keeps the first half honest: the same
    row the policy passes over is accepted by the api when it is posted.
    """
    from magic_cards_model_api.store import store

    wear_and_tear(client, game)
    uid = next(uid for uid, who in store.seat_uids(game).items() if who == "alice")
    board = client.get(f"/api/games/{game}", params={"uid": uid}).json()
    actions = client.get(
        f"/api/games/{game}/legal-actions", params={"player": "alice", "uid": uid}
    ).json()["actions"]

    move = baseline_seat.choose_move(board, actions, "alice")

    assert move.label == "pass_priority", move
    assert [row["spell_face"] for row in actions if row["kind"] == "cast"] == ["Wear", "Tear"]

    row = next(row for row in actions if row.get("spell_face") == "Tear")
    outcome = cast(
        client,
        game,
        card_id=row["card_id"],
        spell_face=row["spell_face"],
        targets=[target(client, game, "Rest in Peace")],
        reasoning="the row the baseline passed over is castable",
    )
    assert outcome["allowed"] is True, outcome
