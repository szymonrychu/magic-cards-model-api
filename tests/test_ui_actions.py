from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

BOLT = {
    "id": "b1",
    "name": "Lightning Bolt",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Instant",
    "cast": {
        "mana_cost": "{R}",
        "needs_x": False,
        "modes": None,
        "targeting": {
            "targets_anything": True,
            "understood": True,
            "minimum": 1,
            "maximum": 1,
            "requirements": [
                {
                    "phrase": "any target",
                    "count": 1,
                    "minimum": 1,
                    "unbounded": False,
                    "variable_count": False,
                    "mana": None,
                    "mode": None,
                    "mode_text": None,
                }
            ],
        },
    },
    "abilities": [],
}
MEMNITE = {
    "id": "mm",
    "name": "Memnite",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Artifact Creature",
    "cast": {
        "mana_cost": "{0}",
        "needs_x": False,
        "modes": None,
        "targeting": {
            "targets_anything": False,
            "understood": True,
            "minimum": 0,
            "maximum": 0,
            "requirements": [],
        },
    },
    "abilities": [],
}
KOZILEK = {
    "id": "kc",
    "name": "Kozilek's Command",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Kindred Instant",
    "cast": {
        "mana_cost": "{X}{C}{C}",
        "needs_x": True,
        "modes": {
            "instruction": "Choose two -",
            "minimum": 2,
            "maximum": 2,
            "repeatable": False,
            "understood": True,
            "options": [
                {"number": 1, "text": "Target player creates X ..."},
                {"number": 2, "text": "Target player scries X, then draws a card."},
                {"number": 3, "text": "Exile target creature with mana value X or less."},
                {"number": 4, "text": "Exile up to X target cards from graveyards."},
            ],
        },
        "targeting": {
            "targets_anything": True,
            "understood": False,
            "minimum": 0,
            "maximum": None,
            "requirements": [],
        },
    },
    "abilities": [],
}
BOLT_AFF = {
    "targets_anything": True,
    "understood": True,
    "minimum": 1,
    "maximum": 1,
    "requirements": [
        {
            "phrase": "any target",
            "count": 1,
            "minimum": 1,
            "unbounded": False,
            "variable_count": False,
            "mode": None,
            "mode_text": None,
        }
    ],
    "division": None,
    "candidates": [
        {"id": "bob", "kind": "player", "name": "bob", "requirements": [0]},
        {"id": "c9", "kind": "card", "name": "Memnite", "requirements": [0]},
    ],
}
KOZ_AFF = {
    "targets_anything": True,
    "understood": True,
    "minimum": 2,
    "maximum": 2,
    "requirements": [
        {
            "phrase": "target player",
            "count": 1,
            "minimum": 1,
            "unbounded": False,
            "variable_count": False,
            "mode": 1,
            "mode_text": "Target player creates X ...",
        },
        {
            "phrase": "target player",
            "count": 1,
            "minimum": 1,
            "unbounded": False,
            "variable_count": False,
            "mode": 2,
            "mode_text": "Target player scries X, then draws a card.",
        },
    ],
    "division": None,
    "candidates": [
        {"id": "alice", "kind": "player", "name": "alice", "requirements": [0, 1]},
        {"id": "bob", "kind": "player", "name": "bob", "requirements": [0, 1]},
    ],
}


def test_memnite_needs_no_choices_and_bolt_does() -> None:
    assert run_module(
        "actions.js",
        "[m.needsChoices(memnite, null), m.needsChoices(bolt, null)]",
        memnite=MEMNITE,
        bolt=BOLT,
    ) == [False, True]


def test_bolt_body_carries_target_and_all_sources() -> None:
    body = run_module(
        "actions.js",
        "(() => { let a = m.begin('cast', bolt, {}); a = m.withAffordance(a, aff); "
        "a = m.chooseTarget(a, 'bob'); "
        "return [m.readyToFire(a), m.bodyFor(a, 'alice', ['m1', 'm2'])]; })()",
        bolt=BOLT,
        aff=BOLT_AFF,
    )
    assert body == [
        True,
        {"player": "alice", "card_id": "b1", "targets": ["bob"], "mana_sources": ["m1", "m2"]},
    ]


