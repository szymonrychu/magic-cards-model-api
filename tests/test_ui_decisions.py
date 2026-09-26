from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

MAY = {
    # Which standing question this is (`repository.pause_generation`), echoed
    # back on `POST .../decide` as `generation` so an answer aimed at a
    # question the resolution has already moved past is refused rather than
    # applied to whatever is standing now.
    "generation": 1,
    "player": "alice",
    "kind": "may",
    "prompt": "Pay 2 life?",
    "source": "Shockland",
    "amount": None,
    "cards": [],
    "targets": [],
    # `PendingDecision.from_engine` reads these straight off the engine's own
    # `Decision`, which never carries `None` for either: `target_minimum` is
    # `int = 0` (0 for a genuinely optional "up to N"/"any number" - never
    # unset), and `moved_label`/`kept_label` default to `""`, not `None` -
    # `tests/test_ui_fixture_shape.py` pins the real shape these fixtures
    # must match.
    "target_minimum": 0,
    "target_maximum": None,
    "affirmative_is_legal": True,
    "options": [],
    "moved_label": "",
    "kept_label": "",
    "card_labels": [],
    "exact": False,
    "default_cards": None,
}
TARGETS = {
    **MAY,
    "kind": "choose_targets",
    "prompt": "Choose a target",
    "targets": [{"kind": "player", "id": "bob"}, {"kind": "card", "id": "c1"}],
    "target_minimum": 1,
    "target_maximum": 1,
    "card_details": {"c1": {"name": "Memnite"}},
}
SCRY = {
    **MAY,
    "kind": "scry",
    "prompt": "Scry 2",
    "cards": ["a", "b"],
    "card_labels": ["Island", "Bolt"],
    "moved_label": "bottom",
    "kept_label": "top",
}
MULLIGAN = {**MAY, "kind": "mulligan", "prompt": "take a mulligan?", "source": None}
QUALITY = {**MAY, "kind": "choose_a_quality", "prompt": "Choose a card name"}
# CR 510.1c, 702.19b: a 6-power trampler blocked by two creatures, so 2 and 3
# damage are each blocker's own lethal (`lethal_damage`, aligned with
# `targets`) and the 1 excess may reach "bob" only once both blockers have
# theirs (`trample` true, 0 lethal for the player entry).
DAMAGE = {
    **MAY,
    "kind": "assign_combat_damage",
    "prompt": "assign 6 combat damage from Grizzly Bears",
    "source": "Grizzly Bears",
    "amount": 6,
    "cards": ["attacker", "blocker1", "blocker2"],
    "card_labels": ["Grizzly Bears", "Savannah Lions", "Elvish Mystic"],
    "targets": [
        {"kind": "card", "id": "blocker1"},
        {"kind": "card", "id": "blocker2"},
        {"kind": "player", "id": "bob"},
    ],
    "lethal_damage": [2, 3, 0],
    "trample": True,
}


def test_every_answerable_kind_has_a_view_model() -> None:
    kinds = run_module("decisions.js", "Object.keys(m.DECISION_KINDS).sort()")
    assert kinds == sorted(
        [
            "may",
            "pay_life",
            "pay_energy",
            "pay_mana",
            "pay_energy_amount",
            "pay_life_amount",
            "scry",
            "surveil",
            "mulligan",
            "opening_hand_action",
            "choose_cards",
            "choose_targets",
            "choose_a_quality",
            "choose_a_mode",
            "put_on_top_or_bottom",
            "put_into_hand_or_graveyard",
            "choose_pile",
            "choose_players",
            "order_triggers",
            "assign_combat_damage",
        ]
    )


def test_may_offers_yes_and_no() -> None:
    vm = run_module("decisions.js", "m.viewModel(p, {})", p=MAY)
    assert vm["submit"]["answer"] == {"accept": True}
    assert vm["secondary"]["answer"] == {"accept": False}


