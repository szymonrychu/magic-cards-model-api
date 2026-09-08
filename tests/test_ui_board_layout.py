"""`ui/board_layout.js` - the pure half of how the battlefield is laid out.

Three questions, answered without a DOM so they can be tested directly (the
`bootTarget` seam in `ui/app.js` is the pattern):

1. which row a permanent belongs in - lands go in the row nearest their own
   controller, everything else in the row further from them;
2. which cards are tucked *under* another card - an Aura or an Equipment
   attached to a permanent, and a card exiled face-up with a permanent
   (imprint, hideaway);
3. what is left over in the shared exile zone once those are pulled out.

No CR number is cited for the row split on purpose: the Comprehensive Rules
say nothing about how the battlefield is physically arranged - it is a shared
zone, and the two-row convention comes from play practice and tournament
board-state clarity, not from a numbered rule.
"""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node


def card(id_, type_line, controller="alice", **over):
    return {
        "id": id_,
        "name": id_,
        "controller": controller,
        "zone": "battlefield",
        "type_line": type_line,
        **over,
    }


def to_card(id_):
    return {"kind": "card", "id": id_, "name": id_}


def base(battlefield, exile=None):
    return {"you": "alice", "battlefield": battlefield, "exile": exile or []}


def layout(state):
    return run_module("board_layout.js", "m.boardLayout(state)", state=state)


def ids(entries):
    return [entry["card"]["id"] for entry in entries]


def under(entries, parent_id):
    entry = next(e for e in entries if e["card"]["id"] == parent_id)
    return [c["id"] for c in entry["under"]]


# ------------------------------------------------------------- the row split


def test_is_land_row_reads_the_current_type_line() -> None:
    """The type line the state view carries is the *computed* one, so an
    animated Mutavault arrives here already saying "Land Creature"."""
    lines = [
        "Basic Land - Forest",
        "Artifact Land",
        "Land Creature - Forest Dryad",
        "Creature - Elemental",
        "Land Creature - Elemental",
        "Enchantment - Aura",
        "Legendary Land",
    ]
    answers = run_module(
        "board_layout.js",
        "lines.map((l) => m.isLandRow({ type_line: l }))",
        lines=lines,
    )
    assert answers == [True, True, False, False, False, False, True]


def test_a_card_with_no_type_line_is_not_a_land() -> None:
    assert run_module("board_layout.js", "m.isLandRow({})") is False


def test_each_seats_lands_and_nonlands_are_split_per_controller() -> None:
    state = base(
        [
            card("f1", "Basic Land - Forest", "alice"),
            card("bear", "Creature - Bear", "alice"),
            card("i1", "Basic Land - Island", "bob"),
            card("bird", "Creature - Bird", "bob"),
            card("ring", "Artifact", "alice"),
        ]
    )
    out = layout(state)
    assert ids(out["seats"]["alice"]["lands"]) == ["f1"]
    assert ids(out["seats"]["alice"]["main"]) == ["bear", "ring"]
    assert ids(out["seats"]["bob"]["lands"]) == ["i1"]
    assert ids(out["seats"]["bob"]["main"]) == ["bird"]


def test_a_land_creature_sits_with_the_creatures() -> None:
    """Dryad Arbor, and any manland that has been activated. It can attack,
    block, and be targeted as a creature, so it belongs in the row combat is
    read off - even though it also taps for mana."""
    state = base([card("arbor", "Land Creature - Forest Dryad")])
    out = layout(state)
    assert ids(out["seats"]["alice"]["lands"]) == []
    assert ids(out["seats"]["alice"]["main"]) == ["arbor"]


def test_a_creature_turned_into_a_land_moves_to_the_land_row() -> None:
    """The other direction - a permanent whose computed type line is a land
    and nothing else (Song of the Dryads) is a land for this purpose."""
    state = base([card("frog", "Land - Forest")])
    assert ids(layout(state)["seats"]["alice"]["lands"]) == ["frog"]


# ------------------------------------------------------------- attachments


def test_an_aura_is_tucked_under_the_creature_it_enchants() -> None:
    state = base(
        [
            card("bear", "Creature - Bear"),
            card("aura", "Enchantment - Aura", attached_to=to_card("bear")),
        ]
    )
    out = layout(state)
    assert ids(out["seats"]["alice"]["main"]) == ["bear"]
    assert under(out["seats"]["alice"]["main"], "bear") == ["aura"]