def test_kozilek_flow_same_player_twice() -> None:
    result = run_module(
        "actions.js",
        """(() => {
          let a = m.begin('cast', koz, {});
          const before = m.affordanceQuery(a);
          a = m.toggleMode(a, 1); a = m.toggleMode(a, 2); a = m.setX(a, 1);
          const q = m.affordanceQuery(a);
          a = m.withAffordance(a, aff);
          a = m.chooseTarget(a, 'alice'); a = m.chooseTarget(a, 'alice');
          return [before, q, m.readyToFire(a), m.bodyFor(a, 'alice', ['w1', 'w2', 'w3'])];
        })()""",
        koz=KOZILEK,
        aff=KOZ_AFF,
    )
    assert result[0] == {}
    assert result[1] == {"modes": [1, 2], "x_value": 1}
    assert result[2] is True
    assert result[3] == {
        "player": "alice",
        "card_id": "kc",
        "targets": ["alice", "alice"],
        "modes": [1, 2],
        "x_value": 1,
        "mana_sources": ["w1", "w2", "w3"],
    }


def test_choosing_a_target_again_unfills_it() -> None:
    targets = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', bolt, {}), aff); "
        "a = m.chooseTarget(a, 'bob'); a = m.chooseTarget(a, 'bob'); "
        "return m.bodyFor(a, 'alice', []).targets; })()",
        bolt=BOLT,
        aff=BOLT_AFF,
    )
    assert targets == []


def test_modes_are_not_ready_until_enough_are_chosen() -> None:
    assert run_module(
        "actions.js",
        "[m.modesReady(m.begin('cast', koz, {})), "
        "m.modesReady(m.toggleMode(m.toggleMode(m.begin('cast', koz, {}), 1), 3))]",
        koz=KOZILEK,
    ) == [False, True]


def test_pinned_source_leads_the_mana_sources() -> None:
    body = run_module(
        "actions.js",
        "m.bodyFor(m.pinSource(m.withAffordance(m.chooseTarget("
        "m.begin('cast', bolt, {}), 'bob'), aff), 'd1', 'R'), 'alice', ['d1', 'm1'])",
        bolt=BOLT,
        aff=BOLT_AFF,
    )
    assert body["mana_sources"] == [{"card_id": "d1", "colors": ["R"]}, "m1"]


def test_a_colourless_pinned_source_leads_the_mana_sources_as_a_bare_id() -> None:
    """Pinning a source with no colour (`mana.js`'s `pin(pinned, id, null)` -
    what `app.js`'s `pinLand` now does when `colorOptions` comes back empty,
    a one-option ability's `mana_options: null`) must not turn into the
    coloured `{card_id, colors}` shape `test_pinned_source_leads_the_mana_sources`
    exercises - it goes out as a bare id, same as any other untapped source,
    still first in the list."""
    body = run_module(
        "actions.js",
        "m.bodyFor(m.pinSource(m.withAffordance(m.chooseTarget("
        "m.begin('cast', bolt, {}), 'bob'), aff), 'd1', null), 'alice', ['d1', 'm1'])",
        bolt=BOLT,
        aff=BOLT_AFF,
    )
    assert body["mana_sources"] == ["d1", "m1"]


def test_activate_body_and_endpoint() -> None:
    land = {
        "id": "d1",
        "name": "Sunbaked Canyon",
        "zone": "battlefield",
        "controller": "alice",
        "abilities": [
            {
                "index": 0,
                "text": "{T}, Pay 1 life: Add {R} or {W}.",
                "is_mana_ability": True,
                "needs_x": False,
                "mana_options": [["R"], ["W"]],
                "targeting": {
                    "targets_anything": False,
                    "understood": True,
                    "minimum": 0,
                    "maximum": 0,
                    "requirements": [],
                },
            }
        ],
    }
    result = run_module(
        "actions.js",
        "(() => { let a = m.begin('activate', land, { abilityIndex: 0 }); "
        "a = m.chooseColor(a, 'W'); "
        "return [m.endpointFor(a), m.bodyFor(a, 'alice', ['d1']), m.readyToFire(a)]; })()",
        land=land,
    )
    assert result == [
        "actions/activate",
        {"player": "alice", "card_id": "d1", "ability_index": 0, "color": "W"},
        True,
    ]


