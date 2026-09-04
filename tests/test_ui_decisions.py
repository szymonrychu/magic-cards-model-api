from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

MAY = {
    "player": "alice",
    "kind": "may",
    "prompt": "Pay 2 life?",
    "source": "Shockland",
    "amount": None,
    "cards": [],
    "targets": [],
    "target_minimum": None,
    "target_maximum": None,
    "affirmative_is_legal": True,
    "options": [],
    "moved_label": None,
    "kept_label": None,
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
    answer = run_module("decisions.js", "m.answerFor('scry', { moved: ['a'], order: ['b'] })")
    assert answer == {"moved": ["a"], "top_order": ["b"]}