def test_several_auras_and_an_equipment_all_tuck_under_the_same_creature() -> None:
    state = base(
        [
            card("bear", "Creature - Bear"),
            card("aura1", "Enchantment - Aura", attached_to=to_card("bear")),
            card("sword", "Artifact - Equipment", attached_to=to_card("bear")),
            card("aura2", "Enchantment - Aura", attached_to=to_card("bear")),
        ]
    )
    out = layout(state)
    assert ids(out["seats"]["alice"]["main"]) == ["bear"]
    assert under(out["seats"]["alice"]["main"], "bear") == ["aura1", "sword", "aura2"]


def test_an_equipment_attached_to_nothing_is_an_ordinary_permanent() -> None:
    state = base([card("sword", "Artifact - Equipment"), card("bear", "Creature - Bear")])
    out = layout(state)
    assert ids(out["seats"]["alice"]["main"]) == ["sword", "bear"]
    assert under(out["seats"]["alice"]["main"], "sword") == []


def test_an_aura_on_a_player_stays_an_ordinary_permanent() -> None:
    """An Aura can be attached to a player, and a player is not a card on
    the battlefield to tuck it under - so it keeps a row of its own rather
    than vanishing."""
    state = base(
        [
            card(
                "curse",
                "Enchantment - Aura Curse",
                attached_to={"kind": "player", "id": "bob", "name": "bob"},
            )
        ]
    )
    assert ids(layout(state)["seats"]["alice"]["main"]) == ["curse"]


def test_an_attachment_naming_a_card_that_is_not_on_the_battlefield_stays_visible() -> None:
    state = base([card("aura", "Enchantment - Aura", attached_to=to_card("gone"))])
    assert ids(layout(state)["seats"]["alice"]["main"]) == ["aura"]


def test_an_aura_on_an_equipment_tucks_under_the_creature_at_the_root() -> None:
    """One flat pile under the permanent that is actually on the
    battlefield in its own right, rather than a tuck inside a tuck nobody
    could read."""
    state = base(
        [
            card("bear", "Creature - Bear"),
            card("sword", "Artifact - Equipment", attached_to=to_card("bear")),
            card("aura", "Enchantment - Aura", attached_to=to_card("sword")),
        ]
    )
    out = layout(state)
    assert ids(out["seats"]["alice"]["main"]) == ["bear"]
    assert under(out["seats"]["alice"]["main"], "bear") == ["sword", "aura"]


def test_an_attachment_cycle_leaves_both_cards_on_the_board() -> None:
    """Nothing the engine models should produce this. If it ever does, the
    board still has to draw both cards rather than loop forever."""
    state = base(
        [
            card("a", "Artifact - Equipment", attached_to=to_card("b")),
            card("b", "Artifact - Equipment", attached_to=to_card("a")),
        ]
    )
    assert sorted(ids(layout(state)["seats"]["alice"]["main"])) == ["a", "b"]


def test_an_attachment_is_tucked_under_its_parents_controller_not_its_own() -> None:
    """Control Magic's own shape: my Aura, their creature. The pile is
    physically where the creature is."""
    state = base(
        [
            card("bird", "Creature - Bird", "bob"),
            card("aura", "Enchantment - Aura", "alice", attached_to=to_card("bird")),
        ]
    )
    out = layout(state)
    assert "alice" not in out["seats"]
    assert under(out["seats"]["bob"]["main"], "bird") == ["aura"]


def test_an_animated_land_carries_its_aura_into_the_creature_row() -> None:
    state = base(
        [
            card("mutavault", "Land Creature - Elemental"),
            card("aura", "Enchantment - Aura", attached_to=to_card("mutavault")),
        ]
    )
    out = layout(state)
    assert ids(out["seats"]["alice"]["lands"]) == []
    assert under(out["seats"]["alice"]["main"], "mutavault") == ["aura"]


# ------------------------------------------------- imprint / hideaway / exile


def test_a_card_exiled_with_a_permanent_is_tucked_under_it() -> None:
    state = base(
        [card("scepter", "Artifact")],
        exile=[
            {
                "id": "bolt",
                "name": "Lightning Bolt",
                "zone": "exile",
                "exile_pile": {"marker": "imprinted", "linked_permanent_id": "scepter"},
            }
        ],
    )
    out = layout(state)
    assert under(out["seats"]["alice"]["main"], "scepter") == ["bolt"]
    assert out["exile"] == []