def test_choose_targets_uses_slots_with_the_minimum() -> None:
    vm = run_module("decisions.js", "m.viewModel(p, { targets: ['bob'] })", p=TARGETS)
    (control,) = [c for c in vm["controls"] if c["type"] == "targets"]
    assert control["slots"][0]["minimum"] == 1
    assert vm["submit"]["enabled"] is True
    assert vm["submit"]["answer"] == {"targets": ["bob"]}
    assert run_module("decisions.js", "m.viewModel(p, {}).submit.enabled", p=TARGETS) is False


def test_scry_answer_carries_moved_and_top_order() -> None:
    answer = run_module("decisions.js", "m.answerFor(p, { moved: ['a'], order: ['b'] })", p=SCRY)
    assert answer == {"moved": ["a"], "top_order": ["b"]}


def test_mulligan_means_mulligan() -> None:
    """CR 103.5 (`machines/decisions.py`'s `ask_mulligan`): `True` takes a
    mulligan, `False` keeps the hand - the opposite polarity of every other
    `yesNo` kind, where the submit button's `accept: true` is the
    affirmative branch. The submit button here must read "Mulligan" and
    send `accept: true`; "Keep" is the secondary, sending `accept: false`."""
    vm = run_module("decisions.js", "m.viewModel(p, {})", p=MULLIGAN)
    assert vm["submit"]["label"] == "Mulligan"
    assert vm["submit"]["answer"] == {"accept": True}
    assert vm["secondary"]["label"] == "Keep"
    assert vm["secondary"]["answer"] == {"accept": False}


def test_an_open_quality_choice_offers_free_text() -> None:
    """`choose_a_quality` with no `options` (a card-name or creature-type
    choice - `schemas.py`'s `choice` field docstring: "empty `options` means
    'any card name' or 'any creature type'") has no printed list to offer,
    unlike `choose_a_mode`'s always-enumerated modes - so it gets a `text`
    control instead of an `options` one, enabled only once something is
    actually typed."""
    empty = run_module("decisions.js", "m.viewModel(p, {})", p=QUALITY)
    assert empty["controls"] == [{"type": "text", "value": ""}]
    assert empty["submit"]["enabled"] is False

    typed = run_module("decisions.js", "m.viewModel(p, { choice: 'Llanowar Elves' })", p=QUALITY)
    assert typed["controls"] == [{"type": "text", "value": "Llanowar Elves"}]
    assert typed["submit"]["enabled"] is True
    assert typed["submit"]["answer"] == {"choice": "Llanowar Elves"}

    blank = run_module("decisions.js", "m.viewModel(p, { choice: '   ' })", p=QUALITY)
    assert blank["submit"]["enabled"] is False


def test_scry_answer_agrees_with_the_view_model_about_which_cards_stay_on_top() -> None:
    """`answerFor` used to filter `draft.order` to `pending.cards` alone,
    while `split` (what the view model actually renders) filters it to the
    *kept* cards - the ones not `moved`. A stale `order` entry for a card
    that was ticked into `moved` disagreed between the two: this pins that
    `answerFor` and the view model's own `submit.answer` are now one
    implementation, so they cannot drift again."""
    draft = {"moved": ["a"], "order": ["a", "b"]}
    answer = run_module("decisions.js", "m.answerFor(p, d)", p=SCRY, d=draft)
    vm_answer = run_module("decisions.js", "m.viewModel(p, d).submit.answer", p=SCRY, d=draft)
    assert answer == {"moved": ["a"], "top_order": ["b"]}
    assert answer == vm_answer


