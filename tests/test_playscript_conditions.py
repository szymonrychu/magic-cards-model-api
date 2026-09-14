"""Conditions: a pure `evaluate(script, view, viewer)` over the REDACTED view.

A condition can only ever read what `visibility.redact_view` hands the seat,
so a script cannot leak hidden information by construction: an opponent's
hand, library or sideboard is read only through what the seat knows
(`known_cards`), and the seat's own library and sideboard only as the
unordered multiset `include=contents` returns. The permutation property at
the bottom proves no condition reads library order.
"""

from __future__ import annotations

import copy
import random
from typing import Any

import pytest
from conftest import add_card, find, force_turn_position, state
from magic_cards_model.machines import Step
from playscript_testkit import (
    FETCH_SHOCK,
    bolt,
    card,
    contents,
    fetchland,
    mountain,
    play,
    state_view,
)

from magic_cards_model_api.playscripts.conditions import (
    ContentsRequired,
    evaluate,
    needs_contents,
    needs_hypothetical,
)
from magic_cards_model_api.playscripts.inputs import input_catalog
from magic_cards_model_api.playscripts.selectors import HypotheticalRequired, matches
from magic_cards_model_api.serialize import state_view as live_state_view
from magic_cards_model_api.store import store
from magic_cards_model_api.visibility import redact_view

HOLDS = {
    "applies": True,
    "failing_index": None,
    "failing_list": None,
    "seen": None,
    "needs_input": None,
    "invalid_input": None,
}


def count(
    zone: str, side: str, op: str, n: int, select: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "kind": "count",
        "zone": zone,
        "side": side,
        "op": op,
        "n": n,
        "select": select or {},
        **extra,
    }


def script(true: list[Any] | None = None, false: list[Any] | None = None) -> dict[str, Any]:
    """A play whose conditions are exactly these - none inherited from the
    worked example it is built on."""
    return play(true=list(true or []), false=list(false or []))


def only(
    condition: dict[str, Any], view: dict[str, Any], *, where: str = "true", **kw: Any
) -> dict[str, Any]:
    return evaluate(play(**{"true": [], "false": [], where: [condition]}), view, "alice", **kw)


def seen(condition: dict[str, Any], view: dict[str, Any], **kw: Any) -> Any:
    """What a condition observed: it is placed in `false`, so a holding
    condition fails there and reports its observation."""
    result = only(condition, view, where="false", **kw)
    return result["seen"] if not result["applies"] else only(condition, view, **kw)["seen"]


def holds(condition: dict[str, Any], view: dict[str, Any], **kw: Any) -> bool:
    return only(condition, view, **kw)["applies"]


# -- the worked example, and the result shape ----------------------------------


def test_the_fetch_shock_example_applies_on_a_healthy_board():
    view = state_view(battlefield=[fetchland()], life={"alice": 20}, library_count={"alice": 40})
    assert evaluate(FETCH_SHOCK, view, "alice") == HOLDS


def test_the_first_failing_true_condition_is_named_with_what_was_seen():
    view = state_view(battlefield=[fetchland()], life={"alice": 5})
    assert evaluate(FETCH_SHOCK, view, "alice") == {
        "applies": False,
        "failing_index": 1,
        "failing_list": "true",
        "seen": 5,
        "needs_input": None,
        "invalid_input": None,
    }


def test_a_false_condition_that_holds_stops_the_script():
    view = state_view(battlefield=[fetchland()], library_count={"alice": 3})
    assert evaluate(FETCH_SHOCK, view, "alice") == {
        "applies": False,
        "failing_index": 0,
        "failing_list": "false",
        "seen": 3,
        "needs_input": None,
        "invalid_input": None,
    }


def test_true_is_checked_before_false():
    view = state_view(battlefield=[], library_count={"alice": 3})
    result = evaluate(FETCH_SHOCK, view, "alice")
    assert (result["failing_list"], result["failing_index"], result["seen"]) == ("true", 0, 0)


def test_a_play_model_evaluates_like_its_dict():
    from magic_cards_model_api.playscripts.schema import Play

    view = state_view(battlefield=[fetchland()])
    assert evaluate(Play.model_validate(FETCH_SHOCK), view, "alice") == HOLDS


def test_evaluate_does_not_mutate_the_view():
    view = state_view(battlefield=[fetchland()], hands={"alice": [bolt()]})
    before = copy.deepcopy(view)
    evaluate(FETCH_SHOCK, view, "alice")
    assert view == before


# -- count over public zones -----------------------------------------------------


