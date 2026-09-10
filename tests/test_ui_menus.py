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

from conftest import add_card, find, state
from ui_node import requires_node, run_module

from magic_cards_model_api.store import store

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


def test_crew_picked_from_a_menu_stages_in_the_ribbon():
    """Crew is not a plan a click produces - it is a key picked out of a
    Vehicle's own menu - and what it begins is the in-flight announcement,
    which is the ribbon panel's, not a menu's. The row survives the confirm
    dialog unchanged: `cast` and `activate` open a confirm *first* and stage
    in the ribbon only once it is picked."""
    assert call('m.choiceForPlan({kind: "crew"})') == {
        "kind": "announcement",
        "surface": "ribbon",
    }


def test_every_action_click_opens_a_confirm_and_a_selection_click_opens_nothing():
    """T5, and the literal inverse of what this case used to assert. A click
    that *acts* - playing a land, casting, activating the one ability a
    permanent has - no longer fires on the click: it opens the same anchored
    picker a multi-ability permanent already opened, with one option in it,
    and that single-option popover IS the confirm.

    A click that *selects* is untouched, and that is the other half of the
    contract: picking a target, cancelling, toggling a blocker into the
    combat draft and a click on a card with nothing to do all still open
    nothing at all. A confirm on a target pick would make a three-target
    spell a nine-click affair."""
    acting = ["land", "cast", "activate"]
    assert (
        call("kinds.map((k) => m.choiceForPlan({kind: k}).surface)", kinds=acting)
        == ["popover"] * 3
    )
    assert call('m.choiceForPlan({kind: "land"})') == {
        "kind": "hand_actions",
        "surface": "popover",
    }
    assert call('m.choiceForPlan({kind: "cast"})') == {
        "kind": "hand_actions",
        "surface": "popover",
    }
    assert call('m.choiceForPlan({kind: "activate"})') == {
        "kind": "abilities",
        "surface": "popover",
    }
    for kind in ("target", "cancel", "block", "none"):
        assert call("m.choiceForPlan({kind: k})", k=kind) is None


def test_a_play_permission_is_an_action_click_and_names_the_same_popover():
    """A dynamic play permission (cascade's hit, suspend's own third ability)
    begins a cast out of exile or a graveyard, so it ACTS - and it is wired
    to the board itself (`#permitted-row`, the zone ribbon) as much as to the
    zones modal. It gets the confirm every other action click gets, on the
    surface a card-scoped choice already had."""
    assert call('m.choiceForPlan({kind: "permission"})') == {
        "kind": "hand_actions",
        "surface": "popover",
    }


# ------------------------------------------------------------ menu items ---


def test_a_land_with_cycling_offers_play_first_then_cycle():
    # `index` is what `serialize._special_actions` actually prints on a
    # cycling offer, and it is the `ability_index` the engine's own
    # `activate_off_zone` row carries - so the item can name the row it is.
    offer = {"index": 0, "keyword": "Cycling", "cost": "{2}"}
    plan = {"kind": "menu", "isLand": True, "specials": [], "cycling": [offer]}
    card = {"id": "c1", "name": "Lonely Sandbar", "special_actions": {"cycling": [offer]}}
    assert call("m.handMenuItems(plan, card)", plan=plan, card=card) == [
        {
            "key": "play",
            "label": "Play",
            "sublabel": "",
            "match": {"card_id": "c1", "kind": "play_land", "ability_index": None},
        },
        {
            "key": "cycle",
            "label": "Cycle",
            "sublabel": "{2}",
            "match": {"card_id": "c1", "kind": "activate_off_zone", "ability_index": 0},
        },
    ]


