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
