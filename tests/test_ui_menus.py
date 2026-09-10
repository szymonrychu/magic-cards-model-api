"""`ui/menus.js` - the pure half of "a click opened a choice; where does it go".

Three questions, all answerable without a DOM (the `ui/board_layout.js` and
`ui/app.js` `bootTarget` seams are the pattern):

1. **which surface** a given choice is presented on. A choice that is *about a
   card* (which way to cast it, which of its abilities, which colour a land
   taps for, which player an attacker is aimed at) is a short list anchored
   over that card. The in-flight announcement is not: it has
   several steps, and it has to stay on screen while the player clicks
   targets on the board, so it is a panel in the control ribbon. A question
   the *game* is asking this seat is turn-scoped and belongs in the one
   prompt box that already asks everything.
2. **which items** a card-scoped menu offers, in order - so the list a click
   produces is decided in one place rather than inline in a handler.
3. **where an anchored menu is positioned**, as arithmetic over rectangles.
   This is the one the owner's complaint is actually about: a menu opened on
   a card in the hand row (the bottom of the window) used to be placed below
   it in document coordinates, which pushed the document taller than the
   viewport and gave the page a vertical scrollbar it did not have. The
   contract asserted here is that the returned box is always inside the
   viewport, so opening one can never change the page's geometry.

No CR number is cited anywhere in this file. Nothing in the Comprehensive
Rules says where a client draws its menus, so the surface split needs none -
and the session that wrote this had no `knowledge_search` available, so no
rule number for the announcement sequence is restated here either
(`ui/style.css`'s `.casting-strip` note already carries the one this repo
verified when that panel was written).
"""

from __future__ import annotations

import re
from pathlib import Path

from ui_node import requires_node, run_module

pytestmark = requires_node

UI = Path(__file__).resolve().parents[1] / "ui"


def call(expression, **bindings):
    return run_module("menus.js", expression, **bindings)


# --------------------------------------------------------------- surfaces ---


def test_card_scoped_choices_get_an_anchored_popover():
    kinds = ["hand_actions", "abilities", "mana_colour", "defender"]
    assert call("kinds.map((k) => m.surfaceForChoice(k))", kinds=kinds) == ["popover"] * 4


def test_the_in_flight_announcement_gets_the_ribbon_panel():
    assert call('m.surfaceForChoice("announcement")') == "ribbon"


def test_turn_scoped_questions_get_the_prompt_box():
    kinds = ["decision", "pregame", "combat", "game_over"]
    assert call("kinds.map((k) => m.surfaceForChoice(k))", kinds=kinds) == ["prompt"] * 4


def test_an_unknown_choice_kind_has_no_surface():
    assert call('m.surfaceForChoice("something-new")') is None


# ------------------------------------------------- plan -> choice mapping ---


def test_a_hand_card_menu_is_a_card_scoped_popover():
    plan = {"kind": "menu", "isLand": False, "specials": ["flashback"], "cycling": []}
    assert call("m.choiceForPlan(plan)", plan=plan) == {
        "kind": "hand_actions",
        "surface": "popover",
    }


def test_a_permanent_with_several_abilities_is_a_card_scoped_popover():
    plan = {"kind": "menu", "abilities": [{"index": 0, "text": "tap"}], "vehicle": False}
    assert call("m.choiceForPlan(plan)", plan=plan) == {"kind": "abilities", "surface": "popover"}


def test_pinning_a_land_and_aiming_an_attacker_are_card_scoped():
    assert call('m.choiceForPlan({kind: "pin"})') == {"kind": "mana_colour", "surface": "popover"}
    assert call('m.choiceForPlan({kind: "attack"})') == {"kind": "defender", "surface": "popover"}


def test_beginning_a_cast_or_an_activation_stages_in_the_ribbon():
    for kind in ("cast", "activate", "crew"):
        assert call("m.choiceForPlan({kind: k})", k=kind) == {
            "kind": "announcement",
            "surface": "ribbon",
        }


def test_a_click_that_acts_directly_opens_no_choice_at_all():
    for kind in ("land", "target", "cancel", "block", "none"):
        assert call("m.choiceForPlan({kind: k})", k=kind) is None


# ------------------------------------------------------------ menu items ---


def test_a_land_with_cycling_offers_play_first_then_cycle():
    plan = {"kind": "menu", "isLand": True, "specials": [], "cycling": [{"cost": "{2}"}]}
    card = {"id": "c1", "name": "Lonely Sandbar", "special_actions": {"cycling": [{"cost": "{2}"}]}}
    assert call("m.handMenuItems(plan, card)", plan=plan, card=card) == [
        {"key": "play", "label": "Play", "sublabel": ""},
        {"key": "cycle", "label": "Cycle", "sublabel": "{2}"},
    ]


