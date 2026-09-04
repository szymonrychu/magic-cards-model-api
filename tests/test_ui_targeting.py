"""Per-slot targeting: Kozilek's Command's two "target player" modes can both
take the same player (CR 115.3 forbids a repeat only within one instance of
the word "target"). The affordance JSON below is the shape
`GET /affordances` returns for modes 1,2 at X=1 (`tests/test_affordance_modes.py`)."""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

KOZILEK_12 = {
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
            "mode_text": (
                "Target player creates X 0/1 colorless Eldrazi Spawn creature tokens with "
                '"Sacrifice this token: Add {C}."'
            ),
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
        {
            "id": "alice",
            "kind": "player",
            "name": "alice",
            "zone": None,
            "controller": None,
            "requirements": [0, 1],
        },
        {
            "id": "bob",
            "kind": "player",
            "name": "bob",
            "zone": None,
            "controller": None,
            "requirements": [0, 1],
        },
    ],
}

UP_TO_X = {
    "targets_anything": True,
    "understood": True,
    "minimum": 0,
    "maximum": 2,
    "requirements": [
        {
            "phrase": "up to 2 target cards from graveyards",
            "count": 2,
            "minimum": 0,
            "unbounded": False,
            "variable_count": False,
            "mode": 4,
            "mode_text": "Exile up to X target cards from graveyards.",
        }
    ],
    "division": None,
    "candidates": [
        {
            "id": "g1",
            "kind": "card",
            "name": "Memnite",
            "zone": "graveyard",
            "controller": "bob",
            "requirements": [0],
        },
        {
            "id": "g2",
            "kind": "card",
            "name": "Wastes",
            "zone": "graveyard",
            "controller": "bob",
            "requirements": [0],
        },
    ],
}


def test_same_player_fills_both_mode_slots() -> None:
    flat = run_module(
        "targeting.js",
        "(() => { let s = m.slotsFromAffordance(aff); s = m.fill(s, 0, 'alice'); "
        "s = m.fill(s, 1, 'alice'); return [m.flatten(s), m.isSatisfied(s)]; })()",
        aff=KOZILEK_12,
    )
    assert flat == [["alice", "alice"], True]


def test_slot_labels_name_their_mode() -> None:
    labels = run_module(
        "targeting.js", "m.slotsFromAffordance(aff).map(s => s.label)", aff=KOZILEK_12
    )
    assert labels == ["Mode 1: target player", "Mode 2: target player"]


def test_active_slot_advances_as_slots_fill() -> None:
    steps = run_module(
        "targeting.js",
        "(() => { let s = m.slotsFromAffordance(aff); const a = m.activeSlotIndex(s); "
        "s = m.fill(s, 0, 'bob'); const b = m.activeSlotIndex(s); s = m.fill(s, 1, 'bob'); "
        "return [a, b, m.activeSlotIndex(s)]; })()",
        aff=KOZILEK_12,
    )
    assert steps == [0, 1, -1]


def test_a_full_slot_refuses_a_second_target() -> None:
    result = run_module(
        "targeting.js",
        "(() => { let s = m.slotsFromAffordance(aff); s = m.fill(s, 0, 'bob'); "
        "return [m.canFill(aff, s[0], 'alice'), m.flatten(m.fill(s, 0, 'alice'))]; })()",
        aff=KOZILEK_12,
    )
    assert result == [False, ["bob"]]


def test_unfill_removes_one_target() -> None:
    result = run_module(
        "targeting.js",
        "(() => { let s = m.slotsFromAffordance(aff); s = m.fill(s, 0, 'g1'); "
        "s = m.fill(s, 0, 'g2'); s = m.unfill(s, 0, 'g1'); return m.flatten(s); })()",
        aff=UP_TO_X,
    )
    assert result == ["g2"]


def test_up_to_slots_are_satisfied_empty() -> None:
    assert (
        run_module("targeting.js", "m.isSatisfied(m.slotsFromAffordance(aff))", aff=UP_TO_X) is True
    )


def test_a_non_candidate_cannot_fill() -> None:
    assert (
        run_module(
            "targeting.js",
            "m.canFill(aff, m.slotsFromAffordance(aff)[0], 'nobody')",
            aff=KOZILEK_12,
        )
        is False
    )


def test_candidate_ids_follow_the_slot() -> None:
    ids = run_module(
        "targeting.js",
        "[...m.candidateIds(aff, m.slotsFromAffordance(aff)[1])].sort()",
        aff=KOZILEK_12,
    )
    assert ids == ["alice", "bob"]


def test_shares_must_sum_to_the_amount_and_be_positive() -> None:
    assert run_module(
        "targeting.js",
        "[m.sharesValid([1, 1], 2), m.sharesValid([2, 0], 2), m.sharesValid([1, 2], 2)]",
    ) == [True, False, False]