def test_a_card_moved_and_kept_again_returns_to_the_order() -> None:
    """`split`'s `order` used to be `(draft.order || kept).filter(kept.includes)`
    - once an explicit order had been drawn while a card sat in `moved`, that
    stale `draft.order` never mentioned it, so filtering down to it alone
    silently dropped the card the moment it was kept again: not offered a
    row to reorder, and missing from the `top_order` the player would go on
    to submit. Move "b", draw an explicit order over what's left ("a"
    alone), then keep "b" back - it must reappear, appended rather than
    lost."""
    after_move = run_module(
        "decisions.js", "m.viewModel(p, d).controls[0].order", p=SCRY, d={"moved": ["b"]}
    )
    assert after_move == ["a"]
    after_keep = run_module(
        "decisions.js",
        "m.viewModel(p, d).controls[0].order",
        p=SCRY,
        d={"moved": [], "order": ["a"]},
    )
    assert after_keep == ["a", "b"]


def test_trigger_order_options_carries_each_abilitys_source_card_id() -> None:
    """`orderMode` (`ui/render/decision_window.js`) highlights the permanent
    that asked each trigger while a player orders CR 603.3b's batch - it needs
    each option's source id to point at. `card_details`' `source_card_id` is
    what names it, positionally aligned with `cards` like the rest of this
    view already reads; `null` when the id has no `card_details` entry at all
    (`t3`) or an entry that itself carries no `source_card_id` (`t4`, the
    unnameable-first-batch case this module's own docstring above talks
    about). Labels are unaffected - `test_trigger_order_labels.py` pins those
    unchanged."""
    pending = {
        **MAY,
        "kind": "order_triggers",
        "cards": ["t1", "t2", "t3", "t4"],
        "card_details": [
            {
                "id": "t1",
                "kind": "ability",
                "name": "Delver of Secrets",
                "text": "look at the top card",
                "source_card_id": "perm1",
            },
            {
                "id": "t2",
                "kind": "ability",
                "name": "Mind Stone",
                "text": "draw a card",
                "source_card_id": "perm2",
            },
            {
                "id": "t4",
                "kind": "ability",
                "name": "Hidden trigger",
                "text": "reveal the top card",
            },
        ],
    }
    options = run_module("decisions.js", "m.triggerOrderOptions(p)", p=pending)
    assert options[0]["sourceId"] == "perm1"
    assert options[1]["sourceId"] == "perm2"
    assert options[2]["sourceId"] is None  # t3: no card_details entry at all
    assert options[3]["sourceId"] is None  # t4: entry present, no source_card_id


# -- the decision window's own draft arithmetic --------------------------------

TRIGGERS = {**MAY, "kind": "order_triggers", "cards": ["t1", "t2", "t3"], "amount": 0}


def _choose(zone: str, **over: object) -> dict:
    return {
        **MAY,
        "kind": "choose_cards",
        "cards": ["c1", "c2"],
        "card_details": [
            {"id": "c1", "name": "A", "zone": zone},
            {"id": "c2", "name": "B", "zone": zone},
        ],
        **over,
    }


def test_the_complex_kinds_are_the_ones_answered_in_the_decision_window() -> None:
    kinds = run_module(
        "decisions.js",
        "Object.keys(m.DECISION_KINDS).filter((k) => m.usesDecisionWindow({ kind: k })).sort()",
    )
    assert kinds == [
        "assign_combat_damage",
        "choose_cards",
        "mulligan",
        "order_triggers",
        "scry",
        "surveil",
    ]
    assert run_module("decisions.js", "m.usesDecisionWindow(null)") is False
    assert (
        run_module("decisions.js", "m.usesDecisionWindow({ kind: 'declare_attackers' })") is False
    )


def test_toggling_a_choice_never_goes_past_the_maximum() -> None:
    result = run_module(
        "decisions.js",
        """[
          m.toggleChoice([], "a", 2),
          m.toggleChoice(["a"], "b", 2),
          m.toggleChoice(["a", "b"], "c", 2),
          m.toggleChoice(["a", "b"], "a", 2),
          m.toggleChoice(["a"], "b", 1),
          m.toggleChoice(["a"], "b", null),
          m.toggleChoice(["a"], "b", 0),
        ]""",
    )
    assert result == [["a"], ["a", "b"], ["a", "b"], ["b"], ["b"], ["a", "b"], ["a"]]