def test_count_on_the_battlefield_reads_the_controller_by_side():
    view = state_view(
        battlefield=[fetchland(), mountain(), mountain(controller="bob"), fetchland(tapped=True)]
    )
    lands = {"types_all": ["Land"]}
    assert seen(count("battlefield", "me", ">=", 1, lands), view) == 3
    assert seen(count("battlefield", "opponent", ">=", 1, lands), view) == 1
    assert seen(count("battlefield", "any", ">=", 1, lands), view) == 4
    assert (
        seen(count("battlefield", "me", ">=", 1, {"tapped": False, "types_all": ["Land"]}), view)
        == 2
    )


def test_count_in_graveyards_exile_and_on_the_stack():
    spell = bolt(zone="stack", controller="bob")
    spell["kind"] = "spell"
    view = state_view(
        graveyards={
            "alice": [bolt(zone="graveyard")],
            "bob": [
                bolt(zone="graveyard", controller="bob"),
                mountain(zone="graveyard", controller="bob"),
            ],
        },
        exile=[bolt(zone="exile"), bolt(zone="exile", controller="bob")],
        stack=[spell],
    )
    instants = {"types_any": ["Instant"]}
    assert seen(count("graveyard", "me", ">=", 9, instants), view) == 1
    assert seen(count("graveyard", "opponent", ">=", 9, {}), view) == 2
    assert seen(count("exile", "any", ">=", 9, instants), view) == 2
    assert seen(count("exile", "opponent", ">=", 9, instants), view) == 1
    assert seen(count("stack", "opponent", ">=", 9, instants), view) == 1
    assert seen(count("stack", "me", ">=", 9, instants), view) == 0


@pytest.mark.parametrize(
    ("op", "n", "expected"),
    [
        (">=", 2, True),
        ("<=", 1, False),
        ("==", 2, True),
        ("!=", 2, False),
        (">", 1, True),
        ("<", 3, True),
    ],
)
def test_every_op_compares(op, n, expected):
    view = state_view(battlefield=[mountain(), mountain()])
    assert holds(count("battlefield", "me", op, n), view) is expected


# -- hidden zones: hand, library, sideboard --------------------------------------


def test_my_hand_is_read_from_my_own_hand():
    view = state_view(hands={"alice": [bolt(), mountain(zone="hand")]})
    assert seen(count("hand", "me", ">=", 9, {"types_any": ["Instant"]}), view) == 1


def test_the_opponents_hand_is_read_only_through_known_cards():
    known_bolt = bolt(controller="bob")
    view = state_view(
        hands={"alice": [], "bob": [bolt(controller="bob"), bolt(controller="bob")]},
        known={"hand": [known_bolt]},
    )
    instants = {"types_any": ["Instant"]}
    assert seen(count("hand", "opponent", ">=", 9, instants), view) == 1, (
        "hands['bob'] is never read, even when something put cards there"
    )


def test_a_known_card_in_my_own_hand_entry_is_not_counted_as_the_opponents():
    view = state_view(known={"hand": [bolt(controller="carol")]}, opponent="bob")
    assert seen(count("hand", "opponent", ">=", 9, {}), view) == 0


def test_my_library_is_the_unordered_contents_multiset():
    lib = contents(
        (mountain(zone="library"), 12), (bolt(zone="library"), 4), (fetchland(zone="library"), 3)
    )
    view = state_view(library_contents=lib)
    assert seen(count("library", "me", ">=", 99, {"types_all": ["Land"]}), view) == 15
    assert seen(count("library", "me", ">=", 99, {"subtypes_any": ["Mountain"]}), view) == 12
    assert seen(count("library", "me", ">=", 99, {}), view) == 19


def test_known_only_on_my_library_reads_only_what_i_know_of_it():
    lib = contents((mountain(zone="library"), 12), (bolt(zone="library"), 4))
    view = state_view(
        library_contents=lib,
        known={"library": [{"index_from_top": 0, "card": bolt(zone="library")}]},
    )
    assert seen(count("library", "me", ">=", 99, {}, known_only=True), view) == 1


def test_the_opponents_library_is_read_only_through_known_cards():
    view = state_view(
        library_contents=contents((mountain(zone="library"), 20)),
        known={
            "library": [
                {"index_from_top": 0, "card": bolt(zone="library", controller="bob")},
                {"index_from_top": 3, "card": mountain(zone="library", controller="bob")},
                {"index_from_top": 1, "card": bolt(zone="library")},
            ]
        },
    )
    view["library_contents"]["bob"] = contents((bolt(zone="library", controller="bob"), 30))
    assert seen(count("library", "opponent", ">=", 99, {}), view) == 2
    assert seen(count("library", "opponent", ">=", 99, {"types_any": ["Instant"]}), view) == 1


