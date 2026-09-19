"""Battles 3 and 4: what a legal-action row claims about an action.

Each defect here was a row saying something the engine would contradict the
moment the seat acted on it:

- **B3-04** - an ability offered from a zone it does not function in. CR
  113.6b: "An ability that states which zones it functions in functions only
  from those zones"; CR 113.6m: "An ability whose cost or effect specifies
  that it moves the object it's on out of a particular zone functions only in
  that zone". An exiled Boseiju's Channel (a hand ability) was offered to both
  seats, and a battlefield Seasoned Pyromancer's "Exile this card from your
  graveyard" ability was offered on the battlefield. CR 602.2: "Only an
  object's controller (or its owner, if it doesn't have a controller) can
  activate its activated ability", and CR 108.4a makes a card with no
  controller answer with its owner - so an opponent's exiled card is never
  this seat's to activate either.
- **B4-05** - a loyalty ability labelled a mana ability. CR 605.1a: a mana
  ability is one that "it's not a loyalty ability". Ugin, Eye of the Storms'
  `0: Add {C}{C}{C}` was an `activate_mana` row, `is_mana_ability: true` in
  the card view, a default mana source for the seat tools, and every refused
  cast told the seat to "activate it manually to include it" mid-cast.
- **B4-02** - a cast row priced at the printed cost. CR 601.2f: the total cost
  is determined with every increase and reduction applied, and CR 118.7a
  keeps a generic reduction off the colored part. Malevolent Rumble read
  `{1}{G}` under Trinisphere and Emrakul, the Promised End `{13}` with two
  card types in the graveyard.
- **B3-05** - a mode the engine cannot read offered as silently castable.
  CR 700.2a: modes are chosen as the spell is cast; the engine's
  `resolve_modal_spell` skips an unreadable mode with a `ModeNotice`, so a
  seat that chose one paid a card for nothing.

Every CR number here was read from the Comprehensive Rules text in the
session that wrote this file. Every card is a real Scryfall printing.
"""

from __future__ import annotations

from conftest import add_card, find, state
from test_legal_actions_endpoint import legal_actions

from magic_cards_model_api.seatkit import untapped_mana_sources


def rows_for(actions: list[dict], card_id: str) -> list[dict]:
    return [action for action in actions if action.get("card_id") == card_id]


# -- B3-04: abilities only from the zone they function in ---------------------