def test_a_mana_ability_activation_offers_another_untapped_source() -> None:
    """A filter land's `{1}, {T}: Add ...` needs its generic mana paid from
    somewhere else - only the land's own id drops out of the offer, not the
    whole list, so a second untapped source is still there to pay it."""
    land = {
        "id": "d1",
        "name": "Cascade Bluffs",
        "zone": "battlefield",
        "controller": "alice",
        "abilities": [
            {
                "index": 0,
                "text": "{1}, {T}: Add {U} or {R}.",
                "is_mana_ability": True,
                "needs_x": False,
                "mana_options": [["U"], ["R"]],
                "targeting": {
                    "targets_anything": False,
                    "understood": True,
                    "minimum": 0,
                    "maximum": 0,
                    "requirements": [],
                },
            }
        ],
    }
    body = run_module(
        "actions.js",
        "m.bodyFor(m.begin('activate', land, { abilityIndex: 0 }), 'alice', ['d1', 'm1'])",
        land=land,
    )
    assert body["mana_sources"] == ["m1"]


# -- specials (CR 116.2: foretell, plot, suspend) never gate on the card's --
# -- own cast.targeting, and still need to pay their own cost --------------


def test_special_action_is_ready_regardless_of_the_cards_own_targeting() -> None:
    """CR 116.2h: foretell exiles a card face down, choosing nothing about
    the spell it will become - Lightning Bolt's "any target" must not block
    a foretell of it from firing."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.begin('special', bolt, { keyword: 'Foretell' }); "
        "return [m.readyToFire(a), m.endpointFor(a)]; })()",
        bolt=BOLT,
    )
    assert result == [True, "actions/foretell"]


def test_special_action_body_carries_mana_sources() -> None:
    """`SpecialActionRequest` accepts `mana_sources`, and the engine only
    auto-taps what it is handed - foretell's `{2}` (and plot's, suspend's)
    has to be payable from the request body like any other cost."""
    body = run_module(
        "actions.js",
        "m.bodyFor(m.begin('special', bolt, { keyword: 'Foretell' }), 'alice', ['m1', 'm2'])",
        bolt=BOLT,
    )
    assert body == {"player": "alice", "card_id": "b1", "mana_sources": ["m1", "m2"]}


DIVIDING_PW = {
    "id": "pw1",
    "name": "Test Planeswalker",
    "zone": "battlefield",
    "controller": "alice",
    "type_line": "Legendary Planeswalker",
    "abilities": [
        {
            "index": 0,
            "text": "-3: Divide two damage among any number of target creatures.",
            "is_mana_ability": False,
            "needs_x": False,
            "mana_options": None,
            "targeting": {
                "targets_anything": True,
                "understood": True,
                "minimum": 1,
                "maximum": 2,
                "requirements": [],
            },
        }
    ],
}
DIVIDING_PW_AFF = {
    "targets_anything": True,
    "understood": True,
    "minimum": 1,
    "maximum": 2,
    "requirements": [
        {
            "phrase": "any number of target creatures",
            "count": 2,
            "minimum": 1,
            "unbounded": False,
            "variable_count": False,
            "mode": None,
            "mode_text": None,
        }
    ],
    "division": {"amount": 2},
    "candidates": [
        {"id": "bob", "kind": "player", "name": "bob", "requirements": [0]},
        {"id": "c9", "kind": "card", "name": "Memnite", "requirements": [0]},
    ],
}


def test_an_activated_abilitys_division_sends_shares_too() -> None:
    """`shares` (CR 601.2d) used to be emitted only for a cast; `readyToFire`
    already enforces `sharesValid` for every kind, so an activated ability
    with a division needs `shares` in its body just as much."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('activate', pw, "
        "{ abilityIndex: 0 }), aff); a = m.chooseTarget(a, 'bob'); "
        "a = m.chooseTarget(a, 'c9'); a = m.setShare(a, 'bob', 1); "
        "a = m.setShare(a, 'c9', 1); "
        "return [m.readyToFire(a), m.bodyFor(a, 'alice', []).shares]; })()",
        pw=DIVIDING_PW,
        aff=DIVIDING_PW_AFF,
    )
    assert result == [True, [1, 1]]