def test_my_sideboard_is_the_contents_multiset_and_the_opponents_only_known_cards():
    view = state_view(
        sideboard_contents=contents((bolt(zone="sideboard"), 3)),
        known={"sideboard": [mountain(zone="sideboard", controller="bob")]},
    )
    assert seen(count("sideboard", "me", ">=", 99, {}), view) == 3
    assert seen(count("sideboard", "opponent", ">=", 99, {}), view) == 1


@pytest.mark.parametrize("zone", ["library", "sideboard"])
def test_my_library_or_sideboard_without_contents_cannot_be_evaluated(zone):
    view = state_view()
    with pytest.raises(ContentsRequired):
        evaluate(play(true=[count(zone, "me", ">=", 1)]), view, "alice")


def test_needs_contents_tells_the_caller_when_include_contents_is_required():
    assert not needs_contents(FETCH_SHOCK)
    assert needs_contents(play(true=[count("library", "me", ">=", 1)]))
    assert needs_contents(play(false=[count("sideboard", "me", ">=", 1)]))
    assert not needs_contents(play(true=[count("library", "me", ">=", 1, known_only=True)]))
    assert not needs_contents(play(true=[count("library", "opponent", ">=", 1)]))
    assert not needs_contents(play(true=[count("hand", "me", ">=", 1)]))
    assert not needs_contents(
        play(true=[{"kind": "zone_size", "zone": "library", "side": "me", "op": ">=", "n": 1}])
    )


# -- zone_size, life, stack_top, land_drop_available, untapped_mana ----------------


def test_zone_size_reads_the_public_counts():
    view = state_view(
        hand_count={"alice": 3, "bob": 5},
        library_count={"alice": 40, "bob": 38},
        graveyard_count={"alice": 2, "bob": 0},
        sideboard_count={"alice": 15, "bob": 14},
        exile=[
            bolt(zone="exile"),
            bolt(zone="exile", controller="bob"),
            bolt(zone="exile", controller="bob"),
        ],
    )

    def size(zone: str, side: str) -> Any:
        return seen({"kind": "zone_size", "zone": zone, "side": side, "op": ">=", "n": 999}, view)

    assert size("hand", "opponent") == 5
    assert size("library", "me") == 40
    assert size("graveyard", "any") == 2
    assert size("sideboard", "opponent") == 14
    assert size("exile", "opponent") == 2
    assert size("hand", "any") == 8


def test_life_by_side():
    view = state_view(life={"alice": 12, "bob": 4})
    assert seen({"kind": "life", "side": "me", "op": ">=", "n": 99}, view) == 12
    assert seen({"kind": "life", "side": "opponent", "op": ">=", "n": 99}, view) == 4
    assert holds({"kind": "life", "side": "opponent", "op": "<=", "n": 5}, view)
    assert holds({"kind": "life", "side": "any", "op": "<=", "n": 5}, view), (
        "any: some player's life"
    )
    assert not holds({"kind": "life", "side": "any", "op": ">", "n": 12}, view)
    assert seen({"kind": "life", "side": "any", "op": ">", "n": 12}, view) == {
        "alice": 12,
        "bob": 4,
    }


def test_stack_top_reads_only_the_top_object_and_its_controller():
    top = bolt(zone="stack", controller="bob")
    top["kind"] = "spell"
    under = card(
        "Bear", type_line="Creature — Bear", zone="stack", controller="alice", power=2, toughness=2
    )
    under["kind"] = "spell"
    view = state_view(stack=[top, under])
    condition = {
        "kind": "stack_top",
        "controller": "opponent",
        "select": {"types_any": ["Instant"]},
    }
    assert holds(condition, view)
    assert not holds({**condition, "controller": "me"}, view)
    assert not holds(
        {"kind": "stack_top", "controller": "me", "select": {"types_any": ["Creature"]}}, view
    )
    assert seen(condition, view) == {"name": "Lightning Bolt", "controller": "bob", "kind": "spell"}


def test_stack_top_on_an_empty_stack_does_not_hold():
    result = only({"kind": "stack_top", "controller": "opponent", "select": {}}, state_view())
    assert (result["applies"], result["seen"]) == (False, None)