def test_a_spell_with_a_special_action_offers_cast_first_then_the_special():
    plan = {"kind": "menu", "isLand": False, "specials": ["suspend"], "cycling": []}
    card = {"id": "c2", "name": "Rift Bolt", "special_actions": {"suspend": {"cost": "{R}"}}}
    assert call("m.handMenuItems(plan, card)", plan=plan, card=card) == [
        {"key": "cast", "label": "Cast", "sublabel": ""},
        {"key": "special:suspend", "label": "Suspend", "sublabel": "{R}"},
    ]


def test_ability_items_carry_their_index_and_a_vehicle_adds_crew_last():
    plan = {
        "kind": "menu",
        "abilities": [
            {"index": 0, "text": "{T}: Add {G}.", "cost": "{T}"},
            {"index": 2, "text": "{2}: Draw a card.", "cost": None},
        ],
        "vehicle": True,
    }
    assert call("m.abilityMenuItems(plan)", plan=plan) == [
        {"key": "activate:0", "label": "{T}: Add {G}.", "sublabel": ""},
        {"key": "activate:2", "label": "{2}: Draw a card.", "sublabel": ""},
        {"key": "crew", "label": "Crew", "sublabel": ""},
    ]


def test_an_ability_does_not_repeat_a_cost_its_own_text_already_opens_with():
    """Walking Ballista in the browser pass: the menu printed "Remove a +1/+1
    counter from this creature: It deals 1 damage to any target." and then,
    in dim type underneath it, "Remove a +1/+1 counter from this creature"
    again. A printed activated ability is "cost: effect", so the cost is
    already the first thing the label says."""
    cost = "Remove a +1/+1 counter from this creature"
    text = f"{cost}: It deals 1 damage to any target."
    plan = {
        "kind": "menu",
        "abilities": [
            {"index": 1, "text": text, "cost": cost},
            # A cost the text does not open with is still worth showing.
            {"index": 3, "text": "Draw a card.", "cost": "{2}, {T}"},
        ],
        "vehicle": False,
    }
    assert call("m.abilityMenuItems(plan)", plan=plan) == [
        {"key": "activate:1", "label": text, "sublabel": ""},
        {"key": "activate:3", "label": "Draw a card.", "sublabel": "{2}, {T}"},
    ]


def test_mana_and_defender_items():
    assert call('m.manaMenuItems(["R", "G"])') == [
        {"key": "colour:R", "label": "Tap for {R}", "sublabel": ""},
        {"key": "colour:G", "label": "Tap for {G}", "sublabel": ""},
    ]
    defenders = [{"id": "p1", "label": "claude"}, {"id": "pw", "label": "Jace"}]
    assert call("m.defenderMenuItems(defenders)", defenders=defenders) == [
        {"key": "defender:p1", "label": "Attack claude", "sublabel": ""},
        {"key": "defender:pw", "label": "Attack Jace", "sublabel": ""},
    ]


# -------------------------------------------------------------- geometry ---

VIEWPORT = {"width": 1440, "height": 900}


def place(anchor, menu, viewport=None, **opts):
    return call(
        "m.anchoredPosition(anchor, menu, viewport, opts)",
        anchor=anchor,
        menu=menu,
        viewport=viewport or VIEWPORT,
        opts=opts,
    )


def rect(left, top, width, height):
    return {"left": left, "top": top, "right": left + width, "bottom": top + height}


def test_with_room_below_the_menu_hangs_under_its_anchor():
    got = place(rect(300, 200, 96, 134), {"width": 180, "height": 160})
    assert got["placement"] == "below"
    assert got["top"] == 200 + 134 + 4
    assert got["left"] == 300


def test_a_card_in_the_hand_row_flips_above_instead_of_growing_the_page():
    # The hand row sits at the bottom of a 900px window: below the card there
    # are 22px, nowhere near the 160px the menu needs. Placing it below is
    # exactly what made the document taller than the viewport.
    anchor = rect(300, 744, 96, 134)
    got = place(anchor, {"width": 180, "height": 160})
    assert got["placement"] == "above"
    assert got["top"] + got["maxHeight"] <= anchor["top"]
    assert got["top"] >= 8