# -- completeness: the exports the eight cases above don't exercise --------


def test_alt_cost_and_pitch_choices_appear_in_the_body() -> None:
    """`chooseAltCost` and `togglePitch` cover Force of Will-style
    alternative costs (CR 118.9): an index into the card's alternative costs,
    plus the cards pitched to pay one that asks for them."""
    body = run_module(
        "actions.js",
        "(() => { let a = m.begin('cast', memnite, {}); a = m.chooseAltCost(a, 0); "
        "a = m.togglePitch(a, 'c2'); a = m.togglePitch(a, 'c3'); a = m.togglePitch(a, 'c3'); "
        "return m.bodyFor(a, 'alice', []); })()",
        memnite=MEMNITE,
    )
    assert body == {
        "player": "alice",
        "card_id": "mm",
        "alternative_cost": 0,
        "choosing": ["c2"],
    }


def test_convoke_and_delve_choices_appear_in_the_body() -> None:
    """`toggleConvoke` (CR 702.51a) and `toggleDelve` (CR 702.66a) each toggle
    an id into their own list; a second click on the same id removes it."""
    body = run_module(
        "actions.js",
        "(() => { let a = m.begin('cast', memnite, {}); a = m.toggleConvoke(a, 'g1'); "
        "a = m.toggleConvoke(a, 'g2'); a = m.toggleConvoke(a, 'g2'); a = m.toggleDelve(a, 'gy1'); "
        "return m.bodyFor(a, 'alice', []); })()",
        memnite=MEMNITE,
    )
    assert body == {
        "player": "alice",
        "card_id": "mm",
        "convoke": [{"card_id": "g1"}],
        "delve": ["gy1"],
    }


SPLIT = {
    "id": "sp1",
    "name": "Electrolyze",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Instant",
    "cast": {
        "mana_cost": "{1}{U}{R}",
        "needs_x": False,
        "modes": None,
        "targeting": {
            "targets_anything": True,
            "understood": True,
            "minimum": 1,
            "maximum": 2,
            "requirements": [
                {
                    "phrase": "up to two target creatures and/or players",
                    "count": 2,
                    "minimum": 1,
                    "unbounded": False,
                    "variable_count": False,
                    "mode": None,
                    "mode_text": None,
                }
            ],
        },
    },
    "abilities": [],
}
SPLIT_AFF = {
    "targets_anything": True,
    "understood": True,
    "minimum": 1,
    "maximum": 2,
    "requirements": [
        {
            "phrase": "up to two target creatures and/or players",
            "count": 2,
            "minimum": 1,
            "unbounded": False,
            "variable_count": False,
            "mode": None,
            "mode_text": None,
        }
    ],
    "division": {"amount": 2},
    "candidates": [
        {"id": "bob", "kind": "player", "name": "bob", "requirements": [0]},
        {"id": "c9", "kind": "card", "name": "Memnite", "requirements": [0]},
    ],
}


