"""`hypothetical_facts.build_hypothetical_facts`: O4/O7's engine-computed
facts, proven against a real `Game` and real Scryfall printings - no mocks,
the same rule this repo follows for the whole `machines.hypothetical` wiring.

Every scenario here mirrors one named in the openings-plays research spec.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import add_card
from magic_cards_model.machines.hypothetical import payability_matrix
from magic_cards_model.models import CardState

from magic_cards_model_api.playscripts.hypothetical_facts import build_hypothetical_facts
from magic_cards_model_api.store import store


def _live(game_id: str):
    live = store.get(game_id)
    assert live is not None
    return live


def _by_name(cards: list[CardState], name: str) -> CardState:
    return next(card for card in cards if card.card.name == name or card.name == name)


def _facts(game_id: str, player: str = "alice") -> dict[str, Any]:
    return build_hypothetical_facts(_live(game_id), player)


def _card_id(live, player: str, zone_cards: list[CardState], name: str) -> str:
    return str(_by_name(zone_cards, name).id)


# -- O4: enters ------------------------------------------------------------------


def test_a_basic_land_always_enters_untapped(client, game):
    add_card(client, game, "Mountain", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    facts = _facts(game)
    mountain_id = str(_by_name(alice.hand, "Mountain").id)
    assert facts["cards"][mountain_id]["enters"] == "untapped"


def test_a_fastland_reads_two_or_fewer_other_lands(client, game):
    add_card(client, game, "Spirebluff Canal", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    canal_id = str(_by_name(alice.hand, "Spirebluff Canal").id)

    # 0 other lands
    assert _facts(game)["cards"][canal_id]["enters"] == "untapped"

    # 2 other lands
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "battlefield")
    assert _facts(game)["cards"][canal_id]["enters"] == "untapped"

    # 3 other lands
    add_card(client, game, "Swamp", "alice", "battlefield")
    assert _facts(game)["cards"][canal_id]["enters"] == "tapped"


def test_arena_of_glory_reads_whether_a_mountain_is_controlled(client, game):
    add_card(client, game, "Arena of Glory", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    arena_id = str(_by_name(alice.hand, "Arena of Glory").id)

    assert _facts(game)["cards"][arena_id]["enters"] == "tapped"

    add_card(client, game, "Mountain", "alice", "battlefield")
    assert _facts(game)["cards"][arena_id]["enters"] == "untapped"


def test_shifting_woodland_reads_yavimayas_continuous_forest_effect(client, game):
    add_card(client, game, "Shifting Woodland", "alice", "hand")
    add_card(client, game, "Island", "alice", "battlefield")
    live = _live(game)
    alice = live.player("alice")
    woodland_id = str(_by_name(alice.hand, "Shifting Woodland").id)

    # No Forest on the battlefield (Island alone) - enters tapped.
    assert _facts(game)["cards"][woodland_id]["enters"] == "tapped"

    # Yavimaya, Cradle of Growth makes every land a Forest too (CR layers),
    # so the Island now satisfies "you control a Forest".
    add_card(client, game, "Yavimaya, Cradle of Growth", "alice", "battlefield")
    assert _facts(game)["cards"][woodland_id]["enters"] == "untapped"


def test_a_surveil_land_that_always_enters_tapped_reads_tapped(client, game):
    add_card(client, game, "Undercity Sewers", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    sewers_id = str(_by_name(alice.hand, "Undercity Sewers").id)
    assert _facts(game)["cards"][sewers_id]["enters"] == "tapped"


def test_a_shockland_is_untapped_if_paid_at_healthy_life(client, game):
    add_card(client, game, "Sacred Foundry", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    assert alice.life == 20
    foundry_id = str(_by_name(alice.hand, "Sacred Foundry").id)
    assert _facts(game)["cards"][foundry_id]["enters"] == "untapped_if_paid"


def test_a_shockland_reads_tapped_with_a_notice_when_the_payment_is_lethal(client, game):
    add_card(client, game, "Sacred Foundry", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    alice.life = 2
    foundry_id = str(_by_name(alice.hand, "Sacred Foundry").id)
    facts = _facts(game)
    assert facts["cards"][foundry_id]["enters"] == "tapped"
    assert any("lethal" in notice.casefold() for notice in facts["notices"])


# -- O7: unlocks, unlocks_by_search -----------------------------------------------


def test_island_unlocks_preordain(client, game):
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    island_id = str(_by_name(alice.hand, "Island").id)
    assert _facts(game)["cards"][island_id]["unlocks"] is True


def test_with_a_swamp_up_island_unlocks_psychic_frog_and_so_does_watery_grave(client, game):
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    island_id = str(_by_name(alice.hand, "Island").id)
    assert _facts(game)["cards"][island_id]["unlocks"] is True

    # Same board, Watery Grave in hand instead of Island.
    live2_id = game
    add_card(client, live2_id, "Watery Grave", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    grave_id = str(_by_name(alice.hand, "Watery Grave").id)
    assert _facts(game)["cards"][grave_id]["unlocks"] is True


def test_eldrazi_temple_unlocks_glaring_fleshraker_but_not_ensnaring_bridge(client, game):
    """Eldrazi Temple's `{T}: Add {C}{C}` is restricted to colorless Eldrazi
    spells and abilities. Glaring Fleshraker ({2}{C}, a colorless Eldrazi
    Drone) can spend it; Ensnaring Bridge ({3}, a plain artifact) cannot -
    with one other untapped Mountain, Temple's restricted 2 plus the
    Mountain's 1 pays Fleshraker's 3, but only Temple's own unrestricted 1
    plus the Mountain's 1 (2 total) is ever usable toward Ensnaring Bridge,
    one short of its 3."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Eldrazi Temple", "alice", "hand")
    add_card(client, game, "Glaring Fleshraker", "alice", "hand")
    add_card(client, game, "Ensnaring Bridge", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    temple = _by_name(alice.hand, "Eldrazi Temple")
    fleshraker = _by_name(alice.hand, "Glaring Fleshraker")
    bridge = _by_name(alice.hand, "Ensnaring Bridge")

    facts = _facts(game)
    assert facts["cards"][str(temple.id)]["unlocks"] is True

    matrix = payability_matrix(live, "alice", [fleshraker, bridge], [temple])
    column = next(entry for entry in matrix.lands if entry.land_id == temple.id)
    fleshraker_answer = next(row for row in column.cards if row.card_id == fleshraker.id)
    bridge_answer = next(row for row in column.cards if row.card_id == bridge.id)
    assert fleshraker_answer.payable is True
    assert bridge_answer.payable is False, "Temple's restricted mana must not pay for Bridge"


def test_eldrazi_temple_never_unlocks_basking_broodscale_for_lack_of_green(client, game):
    """Basking Broodscale ({1}{G}) is itself a colorless (devoid) Eldrazi, so
    Temple's `{C}{C}` restriction would apply to it - but Temple makes only
    colorless mana, and Broodscale still needs a green source regardless."""
    add_card(client, game, "Eldrazi Temple", "alice", "hand")
    add_card(client, game, "Basking Broodscale", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    temple_id = str(_by_name(alice.hand, "Eldrazi Temple").id)
    assert _facts(game)["cards"][temple_id]["unlocks"] is False


def test_eldrazi_temple_never_unlocks_an_x_spell_payable_only_at_x_zero(client, game):
    """Item 2: eldrazi-ramp T1, Temple + Mosswort Bridge in hand, Kozilek's
    Command ({X}{C}{C}, a colorless Eldrazi Kindred Instant) + Fanatic of
    Rhonas. With no other land, Temple's restricted `{C}{C}` alone pays
    Command only at X=0 - a cast nobody wants (0 tokens, scry 0, exile
    nothing). An X-cost card must never count as unlocked on the strength of
    its X=0 announcement alone (CR 107.1b names X=0 the cheapest, not the
    only, legal one)."""
    add_card(client, game, "Eldrazi Temple", "alice", "hand")
    add_card(client, game, "Mosswort Bridge", "alice", "hand")
    add_card(client, game, "Kozilek's Command", "alice", "hand")
    add_card(client, game, "Fanatic of Rhonas", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    temple = _by_name(alice.hand, "Eldrazi Temple")
    command = _by_name(alice.hand, "Kozilek's Command")

    matrix = payability_matrix(live, "alice", [command], [temple])
    column = next(entry for entry in matrix.lands if entry.land_id == temple.id)
    command_answer = next(row for row in column.cards if row.card_id == command.id)
    assert command_answer.payable is True, "X=0 is genuinely payable under Temple alone"

    facts = _facts(game)
    assert facts["cards"][str(temple.id)]["unlocks"] is False


def test_fiery_islet_unlocks_preordain_with_its_own_life_paid_noted(client, game):
    """Item 4: Fiery Islet's only mana ability is a life-paid one ("{T}, Pay
    1 life: Add {U} or {R}."), so unlocking Preordain ({U}) with it costs 1
    life - `unlocks_life_paid` surfaces that so a play can see the cost
    rather than treating every unlock as free."""
    add_card(client, game, "Fiery Islet", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    islet_id = str(_by_name(alice.hand, "Fiery Islet").id)
    facts = _facts(game)
    assert facts["cards"][islet_id]["unlocks"] is True
    assert facts["cards"][islet_id]["unlocks_life_paid"] == 1


def test_a_land_with_no_life_paid_unlock_reads_zero(client, game):
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    island_id = str(_by_name(alice.hand, "Island").id)
    facts = _facts(game)
    assert facts["cards"][island_id]["unlocks"] is True
    assert facts["cards"][island_id]["unlocks_life_paid"] == 0


def test_polluted_delta_unlocks_by_search_via_a_watery_grave_in_the_library(client, game):
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Polluted Delta", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Watery Grave", "alice", "library")
    live = _live(game)
    alice = live.player("alice")
    delta_id = str(_by_name(alice.hand, "Polluted Delta").id)
    facts = _facts(game)
    assert facts["cards"][delta_id]["unlocks_by_search"] is True


def test_polluted_delta_fails_closed_when_the_fetch_cost_and_the_shock_are_lethal_together(
    client, game
):
    """D3: Polluted Delta's own fetch cost is "Pay 1 life"; Watery Grave's
    optional payment is "pay 2 life" (CR 614.1c). Neither alone is lethal
    at 3 life read in isolation - the fetch leaves 2, and the shock's own
    `lethal_if_paid` check (against life *now*, before the fetch's own cost
    is paid) reads 3 - 2 = 1, not lethal either - but going through with
    both in order (CR 601.2h, CR 119.4) leaves 0, a state-based loss (CR
    704.5a). unlocks_by_search must fail closed rather than claim this is
    safe."""
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Polluted Delta", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Watery Grave", "alice", "library")
    live = _live(game)
    alice = live.player("alice")
    alice.life = 3
    delta_id = str(_by_name(alice.hand, "Polluted Delta").id)

    assert _facts(game)["cards"][delta_id]["unlocks_by_search"] is False


def test_polluted_delta_unlocks_by_search_once_life_covers_both_costs(client, game):
    """The same board, one life higher: 4 - (1 fetch + 2 shock) = 1, alive,
    so it is safe and unlocks_by_search reads True again."""
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Polluted Delta", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Watery Grave", "alice", "library")
    live = _live(game)
    alice = live.player("alice")
    alice.life = 4
    delta_id = str(_by_name(alice.hand, "Polluted Delta").id)

    assert _facts(game)["cards"][delta_id]["unlocks_by_search"] is True


def test_without_the_swamp_island_does_not_unlock_psychic_frog(client, game):
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Island", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    island_id = str(_by_name(alice.hand, "Island").id)
    assert _facts(game)["cards"][island_id]["unlocks"] is False


def test_a_fetch_with_no_unlocking_library_land_does_not_unlock_by_search(client, game):
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Polluted Delta", "alice", "hand")
    add_card(client, game, "Watery Grave", "alice", "library")  # no Swamp up: does not unlock
    live = _live(game)
    alice = live.player("alice")
    delta_id = str(_by_name(alice.hand, "Polluted Delta").id)
    assert _facts(game)["cards"][delta_id]["unlocks_by_search"] is False


def test_a_tapped_surveil_land_never_unlocks(client, game):
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Undercity Sewers", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    sewers_id = str(_by_name(alice.hand, "Undercity Sewers").id)
    facts = _facts(game)
    assert facts["cards"][sewers_id]["enters"] == "tapped"
    assert facts["cards"][sewers_id]["unlocks"] is False


def test_dryad_arbor_never_unlocks(client, game):
    """A land Creature is summoning sick the turn it enters (CR 302.6), so
    its own `{T}: Add {G}` cannot be activated yet even though it enters
    untapped - it can never unlock anything on the turn it is played."""
    add_card(client, game, "Dryad Arbor", "alice", "hand")
    add_card(client, game, "Basking Broodscale", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    arbor_id = str(_by_name(alice.hand, "Dryad Arbor").id)
    facts = _facts(game)
    assert facts["cards"][arbor_id]["enters"] == "untapped"
    assert facts["cards"][arbor_id]["unlocks"] is False


# -- unlocks/unlocks_by_search/unlocks_by_basic_search need a legal target too ----


def test_polluted_delta_does_not_unlock_by_basic_search_for_a_pushless_push(client, game):
    """The defect: dimir T1, Polluted Delta + Undercity Sewers + Fatal Push +
    Psychic Frog in hand, empty opposing board, a library Swamp. Fetching the
    Swamp pays Fatal Push's {B}, but Fatal Push ("target creature") has no
    legal target with no creature anywhere - `unlocks`/`unlocks_by_search`/
    `unlocks_by_basic_search` must read False, not count a merely payable,
    untargetable cast as an unlock."""
    add_card(client, game, "Polluted Delta", "alice", "hand")
    add_card(client, game, "Undercity Sewers", "alice", "hand")
    add_card(client, game, "Fatal Push", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Swamp", "alice", "library")
    live = _live(game)
    alice = live.player("alice")
    delta_id = str(_by_name(alice.hand, "Polluted Delta").id)

    facts = _facts(game)
    assert facts["cards"][delta_id]["unlocks_by_search"] is False
    assert facts["cards"][delta_id]["unlocks_by_basic_search"] is False


def test_godless_shrine_does_not_unlock_ephemerate_with_no_creature_to_target(client, game):
    """The defect's second board: esper T1, Godless Shrine + Shadowy
    Backstreet + Ephemerate + Emperor of Bones in hand, no creatures
    anywhere. Shrine's own W pays Ephemerate's {W}, but Ephemerate ("target
    creature you control") has no legal target with no creature controlled -
    `unlocks` must read False."""
    add_card(client, game, "Godless Shrine", "alice", "hand")
    add_card(client, game, "Shadowy Backstreet", "alice", "hand")
    add_card(client, game, "Ephemerate", "alice", "hand")
    add_card(client, game, "Emperor of Bones", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    shrine_id = str(_by_name(alice.hand, "Godless Shrine").id)

    facts = _facts(game)
    assert facts["cards"][shrine_id]["unlocks"] is False


def test_fatal_push_still_unlocks_by_search_with_an_opposing_creature_present(client, game):
    """The same dimir board as the defect above, with one opposing creature
    added: Fatal Push now has a legal target, so fetching the Swamp is a
    real unlock again - the target-legality check must not fail closed on a
    card that genuinely has a target available."""
    add_card(client, game, "Polluted Delta", "alice", "hand")
    add_card(client, game, "Undercity Sewers", "alice", "hand")
    add_card(client, game, "Fatal Push", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Swamp", "alice", "library")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    live = _live(game)
    alice = live.player("alice")
    delta_id = str(_by_name(alice.hand, "Polluted Delta").id)

    facts = _facts(game)
    assert facts["cards"][delta_id]["unlocks_by_search"] is True
    assert facts["cards"][delta_id]["unlocks_by_basic_search"] is True


def test_a_targetless_card_still_unlocks(client, game):
    """A card with no target at all (Psychic Frog casts with no `target`
    clause) must still unlock - the target-legality check must pass an
    option automatically when it has no mandatory target slot, not only when
    one is present and filled."""
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    island_id = str(_by_name(alice.hand, "Island").id)
    assert _facts(game)["cards"][island_id]["unlocks"] is True


# -- castable_now (item 3, affordable ignoring targets) -----------------------


def test_fatal_push_is_not_castable_now_with_no_creature_anywhere(client, game):
    """Item 3: dimir T1, Undercity Sewers + Polluted Delta + Fatal Push +
    Psychic Frog in hand, empty board both sides. Fatal Push ({B}, "target
    creature") has no legal target at all with no creature anywhere, so
    `legal_actions` never offers a cast row for it regardless of mana -
    `castable_now` reads that (`Payability.options` for the real board, no
    assumed land), the same "now" answer `unlocks` already computes for
    every hand card."""
    add_card(client, game, "Undercity Sewers", "alice", "hand")
    add_card(client, game, "Polluted Delta", "alice", "hand")
    add_card(client, game, "Fatal Push", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    push_id = str(_by_name(alice.hand, "Fatal Push").id)
    assert _facts(game)["cards"][push_id]["castable_now"] is False


def test_fatal_push_is_castable_now_with_a_creature_on_the_board(client, game):
    add_card(client, game, "Swamp", "alice", "hand")
    add_card(client, game, "Fatal Push", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    live = _live(game)
    alice = live.player("alice")
    push_id = str(_by_name(alice.hand, "Fatal Push").id)
    assert _facts(game)["cards"][push_id]["castable_now"] is True


# -- visibility, and no-op scoping -------------------------------------------------


def test_the_response_never_names_a_library_card_or_depends_on_library_order(client, game):
    """D1: `cards` used to key an entry for every library land too (by its
    own card id), and `notices` followed the same matrix order - library
    order for the library lands - so the response leaked a hidden zone's
    order and identities (CR 401.2) exactly where `serialize._contents_
    view` is deliberately built not to. Only the per-hand-card results
    (`unlocks_by_search` included) ever reach the wire; library facts are
    used to compute those and never returned themselves. Shuffling the
    library must not change the response's bytes at all, and no library
    card's id may appear in it."""
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Polluted Delta", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Watery Grave", "alice", "library")
    add_card(client, game, "Sacred Foundry", "alice", "library")
    add_card(client, game, "Mountain", "alice", "library")
    live = _live(game)
    alice = live.player("alice")
    library_ids = {str(card.id) for card in alice.library}

    before = json.dumps(_facts(game))
    for card_id in library_ids:
        assert card_id not in before

    live.shuffle_library("alice")
    after = json.dumps(_facts(game))

    assert before == after


def test_facts_are_scoped_to_the_players_own_hand_and_library(client, game):
    add_card(client, game, "Mountain", "bob", "hand")
    add_card(client, game, "Mountain", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    bob = live.player("bob")
    facts = _facts(game, "alice")
    assert str(_by_name(alice.hand, "Mountain").id) in facts["cards"]
    assert str(_by_name(bob.hand, "Mountain").id) not in facts["cards"]


def test_no_hand_or_no_land_candidates_returns_no_facts(client, game):
    live = _live(game)
    assert build_hypothetical_facts(live, "alice") == {"cards": {}, "notices": []}
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    assert build_hypothetical_facts(_live(game), "alice") == {"cards": {}, "notices": []}


# -- unlocks_by_search on the viewer's own battlefield fetches --------------------


def test_a_battlefield_fetch_gets_its_own_unlocks_by_search_too(client, game):
    """Some decks choose AMONG SEVERAL already-in-play, untapped fetches by
    `unlocks_by_search` (a battlefield `source.select`, not a hand `card.
    select`) - Polluted Delta's own search reaches the library Island that
    unlocks Psychic Frog (Swamp already up, same combo `test_with_a_swamp_
    up_island_unlocks_psychic_frog_and_so_does_watery_grave` proves), while
    Bloodstained Mire's "Swamp or Mountain" clause cannot reach it at all.
    `unlocks_by_search` is public information the instant it is asked about
    a permanent already on the battlefield (CR 401.3), so this needs no
    `candidate_ids` gate - unconditional, exactly like a hand card's own."""
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Polluted Delta", "alice", "battlefield")
    add_card(client, game, "Bloodstained Mire", "alice", "battlefield")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Island", "alice", "library")
    live = _live(game)
    delta = next(c for c in live.permanents_controlled_by("alice") if c.name == "Polluted Delta")
    mire = next(c for c in live.permanents_controlled_by("alice") if c.name == "Bloodstained Mire")

    facts = _facts(game)

    assert facts["cards"][str(delta.id)]["unlocks_by_search"] is True, (
        "Polluted Delta's own search reaches the library Island that unlocks Psychic Frog"
    )
    assert facts["cards"][str(mire.id)]["unlocks_by_search"] is False, (
        "Bloodstained Mire's Swamp-or-Mountain clause cannot reach any unlocking library land"
    )


def test_a_battlefield_fetch_never_gets_a_library_style_enters_or_unlocks_entry(client, game):
    """Confirms the scope precisely: a battlefield permanent is never a land
    `payability_matrix` can be asked to "enter" (it already has), so widening
    `unlocks_by_search` to the battlefield adds exactly that one field to a
    battlefield card's entry, never `enters`/`unlocks` alongside it."""
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Polluted Delta", "alice", "battlefield")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Island", "alice", "library")
    live = _live(game)
    delta = next(c for c in live.permanents_controlled_by("alice") if c.name == "Polluted Delta")

    facts = _facts(game)

    assert set(facts["cards"][str(delta.id)]) == {"unlocks_by_search", "unlocks_by_basic_search"}


# -- unlocks_by_basic_search: narrowed to a basic land the search could find ------


def test_a_shock_only_unlock_reads_unlocks_by_search_but_not_by_basic(client, game):
    """Flooded Strand ("a Plains or Island card") can only reach Watery Grave
    ("Land - Island Swamp", so a legal find by its Island half) among these
    library cards, and Watery Grave is what unlocks Thoughtseize's B - not a
    basic, so a play that only wants a free basic find must read this False
    even though `unlocks_by_search` itself is True. Island is in the library
    too, reachable and a basic, but produces only U and unlocks nothing."""
    add_card(client, game, "Flooded Strand", "alice", "hand")
    add_card(client, game, "Thoughtseize", "alice", "hand")
    add_card(client, game, "Watery Grave", "alice", "library")
    add_card(client, game, "Island", "alice", "library")
    live = _live(game)
    alice = live.player("alice")
    strand_id = str(_by_name(alice.hand, "Flooded Strand").id)

    facts = _facts(game)

    assert facts["cards"][strand_id]["unlocks_by_search"] is True
    assert facts["cards"][strand_id]["unlocks_by_basic_search"] is False


def test_a_battlefield_fetch_reads_unlocks_by_basic_search_too(client, game):
    """The same shape as the hand-card test above, with the fetch already on
    the battlefield instead - `unlocks_by_basic_search` is unconditional for
    a battlefield permanent exactly as `unlocks_by_search` already is."""
    add_card(client, game, "Flooded Strand", "alice", "battlefield")
    add_card(client, game, "Thoughtseize", "alice", "hand")
    add_card(client, game, "Watery Grave", "alice", "library")
    add_card(client, game, "Island", "alice", "library")
    live = _live(game)
    strand = next(c for c in live.permanents_controlled_by("alice") if c.name == "Flooded Strand")

    facts = _facts(game)

    assert facts["cards"][str(strand.id)]["unlocks_by_search"] is True
    assert facts["cards"][str(strand.id)]["unlocks_by_basic_search"] is False


# -- candidate_ids: a fetch's own choose_cards pick sees its library candidates ---


def test_candidate_ids_writes_enters_and_unlocks_for_a_named_library_land(client, game):
    """The defect: `open-fetch-unlocks`-style plays pick a library land by
    `unlocks: true`, and before `candidate_ids` existed a library land never
    carried that fact at all, so the pick could never match one. Naming the
    land's id in `candidate_ids` is what makes its `enters`/`unlocks` show up,
    computed the same way a hand land's already is."""
    add_card(client, game, "Ragavan, Nimble Pilferer", "alice", "hand")
    add_card(client, game, "Mountain", "alice", "library")
    add_card(client, game, "Sacred Foundry", "alice", "library")
    live = _live(game)
    alice = live.player("alice")
    mountain = _by_name(alice.library, "Mountain")
    foundry = _by_name(alice.library, "Sacred Foundry")

    facts = build_hypothetical_facts(live, "alice", candidate_ids=frozenset({str(mountain.id)}))

    assert facts["cards"][str(mountain.id)]["enters"] == "untapped"
    assert facts["cards"][str(mountain.id)]["unlocks"] is True, (
        "Mountain pays Ragavan's R, nothing else in this board can"
    )
    assert str(foundry.id) not in facts["cards"], (
        "Sacred Foundry was never named in candidate_ids, so it must stay invisible "
        "even though it is a legal land in the same library"
    )


def test_no_candidate_ids_returns_no_library_land_facts_library_order_unchanged(client, game):
    """D1 unchanged: with no `candidate_ids` at all (the default), a library
    land's facts never reach the response, exactly as before this fix."""
    add_card(client, game, "Ragavan, Nimble Pilferer", "alice", "hand")
    add_card(client, game, "Mountain", "alice", "library")
    add_card(client, game, "Sacred Foundry", "alice", "library")
    live = _live(game)
    alice = live.player("alice")
    mountain = _by_name(alice.library, "Mountain")
    foundry = _by_name(alice.library, "Sacred Foundry")

    facts = build_hypothetical_facts(live, "alice")

    assert str(mountain.id) not in facts["cards"]
    assert str(foundry.id) not in facts["cards"]


def test_candidate_ids_naming_a_hand_land_changes_nothing_it_already_had(client, game):
    """A hand land named in `candidate_ids` too (redundant - it already gets
    its own facts unconditionally) still gets exactly the one entry, not a
    different one, and its notices still land in `notices` as before."""
    add_card(client, game, "Sacred Foundry", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    alice.life = 2
    foundry_id = str(_by_name(alice.hand, "Sacred Foundry").id)

    with_candidate = build_hypothetical_facts(live, "alice", candidate_ids=frozenset({foundry_id}))
    without = build_hypothetical_facts(live, "alice")

    assert with_candidate == without
    assert with_candidate["cards"][foundry_id]["enters"] == "tapped"
    assert any("lethal" in notice.casefold() for notice in with_candidate["notices"])


# -- timing on a realistic board --------------------------------------------------


def test_timing_on_a_realistic_board(client, game):
    """Not a strict benchmark - a guard against a per-pair engine call
    sneaking back in. One `payability_matrix` call over a 7-card hand and
    ~19 candidate lands (7 in hand, the rest distinct printings in the
    library) is ~0.3s on the engine's own equivalent test; this settles for
    a looser bound so a slower or loaded machine does not fail on noise."""
    import time

    for name in (
        "Psychic Frog",
        "Lightning Bolt",
        "Preordain",
        "Polluted Delta",
        "Watery Grave",
        "Spirebluff Canal",
        "Basking Broodscale",
    ):
        add_card(client, game, name, "alice", "hand")
    add_card(client, game, "Swamp", "alice", "battlefield")
    for name in (
        "Island",
        "Swamp",
        "Mountain",
        "Forest",
        "Plains",
        "Watery Grave",
        "Sacred Foundry",
        "Arena of Glory",
        "Undercity Sewers",
        "Dryad Arbor",
        "Eldrazi Temple",
        "Spirebluff Canal",
    ):
        add_card(client, game, name, "alice", "library")

    live = _live(game)
    started = time.perf_counter()
    facts = build_hypothetical_facts(live, "alice")
    elapsed = time.perf_counter() - started

    assert facts["cards"]
    assert elapsed < 3.0, f"build_hypothetical_facts took {elapsed:.2f}s"
    print(f"\nbuild_hypothetical_facts: {elapsed * 1000:.1f} ms for a 7-card hand, 19 lands")


# -- T3: payable_any and unlocks_next, the `next_turn` horizon --------------------
#
# The flag is named `next_turn`: `build_hypothetical_facts(game, player,
# candidate_ids=..., next_turn=True)` runs the second `payability_matrix`
# pass with `assuming_untapped=True` and adds `payable_any` (hand cards) and
# `unlocks_next` (the same lands `unlocks` is offered on). Unset, nothing
# about the answer moves.


def _horizon(game_id: str, player: str = "alice", **kwargs: Any) -> dict[str, Any]:
    return build_hypothetical_facts(_live(game_id), player, next_turn=True, **kwargs)


def test_payable_any_is_not_now_for_a_targetable_card_whose_colour_is_unpayable(client, game):
    """The entire point of the fix: `castable_now` is the colour-blind read -
    it only asks whether `legal_actions` offers a row with its mandatory
    targets filled, never whether the mana is there. Fatal Push ({B}) with a
    Mountain up and an opposing Grizzly Bears to kill is `castable_now:
    true` and still not payable, so `payable_any` must read the Swamp in
    hand ("with_a_land"), never "now"."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Swamp", "alice", "hand")
    add_card(client, game, "Fatal Push", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    live = _live(game)
    alice = live.player("alice")
    push_id = str(_by_name(alice.hand, "Fatal Push").id)

    facts = _horizon(game)

    assert facts["cards"][push_id]["castable_now"] is True
    assert facts["cards"][push_id]["payable_any"] == "with_a_land"


def test_payable_any_is_now_when_the_card_is_payable_and_has_a_legal_target(client, game):
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Fatal Push", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    live = _live(game)
    alice = live.player("alice")
    push_id = str(_by_name(alice.hand, "Fatal Push").id)

    assert _horizon(game)["cards"][push_id]["payable_any"] == "now"


def test_payable_any_is_never_now_without_a_legal_target(client, game):
    """The same board as above with no creature anywhere: the {B} is payable
    right now, but every cast row Fatal Push offers has an empty mandatory
    target slot, so `_has_a_payable_targeted_option` refuses "now". Only the
    "now" tier asks that - the later ones are about mana reach alone, and
    the Island's own column says the mana is there - so it reads
    "with_a_land" rather than "now". The claim the vocabulary makes is about
    when the cost could be paid, and only "now" is also a claim that the
    spell could be cast this instant."""
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Fatal Push", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    push_id = str(_by_name(alice.hand, "Fatal Push").id)

    facts = _horizon(game)

    assert facts["cards"][push_id]["castable_now"] is False
    assert facts["cards"][push_id]["payable_any"] == "with_a_land"


def test_payable_any_is_next_turn_when_only_the_untap_step_pays_it(client, game):
    """Preordain ({U}) with the only Island already tapped: unpayable now,
    unpayable with the Mountain in hand, payable the moment the untap step
    has happened - which is exactly what the second `assuming_untapped` pass
    answers."""
    add_card(client, game, "Island", "alice", "battlefield", tapped=True)
    add_card(client, game, "Mountain", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    preordain_id = str(_by_name(alice.hand, "Preordain").id)

    assert _horizon(game)["cards"][preordain_id]["payable_any"] == "next_turn"


def test_payable_any_is_no_when_nothing_in_reach_pays_it(client, game):
    """Psychic Frog ({U}{B}) with an empty battlefield and a lone Mountain in
    hand: no colour, no land drop that helps, and nothing to untap."""
    add_card(client, game, "Mountain", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    frog_id = str(_by_name(alice.hand, "Psychic Frog").id)

    assert _horizon(game)["cards"][frog_id]["payable_any"] == "no"


def test_an_x_cost_hand_card_gets_no_payable_any_entry_at_all(client, game):
    """Item 2's fail-closed rule, the same one `unlocks` already applies via
    `_has_x`: `payability_matrix` only ever judges an X spell at X=0 (CR
    107.1b's cheapest legal announcement), so a "yes" there says nothing
    about the X a play actually wants. Postmortem Lunge ({X}{B/P}) therefore
    carries no `payable_any` key at all - not "no", which would be a claim -
    while the ordinary card beside it in the same hand still gets one."""
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Postmortem Lunge", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    lunge_id = str(_by_name(alice.hand, "Postmortem Lunge").id)
    preordain_id = str(_by_name(alice.hand, "Preordain").id)

    facts = _horizon(game)

    assert "payable_any" not in facts["cards"][lunge_id]
    assert facts["cards"][preordain_id]["payable_any"] == "with_a_land"


def test_unlocks_next_reads_the_board_after_the_untap_step(client, game):
    """Swamp tapped, Island in hand, Psychic Frog ({U}{B}) in hand. Today's
    `unlocks` is False - the Island alone only ever makes {U}, and the Swamp
    is tapped - but once the untap step has happened the Swamp pays the {B}
    and the Island is exactly what turns the Frog on, so `unlocks_next` is
    True."""
    add_card(client, game, "Swamp", "alice", "battlefield", tapped=True)
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    island_id = str(_by_name(alice.hand, "Island").id)

    facts = _horizon(game)

    assert facts["cards"][island_id]["unlocks"] is False
    assert facts["cards"][island_id]["unlocks_next"] is True


def test_unlocks_next_is_false_when_the_untap_step_already_pays_it(client, game):
    """The mirror image, and the reason `unlocks_next` compares against the
    *assuming_untapped* now row rather than the real one: the tapped Island
    on the battlefield pays Preordain by itself once it untaps, so the
    Island in hand adds nothing that was not already there next turn -
    `unlocks_next` is False even though today's `unlocks` is True."""
    add_card(client, game, "Island", "alice", "battlefield", tapped=True)
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    island_id = str(_by_name(alice.hand, "Island").id)

    facts = _horizon(game)

    assert facts["cards"][island_id]["unlocks"] is True
    assert facts["cards"][island_id]["unlocks_next"] is False


def test_a_land_whose_untapped_option_is_lethal_never_reads_unlocks_next(client, game):
    """CR 704.5a, applied to `unlocks_next` exactly as `enters`/`unlocks`
    already apply it: Sacred Foundry's untapped option costs 2 life, so at 2
    life it is not an option a player can use and the land fails closed -
    with the notice that says why."""
    add_card(client, game, "Sacred Foundry", "alice", "hand")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    foundry_id = str(_by_name(alice.hand, "Sacred Foundry").id)

    assert _horizon(game)["cards"][foundry_id]["unlocks_next"] is True

    alice.life = 2
    lethal = _horizon(game)

    assert lethal["cards"][foundry_id]["unlocks_next"] is False
    assert any(
        "lethal" in notice.casefold() and "unlocks_next" in notice
        for notice in lethal["notices"]
    ), lethal["notices"]


def test_unlocks_next_is_offered_on_exactly_the_lands_unlocks_is_offered_on(client, game):
    """Requirement (d): the CR 401.2 argument in the module docstring stays
    true verbatim only if `unlocks_next` never widens the set of lands whose
    facts reach the wire. A hand land always, a `candidate_ids` land too,
    and no other library card ever."""
    add_card(client, game, "Ragavan, Nimble Pilferer", "alice", "hand")
    add_card(client, game, "Mountain", "alice", "library")
    add_card(client, game, "Sacred Foundry", "alice", "library")
    live = _live(game)
    alice = live.player("alice")
    mountain = _by_name(alice.library, "Mountain")
    foundry = _by_name(alice.library, "Sacred Foundry")

    named = build_hypothetical_facts(
        live, "alice", candidate_ids=frozenset({str(mountain.id)}), next_turn=True
    )
    assert "unlocks_next" in named["cards"][str(mountain.id)]
    assert str(foundry.id) not in named["cards"]

    unnamed = _horizon(game)
    assert str(mountain.id) not in unnamed["cards"]
    assert str(foundry.id) not in unnamed["cards"]

    for facts in (named, unnamed):
        for card_id, entry in facts["cards"].items():
            assert ("unlocks_next" in entry) == ("unlocks" in entry), (card_id, entry)


def test_a_hand_land_carries_unlocks_next_beside_unlocks(client, game):
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    live = _live(game)
    alice = live.player("alice")
    island_id = str(_by_name(alice.hand, "Island").id)

    entry = _horizon(game)["cards"][island_id]

    assert entry["unlocks"] is True
    assert entry["unlocks_next"] is True


def test_with_a_land_names_its_land_drop_assumption_in_a_notice(client, game):
    """D-3: `with_a_land` answers "some land in hand pays it", and says
    nothing about whether the land drop is still available (CR 305.2) - the
    same thing `unlocks` has always assumed. Rather than gate the fact (and
    so make `payable_any` and `unlocks` disagree about the very same land on
    the very same board), the limit is named in a notice whenever the engine
    itself says the drop is gone."""
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Preordain", "alice", "hand")
    live = _live(game)

    assert not any("305.2" in notice for notice in _horizon(game)["notices"])

    live.lands_played["alice"] = live.land_allowance("alice")
    spent = _horizon(game)

    assert any("with_a_land" in notice and "305.2" in notice for notice in spent["notices"]), (
        spent["notices"]
    )


def test_with_the_flag_unset_nothing_moves_and_the_second_pass_never_runs(client, game):
    """Requirement (e): the default answer is byte for byte what it was
    before this fix existed, and the extra `assuming_untapped` pass costs
    nothing unless it was asked for - proved by counting the
    `payability_matrix` calls rather than by timing."""
    import magic_cards_model_api.playscripts.hypothetical_facts as module

    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    live = _live(game)

    real = module.payability_matrix
    calls: list[bool] = []

    def counting(*args: Any, **kwargs: Any):
        calls.append(bool(kwargs.get("assuming_untapped", False)))
        return real(*args, **kwargs)

    module.payability_matrix = counting
    try:
        implicit = json.dumps(build_hypothetical_facts(live, "alice"))
        assert calls == [False]

        calls.clear()
        explicit_off = json.dumps(build_hypothetical_facts(live, "alice", next_turn=False))
        assert calls == [False]

        calls.clear()
        on = build_hypothetical_facts(live, "alice", next_turn=True)
        assert calls == [False, True]
    finally:
        module.payability_matrix = real

    assert implicit == explicit_off
    assert "payable_any" not in implicit
    assert "unlocks_next" not in implicit
    assert any("payable_any" in entry for entry in on["cards"].values())
    assert any("unlocks_next" in entry for entry in on["cards"].values())


def test_the_existing_facts_are_untouched_by_the_flag(client, game):
    """Requirement (f): turning the horizon on adds keys and never edits one.
    Every fact the default answer carries reads the same with the flag set."""
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Polluted Delta", "alice", "hand")
    add_card(client, game, "Psychic Frog", "alice", "hand")
    add_card(client, game, "Fatal Push", "alice", "hand")
    add_card(client, game, "Island", "alice", "library")
    live = _live(game)

    plain = build_hypothetical_facts(live, "alice")
    horizon = build_hypothetical_facts(live, "alice", next_turn=True)

    assert set(plain["cards"]) == set(horizon["cards"])
    for card_id, entry in plain["cards"].items():
        for key, value in entry.items():
            assert horizon["cards"][card_id][key] == value, (card_id, key)
    for notice in plain["notices"]:
        assert notice in horizon["notices"]
