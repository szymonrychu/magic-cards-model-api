"""Naming a CR 118.9 alternative cost the way a seat actually reads it.

The defect, from a live prod game: an AI seat on its opponent's turn called

    cast(card=<Force of Negation>, alternative_cost="exile a blue card from
         your hand", choosing=<a blue card>, targets=<Trinisphere on the stack>)

- the exact label its own `legal-actions` row printed for the pitch - and was
refused, because the engine's `Casting.use_alternative_cost` only matched an
option's `keyword` or `source_name`, never the `description` that
`legal_actions` advertises for a printed pitch offer. The seat then spent
model turns guessing spellings. A cast now takes the advertised label, the
printed phrase (any case, punctuation, "you may ..." / "... rather than pay
this spell's mana cost" framing), or the option's index / keyword / source
name, and an unmatched value is refused with every accepted value listed.

Real printings, real engine, played through the real API.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_cast_cost_choices import answer
from test_mcp_input_required import call_tool, wired

from magic_cards_model_api.mcp import build_server

FON_PHRASE = "exile a blue card from your hand"


def _post(client: TestClient, game: str, path: str, body: dict[str, Any]) -> dict:
    response = client.post(f"/api/games/{game}/{path}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def fon_board(client: TestClient, game: str) -> dict[str, str]:
    """Bob's main phase, bob's Trinisphere on the stack, alice holding
    priority with Force of Negation and a blue card (Preordain) in hand and
    no lands - so only the pitch can pay."""
    for _ in range(3):
        add_card(client, game, "Wastes", "bob", "battlefield")
    add_card(client, game, "Trinisphere", "bob", "hand")
    add_card(client, game, "Force of Negation", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
    board = state(client, game)
    wastes = [c["id"] for c in board["battlefield"] if c["name"] == "Wastes"]
    cast = _post(
        client,
        game,
        "actions/cast",
        {"player": "bob", "card": "Trinisphere", "mana_sources": wastes},
    )
    assert cast["allowed"] is True, cast
    passed = _post(client, game, "actions/pass-priority", {"player": "bob"})
    assert passed["allowed"] is True, passed
    board = state(client, game)
    return {
        "fon": find(board["hands"]["alice"], "Force of Negation")["id"],
        "pitch": find(board["hands"]["alice"], "Preordain")["id"],
        "trinisphere": find(board["stack"], "Trinisphere")["id"],
    }


def legal_actions(client: TestClient, game: str) -> list[dict]:
    from conftest import _ADMIN_KEYS

    response = client.get(
        f"/api/games/{game}/legal-actions", params={"player": "alice", "uid": _ADMIN_KEYS[game]}
    )
    assert response.status_code == 200, response.text
    return response.json()["actions"]


def cast_fon(client: TestClient, game: str, ids: dict[str, str], **body: Any) -> dict:
    return _post(
        client,
        game,
        "actions/cast",
        {
            "player": "alice",
            "card_id": ids["fon"],
            "targets": [ids["trinisphere"]],
            **body,
        },
    )


def assert_pitched(client: TestClient, game: str) -> None:
    board = state(client, game)
    assert sorted(c["name"] for c in board["stack"]) == ["Force of Negation", "Trinisphere"], board[
        "stack"
    ]
    assert [c["name"] for c in board["exile"]] == ["Preordain"], board["exile"]


def test_the_legal_actions_label_for_the_pitch_is_accepted_verbatim(client, game):
    ids = fon_board(client, game)
    rows = legal_actions(client, game)
    pitch_rows = [
        row
        for row in rows
        if row.get("kind") == "cast"
        and row.get("card_id") == ids["fon"]
        and row.get("alternative_cost")
    ]
    assert pitch_rows, rows
    label = pitch_rows[0]["alternative_cost"]

    outcome = cast_fon(client, game, ids, alternative_cost=label, choosing=[ids["pitch"]])

    assert outcome["allowed"] is True, outcome
    assert_pitched(client, game)


def test_the_printed_phrase_is_accepted_in_any_case_and_framing(client, game):
    ids = fon_board(client, game)
    spelled = (
        "If it's not your turn, you may Exile a blue card from your hand rather than "
        "pay this spell's mana cost."
    )

    outcome = cast_fon(client, game, ids, alternative_cost=spelled, choosing=[ids["pitch"]])

    assert outcome["allowed"] is True, outcome
    assert_pitched(client, game)


def test_capitalised_phrase_from_the_live_game_is_accepted(client, game):
    ids = fon_board(client, game)
    outcome = cast_fon(
        client,
        game,
        ids,
        alternative_cost="Exile a blue card from your hand",
        choosing=[ids["pitch"]],
    )
    assert outcome["allowed"] is True, outcome
    assert_pitched(client, game)


def test_the_canonical_value_on_the_affordance_is_accepted(client, game):
    ids = fon_board(client, game)
    affordance = client.get(
        f"/api/games/{game}/affordances/{ids['fon']}", params={"player": "alice"}
    ).json()
    option = affordance["alternative_costs"][0]
    assert option["alternative_cost"] == FON_PHRASE, option

    outcome = cast_fon(
        client, game, ids, alternative_cost=option["alternative_cost"], choosing=[ids["pitch"]]
    )

    assert outcome["allowed"] is True, outcome
    assert_pitched(client, game)


def test_garbage_is_refused_naming_the_card_and_every_accepted_value(client, game):
    ids = fon_board(client, game)
    outcome = cast_fon(
        client, game, ids, alternative_cost="pitch something", choosing=[ids["pitch"]]
    )

    assert outcome["allowed"] is False, outcome
    reason = outcome["reason"]
    assert "Force of Negation" in reason, reason
    assert "'pitch something'" in reason, reason
    assert f"'{FON_PHRASE}'" in reason, reason
    board = state(client, game)
    assert [c["name"] for c in board["stack"]] == ["Trinisphere"]
    assert "Force of Negation" in [c["name"] for c in board["hands"]["alice"]]


def test_the_pitched_card_may_be_named_in_exiling_too(client, game):
    """The live seat's second guess: `exiling` rather than `choosing`. A card
    with no printed additional cost has nothing else `exiling` could mean, so
    it names the pitch's objects rather than being silently dropped."""
    ids = fon_board(client, game)
    outcome = cast_fon(client, game, ids, alternative_cost=FON_PHRASE, exiling=[ids["pitch"]])
    assert outcome["allowed"] is True, outcome
    assert_pitched(client, game)