def test_land_drop_available_on_my_turn_with_a_drop_left():
    drop = {"kind": "land_drop_available", "value": True}
    no_drop = {"kind": "land_drop_available", "value": False}
    assert holds(drop, state_view(active="alice"))
    assert not holds(no_drop, state_view(active="alice"))
    assert not holds(drop, state_view(active="alice", lands_played={"alice": 1}))
    assert holds(
        drop, state_view(active="alice", lands_played={"alice": 1}, land_allowance={"alice": 2})
    )
    assert not holds(drop, state_view(active="bob")), "no land drop on the opponent's turn"
    assert holds(no_drop, state_view(active="bob"))
    assert seen(drop, state_view(active="alice")) is True


def test_untapped_mana_counts_untapped_mana_sources_i_control():
    view = state_view(
        battlefield=[mountain(), mountain(tapped=True), mountain(controller="bob"), fetchland()]
    )
    assert seen({"kind": "untapped_mana", "op": ">=", "n": 99}, view) == 1


# -- turn ---------------------------------------------------------------------


def turn(op: str, n: int) -> dict[str, Any]:
    return {"kind": "turn", "op": op, "n": n}


def test_turn_reads_turn_number():
    assert holds(turn("==", 1), state_view(turn_number=1))
    assert not holds(turn("==", 2), state_view(turn_number=1))
    assert seen(turn("<=", 2), state_view(turn_number=5)) == 5


def test_turn_holds_for_my_nth_turn_on_the_play_and_on_the_draw():
    # TurnMachine.turn_number counts rounds of the table (magic_cards_model.
    # machines.turn.TurnMachine.begin_next_turn), so it does not advance until
    # the rotation wraps back to seat 0 - meaning both the starting player's
    # first turn (on the play) and the second player's first turn (on the
    # draw) share turn_number 1. See test_turn_number_matches_the_engines_
    # turnmachine_semantics below for the live engine proof.
    on_the_play = state_view(viewer="alice", active="alice", turn_number=1)
    on_the_draw = state_view(viewer="bob", active="bob", turn_number=1)
    assert holds(turn("==", 1), on_the_play)
    assert holds(turn("==", 1), on_the_draw)


def test_turn_condition_does_not_advance_for_an_extra_turn():
    # CR 500.7's extra turn is inserted without incrementing turn_number
    # (TurnMachine.begin_next_turn), so a second consecutive turn for one
    # player still reads the same turn number.
    assert holds(turn("==", 2), state_view(turn_number=2))


def test_a_null_turn_number_never_holds():
    view = state_view(turn_number=None)
    for op in ("<", "<=", "==", "!=", ">", ">="):
        assert not holds(turn(op, 0), view)
    assert seen(turn(">=", 0), view) is None


def test_turn_number_matches_the_engines_turnmachine_semantics(game):
    """Verifies the view's turn_number against the live engine's own
    TurnMachine (not just the condition's reading of it): turn_number counts
    rounds of the table, so it stays 1 for both seats' first turns and only
    becomes 2 once the rotation wraps back to the starting seat; an extra
    turn (CR 500.7) is spliced in without moving it at all."""
    live = store.get(game)
    turn_machine = live.turn
    turn_machine.begin_first_turn()
    assert (turn_machine.active_player, turn_machine.turn_number) == ("alice", 1)
    while turn_machine.active_player == "alice":
        turn_machine.advance()
    assert (turn_machine.active_player, turn_machine.turn_number) == ("bob", 1), (
        "bob's first turn, on the draw, is turn_number 1 too"
    )
    while turn_machine.active_player == "bob":
        turn_machine.advance()
    assert (turn_machine.active_player, turn_machine.turn_number) == ("alice", 2)

    turn_machine.grant_extra_turn("alice")
    turns_taken = turn_machine.turns_taken
    while turn_machine.turns_taken == turns_taken:
        turn_machine.advance()
    assert (turn_machine.active_player, turn_machine.turn_number) == ("alice", 2), (
        "the extra turn does not advance turn_number"
    )


# -- input conditions -------------------------------------------------------------

CATALOG = input_catalog(
    [
        {
            "name": "plan",
            "type": "enum",
            "values": ["aggro", "control"],
            "description": "d",
            "required": False,
        },
        {"name": "hold_up", "type": "bool", "description": "d", "required": False},
        {"name": "clock", "type": "int", "min": 0, "max": 10, "description": "d", "required": True},
    ],
    ["dimir-frog", "living-end"],
)


def inp(name: str, op: str, value: Any) -> dict[str, Any]:
    return {"kind": "input", "name": name, "op": op, "value": value}


