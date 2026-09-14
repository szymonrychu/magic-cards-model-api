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