def test_a_spell_with_a_special_action_offers_cast_first_then_the_special():
    plan = {"kind": "menu", "isLand": False, "specials": ["suspend"], "cycling": []}
    card = {"id": "c2", "name": "Rift Bolt", "special_actions": {"suspend": {"cost": "{R}"}}}
    assert call("m.handMenuItems(plan, card)", plan=plan, card=card) == [
        {
            "key": "cast",
            "label": "Cast",
            "sublabel": "",
            "match": {"card_id": "c2", "kind": "cast", "ability_index": None},
        },
        {
            "key": "special:suspend",
            "label": "Suspend",
            "sublabel": "{R}",
            "match": {"card_id": "c2", "kind": "suspend", "ability_index": None},
        },
    ]


def test_a_permission_offers_one_cast_item_keyed_to_the_engines_own_row():
    """One option, which IS the confirm - and it names the row
    `legal_actions` emits for a permission cast: the card, plain `cast`, and
    no ability index. So the same enrichment every other action menu gets
    finds it."""
    permission = {
        "card": {"id": "x1", "name": "Tarmogoyf"},
        "granted_by": "Bloodbraid Elf",
        "from_zone": "exile",
        "action": "cast",
        "usable_now": True,
    }
    assert call("m.permissionMenuItems(permission)", permission=permission) == [
        {
            "key": "cast",
            "label": "Cast",
            "sublabel": "Bloodbraid Elf",
            "match": {"card_id": "x1", "kind": "cast", "ability_index": None},
        }
    ]


def test_a_permission_naming_no_card_still_builds_a_menu_that_enriches_nothing():
    """`match: null` is "no row in the engine's answer identifies this", the
    same answer Crew gets - never a match that would silently disable an
    option because the id was missing."""
    permission = {"card": {}, "from_zone": "graveyard"}
    assert call("m.permissionMenuItems(permission)", permission=permission) == [
        {"key": "cast", "label": "Cast", "sublabel": "", "match": None}
    ]