@pytest.mark.parametrize(
    ("condition", "inputs", "expected"),
    [
        (inp("plan", "eq", "aggro"), {"plan": "aggro"}, True),
        (inp("plan", "ne", "aggro"), {"plan": "aggro"}, False),
        (inp("plan", "in", ["aggro", "control"]), {"plan": "control"}, True),
        (inp("plan", "not_in", ["aggro"]), {"plan": "control"}, True),
        (inp("hold_up", "eq", True), {"hold_up": False}, False),
        (inp("clock", "<", 3), {"clock": 2}, True),
        (inp("clock", "<=", 3), {"clock": 4}, False),
        (inp("clock", ">", 3), {"clock": 4}, True),
        (inp("clock", ">=", 3), {"clock": 3}, True),
        (inp("opposing_archetype", "eq", "dimir-frog"), {"opposing_archetype": "dimir-frog"}, True),
    ],
)
def test_an_input_condition_reads_the_callers_inputs(condition, inputs, expected):
    assert (
        holds(condition, state_view(), inputs={**{"clock": 5}, **inputs}, catalog=CATALOG)
        is expected
    )


def test_an_input_condition_in_false_must_not_hold():
    result = only(
        inp("plan", "eq", "aggro"),
        state_view(),
        where="false",
        inputs={"plan": "aggro", "clock": 1},
        catalog=CATALOG,
    )
    assert (result["applies"], result["failing_list"], result["seen"]) == (False, "false", "aggro")


def test_a_missing_optional_input_makes_the_condition_not_hold():
    result = only(inp("plan", "eq", "aggro"), state_view(), inputs={}, catalog=CATALOG)
    assert result == {
        "applies": False,
        "failing_index": 0,
        "failing_list": "true",
        "seen": None,
        "needs_input": None,
        "invalid_input": None,
    }
    assert not holds(inp("opposing_archetype", "eq", "*"), state_view(), inputs={}, catalog=CATALOG)
    assert only(
        inp("plan", "eq", "aggro"), state_view(), where="false", inputs={}, catalog=CATALOG
    )["applies"]


def test_a_missing_required_input_makes_the_script_unevaluable():
    for where in ("true", "false"):
        result = only(inp("clock", "<", 3), state_view(), where=where, inputs={}, catalog=CATALOG)
        assert result["needs_input"] == "clock"
        assert result["applies"] is False
        assert result["failing_list"] == where and result["failing_index"] == 0


def test_inputs_default_to_none_and_the_catalog_to_the_built_in():
    assert not only(inp("opposing_archetype", "eq", "*"), state_view())["applies"]
    assert only(
        inp("opposing_archetype", "eq", "*"), state_view(), inputs={"opposing_archetype": "*"}
    )["applies"]


# -- no condition reads library order ------------------------------------------------

BATTERY = [
    script(true=[count("library", "me", ">=", 3, {"types_all": ["Land"]})]),
    script(true=[count("library", "me", "==", 1, {"types_any": ["Instant"]})]),
    script(false=[count("library", "me", ">", 0, {"subtypes_any": ["Forest"]})]),
    script(true=[count("library", "me", ">=", 1, {}, known_only=True)]),
    script(true=[count("library", "opponent", ">=", 1, {"types_any": ["Instant"]})]),
    script(true=[{"kind": "zone_size", "zone": "library", "side": "me", "op": ">=", "n": 5}]),
    script(true=[count("sideboard", "me", ">=", 0, {})]),
    script(
        true=[
            count("library", "me", "<=", 2, {"colors_any": ["R"]}),
            count("library", "any", ">=", 0, {}),
        ]
    ),
]


def test_permuting_a_library_never_changes_an_evaluation():
    alice_lib = [
        mountain(zone="library"),
        bolt(zone="library"),
        fetchland(zone="library"),
        card("Forest", type_line="Basic Land — Forest", zone="library"),
    ]
    view = state_view(
        library_contents=contents(*[(c, i + 1) for i, c in enumerate(alice_lib)]),
        sideboard_contents=contents((bolt(zone="sideboard"), 2)),
        known={
            "library": [
                {"index_from_top": 0, "card": bolt(zone="library", controller="bob")},
                {"index_from_top": 2, "card": mountain(zone="library")},
                {"index_from_top": 5, "card": mountain(zone="library", controller="bob")},
            ]
        },
    )
    baseline = [evaluate(script, view, "alice") for script in BATTERY]
    rng = random.Random(20260913)
    for _ in range(60):
        permuted = copy.deepcopy(view)
        rng.shuffle(permuted["library_contents"]["alice"])
        records = permuted["known_cards"]["alice"]["library"]
        rng.shuffle(records)
        positions = rng.sample(range(40), len(records))
        for record, position in zip(records, positions, strict=True):
            record["index_from_top"] = position
        assert [evaluate(script, permuted, "alice") for script in BATTERY] == baseline