def test_setshare_divides_damage_between_two_targets() -> None:
    """CR 601.2d: a divided effect's `shares` line up with `targets`, one
    entry per target in the same order."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', split, {}), aff); "
        "a = m.chooseTarget(a, 'bob'); a = m.chooseTarget(a, 'c9'); "
        "a = m.setShare(a, 'bob', 1); a = m.setShare(a, 'c9', 1); "
        "return [m.readyToFire(a), m.bodyFor(a, 'alice', []).shares]; })()",
        split=SPLIT,
        aff=SPLIT_AFF,
    )
    assert result == [True, [1, 1]]


def test_unpin_source_drops_it_from_the_body() -> None:
    sources = run_module(
        "actions.js",
        "(() => { let a = m.pinSource(m.begin('cast', memnite, {}), 'd1', 'R'); "
        "a = m.unpinSource(a, 'd1'); "
        "return m.bodyFor(a, 'alice', ['d1', 'm1']).mana_sources; })()",
        memnite=MEMNITE,
    )
    assert sources == ["d1", "m1"]


KARN = {
    "id": "k1",
    "name": "Karn, the Great Creator",
    "zone": "battlefield",
    "controller": "alice",
    "type_line": "Legendary Planeswalker",
    "abilities": [
        {
            "index": 0,
            "text": "+1: Up to one target noncreature artifact...",
            "is_mana_ability": False,
            "needs_x": False,
            "mana_options": None,
            "targeting": {
                "targets_anything": True,
                "understood": True,
                "minimum": 0,
                "maximum": 1,
                "requirements": [],
            },
        }
    ],
}
KARN_AFF = {
    "targets_anything": True,
    "understood": True,
    "minimum": 0,
    "maximum": 1,
    "requirements": [
        {
            "phrase": "up to one target noncreature artifact",
            "count": 1,
            "minimum": 0,
            "unbounded": False,
            "variable_count": False,
            "mode": None,
            "mode_text": None,
        }
    ],
    "division": None,
    "candidates": [{"id": "c9", "kind": "card", "name": "Memnite", "requirements": [0]}],
}


def test_an_up_to_slot_needs_done_before_it_can_fire_empty() -> None:
    """Karn, the Great Creator's `+1` again: "up to one" (CR 115.6) must not
    fire empty just because nothing was clicked - `markDone` is the
    deliberate way to say "I looked, and I'm choosing none"."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('activate', karn, "
        "{ abilityIndex: 0 }), aff); const before = m.readyToFire(a); "
        "a = m.markDone(a); return [before, m.readyToFire(a)]; })()",
        karn=KARN,
        aff=KARN_AFF,
    )
    assert result == [False, True]


def test_needs_explicit_fire_covers_pinned_division_and_up_to_slots() -> None:
    """`needsExplicitFire` is the one predicate `ribbon.js`'s Cast button and
    `readyToFire`'s own open-slot gate both read - pinned mana, a division,
    or any "up to"/unbounded slot (CR 115.6, CR 601.2d) all mean "wait for
    the player to say so" rather than auto-firing the moment the
    announcement becomes legal. The filled-to-capacity "up to one" case is
    the one that regressed: an "up to one" slot that already has its one
    target chosen is still not exact, and must still ask for an explicit
    Cast rather than firing on its own."""
    result = run_module(
        "actions.js",
        """[
          m.needsExplicitFire({
            pinned: [{ card_id: 'd1', color: 'R' }], affordance: null, slots: [],
          }),
          m.needsExplicitFire({ pinned: [], affordance: { division: { amount: 2 } },
            slots: [{ count: 1, minimum: 1, targets: ['a'] }] }),
          m.needsExplicitFire({ pinned: [], affordance: null,
            slots: [{ count: 1, minimum: 0, targets: [] }] }),
          m.needsExplicitFire({ pinned: [], affordance: null,
            slots: [{ count: 1, minimum: 0, targets: ['a'] }] }),
          m.needsExplicitFire({ pinned: [], affordance: null,
            slots: [{ count: null, minimum: 0, targets: [] }] }),
          m.needsExplicitFire({ pinned: [], affordance: null,
            slots: [{ count: 1, minimum: 1, targets: ['a'] }] }),
        ]""",
    )
    assert result == [
        True,  # pinned mana alone
        True,  # a division alone, slot itself exact
        True,  # "up to one", empty
        True,  # "up to one", already filled to its cap - the regression case
        True,  # unbounded (count: null), empty
        False,  # an ordinary exact slot, nothing else in play
    ]