def test_the_mcp_cast_tool_pitches_force_of_negation_by_printed_phrase(client, game, monkeypatch):
    ids = fon_board(client, game)
    seats, uids = wired(client, monkeypatch, game)
    server = build_server(seats)

    result = call_tool(
        server,
        "cast",
        {
            "player_uid": uids["alice"],
            "card": ids["fon"],
            "alternative_cost": FON_PHRASE,
            "choosing": ids["pitch"],
            "targets": ids["trinisphere"],
            "hold_priority": True,
            "reasoning": "pitching to counter Trinisphere",
        },
    )

    assert answer(result)["outcome"]["allowed"] is True, answer(result)
    assert_pitched(client, game)


# -- other alternative-cost cards --------------------------------------------


def test_force_of_will_pitch_by_printed_phrase(client, game):
    """Force of Will: "You may pay 1 life and exile a blue card from your
    hand rather than pay this spell's mana cost." - any turn."""
    add_card(client, game, "Force of Will", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    add_card(client, game, "Mountain", "bob", "battlefield")
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")["id"]
    bolt = _post(
        client,
        game,
        "actions/cast",
        {
            "player": "bob",
            "card": "Lightning Bolt",
            "targets": ["alice"],
            "mana_sources": [mountain],
        },
    )
    assert bolt["allowed"] is True, bolt
    assert _post(client, game, "actions/pass-priority", {"player": "bob"})["allowed"]
    board = state(client, game)

    outcome = _post(
        client,
        game,
        "actions/cast",
        {
            "player": "alice",
            "card": "Force of Will",
            "alternative_cost": "PAY 1 LIFE AND EXILE A BLUE CARD FROM YOUR HAND.",
            "choosing": [find(board["hands"]["alice"], "Preordain")["id"]],
            "targets": [find(board["stack"], "Lightning Bolt")["id"]],
        },
    )

    assert outcome["allowed"] is True, outcome
    assert [c["name"] for c in state(client, game)["exile"]] == ["Preordain"]


def test_evoke_by_keyword_label_and_by_its_cost_words(client, game):
    """Solitude's evoke is a keyword-sourced alternative cost: its canonical
    value is the keyword, and the cost's own words are accepted too."""
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")
    board = state(client, game)
    solitude = find(board["hands"]["alice"], "Solitude")["id"]
    affordance = client.get(
        f"/api/games/{game}/affordances/{solitude}", params={"player": "alice"}
    ).json()
    evoke = next(alt for alt in affordance["alternative_costs"] if alt["keyword"] == "Evoke")
    assert evoke["alternative_cost"] == "Evoke", evoke

    outcome = _post(
        client,
        game,
        "actions/cast",
        {
            "player": "alice",
            "card_id": solitude,
            "alternative_cost": "evoke",
            "choosing": [find(board["hands"]["alice"], "Savannah Lions")["id"]],
        },
    )

    assert outcome["allowed"] is True, outcome
    assert [c["name"] for c in state(client, game)["exile"]] == ["Savannah Lions"]


def test_evoke_named_by_its_cost_words(client, game):
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")
    board = state(client, game)

    outcome = _post(
        client,
        game,
        "actions/cast",
        {
            "player": "alice",
            "card": "Solitude",
            "alternative_cost": "Exile a white card from your hand",
            "choosing": [find(board["hands"]["alice"], "Savannah Lions")["id"]],
        },
    )

    assert outcome["allowed"] is True, outcome
    assert [c["name"] for c in state(client, game)["exile"]] == ["Savannah Lions"]


def test_a_mana_cost_cast_is_unaffected(client, game):
    """No alternative cost and no objects named: Force of Negation is cast
    for its printed {1}{U}{U} and the blue card stays in hand."""
    ids = fon_board(client, game)
    for _ in range(3):
        add_card(client, game, "Island", "alice", "battlefield")
    islands = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Island"]

    outcome = cast_fon(client, game, ids, mana_sources=islands)

    assert outcome["allowed"] is True, outcome
    board = state(client, game)
    assert board["exile"] == []
    assert "Preordain" in [c["name"] for c in board["hands"]["alice"]]
    assert all(c["tapped"] for c in board["battlefield"] if c["name"] == "Island")


def test_the_pitch_named_only_by_its_objects_announces_the_only_alternative_cost(client, game):
    """The live seat's first guess: no `alternative_cost`, the blue card in
    `exiling`. That was silently charged the mana cost ("pool {} cannot pay
    {1}{U}{U}"); objects named for a card that prints no additional cost can
    only be for its alternative cost, so the only available one is used."""
    ids = fon_board(client, game)
    outcome = cast_fon(client, game, ids, exiling=[ids["pitch"]])
    assert outcome["allowed"] is True, outcome
    assert_pitched(client, game)


def test_a_cost_floor_on_the_battlefield_still_charges_the_pitched_spell(client, game):
    """Trinisphere untapped on the battlefield (CR 601.2f): the pitched Force
    of Negation costs {3}. Named by its printed phrase, the pitch is accepted
    and the floor is what remains to pay - refused for mana, not for naming."""
    add_card(client, game, "Trinisphere", "bob", "battlefield")
    # Shock costs three under Trinisphere's floor too.
    for _ in range(3):
        add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Shock", "bob", "hand")
    add_card(client, game, "Force of Negation", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
    mountains = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Mountain"]
    shock = _post(
        client,
        game,
        "actions/cast",
        {"player": "bob", "card": "Shock", "targets": ["alice"], "mana_sources": mountains},
    )
    assert shock["allowed"] is True, shock
    assert _post(client, game, "actions/pass-priority", {"player": "bob"})["allowed"]
    board = state(client, game)
    body = {
        "player": "alice",
        "card": "Force of Negation",
        "alternative_cost": "Exile a blue card from your hand.",
        "choosing": [find(board["hands"]["alice"], "Preordain")["id"]],
        "targets": [find(board["stack"], "Shock")["id"]],
    }

    broke = _post(client, game, "actions/cast", {**body, "dry_run": True})
    assert broke["allowed"] is False, broke
    assert "{3}" in broke["reason"] and "alternative cost" not in broke["reason"], broke

    for _ in range(3):
        add_card(client, game, "Island", "alice", "battlefield")
    islands = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Island"]
    paid = _post(client, game, "actions/cast", {**body, "mana_sources": islands})
    assert paid["allowed"] is True, paid
    board = state(client, game)
    assert [c["name"] for c in board["exile"]] == ["Preordain"]
    assert all(c["tapped"] for c in board["battlefield"] if c["name"] == "Island")


# -- every alternative-cost row the pool can produce -------------------------


def _pool_alternative_cost_cards() -> list[str]:
    """Every card in `decks/*.txt` whose printing the engine reads a CR 118.9
    alternative cost off - a printed offer, or a keyword that replaces the
    mana cost. Read from the engine, never listed by hand."""
    import re
    from pathlib import Path

    from magic_cards_model.machines.casting import alternative_cost_offers, keyword_cast_options
    from magic_cards_model.models import Card

    from magic_cards_model_api.scryfall import fetch_card

    names: set[str] = set()
    for deck in (Path(__file__).resolve().parents[1] / "decks").glob("*.txt"):
        for line in deck.read_text().splitlines():
            match = re.match(r"\d+ (.+)$", line.strip())
            if match:
                names.add(match.group(1).strip())
    found = []
    for name in sorted(names):
        card = Card.model_validate(fetch_card(name))
        if alternative_cost_offers(card) or any(
            not option.pays_mana_cost for option in keyword_cast_options(card)
        ):
            found.append(name)
    return found


#: One card of each colour, twice, so any "exile a <colour> card" / "exile
#: two <colour> cards" pitch has something to pay with.
_FODDER = ("Preordain", "Savannah Lions", "Llanowar Elves", "Duress", "Lightning Bolt")


def _rows_on(client: TestClient, game: str, name: str, *, opponents_turn: bool) -> list[dict]:
    """The card in alice's hand and her graveyard (flashback, escape and the
    other graveyard permissions), on her own main phase or with bob's Shock on
    the stack on his turn."""
    add_card(client, game, name, "alice", "hand")
    add_card(client, game, name, "alice", "graveyard")
    for fodder in _FODDER:
        for _ in range(2):
            add_card(client, game, fodder, "alice", "hand")
    for land in ("Island", "Plains", "Forest", "Swamp", "Mountain"):
        add_card(client, game, land, "alice", "battlefield")
    if opponents_turn:
        add_card(client, game, "Mountain", "bob", "battlefield")
        add_card(client, game, "Shock", "bob", "hand")
        force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
        board = state(client, game)
        mountain = next(
            c["id"]
            for c in board["battlefield"]
            if c["name"] == "Mountain" and c["controller"] == "bob"
        )
        shock = _post(
            client,
            game,
            "actions/cast",
            {"player": "bob", "card": "Shock", "targets": ["alice"], "mana_sources": [mountain]},
        )
        assert shock["allowed"] is True, shock
        assert _post(client, game, "actions/pass-priority", {"player": "bob"})["allowed"]
    board = state(client, game)
    copies = {
        find(board["hands"]["alice"], name)["id"],
        find(board["graveyards"]["alice"], name)["id"],
    }
    return [
        row
        for row in legal_actions(client, game)
        if row["kind"] == "cast" and row["card_id"] in copies and row.get("alternative_cost")
    ]


def _targets_fillable(row: dict) -> bool:
    return all(
        len(slot.get("candidates") or []) + len(slot.get("player_candidates") or [])
        >= slot["minimum"]
        for slot in row.get("target_slots") or []
    )


def _dry_run_row(client: TestClient, game: str, row: dict) -> dict:
    affordance = client.get(
        f"/api/games/{game}/affordances/{row['card_id']}", params={"player": "alice"}
    ).json()
    option = next(
        alt
        for alt in affordance["alternative_costs"]
        if alt["alternative_cost"] == row["alternative_cost"]
    )
    targets: list[str] = []
    for slot in row.get("target_slots") or []:
        pool = list(slot.get("candidates") or []) + list(slot.get("player_candidates") or [])
        targets.extend(pool[: slot["minimum"]])
    body: dict[str, Any] = {
        "player": "alice",
        "card_id": row["card_id"],
        "alternative_cost": row["alternative_cost"],
        "choosing": [c["id"] for c in option["candidates"][: option["requires"]]],
        "dry_run": True,
    }
    if targets:
        body["targets"] = targets
    return _post(client, game, "actions/cast", body)


@pytest.mark.parametrize("name", _pool_alternative_cost_cards())
@pytest.mark.parametrize("opponents_turn", [False, True], ids=["own-turn", "opponents-turn"])
def test_every_pool_alternative_cost_row_label_is_accepted_by_cast(
    client, game, name, opponents_turn
):
    """The regression the prod refusal calls for: whatever `alternative_cost`
    a `legal-actions` row prints, a dry-run cast sending exactly that value is
    allowed - the row and the cast lookup share one source of truth."""
    rows = _rows_on(client, game, name, opponents_turn=opponents_turn)
    for row in rows:
        outcome = _dry_run_row(client, game, row)
        if row["affordable"] == "yes" and _targets_fillable(row):
            assert outcome["allowed"] is True, (name, row["alternative_cost"], outcome)
        else:
            # A row this board cannot pay for (Detective's Phoenix's bestow
            # collects evidence from an empty graveyard) or has no legal
            # target for is still refused - for that, never for the name it
            # printed.
            reason = outcome.get("reason") or ""
            assert "no alternative cost" not in reason, (name, row["alternative_cost"], reason)
            assert "matches more than one" not in reason, (name, row["alternative_cost"], reason)


def test_the_pool_sweep_covers_the_printed_pitch_cards():
    names = _pool_alternative_cost_cards()
    assert {"Force of Negation", "Solitude"} <= set(names), names