def test_ability_items_carry_their_index_and_a_vehicle_adds_crew_last():
    plan = {
        "kind": "menu",
        "abilities": [
            {"index": 0, "text": "{T}: Add {G}.", "cost": "{T}", "is_mana_ability": True},
            {"index": 2, "text": "{2}: Draw a card.", "cost": None, "is_mana_ability": False},
        ],
        "vehicle": True,
    }
    card = {"id": "p1", "name": "Vehicle of Some Kind", "abilities": plan["abilities"]}
    assert call("m.abilityMenuItems(plan, card)", plan=plan, card=card) == [
        {
            "key": "activate:0",
            "label": "{T}: Add {G}.",
            "sublabel": "",
            "match": {"card_id": "p1", "kind": "activate_mana", "ability_index": 0},
        },
        {
            "key": "activate:2",
            "label": "{2}: Draw a card.",
            "sublabel": "",
            "match": {"card_id": "p1", "kind": "activate", "ability_index": 2},
        },
        # Crew is the one item with no row in the engine's enumeration to
        # match: `ActionKind` has no `crew`, so it is never enriched and
        # never wrongly disabled.
        {"key": "crew", "label": "Crew", "sublabel": "", "match": None},
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
    card = {"id": "wb", "name": "Walking Ballista", "abilities": plan["abilities"]}
    assert call("m.abilityMenuItems(plan, card)", plan=plan, card=card) == [
        {
            "key": "activate:1",
            "label": text,
            "sublabel": "",
            "match": {"card_id": "wb", "kind": "activate", "ability_index": 1},
        },
        {
            "key": "activate:3",
            "label": "Draw a card.",
            "sublabel": "{2}, {T}",
            "match": {"card_id": "wb", "kind": "activate", "ability_index": 3},
        },
    ]


def test_mana_and_defender_items():
    """Neither of these is an action menu: pinning a mana source and aiming
    an attacker are selections inside a flow that has already begun, so
    neither carries a `match` and neither is ever enriched."""
    assert call('m.manaMenuItems(["R", "G"])') == [
        {"key": "colour:R", "label": "Tap for {R}", "sublabel": "", "match": None},
        {"key": "colour:G", "label": "Tap for {G}", "sublabel": "", "match": None},
    ]
    defenders = [{"id": "p1", "label": "claude"}, {"id": "pw", "label": "Jace"}]
    assert call("m.defenderMenuItems(defenders)", defenders=defenders) == [
        {"key": "defender:p1", "label": "Attack claude", "sublabel": "", "match": None},
        {"key": "defender:pw", "label": "Attack Jace", "sublabel": "", "match": None},
    ]
    abilities = [{"index": 0, "text": "{T}: Add {C}.", "cost": "{T}"}]
    assert call("m.manaAbilityMenuItems(abilities)", abilities=abilities) == [
        {"key": "ability:0", "label": "{T}: Add {C}.", "sublabel": "", "match": None},
    ]


# ---------------------------------------------- the one-option confirm ---


def test_a_plain_land_click_offers_exactly_one_option_and_it_is_play():
    """`planHandCardClick`'s `{kind: "land"}` used to reach `playLand`
    directly. It builds the same one-item menu every other hand click builds
    now - no separate confirm component, no Cancel item (`showPopover`
    already leaves on Escape, on a click away, and on a pick)."""
    card = {"id": "land1", "name": "Mountain", "type_line": "Basic Land - Mountain"}
    assert call('m.handMenuItems({kind: "land"}, card)', card=card) == [
        {
            "key": "play",
            "label": "Play",
            "sublabel": "",
            "match": {"card_id": "land1", "kind": "play_land", "ability_index": None},
        },
    ]


def test_a_plain_cast_click_offers_exactly_one_option_and_it_is_cast():
    card = {"id": "mm", "name": "Memnite", "type_line": "Artifact Creature"}
    assert call('m.handMenuItems({kind: "cast"}, card)', card=card) == [
        {
            "key": "cast",
            "label": "Cast",
            "sublabel": "",
            "match": {"card_id": "mm", "kind": "cast", "ability_index": None},
        },
    ]


def test_a_one_ability_permanent_offers_that_ability_read_off_the_card():
    """The `{kind: "activate", abilityIndex}` plan names no ability list of
    its own - the one ability is read off the card here, so the confirm says
    what it is about to do rather than "OK"."""
    card = {
        "id": "pw1",
        "name": "Test Planeswalker",
        "abilities": [{"index": 0, "text": "+1: draw a card", "is_mana_ability": False}],
    }
    assert call('m.abilityMenuItems({kind: "activate", abilityIndex: 0}, card)', card=card) == [
        {
            "key": "activate:0",
            "label": "+1: draw a card",
            "sublabel": "",
            "match": {"card_id": "pw1", "kind": "activate", "ability_index": 0},
        },
    ]


def test_a_one_ability_permanent_whose_ability_is_a_mana_ability_matches_activate_mana():
    card = {
        "id": "b1",
        "name": "Mountain",
        "abilities": [{"index": 0, "text": "{T}: Add {R}.", "is_mana_ability": True}],
    }
    items = call('m.abilityMenuItems({kind: "activate", abilityIndex: 0}, card)', card=card)
    assert items[0]["match"] == {"card_id": "b1", "kind": "activate_mana", "ability_index": 0}


# ------------------------------------------------------- enrichment ---
#
# `GET .../legal-actions` is asked for on the click, in parallel with the
# menu opening, and folded into the menu that is already on screen. The
# engine's own `affordable`/`unaffordable_reason` is what it carries: CR
# 118.6 makes an unpayable cast a legal action, so the row is there either
# way and the menu says which it is rather than hiding it.


def action(**fields):
    row = {
        "kind": "cast",
        "card_id": "mm",
        "ability_index": None,
        "affordable": "yes",
        "unaffordable_reason": None,
    }
    row.update(fields)
    return row


ITEMS = [
    {
        "key": "cast",
        "label": "Cast",
        "sublabel": "",
        "match": {"card_id": "mm", "kind": "cast", "ability_index": None},
    },
]


def test_an_unaffordable_row_disables_its_item_and_prints_the_engines_own_reason():
    rows = [action(affordable="no", unaffordable_reason="not enough mana: needs {R}{R}")]
    assert call("m.enrichItems(items, rows)", items=ITEMS, rows=rows) == [
        {
            "key": "cast",
            "label": "Cast",
            "sublabel": "not enough mana: needs {R}{R}",
            "match": ITEMS[0]["match"],
            "disabled": True,
        },
    ]


def test_an_affordable_row_leaves_its_item_exactly_as_it_opened():
    assert call("m.enrichItems(items, rows)", items=ITEMS, rows=[action()]) == ITEMS


def test_an_unknown_affordability_is_not_a_no_and_leaves_the_item_alone():
    """A modal or X spell before its parameters are announced reports
    `"unknown"`, which is a real, distinct answer - folding it into "no"
    would grey out an option the player can in fact take."""
    rows = [action(affordable="unknown", unaffordable_reason="X is not announced yet")]
    assert call("m.enrichItems(items, rows)", items=ITEMS, rows=rows) == ITEMS


def test_a_row_for_another_card_or_another_ability_never_touches_this_item():
    rows = [
        action(card_id="other", affordable="no", unaffordable_reason="wrong card"),
        action(kind="play_land", affordable="no", unaffordable_reason="wrong kind"),
        action(
            kind="activate",
            ability_index=2,
            affordable="no",
            unaffordable_reason="wrong index",
        ),
    ]
    assert call("m.enrichItems(items, rows)", items=ITEMS, rows=rows) == ITEMS


def test_an_item_the_enumeration_says_nothing_about_is_left_alone():
    """Crew carries no `match` at all, and an empty answer says nothing
    about anything - neither is evidence that an option is unavailable."""
    items = ITEMS + [{"key": "crew", "label": "Crew", "sublabel": "", "match": None}]
    assert call("m.enrichItems(items, rows)", items=items, rows=[]) == items


def test_a_second_affordable_row_for_the_same_item_keeps_it_enabled():
    """Evoke: one card, two `cast` rows - the printed cost unaffordable, the
    alternative cost payable. The menu's single Cast item begins the
    announcement where that alternative is chosen, so disabling it because
    the *first* matching row said no would take away a cast the player can
    actually make."""
    rows = [
        action(affordable="no", unaffordable_reason="not enough mana", alternative_cost=None),
        action(affordable="yes", alternative_cost="Evoke"),
    ]
    assert call("m.enrichItems(items, rows)", items=ITEMS, rows=rows) == ITEMS


def test_an_unaffordable_row_with_no_reason_keeps_the_sublabel_it_opened_with():
    items = [{**ITEMS[0], "sublabel": "{2}{R}"}]
    rows = [action(affordable="no", unaffordable_reason=None)]
    assert call("m.enrichItems(items, rows)", items=items, rows=rows) == [
        {**items[0], "disabled": True},
    ]


def test_enriching_never_reorders_or_drops_an_item():
    items = [
        {"key": "cast", "label": "Cast", "sublabel": "", "match": ITEMS[0]["match"]},
        {
            "key": "special:suspend",
            "label": "Suspend",
            "sublabel": "{R}",
            "match": {"card_id": "mm", "kind": "suspend", "ability_index": None},
        },
        {"key": "crew", "label": "Crew", "sublabel": "", "match": None},
    ]
    rows = [action(kind="suspend", affordable="no", unaffordable_reason="no mana")]
    got = call("m.enrichItems(items, rows)", items=items, rows=rows)
    assert [i["key"] for i in got] == ["cast", "special:suspend", "crew"]
    assert [bool(i.get("disabled")) for i in got] == [False, True, False]


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
        {"key": "ability:0", "label": "{T}: Add {C}.", "sublabel": "", "match": None},
        {
            "key": "ability:1",
            "label": "{T}: Add {C}{C}. Spend this mana only to cast Eldrazi spells.",
            "sublabel": "",
            "match": None,
        },
    ]