def test_an_ordinary_exiled_card_stays_in_the_exile_zone() -> None:
    state = base(
        [card("bear", "Creature - Bear")],
        exile=[{"id": "gone", "name": "Gone", "zone": "exile"}],
    )
    out = layout(state)
    assert [c["id"] for c in out["exile"]] == ["gone"]
    assert under(out["seats"]["alice"]["main"], "bear") == []


def test_a_card_exiled_with_a_permanent_that_has_left_stays_in_exile() -> None:
    """Nothing unlinks an exiled card when its permanent goes away, so the
    id can name a permanent that is no longer on the battlefield. It is
    still in exile, and exile is where it must be drawn."""
    state = base(
        [],
        exile=[
            {
                "id": "bolt",
                "name": "Lightning Bolt",
                "zone": "exile",
                "exile_pile": {"marker": "imprinted", "linked_permanent_id": "scepter"},
            }
        ],
    )
    assert [c["id"] for c in layout(state)["exile"]] == ["bolt"]


def test_attachments_come_before_exiled_cards_in_one_pile() -> None:
    state = base(
        [
            card("scepter", "Artifact"),
            card("aura", "Enchantment - Aura", attached_to=to_card("scepter")),
        ],
        exile=[
            {
                "id": "bolt",
                "name": "Lightning Bolt",
                "zone": "exile",
                "exile_pile": {"marker": "imprinted", "linked_permanent_id": "scepter"},
            }
        ],
    )
    out = layout(state)
    assert under(out["seats"]["alice"]["main"], "scepter") == ["aura", "bolt"]


def test_an_empty_board_gives_empty_rows_and_no_exile() -> None:
    out = layout(base([]))
    assert out["seats"] == {}
    assert out["exile"] == []


def test_seat_rows_fills_in_the_empty_rows_for_a_seat_with_nothing_out() -> None:
    assert run_module(
        "board_layout.js",
        'm.seatRows(m.boardLayout({ battlefield: [] }), "alice")',
    ) == {"lands": [], "main": []}


def test_a_missing_exile_key_is_tolerated() -> None:
    out = run_module(
        "board_layout.js",
        'm.boardLayout({ battlefield: [{ id: "f", type_line: "Land", controller: "alice" }] })',
    )
    assert out["exile"] == []
    assert [e["card"]["id"] for e in out["seats"]["alice"]["lands"]] == ["f"]


# --------------------------------------------------------- the zone ribbon
#
# The vertical strip of graveyards and exiles between the battlefield and the
# control ribbon. Its ordering is the same mirroring the battlefield rows use:
# the far seat's zones far from you, your own nearest you.


def exiled(id_, **over):
    return {"id": id_, "name": id_, "zone": "exile", **over}


def ribbon_state(exile=None, graveyards=None, battlefield=None, you="alice"):
    return {
        "you": you,
        "players": [{"username": "alice"}, {"username": "bob"}],
        "battlefield": battlefield or [],
        "exile": exile or [],
        "graveyards": graveyards or {},
    }


def ribbon(state):
    return run_module("board_layout.js", "m.zoneRibbon(state)", state=state)


def keys(rows):
    return [row["key"] for row in rows]


def cards_in(rows, key):
    return [c["id"] for c in next(r for r in rows if r["key"] == key)["cards"]]


def test_the_ribbon_runs_from_the_opponents_zones_down_to_your_own() -> None:
    """The far seat's zones sit far from you and your own sit near you - the
    same mirroring the battlefield rows already use, so the two halves read as
    a reflection rather than as one uniform top-to-bottom list."""
    rows = ribbon(ribbon_state())
    assert keys(rows) == ["opp-graveyard", "opp-exile", "you-exile", "you-graveyard"]
    assert [row["side"] for row in rows] == ["opp", "opp", "you", "you"]
    assert [row["zone"] for row in rows] == ["graveyard", "exile", "exile", "graveyard"]


def test_each_row_names_the_seat_whose_zone_it_is() -> None:
    rows = ribbon(ribbon_state())
    assert [row["seat"] for row in rows] == ["bob", "bob", "alice", "alice"]
    assert [row["seatLabel"] for row in rows] == ["bob", "bob", "alice", "alice"]