def test_the_box_never_leaves_the_viewport_wherever_the_anchor_is():
    menu = {"width": 220, "height": 260}
    anchors = [
        rect(0, 0, 96, 134),
        rect(1380, 10, 60, 134),
        rect(1400, 800, 40, 90),
        rect(-20, 700, 96, 134),
        rect(700, 430, 96, 134),
    ]
    boxes = call(
        "anchors.map((a) => m.anchoredPosition(a, menu, viewport, {}))",
        anchors=anchors,
        menu=menu,
        viewport=VIEWPORT,
    )
    for box in boxes:
        assert box["left"] >= 8
        assert box["left"] + menu["width"] <= VIEWPORT["width"] - 8
        assert box["top"] >= 8
        assert box["top"] + box["maxHeight"] <= VIEWPORT["height"] - 8


def test_a_menu_taller_than_the_window_is_capped_and_scrolls_in_place():
    got = place(rect(300, 400, 96, 134), {"width": 180, "height": 4000})
    assert got["maxHeight"] <= VIEWPORT["height"] - 16
    assert got["top"] >= 8
    assert got["top"] + got["maxHeight"] <= VIEWPORT["height"] - 8


def test_a_narrow_window_still_fits_the_menu():
    # The 1100px breakpoint stacks the grid; the menu still has to land inside.
    narrow = {"width": 380, "height": 640}
    got = place(rect(300, 500, 96, 134), {"width": 340, "height": 200}, viewport=narrow)
    assert got["left"] >= 8
    assert got["left"] + 340 <= narrow["width"] - 8
    assert got["top"] + got["maxHeight"] <= narrow["height"] - 8


# ------------------------------------------------------------- structure ---


def test_the_casting_strip_is_in_the_control_ribbon_not_inside_the_board():
    """The owner's "choices are rendered under the cards".

    `#casting-strip` used to be a row of `#mtg-board`, between the land row
    and the hand: starting a cast inserted a block into the board's own flex
    column, so every card above it moved. It is a panel in `#side-panel` now,
    beside the board rather than inside it.
    """
    html = (UI / "index.html").read_text()
    board = re.search(r'<div id="mtg-board">(.*?)\n  </div>', html, re.S)
    assert board, "could not find #mtg-board in ui/index.html"
    assert 'id="casting-strip"' not in board.group(1)
    side = re.search(r'<aside id="side-panel">(.*?)</aside>', html, re.S)
    assert side, "could not find #side-panel in ui/index.html"
    assert 'id="casting-strip"' in side.group(1)


def test_the_anchored_menu_is_viewport_fixed_never_document_absolute():
    """A `position: absolute` menu is placed in document coordinates, so one
    hanging past the bottom of the window extends the document and the page
    grows a scrollbar. Fixed positioning cannot do that."""
    css = (UI / "style.css").read_text()
    block = re.search(r"\n\.popover \{(.*?)\}", css, re.S)
    assert block, "could not find the .popover rule in ui/style.css"
    assert "position: fixed" in block.group(1)
    assert "position: absolute" not in block.group(1)
    assert "overflow-y: auto" in block.group(1)


# -- choosing WHICH mana ability (API2) -------------------------------------


def test_choosing_which_mana_ability_is_also_a_card_scoped_popover():
    """Pinning a source with more than one mana ability asks *which* one
    before it asks which colour - a choice about a card, so the same
    anchored popover the colour choice already uses."""
    assert call('m.surfaceForChoice("mana_ability")') == "popover"


def test_mana_ability_items_show_each_abilitys_own_text_keyed_by_its_index():
    """Eldrazi Temple: the restricted {C}{C} has to be identifiable from the
    menu, which means the item's label is the ability's whole printed text -
    the restriction sentence included - and its key is the ability's own
    index, the one that goes on the wire."""
    abilities = [
        {"index": 0, "text": "{T}: Add {C}.", "cost": "{T}"},
        {
            "index": 1,
            "text": "{T}: Add {C}{C}. Spend this mana only to cast Eldrazi spells.",
            "cost": "{T}",
        },
    ]
    assert call("m.manaAbilityMenuItems(abilities)", abilities=abilities) == [
        {"key": "ability:0", "label": "{T}: Add {C}.", "sublabel": ""},
        {
            "key": "ability:1",
            "label": "{T}: Add {C}{C}. Spend this mana only to cast Eldrazi spells.",
            "sublabel": "",
        },
    ]


def test_a_mana_ability_whose_text_does_not_open_with_its_cost_keeps_it_dim():
    abilities = [{"index": 2, "text": "Add {G}{G}.", "cost": "{T}, Sacrifice this land"}]
    assert call("m.manaAbilityMenuItems(abilities)", abilities=abilities) == [
        {"key": "ability:2", "label": "Add {G}{G}.", "sublabel": "{T}, Sacrifice this land"},
    ]