def test_a_mana_ability_whose_text_does_not_open_with_its_cost_keeps_it_dim():
    abilities = [{"index": 2, "text": "Add {G}{G}.", "cost": "{T}, Sacrifice this land"}]
    assert call("m.manaAbilityMenuItems(abilities)", abilities=abilities) == [
        {
            "key": "ability:2",
            "label": "Add {G}{G}.",
            "sublabel": "{T}, Sacrifice this land",
            "match": None,
        },
    ]


# ------------------------- the enrichment, against the real endpoint ---
#
# Everything above hands `enrichItems` rows written by hand. The matching
# rule (`card_id` + `kind` + `ability_index`) is only worth anything if those
# three fields line up with what the two REAL surfaces emit - the board view
# the menu is built from (`GET /api/games/{id}`) and the enumeration it is
# enriched from (`GET /api/games/{id}/legal-actions`). A card id that is a
# dashed uuid on one side and something else on the other would make every
# match silently miss, and nothing in a hand-written row could catch it. So
# these two build a real board through the real API and run the real
# `menus.js` over both answers, unedited.


def _seat_uid(game_id: str, player: str) -> str:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}[player]


def _legal_actions(client, game_id: str, player: str = "alice") -> list[dict]:
    response = client.get(
        f"/api/games/{game_id}/legal-actions",
        params={"player": player, "uid": _seat_uid(game_id, player)},
    )
    assert response.status_code == 200, response.text
    return response.json()["actions"]