def test_an_exiled_channel_card_is_offered_to_neither_seat(client, game):
    """Boseiju's Channel discards the card from hand (CR 113.6j), so in exile
    it functions nowhere - and bob never owned it (CR 602.2, CR 108.4a)."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Boseiju, Who Endures", "alice", "exile")
    boseiju = find(state(client, game)["exile"], "Boseiju, Who Endures")["id"]

    assert rows_for(legal_actions(client, game, "alice"), boseiju) == []
    assert rows_for(legal_actions(client, game, "bob"), boseiju) == []


def test_a_channel_card_in_hand_is_still_offered(client, game):
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Boseiju, Who Endures", "alice", "hand")
    boseiju = find(state(client, game)["hands"]["alice"], "Boseiju, Who Endures")["id"]

    rows = rows_for(legal_actions(client, game, "alice"), boseiju)
    (row,) = [row for row in rows if row["kind"] == "activate_off_zone"]
    assert row["from_zone"] == "hand"


def test_a_battlefield_creatures_graveyard_ability_is_not_offered(client, game):
    """Seasoned Pyromancer's "{3}{R}{R}, Exile this card from your graveyard:
    ..." moves the card out of the graveyard, so it functions only there
    (CR 113.6m)."""
    for _ in range(5):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Seasoned Pyromancer", "alice", "battlefield")
    pyromancer = find(state(client, game)["battlefield"], "Seasoned Pyromancer")["id"]

    assert rows_for(legal_actions(client, game, "alice"), pyromancer) == []


def test_an_ordinary_battlefield_ability_is_still_offered(client, game):
    """The same rule, from the other side: Goblin Bombardment's sacrifice
    ability names no other zone, so it stays on the battlefield list."""
    add_card(client, game, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    bombardment = find(state(client, game)["battlefield"], "Goblin Bombardment")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), bombardment)
    assert row["kind"] == "activate"


# -- B4-05: loyalty abilities are never mana abilities -------------------------


def ugin_board(client, game) -> dict:
    add_card(client, game, "Ugin, Eye of the Storms", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Emrakul, the Promised End", "alice", "hand")
    board = state(client, game)
    return {
        "ugin": find(board["battlefield"], "Ugin, Eye of the Storms")["id"],
        "forest": find(board["battlefield"], "Forest")["id"],
        "emrakul": find(board["hands"]["alice"], "Emrakul, the Promised End")["id"],
    }


def test_a_loyalty_ability_that_adds_mana_is_an_ordinary_activation_row(client, game):
    rig = ugin_board(client, game)

    rows = rows_for(legal_actions(client, game, "alice"), rig["ugin"])
    zero = next(row for row in rows if row["cost"] == "0")
    assert zero["kind"] == "activate", zero
    assert all(row["kind"] != "activate_mana" for row in rows), rows


def test_the_card_view_does_not_call_a_loyalty_ability_a_mana_ability(client, game):
    rig = ugin_board(client, game)
    board = state(client, game)

    ugin = next(card for card in board["battlefield"] if card["id"] == rig["ugin"])
    zero = next(ability for ability in ugin["abilities"] if ability["cost"] == "0")
    assert zero["is_mana_ability"] is False
    assert zero["mana"] is False
    assert rig["ugin"] not in untapped_mana_sources(board, "alice")
    assert rig["forest"] in untapped_mana_sources(board, "alice")


def test_no_row_or_refusal_advises_activating_a_loyalty_ability_mid_cast(client, game):
    rig = ugin_board(client, game)

    (emrakul,) = rows_for(legal_actions(client, game, "alice"), rig["emrakul"])
    assert emrakul["affordable"] == "no"
    assert "Ugin" not in (emrakul["unaffordable_reason"] or ""), emrakul
    assert "activate it manually" not in (emrakul["unaffordable_reason"] or "")

    refused = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": rig["emrakul"],
            # What the seat tools offered by default before this fix: every
            # untapped source a card view called a mana ability, Ugin included.
            "mana_sources": [rig["forest"], rig["ugin"]],
            "dry_run": True,
        },
    ).json()
    assert refused["allowed"] is False, refused
    assert "Ugin" not in refused["reason"], refused
    assert "no combination of the available mana sources" in refused["reason"], refused


def test_every_loyalty_ability_the_engine_allows_is_offered(client, game):
    """CR 606.6: the -11 is activatable once the counters are there."""
    rig = ugin_board(client, game)
    patched = client.patch(
        f"/api/games/{game}/cards/{rig['ugin']}", json={"counters": {"loyalty": 11}}
    )
    assert patched.status_code == 200, patched.text

    costs = {row["cost"] for row in rows_for(legal_actions(client, game, "alice"), rig["ugin"])}
    assert costs == {"+2", "0", "−11"}, costs


# -- B4-02: the cost the engine will charge ------------------------------------


def test_a_board_wide_floor_raises_the_row_to_what_the_cast_charges(client, game):
    """Trinisphere: "each spell that would cost less than three mana to cast
    costs three mana to cast" - Malevolent Rumble is charged {2}{G}."""
    for _ in range(3):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Malevolent Rumble", "alice", "hand")
    add_card(client, game, "Trinisphere", "bob", "battlefield")
    rumble = find(state(client, game)["hands"]["alice"], "Malevolent Rumble")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), rumble)
    assert row["cost"] == "{2}{G}", row
    assert row["mana_cost"] == "{2}{G}", row
    assert row["printed_cost"] == "{1}{G}", row
    assert row["affordable"] == "yes", row

    dry = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": rumble, "dry_run": True},
    ).json()
    assert dry["cost"] == row["cost"], dry


