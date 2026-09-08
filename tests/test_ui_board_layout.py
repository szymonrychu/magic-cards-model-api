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