def _cast_menu(card: dict, rows: list[dict]) -> list[dict]:
    items = call('m.handMenuItems({kind: "cast"}, card)', card=card)
    return call("m.enrichItems(items, rows)", items=items, rows=rows)


def test_a_real_unpayable_cast_greys_out_the_real_menu_it_opened(client, game):
    """Lightning Bolt in hand and not a land on the battlefield. The engine
    still lists the cast - an unpayable cost does not make it illegal - and
    the one-option confirm the click opened says why it will not work."""
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")
    rows = _legal_actions(client, game)

    (cast_row,) = [r for r in rows if r["kind"] == "cast" and r["card_id"] == bolt["id"]]
    assert cast_row["affordable"] == "no"

    (item,) = _cast_menu(bolt, rows)
    assert item["disabled"] is True
    assert item["sublabel"] == cast_row["unaffordable_reason"]
    assert item["sublabel"], "the engine's own reason is what the option prints"


def test_a_real_payable_cast_leaves_the_real_menu_exactly_as_it_opened(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = find(state(client, game)["hands"]["alice"], "Lightning Bolt")
    rows = _legal_actions(client, game)

    (cast_row,) = [r for r in rows if r["kind"] == "cast" and r["card_id"] == bolt["id"]]
    assert cast_row["affordable"] == "yes"

    (item,) = _cast_menu(bolt, rows)
    assert "disabled" not in item
    assert item == {
        "key": "cast",
        "label": "Cast",
        "sublabel": "",
        "match": {"card_id": bolt["id"], "kind": "cast", "ability_index": None},
    }


def test_a_real_battlefield_abilitys_own_index_is_the_one_the_menu_matches_on(client, game):
    """The other half of the matching rule: a permanent's ability menu keys
    on the ability index the board view printed, and that is the number the
    enumeration's `activate_mana` row carries for the same ability."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    mountain = find(state(client, game)["battlefield"], "Mountain")
    rows = _legal_actions(client, game)

    items = call('m.abilityMenuItems({kind: "activate", abilityIndex: 0}, card)', card=mountain)
    (item,) = items
    matching = [
        r
        for r in rows
        if r["card_id"] == item["match"]["card_id"]
        and r["kind"] == item["match"]["kind"]
        and r["ability_index"] == item["match"]["ability_index"]
    ]
    assert len(matching) == 1, [r for r in rows if r["card_id"] == mountain["id"]]
    assert matching[0]["kind"] == "activate_mana"
    assert call("m.enrichItems(items, rows)", items=items, rows=rows) == items