# -- the live serializer: the same shapes, from a real game ----------------------------


def uid_for(game_id: str, player: str) -> str:
    return next(uid for uid, who in store.seat_uids(game_id).items() if who == player)


def test_permuting_a_real_engine_library_never_changes_an_evaluation(client, game):
    for name in ("Forest", "Forest", "Mountain", "Lightning Bolt", "Island", "Mountain"):
        add_card(client, game, name, "alice", "library")
    live = store.get(game)
    alice = next(player for player in live.players if player.username == "alice")
    original = list(alice.library)
    assert len(original) == 6
    rng = random.Random(7)
    results = []
    try:
        for _ in range(10):
            order = original[:]
            rng.shuffle(order)
            alice.library[:] = order
            view = redact_view(live_state_view(live, game, include_contents=True), "alice")
            results.append([evaluate(script, view, "alice") for script in BATTERY])
    finally:
        alice.library[:] = original
    assert all(result == results[0] for result in results)
    assert results[0][0]["applies"] is True, "the 4 real lands in the library were counted"
    assert results[0][1]["applies"] is True, "exactly one real instant"


def test_land_drop_available_reads_the_live_view(client, game):
    uid = uid_for(game, "alice")
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    add_card(client, game, "Forest", "alice", "hand")
    drop = script(true=[{"kind": "land_drop_available", "value": True}])

    before = state(client, game, uid)
    me = next(p for p in before["players"] if p["username"] == "alice")
    assert (me["lands_played"], me["land_allowance"]) == (0, 1)
    assert evaluate(drop, before, "alice")["applies"] is True
    assert evaluate(drop, state(client, game, uid_for(game, "bob")), "bob")["applies"] is False

    forest = find(before["hands"]["alice"], "Forest")["id"]
    played = client.post(
        f"/api/games/{game}/actions/play-land",
        params={"uid": uid},
        json={"player": "alice", "card_id": forest},
    )
    assert played.status_code == 200, played.text

    after = state(client, game, uid)
    me = next(p for p in after["players"] if p["username"] == "alice")
    assert me["lands_played"] == 1
    result = evaluate(drop, after, "alice")
    assert (result["applies"], result["seen"]) == (False, False)


def test_the_live_view_carries_the_engines_colors_and_mana_value(client, game):
    uid = uid_for(game, "alice")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Forest", "alice", "battlefield")
    view = state(client, game, uid)
    bolt_view = find(view["hands"]["alice"], "Lightning Bolt")
    forest_view = find(view["battlefield"], "Forest")
    assert (bolt_view["colors"], bolt_view["mana_value"]) == (["R"], 1)
    assert (forest_view["colors"], forest_view["mana_value"]) == ([], 0)
    assert matches({"colors_any": ["R"], "mana_value": {"op": "==", "n": 1}}, bolt_view)
    assert not matches({"colors_any": ["G"]}, forest_view)
    assert evaluate(
        script(true=[count("hand", "me", "==", 1, {"colors_any": ["red"]})]), view, "alice"
    )["applies"]


# -- supplied inputs are checked against the catalog (battle 1, D04) ---------------

D04_CATALOG = input_catalog(
    [
        {
            "name": "plan_stance",
            "type": "enum",
            "values": ["grind", "race"],
            "description": "d",
            "required": False,
        },
        {"name": "hold_discard", "type": "bool", "description": "d", "required": False},
        {
            "name": "clock",
            "type": "int",
            "min": 0,
            "max": 10,
            "description": "d",
            "required": False,
        },
    ],
    ["esper-blink", "dimir-frog"],
)
VALID_INPUTS = {"opposing_archetype": "esper-blink", "plan_stance": "grind", "clock": 4}