def test_the_ribbon_mirrors_for_the_other_seat() -> None:
    """Read from bob's chair, the same board puts bob's zones nearest bob: the
    four rows keep their order, the seats in them swap."""
    state = ribbon_state(
        graveyards={"alice": [exiled("a-gy")], "bob": [exiled("b-gy")]},
        exile=[exiled("a-ex", controller="alice"), exiled("b-ex", controller="bob")],
    )
    mine = ribbon(state)
    theirs = ribbon({**state, "you": "bob"})
    assert keys(mine) == keys(theirs)
    assert cards_in(mine, "you-graveyard") == ["a-gy"]
    assert cards_in(mine, "opp-graveyard") == ["b-gy"]
    assert cards_in(theirs, "you-graveyard") == ["b-gy"]
    assert cards_in(theirs, "opp-graveyard") == ["a-gy"]
    assert cards_in(mine, "you-exile") == ["a-ex"]
    assert cards_in(theirs, "you-exile") == ["b-ex"]


def test_exile_is_split_by_whose_card_it_is() -> None:
    """Exile is one shared zone and the state view sends one list, so the two
    exile rows are that list split by the player each card belongs to."""
    state = ribbon_state(
        exile=[
            exiled("mine", controller="alice"),
            exiled("theirs", controller="bob"),
            exiled("mine-by-owner", owner="alice"),
            exiled("theirs-by-owner", owner="bob"),
        ]
    )
    rows = ribbon(state)
    assert cards_in(rows, "you-exile") == ["mine", "mine-by-owner"]
    assert cards_in(rows, "opp-exile") == ["theirs", "theirs-by-owner"]


def test_an_exiled_cards_controller_wins_over_its_owner() -> None:
    """`controller or owner` is the fallback `serialize.py` itself uses to
    decide who could cast a card sitting in exile, so a card exiled under my
    control is in my pile even when it is their card."""
    state = ribbon_state(exile=[exiled("grief", controller="alice", owner="bob")])
    rows = ribbon(state)
    assert cards_in(rows, "you-exile") == ["grief"]
    assert cards_in(rows, "opp-exile") == []


def test_an_exiled_card_belonging_to_nobody_is_still_drawn() -> None:
    """Nothing the state view sends should look like this, but a card it
    cannot attribute must still appear somewhere rather than vanish out of a
    public zone - it is drawn on the far side."""
    rows = ribbon(ribbon_state(exile=[exiled("orphan")]))
    assert cards_in(rows, "opp-exile") == ["orphan"]
    assert cards_in(rows, "you-exile") == []


def test_a_card_exiled_with_a_permanent_is_not_in_the_ribbon() -> None:
    """It is drawn tucked under that permanent instead: the ribbon shows what
    `boardLayout` left in exile, not the raw zone."""
    state = ribbon_state(
        battlefield=[card("scepter", "Artifact")],
        exile=[
            exiled(
                "bolt",
                controller="alice",
                exile_pile={"marker": "imprinted", "linked_permanent_id": "scepter"},
            )
        ],
    )
    rows = ribbon(state)
    assert cards_in(rows, "you-exile") == []
    assert cards_in(rows, "opp-exile") == []


def test_the_four_rows_are_there_even_with_every_zone_empty() -> None:
    """The ribbon keeps its shape from turn one, so the battlefield does not
    reflow the first time a card reaches a graveyard."""
    rows = ribbon(ribbon_state())
    assert keys(rows) == ["opp-graveyard", "opp-exile", "you-exile", "you-graveyard"]
    assert all(row["cards"] == [] for row in rows)


def test_a_missing_graveyards_key_is_tolerated() -> None:
    rows = run_module(
        "board_layout.js",
        'm.zoneRibbon({ you: "alice", players: [{ username: "alice" }] })',
    )
    assert keys(rows) == ["opp-graveyard", "opp-exile", "you-exile", "you-graveyard"]
    assert all(row["cards"] == [] for row in rows)


def test_a_seat_with_no_opponent_yet_still_gets_the_far_rows() -> None:
    rows = run_module(
        "board_layout.js",
        'm.zoneRibbon({ you: "alice", players: [{ username: "alice" }] })',
    )
    assert [row["seat"] for row in rows] == [None, None, "alice", "alice"]
    assert [row["seatLabel"] for row in rows] == ["opponent", "opponent", "alice", "alice"]


def test_the_ribbon_reuses_a_layout_it_is_handed() -> None:
    """`render/board.js` has already computed `boardLayout(state)` to draw the
    battlefield, and the ribbon takes that answer rather than recomputing it."""
    state = ribbon_state(exile=[exiled("mine", controller="alice")])
    rows = run_module(
        "board_layout.js",
        "m.zoneRibbon(state, m.boardLayout(state))",
        state=state,
    )
    assert cards_in(rows, "you-exile") == ["mine"]