def test_needs_explicit_fire_waits_for_an_unchosen_available_alternative_cost() -> None:
    """The owner's report ("I'm not able to evoke through webUI ... I tried to
    evoke Wistfulness"): a non-targeting cast with no unbounded slot and no
    pinned mana used to be treated as decision-free and fired the instant its
    affordance landed, so the evoke chip `ribbon.js`'s `renderAltCosts` draws
    was never clickable - the cast had already gone out at full price.

    A CR 118.9 alternative cost the affordance published as an option is a
    real, unmade choice, exactly like an unbounded target slot already is -
    it must hold the fire until the player picks it or the announcement is
    marked done some other way. Nothing here names a keyword: the branch is
    on `alternative_costs[i].available`, published by the engine, never on
    what the option is called."""
    no_targets_no_alt = {
        "pinned": [],
        "affordance": {"alternative_costs": []},
        "slots": [],
        "altCost": None,
    }
    available_alt = {
        "pinned": [],
        "affordance": {
            "alternative_costs": [
                {"index": 0, "keyword": "Evoke", "available": True, "unavailable": None}
            ]
        },
        "slots": [],
        "altCost": None,
    }
    only_unavailable_alt = {
        "pinned": [],
        "affordance": {
            "alternative_costs": [
                {
                    "index": 0,
                    "keyword": "Evoke",
                    "available": False,
                    "unavailable": "not enough mana",
                }
            ]
        },
        "slots": [],
        "altCost": None,
    }
    chosen_alt = {
        "pinned": [],
        "affordance": {
            "alternative_costs": [
                {"index": 0, "keyword": "Evoke", "available": True, "unavailable": None}
            ]
        },
        "slots": [],
        "altCost": 0,
    }
    targets_and_available_alt = {
        "pinned": [],
        "affordance": {
            "alternative_costs": [
                {"index": 0, "keyword": "Evoke", "available": True, "unavailable": None}
            ]
        },
        "slots": [{"count": 1, "minimum": 1, "targets": ["a"]}],
        "altCost": None,
    }
    result = run_module(
        "actions.js",
        """[
          m.needsExplicitFire(no_targets_no_alt),
          m.needsExplicitFire(available_alt),
          m.needsExplicitFire(only_unavailable_alt),
          m.needsExplicitFire(chosen_alt),
          m.needsExplicitFire(targets_and_available_alt),
        ]""",
        no_targets_no_alt=no_targets_no_alt,
        available_alt=available_alt,
        only_unavailable_alt=only_unavailable_alt,
        chosen_alt=chosen_alt,
        targets_and_available_alt=targets_and_available_alt,
    )
    assert result == [
        False,  # no targets, no alternative cost - auto-fires, unchanged
        True,  # an available alternative cost, unchosen - waits
        False,  # the only alternative cost is unavailable - auto-fires
        False,  # the available alternative cost was already chosen - fires
        True,  # an exact filled target slot AND an available alternative cost - waits
    ]


# -- crew (CR 702.122a): the Vehicle's own action kind ----------------------

SMUGGLERS_COPTER = {
    "id": "sc",
    "name": "Smuggler's Copter",
    "zone": "battlefield",
    "controller": "alice",
    "type_line": "Artifact Vehicle",
    "abilities": [],
}