@pytest.mark.parametrize(
    ("supplied", "name", "value"),
    [
        ({"opposing_archetype": "not-a-deck"}, "opposing_archetype", "not-a-deck"),
        ({"plan_stance": "turtle"}, "plan_stance", "turtle"),
        ({"hold_discard": "yes"}, "hold_discard", "yes"),
        ({"clock": 11}, "clock", 11),
        ({"clock": -1}, "clock", -1),
        ({"clock": True}, "clock", True),
        ({"clock": "4"}, "clock", "4"),
        ({"plan_stanse": "grind"}, "plan_stanse", "grind"),
    ],
)
def test_a_supplied_input_outside_the_catalog_is_reported_not_read_as_not_holding(
    supplied, name, value
):
    holding = inp("opposing_archetype", "ne", "dimir-frog")
    result = evaluate(
        script(true=[holding]),
        state_view(),
        "alice",
        inputs={**VALID_INPUTS, **supplied},
        catalog=D04_CATALOG,
    )
    assert result["applies"] is False
    assert (result["failing_list"], result["failing_index"], result["needs_input"]) == (
        None,
        None,
        None,
    )
    invalid = result["invalid_input"]
    assert (invalid["name"], invalid["value"]) == (name, value)
    assert isinstance(invalid["reason"], str) and name in invalid["reason"]


def test_an_invalid_input_is_reported_even_when_no_condition_reads_it():
    result = evaluate(
        script(), state_view(), "alice", inputs={"hold_discard": 1}, catalog=D04_CATALOG
    )
    assert result["applies"] is False
    assert result["invalid_input"]["name"] == "hold_discard"


def test_valid_inputs_report_no_invalid_input():
    result = evaluate(
        script(true=[inp("plan_stance", "eq", "grind"), inp("clock", "<=", 10)]),
        state_view(),
        "alice",
        inputs={**VALID_INPUTS, "hold_discard": False},
        catalog=D04_CATALOG,
    )
    assert result == HOLDS


def test_the_built_in_archetype_is_checked_without_a_declared_catalog():
    result = evaluate(script(), state_view(), "alice", inputs={"opposing_archetype": "dimir-frog"})
    assert result["invalid_input"]["name"] == "opposing_archetype"
    assert evaluate(script(), state_view(), "alice", inputs={"opposing_archetype": "*"}) == HOLDS


def test_a_missing_optional_input_still_does_not_hold_and_is_not_invalid():
    result = evaluate(
        script(true=[inp("hold_discard", "eq", True)]),
        state_view(),
        "alice",
        inputs={"opposing_archetype": "esper-blink"},
        catalog=D04_CATALOG,
    )
    assert (result["applies"], result["failing_list"], result["invalid_input"]) == (
        False,
        "true",
        None,
    )


# -- missing_from in a condition's selector --------------------------------------

TRON = ["Tower", "Mine", "Power-Plant"]
PIECE = {
    "types_all": ["Land"],
    "subtypes_any": TRON,
    "missing_from": {"zones": ["battlefield", "hand"], "side": "me", "by": "subtype"},
}


def tron(piece: str, **kw: Any) -> dict[str, Any]:
    return card(f"Urza's {piece.replace('-', ' ')}", type_line=f"Land — Urza's {piece}", **kw)


def test_a_count_with_missing_from_counts_only_the_pieces_not_already_held():
    stocked = contents(*((tron(piece, zone="library"), 2) for piece in TRON))
    view = state_view(
        battlefield=[tron("Tower")],
        hands={"alice": [tron("Mine", zone="hand")]},
        library_contents=stocked,
    )
    assert seen(count("library", "me", ">=", 1, PIECE), view) == 2
    assert holds(count("library", "me", ">=", 1, PIECE), view)
    assembled = state_view(
        battlefield=[tron("Tower")],
        hands={"alice": [tron("Mine", zone="hand"), tron("Power-Plant", zone="hand")]},
        library_contents=stocked,
    )
    assert seen(count("library", "me", ">=", 1, PIECE), assembled) == 0
    assert not holds(count("library", "me", ">=", 1, PIECE), assembled)


def test_a_stack_top_selector_takes_missing_from():
    top = card("Shock", type_line="Instant", zone="stack", controller="bob")
    view = state_view(stack=[top], graveyards={"alice": [card("Shock", type_line="Instant")]})
    fresh = {"missing_from": {"zones": ["graveyard"], "side": "me", "by": "name"}}
    assert not holds({"kind": "stack_top", "controller": "opponent", "select": fresh}, view)
    assert holds(
        {
            "kind": "stack_top",
            "controller": "opponent",
            "select": {**fresh, "missing_from": {**fresh["missing_from"], "side": "opponent"}},
        },
        view,
    )