def test_a_floor_the_lands_cannot_meet_makes_the_row_unaffordable(client, game):
    for _ in range(2):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Malevolent Rumble", "alice", "hand")
    add_card(client, game, "Trinisphere", "bob", "battlefield")
    rumble = find(state(client, game)["hands"]["alice"], "Malevolent Rumble")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), rumble)
    assert row["cost"] == "{2}{G}", row
    assert row["affordable"] == "no", row


def test_a_flat_increase_is_charged_on_every_spell(client, game):
    """Sphere of Resistance: "Spells cost {1} more to cast.\""""
    for _ in range(3):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    add_card(client, game, "Sphere of Resistance", "bob", "battlefield")
    bears = find(state(client, game)["hands"]["alice"], "Grizzly Bears")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), bears)
    assert row["cost"] == "{2}{G}", row
    assert row["printed_cost"] == "{1}{G}", row


def test_a_self_reduction_is_charged_off_the_row(client, game):
    """Emrakul: "This spell costs {1} less to cast for each card type among
    cards in your graveyard" - a creature and a sorcery take two off."""
    add_card(client, game, "Emrakul, the Promised End", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "alice", "graveyard")
    add_card(client, game, "Mind Rot", "alice", "graveyard")
    emrakul = find(state(client, game)["hands"]["alice"], "Emrakul, the Promised End")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), emrakul)
    assert row["cost"] == "{11}", row
    assert row["printed_cost"] == "{13}", row


def test_an_unmodified_row_carries_no_printed_cost(client, game):
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    bears = find(state(client, game)["hands"]["alice"], "Grizzly Bears")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), bears)
    assert row["cost"] == "{1}{G}"
    assert "printed_cost" not in row
    assert "x_costs" not in row


def dry_cast_cost(client, game, card: str, **extra) -> str:
    """What `Casting.total_cost` charges, read through the cast route's dry
    run - the engine's own answer the row has to agree with."""
    answer = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": card, "dry_run": True, **extra},
    ).json()
    assert "cost" in answer, answer
    return answer["cost"]


def test_an_x_spell_under_a_flat_increase_carries_the_increase_for_every_x(client, game):
    """CR 601.2f on an X spell: X is announced first (CR 107.3a), then Sphere
    of Resistance's {1} is added - Meltdown is {X}{1}{R} whatever X is."""
    for _ in range(4):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Meltdown", "alice", "hand")
    add_card(client, game, "Sphere of Resistance", "bob", "battlefield")
    meltdown = find(state(client, game)["hands"]["alice"], "Meltdown")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), meltdown)
    assert row["cost"] == "{X}{1}{R}", row
    assert row["mana_cost"] == "{X}{1}{R}", row
    assert row["printed_cost"] == "{X}{R}", row
    assert row["x_range"] == [0, None], row
    assert "x_costs" not in row, row
    for x_value, charged in ((0, "{1}{R}"), (2, "{3}{R}")):
        assert dry_cast_cost(client, game, meltdown, x_value=x_value) == charged