def test_placing_a_card_in_a_split_lane() -> None:
    result = run_module(
        "decisions.js",
        """[
          m.placeInSplit([], ["a", "b", "c"], "a", "moved", 0),
          m.placeInSplit(["a"], ["b", "c"], "c", "kept", 0),
          m.placeInSplit(["a", "b"], ["c"], "a", "moved", 5),
          m.placeInSplit(["a"], ["b", "c"], "a", "kept", 1),
        ]""",
    )
    assert result == [
        {"moved": ["a"], "order": ["b", "c"]},
        {"moved": ["a"], "order": ["c", "b"]},
        {"moved": ["b", "a"], "order": ["c"]},
        {"moved": [], "order": ["b", "a", "c"]},
    ]


# -- L1a: split()/viewModel must read amount/exact, the same as chooseCards
# already does, instead of always allowing any split and saying nothing
# about the count validate_answer actually enforces (PR #31) -------------

SCRY_ANY_NUMBER = {**SCRY, "amount": 2, "exact": False}
SPLIT_EXACT_ONE = {**SCRY, "cards": ["a", "b"], "amount": 1, "exact": True}
SPLIT_UP_TO_ONE = {**SCRY, "cards": ["a"], "amount": 1, "exact": False}


def test_split_states_any_number_when_the_ceiling_is_the_whole_look() -> None:
    control = run_module(
        "decisions.js", "m.viewModel(p, { moved: [] }).controls[0]", p=SCRY_ANY_NUMBER
    )
    assert "any number" in control["countHint"].lower()
    assert (
        run_module(
            "decisions.js",
            "m.viewModel(p, { moved: ['a', 'b'] }).submit.enabled",
            p=SCRY_ANY_NUMBER,
        )
        is True
    )


def test_split_refuses_a_count_other_than_the_exact_amount() -> None:
    assert (
        run_module(
            "decisions.js", "m.viewModel(p, { moved: ['a'] }).submit.enabled", p=SPLIT_EXACT_ONE
        )
        is True
    )
    assert (
        run_module(
            "decisions.js", "m.viewModel(p, { moved: [] }).submit.enabled", p=SPLIT_EXACT_ONE
        )
        is False
    )
    assert (
        run_module(
            "decisions.js",
            "m.viewModel(p, { moved: ['a', 'b'] }).submit.enabled",
            p=SPLIT_EXACT_ONE,
        )
        is False
    )
    control = run_module("decisions.js", "m.viewModel(p, {}).controls[0]", p=SPLIT_EXACT_ONE)
    assert "exactly 1" in control["countHint"]


def test_split_allows_up_to_the_ceiling_never_past_it() -> None:
    assert (
        run_module(
            "decisions.js", "m.viewModel(p, { moved: [] }).submit.enabled", p=SPLIT_UP_TO_ONE
        )
        is True
    )
    assert (
        run_module(
            "decisions.js", "m.viewModel(p, { moved: ['a'] }).submit.enabled", p=SPLIT_UP_TO_ONE
        )
        is True
    )
    control = run_module("decisions.js", "m.viewModel(p, {}).controls[0]", p=SPLIT_UP_TO_ONE)
    assert "up to 1" in control["countHint"]


def test_split_with_no_amount_at_all_stays_unconstrained() -> None:
    """An older engine build (or any payload with `amount: None`) must not be
    newly refused - the same "no ceiling to enforce" fallback `chooseCards`
    already gets."""
    assert (
        run_module("decisions.js", "m.viewModel(p, { moved: ['a', 'b'] }).submit.enabled", p=SCRY)
        is True
    )
    control = run_module("decisions.js", "m.viewModel(p, {}).controls[0]", p=SCRY)
    assert control["countHint"] is None


def test_placing_an_id_in_a_list() -> None:
    result = run_module(
        "decisions.js",
        """[
          m.placeInList(["a", "b", "c"], "c", 0),
          m.placeInList(["a", "b", "c"], "a", 2),
          m.placeInList(["a", "b", "c"], "b", -3),
        ]""",
    )
    assert result == [["c", "a", "b"], ["b", "c", "a"], ["b", "a", "c"]]