def test_needs_contents_when_any_missing_from_reads_my_own_library_or_sideboard():
    def reading(zones: list[str], side: str = "me") -> dict[str, Any]:
        return {"missing_from": {"zones": zones, "side": side, "by": "name"}}

    assert needs_contents(play(true=[count("hand", "me", ">=", 1, reading(["library"]))]))
    assert needs_contents(play(true=[count("hand", "me", ">=", 1, reading(["hand", "sideboard"]))]))
    assert not needs_contents(
        play(true=[count("hand", "me", ">=", 1, reading(["library"], "opponent"))])
    )
    assert not needs_contents(play(true=[count("hand", "me", ">=", 1, reading(["hand"]))]))
    move = copy.deepcopy(FETCH_SHOCK["moves"][0])
    move["answers"][0]["pick"]["prefer"] = [reading(["library"])]
    assert needs_contents(move["answers"][0])
    move["source"]["select"] = reading(["library"])
    assert needs_contents(move)


def test_needs_contents_reads_only_what_the_caller_is_about_to_evaluate():
    """A play's conditions are evaluated without its moves, a move is built
    without its answers, and each answer fetches for itself: a `missing_from`
    over my library in an answer never makes the play or the move fetch."""
    move = copy.deepcopy(FETCH_SHOCK["moves"][0])
    move["answers"][0]["pick"]["prefer"] = [
        {"missing_from": {"zones": ["library"], "side": "me", "by": "name"}}
    ]
    assert needs_contents(move["answers"][0])
    assert not needs_contents(move)
    assert not needs_contents(play(moves=[move]))


# -- O4/O7: needs_hypothetical, and evaluate() threading hypo/untapped_mana -------


def test_needs_hypothetical_tells_the_caller_a_selector_reads_enters_or_unlocks():
    assert not needs_hypothetical(FETCH_SHOCK)
    assert needs_hypothetical(
        play(true=[count("hand", "me", ">=", 1, {"enters_any": ["untapped"]})])
    )
    assert needs_hypothetical(play(true=[count("hand", "me", ">=", 1, {"unlocks": True})]))
    assert needs_hypothetical(
        play(true=[count("hand", "me", ">=", 1, {"unlocks_by_search": True})])
    )
    assert not needs_hypothetical(
        play(true=[count("hand", "me", ">=", 1, {"affordable": {"extra": 0}})])
    )
    assert not needs_hypothetical(play(true=[count("hand", "me", ">=", 1, {"tapped": False})]))


def test_needs_hypothetical_reads_a_stack_top_selector_too():
    top = {
        "kind": "stack_top",
        "controller": "opponent",
        "select": {"unlocks": True},
    }
    assert needs_hypothetical(play(true=[top]))


def test_needs_hypothetical_reads_only_what_the_caller_is_about_to_evaluate():
    move = copy.deepcopy(FETCH_SHOCK["moves"][0])
    move["answers"][0]["pick"]["prefer"] = [{"unlocks": True}]
    assert needs_hypothetical(move["answers"][0])
    assert not needs_hypothetical(move)
    assert not needs_hypothetical(play(moves=[move]))


def test_needs_contents_is_also_true_for_unlocks_by_search():
    assert needs_contents(play(true=[count("hand", "me", ">=", 1, {"unlocks_by_search": True})]))
    assert not needs_contents(play(true=[count("hand", "me", ">=", 1, {"unlocks": True})]))


def test_a_selector_reading_enters_or_unlocks_cannot_be_evaluated_without_hypothetical_facts():
    view = state_view(hands={"alice": [mountain(zone="hand")]})
    doc = play(true=[count("hand", "me", ">=", 1, {"enters_any": ["untapped"]})])
    with pytest.raises(HypotheticalRequired):
        evaluate(doc, view, "alice")


def test_evaluate_passes_hypothetical_facts_through_to_matches():
    land = mountain(zone="hand")
    view = state_view(hands={"alice": [land]})
    doc = play(true=[count("hand", "me", ">=", 1, {"enters_any": ["untapped"]})])
    holding = evaluate(doc, view, "alice", hypothetical={land["id"]: {"enters": "untapped"}})
    assert holding["applies"]
    not_holding = evaluate(doc, view, "alice", hypothetical={land["id"]: {"enters": "tapped"}})
    assert not not_holding["applies"]


def test_evaluate_computes_untapped_mana_for_affordable_selectors():
    spell = bolt(zone="hand")
    source = mountain()
    view = state_view(hands={"alice": [spell]}, battlefield=[source])
    doc = play(true=[count("hand", "me", ">=", 1, {"affordable": {"extra": 0}})])
    assert evaluate(doc, view, "alice")["applies"]