def test_a_repriced_x_row_is_judged_not_forced_unknown(client, game):
    """The row's affordability used to be forced `unknown` for every X row a
    cost modifier repriced (`with_charged_cost`), even though the row is
    priced at X=0 like any other X row once the engine judges X spells that
    way (`legal_actions.py` 2e3fedc). Walking Ballista under Sphere of
    Resistance is charged {1} at X=0 (the flat increase alone, X folded to
    nothing), and a real Mountain can pay it - a real `yes`, not a blanket
    `unknown`."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Walking Ballista", "alice", "hand")
    add_card(client, game, "Sphere of Resistance", "bob", "battlefield")
    ballista = find(state(client, game)["hands"]["alice"], "Walking Ballista")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), ballista)
    assert row["affordable"] == "yes", row
    assert row["unaffordable_reason"] is None, row


def test_a_repriced_x_row_with_no_source_to_pay_it_is_no_not_unknown(client, game):
    """The same row with nothing on the battlefield to pay the {1} Sphere of
    Resistance adds: a real `no`, still never the blanket `unknown` the row
    used to be forced to for carrying an `x_range` at all."""
    add_card(client, game, "Walking Ballista", "alice", "hand")
    add_card(client, game, "Sphere of Resistance", "bob", "battlefield")
    ballista = find(state(client, game)["hands"]["alice"], "Walking Ballista")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), ballista)
    assert row["affordable"] == "no", row


def test_an_x_spell_under_a_floor_lists_each_x_the_floor_raises(client, game):
    """Trinisphere reads the cost X has already left (CR 601.2f): Walking
    Ballista is {3} at X=0 and X=1, and {X}{X} from X=2 on."""
    for _ in range(4):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Walking Ballista", "alice", "hand")
    add_card(client, game, "Trinisphere", "bob", "battlefield")
    ballista = find(state(client, game)["hands"]["alice"], "Walking Ballista")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), ballista)
    assert row["cost"] == "{X}{X}", row
    assert row["printed_cost"] == "{X}{X}", row
    assert row["x_costs"] == [
        {"x": 0, "cost": "{3}", "mana_cost": "{3}"},
        {"x": 1, "cost": "{3}", "mana_cost": "{3}"},
    ], row
    for entry in row["x_costs"]:
        assert dry_cast_cost(client, game, ballista, x_value=entry["x"]) == entry["cost"]
    assert dry_cast_cost(client, game, ballista, x_value=2) == "{4}"


def test_an_x_spell_whose_reduction_eats_into_x_says_so(client, game):
    """Ruby Medallion's {1} off Meltdown comes out of the generic mana X adds
    (CR 118.7a): {R} at X=0 and X=1, {1}{R} at X=2. No single mana cost with
    an X in it says that, so the row states the reduction in words and
    lists the X the words do not cover."""
    for _ in range(3):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Meltdown", "alice", "hand")
    add_card(client, game, "Ruby Medallion", "alice", "battlefield")
    meltdown = find(state(client, game)["hands"]["alice"], "Meltdown")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), meltdown)
    assert row["cost"] == "{X}{R} less {1}", row
    assert row["mana_cost"] is None, row
    assert row["printed_cost"] == "{X}{R}", row
    assert row["x_costs"] == [{"x": 0, "cost": "{R}", "mana_cost": "{R}"}], row
    assert row["affordable"] == "unknown", row
    for x_value, charged in ((0, "{R}"), (1, "{R}"), (2, "{1}{R}"), (3, "{2}{R}")):
        assert dry_cast_cost(client, game, meltdown, x_value=x_value) == charged


def test_a_modal_x_spell_is_repriced_too(client, game):
    """Kozilek's Command has an X and modes, so the engine's row carries no
    `x_range` - its X is still in the cost Sphere of Resistance taxes."""
    add_card(client, game, "Wastes", "alice", "battlefield")
    add_card(client, game, "Kozilek's Command", "alice", "hand")
    add_card(client, game, "Sphere of Resistance", "bob", "battlefield")
    command = find(state(client, game)["hands"]["alice"], "Kozilek's Command")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), command)
    assert row["cost"] == "{X}{1}{C}{C}", row
    assert row["printed_cost"] == "{X}{C}{C}", row


def test_an_alternative_cost_with_no_mana_is_taxed(client, game):
    """Solitude's evoke cost is a card, not mana (CR 118.9), and Sphere of
    Resistance adds {1} to it all the same (CR 118.9d, CR 601.2f)."""
    add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Solitude", "alice", "hand")
    add_card(client, game, "Savannah Lions", "alice", "hand")
    add_card(client, game, "Sphere of Resistance", "bob", "battlefield")
    hand = state(client, game)["hands"]["alice"]
    solitude = find(hand, "Solitude")["id"]
    lions = find(hand, "Savannah Lions")["id"]

    (evoke,) = [
        row
        for row in rows_for(legal_actions(client, game, "alice"), solitude)
        if row["keyword"] == "Evoke"
    ]
    assert evoke["mana_cost"] == "{1}", evoke
    assert evoke["cost"].startswith("{1}, "), evoke
    assert evoke["printed_cost"] == evoke["cost"].removeprefix("{1}, "), evoke
    # The dry run's payment names the mana it opened; the pitched card is
    # paid beside it, and the row's `cost` carries both.
    charged = dry_cast_cost(client, game, solitude, keyword="Evoke", choosing=[lions])
    assert charged == evoke["mana_cost"], (charged, evoke)


def test_a_row_no_cast_option_prints_is_repriced_and_not_judged(client, game):
    """The fallback: a cast row whose cost strings match none of the card's
    cast options still shows the charged mana, and says its payability was
    not judged rather than quoting the printed cost."""
    from magic_cards_model.models import Action
    from magic_cards_model.models.action import ActionKind, Affordability
    from magic_cards_model.models.zone import Zone

    from magic_cards_model_api.action_rows import _Rows
    from magic_cards_model_api.store import store

    add_card(client, game, "Grizzly Bears", "alice", "hand")
    add_card(client, game, "Sphere of Resistance", "bob", "battlefield")
    engine_game = store.get(game)
    (bears,) = engine_game.player("alice").hand
    row = Action(
        kind=ActionKind.CAST,
        player="alice",
        rule="CR 601.2",
        card_id=bears.id,
        from_zone=Zone.HAND,
        cost="{1}{G}, sacrifice a creature",
        mana_cost="{1}{G}",
        affordable=Affordability.YES,
    )

    repriced, extra = _Rows(engine_game, "alice").with_charged_cost(row)
    assert repriced.mana_cost == "{2}{G}", repriced
    assert repriced.cost == "{2}{G}, sacrifice a creature", repriced
    assert repriced.affordable is Affordability.UNKNOWN, repriced
    assert "601.2f" in (repriced.unaffordable_reason or ""), repriced
    assert extra == {"printed_cost": "{1}{G}, sacrifice a creature"}, extra


# -- B4-02 on activations: the engine charges these rows' own cost ------------


def test_an_off_zone_row_shows_its_own_reduction(client, game):
    """Takenuma's Channel "costs {1} less to activate for each legendary
    creature you control" (CR 601.2f via CR 602.2b): {2}{B} with Emrakul out,
    and Sphere of Resistance, which taxes spells only, adds nothing."""
    for _ in range(3):
        add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Emrakul, the Promised End", "alice", "battlefield")
    add_card(client, game, "Takenuma, Abandoned Mire", "alice", "hand")
    add_card(client, game, "Sphere of Resistance", "bob", "battlefield")
    takenuma = find(state(client, game)["hands"]["alice"], "Takenuma, Abandoned Mire")["id"]

    (row,) = [
        row
        for row in rows_for(legal_actions(client, game, "alice"), takenuma)
        if row["kind"] == "activate_off_zone"
    ]
    assert row["mana_cost"] == "{2}{B}", row
    dry = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": takenuma,
            "ability_index": row["ability_index"],
            "dry_run": True,
        },
    ).json()
    # The dry run's payment names the mana it opened; the discard is paid
    # beside it, and the row's `cost` carries both.
    assert dry["cost"] == row["mana_cost"], (dry, row)
    assert row["cost"] == "{2}{B}, Discard this card", row


def test_a_battlefield_activation_row_is_what_the_activation_charges(client, game):
    for _ in range(4):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Walking Ballista", "alice", "battlefield")
    add_card(client, game, "Sphere of Resistance", "bob", "battlefield")
    ballista = find(state(client, game)["battlefield"], "Walking Ballista")["id"]

    (row,) = [
        row
        for row in rows_for(legal_actions(client, game, "alice"), ballista)
        if row["cost"] and row["mana_cost"]
    ]
    assert row["mana_cost"] == "{4}", row
    dry = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": ballista,
            "ability_index": row["ability_index"],
            "dry_run": True,
        },
    ).json()
    assert dry["cost"] == row["cost"], (dry, row)


def test_an_equip_row_is_what_the_equip_charges(client, game):
    add_card(client, game, "Blade of the Bloodchief", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Sphere of Resistance", "bob", "battlefield")
    board = state(client, game)
    blade = find(board["battlefield"], "Blade of the Bloodchief")["id"]
    bears = find(board["battlefield"], "Grizzly Bears")["id"]

    (row,) = [
        row
        for row in rows_for(legal_actions(client, game, "alice"), blade)
        if row["kind"] == "equip"
    ]
    assert row["cost"] == "{1}", row
    dry = client.post(
        f"/api/games/{game}/actions/equip",
        json={"player": "alice", "card_id": blade, "target": bears, "dry_run": True},
    ).json()
    assert dry["cost"] == row["cost"], (dry, row)


# -- B3-05: a mode the engine cannot read --------------------------------------


def charm_board(client, game) -> dict:
    add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Thraben Charm", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Goblin Bombardment", "bob", "battlefield")
    board = state(client, game)
    return {
        "charm": find(board["hands"]["alice"], "Thraben Charm")["id"],
        "plains": [card["id"] for card in board["battlefield"] if card["name"] == "Plains"],
        "bears": find(board["battlefield"], "Grizzly Bears")["id"],
        "bombardment": find(board["battlefield"], "Goblin Bombardment")["id"],
    }


def test_the_row_names_each_mode_the_engine_cannot_read(client, game):
    rig = charm_board(client, game)

    (row,) = rows_for(legal_actions(client, game, "alice"), rig["charm"])
    unreadable = {entry["mode_number"]: entry for entry in row["unreadable_modes"]}
    # The Reality Fracture number sweep taught the engine to read mode 1's
    # dynamic damage amount ("equal to twice the number of creatures you
    # control") - mode 3's "any number of target players' graveyards" is the
    # one this pool still cannot read, so it is the case this test now proves.
    assert 1 not in unreadable, row
    assert 3 in unreadable, row
    assert unreadable[3]["text"].startswith("Exile any number of target players"), unreadable
    assert "CR 700.2a" in unreadable[3]["notice"], unreadable


def test_casting_an_unreadable_mode_is_refused_and_keeps_the_card(client, game):
    rig = charm_board(client, game)

    answer = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": rig["charm"],
            "modes": [3],
            "targets": ["bob"],
            "mana_sources": rig["plains"],
        },
    ).json()
    assert answer["allowed"] is False, answer
    assert "mode 3" in answer["reason"], answer
    after = state(client, game)
    assert find(after["hands"]["alice"], "Thraben Charm")["id"] == rig["charm"]
    assert not any(card["tapped"] for card in after["battlefield"] if card["name"] == "Plains")


def test_a_readable_mode_still_casts(client, game):
    rig = charm_board(client, game)

    answer = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": rig["charm"],
            "modes": [2],
            "targets": [rig["bombardment"]],
            "mana_sources": rig["plains"],
        },
    ).json()
    assert answer["allowed"] is True, answer


def test_a_modal_row_with_every_mode_readable_carries_no_warning(client, game):
    """Kozilek's Command reads all four of its modes, so its cast row lists
    them and warns about none - the warning is the unreadable mode's, never
    every modal row's."""
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Kozilek's Command", "alice", "hand")
    command = find(state(client, game)["hands"]["alice"], "Kozilek's Command")["id"]

    (row,) = rows_for(legal_actions(client, game, "alice"), command)
    assert row["kind"] == "cast", row
    assert len(row["modes"]) == 4, row
    assert not row.get("unreadable_modes"), row