def test_crew_needs_at_least_one_creature() -> None:
    """CR 702.122b: a creature crews a Vehicle when it's tapped to pay the
    cost - `crewing` is that set of creatures, and it must be non-empty
    before the request is ready to fire."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.begin('crew', copter, {}); const before = m.readyToFire(a); "
        "a = m.toggleCrewing(a, 'g1'); "
        "return [before, m.readyToFire(a), m.endpointFor(a), m.bodyFor(a, 'alice', [])]; })()",
        copter=SMUGGLERS_COPTER,
    )
    assert result == [
        False,
        True,
        "actions/crew",
        {"player": "alice", "card_id": "sc", "crewing": ["g1"]},
    ]


def test_toggling_crewing_twice_removes_it() -> None:
    crewing = run_module(
        "actions.js",
        "(() => { let a = m.begin('crew', copter, {}); a = m.toggleCrewing(a, 'g1'); "
        "a = m.toggleCrewing(a, 'g1'); return m.bodyFor(a, 'alice', []).crewing; })()",
        copter=SMUGGLERS_COPTER,
    )
    assert crewing == []


# -- CR 601.3 play permissions: casting from a zone other than the hand -----


def test_from_zone_is_sent_only_when_begun_from_a_permission() -> None:
    """`fromZone` (CR 601.3's dynamic play permissions - cascade, suspend's
    own third ability) defaults to unset, matching `CastRequest.from_zone`'s
    own "hand" default (`schemas.py`) - only a cast begun through
    `beginCastFromPermission` sends it at all."""
    result = run_module(
        "actions.js",
        "(() => { const ordinary = m.begin('cast', memnite, {}); "
        "const fromExile = m.begin('cast', memnite, { fromZone: 'exile' }); "
        "return [m.bodyFor(ordinary, 'alice', []), m.bodyFor(fromExile, 'alice', [])]; })()",
        memnite=MEMNITE,
    )
    assert "from_zone" not in result[0]
    assert result[1]["from_zone"] == "exile"


# -- targetSlotIndex: the one place a board click's slot is decided --------


# -- item 7: a mode/X change drops a click still queued from before it -----


def test_toggling_a_mode_drops_any_queued_pending_target() -> None:
    """A click queued before the affordance arrived (`chooseTarget`'s own
    `pendingTargets` escape hatch) must not survive a mode change: the change
    starts a fresh affordance fetch against a possibly different targeting
    profile, and replaying a stale click into it once that fetch lands could
    fill a slot the player never actually clicked for under the new modes."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.chooseTarget(m.begin('cast', koz, {}), 'alice'); "
        "const queuedBeforeToggle = a.pendingTargets; "
        "a = m.toggleMode(a, 1); "
        "const queuedAfterToggle = a.pendingTargets; "
        "a = m.withAffordance(a, koz_aff); "
        "return [queuedBeforeToggle, queuedAfterToggle, m.bodyFor(a, 'alice', []).targets]; })()",
        koz=KOZILEK,
        koz_aff=KOZ_AFF,
    )
    assert result == [["alice"], [], []]


def test_announcing_x_drops_any_queued_pending_target() -> None:
    """Same reasoning as the mode-toggle case above: announcing X restarts
    the affordance fetch too."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.chooseTarget(m.begin('cast', koz, {}), 'alice'); "
        "a = m.setX(a, 2); "
        "const queuedAfterX = a.pendingTargets; "
        "a = m.withAffordance(a, koz_aff); "
        "return [queuedAfterX, m.bodyFor(a, 'alice', []).targets]; })()",
        koz=KOZILEK,
        koz_aff=KOZ_AFF,
    )
    assert result == [[], []]


def test_target_slot_index_defers_to_active_slot_index_by_default() -> None:
    """No manual focus (`focusSlot: null`, `begin`'s own default) means the
    board click and `render/board.js`'s highlight both fall back to
    `targeting.js`'s own `activeSlotIndex` guess."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', koz, {}), koz_aff); "
        "a = m.toggleMode(a, 1); a = m.toggleMode(a, 2); "
        "a = m.withAffordance(a, koz_aff); "
        "return m.targetSlotIndex(a); })()",
        koz=KOZILEK,
        koz_aff=KOZ_AFF,
    )
    assert result == 0


def test_target_slot_index_honours_an_explicit_focus() -> None:
    """A slot chip's own click (`onFocusSlot`) pins `action.focusSlot` -
    `targetSlotIndex` must return exactly that slot rather than whichever
    `activeSlotIndex` would otherwise pick."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', koz, {}), koz_aff); "
        "a = m.toggleMode(a, 1); a = m.toggleMode(a, 2); "
        "a = m.withAffordance(a, koz_aff); "
        "a = { ...a, focusSlot: 1 }; "
        "return m.targetSlotIndex(a); })()",
        koz=KOZILEK,
        koz_aff=KOZ_AFF,
    )
    assert result == 1


def test_target_slot_index_ignores_a_stale_out_of_range_focus() -> None:
    """A focus left over from a slot list that has since shrunk (a mode
    toggled off, clearing `slots`) must not point past the end of the new
    one - falling back to `activeSlotIndex` is the safe default."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', bolt, {}), aff); "
        "a = { ...a, focusSlot: 5 }; "
        "return m.targetSlotIndex(a); })()",
        bolt=BOLT,
        aff=BOLT_AFF,
    )
    assert result == 0