def test_the_shown_trigger_order_is_the_offered_order_until_one_is_drawn() -> None:
    result = run_module(
        "decisions.js",
        "[m.shownTriggerOrder(p, {}),"
        " m.shownTriggerOrder(p, { order: ['t3', 't1', 't2'] }),"
        " m.shownTriggerOrder(p, { order: ['t2'] })]",
        p=TRIGGERS,
    )
    assert result == [["t1", "t2", "t3"], ["t3", "t1", "t2"], ["t1", "t2", "t3"]]


def test_choose_cards_names_its_zero_answer_by_where_the_cards_are() -> None:
    library = run_module(
        "decisions.js", "m.viewModel(p, {})", p=_choose("library", exact=False, amount=1)
    )
    assert library["secondary"] == {"label": "Find nothing", "answer": {"card_ids": []}}
    field = run_module(
        "decisions.js", "m.viewModel(p, {})", p=_choose("battlefield", exact=False, amount=1)
    )
    assert field["secondary"] == {"label": "Choose none", "answer": {"card_ids": []}}
    exact_zero = run_module(
        "decisions.js", "m.viewModel(p, {})", p=_choose("hand", exact=True, amount=0)
    )
    assert exact_zero["secondary"] is None
    assert exact_zero["submit"]["label"] == "Choose none"
    assert exact_zero["submit"]["answer"] == {"card_ids": []}


def test_assign_combat_damage_rows_carry_lethal_and_drafted_amounts() -> None:
    rows = run_module(
        "decisions.js",
        "m.assignDamageRows(p, { damage: { 'card:blocker1': 2 } })",
        p=DAMAGE,
    )
    assert rows == [
        {
            "key": "card:blocker1",
            "id": "blocker1",
            "kind": "card",
            "name": "Savannah Lions",
            "lethal": 2,
            "amount": 2,
        },
        {
            "key": "card:blocker2",
            "id": "blocker2",
            "kind": "card",
            "name": "Elvish Mystic",
            "lethal": 3,
            "amount": 0,
        },
        {
            "key": "player:bob",
            "id": "bob",
            "kind": "player",
            "name": "bob",
            "lethal": 0,
            "amount": 0,
        },
    ]


def test_assign_combat_damage_view_model_gates_submit_on_the_total() -> None:
    short = run_module(
        "decisions.js",
        "m.viewModel(p, d)",
        p=DAMAGE,
        d={"damage": {"card:blocker1": 2, "card:blocker2": 3}},
    )
    assert short["submit"]["enabled"] is False
    assert short["controls"][0]["total"] == 5
    assert short["controls"][0]["power"] == 6

    exact = run_module(
        "decisions.js",
        "m.viewModel(p, d)",
        p=DAMAGE,
        d={"damage": {"card:blocker1": 2, "card:blocker2": 3, "player:bob": 1}},
    )
    assert exact["submit"]["enabled"] is True
    assert exact["submit"]["answer"] == {
        "damage": [
            {"target": "blocker1", "amount": 2},
            {"target": "blocker2", "amount": 3},
            {"target": "bob", "amount": 1},
        ]
    }


def test_assign_combat_damage_answer_drops_zero_amount_recipients() -> None:
    vm = run_module(
        "decisions.js",
        "m.viewModel(p, d)",
        p=DAMAGE,
        d={"damage": {"card:blocker1": 6, "card:blocker2": 0}},
    )
    assert vm["submit"]["answer"] == {"damage": [{"target": "blocker1", "amount": 6}]}


def test_assign_combat_damage_uses_the_decision_window() -> None:
    assert run_module("decisions.js", "m.usesDecisionWindow(p)", p=DAMAGE) is True
    assert run_module("decisions.js", "m.usesDecisionPopover(p)", p=DAMAGE) is False
