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
            "order_triggers",
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
    assert kinds == ["choose_cards", "mulligan", "order_triggers", "scry", "surveil"]
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
