import pytest

from magic_cards_model_api.sim.scenario import check_expect, state_diff, validate_scenario

SETUP = {"players": [{"name": "alice"}, {"name": "bob"}]}


def _card(cid, name, label=None, **extra):
    out = {"id": cid, "name": name, **extra}
    if label:
        out["label"] = label
    return out


def _state(bob_life=20, bears_zone="battlefield", tapped=False, counters=None, extra=None):
    bears = _card("c1", "Grizzly Bears", "x", **({"tapped": True} if tapped else {}))
    if counters:
        bears["counters"] = counters
    zones = {"battlefield": [], "graveyard": [], "hand": [], "exile": []}
    zones[bears_zone] = [bears]
    bob = {"life": bob_life, "hand": [], "battlefield": [], "graveyard": [], "exile": []}
    alice = {"life": 20, **zones}
    alice["battlefield"] = alice["battlefield"] + (extra or [])
    return {"step": "precombat_main", "winner": None, "players": {"alice": alice, "bob": bob},
            "stack": []}


def test_scenario_accepts_all_step_kinds():
    sc = validate_scenario(
        {
            "setup": SETUP,
            "steps": [
                {"act": {"action": "pass", "player": "alice"}},
                {"decide": {"player": "bob", "answer": {"choice": 1}}},
                {"expect": {"life": {"bob": 17}, "step": "end"}},
                {"note": "hi"},
            ],
        },
        200,
    )
    assert sc.answers == "stop"
    assert len(sc.steps) == 4


def test_scenario_rejects_unknown_step_kind():
    with pytest.raises(ValueError):
        validate_scenario({"setup": SETUP, "steps": [{"explode": {}}]}, 200)


def test_scenario_rejects_unknown_action_and_extra_keys():
    with pytest.raises(ValueError):
        validate_scenario({"setup": SETUP, "steps": [{"act": {"action": "nope"}}]}, 200)
    with pytest.raises(ValueError):
        both = {"note": "a", "act": {"action": "pass"}}
        validate_scenario({"setup": SETUP, "steps": [both]}, 200)


def test_scenario_rejects_more_than_max_steps():
    steps = [{"note": "n"}] * 3
    with pytest.raises(ValueError, match="MTG_SIM_MAX_SCRIPT_STEPS"):
        validate_scenario({"setup": SETUP, "steps": steps}, 2)


def test_state_diff_reports_life_and_moves():
    before = _state()
    after = _state(bob_life=17, bears_zone="graveyard")
    assert state_diff(before, after) == {
        "life": {"bob": [20, 17]},
        "moved": [{"card": "@x", "from": "alice.battlefield", "to": "alice.graveyard"}],
    }


def test_state_diff_tap_counters_added_removed():
    before = _state()
    token = _card("t1", "Goblin")
    after = _state(tapped=True, counters={"+1/+1": 1}, extra=[token])
    assert state_diff(before, after) == {
        "added": [{"card": "Goblin", "zone": "alice.battlefield"}],
        "tapped": ["@x"],
        "counters": [{"card": "@x", "from": {}, "to": {"+1/+1": 1}}],
    }
    assert state_diff(after, before) == {
        "removed": [{"card": "Goblin", "zone": "alice.battlefield"}],
        "untapped": ["@x"],
        "counters": [{"card": "@x", "from": {"+1/+1": 1}, "to": {}}],
    }
    assert state_diff(before, before) == {}


def test_check_expect_accepts_labels_and_names():
    state = _state(bob_life=17)
    labels = {"x": "c1"}
    expect = {
        "life": {"bob": 17},
        "zones": {"alice.battlefield": ["@x"], "alice.hand": [], "bob.graveyard": []},
        "step": "precombat_main",
    }
    assert check_expect(expect, state, labels) == []
    assert check_expect({"zones": {"alice.battlefield": ["Grizzly Bears"]}}, state, labels) == []


def test_check_expect_reports_each_mismatch():
    state = _state(bob_life=17)
    fails = check_expect(
        {
            "life": {"bob": 20, "carol": 1},
            "zones": {"alice.graveyard": ["@x"], "alice.battlefield": ["Lightning Bolt"]},
            "step": "end",
            "winner": "alice",
        },
        state,
        {"x": "c1"},
    )
    assert len(fails) == 6
    assert any("bob" in f and "20" in f and "17" in f for f in fails)
    assert any("carol" in f for f in fails)
    assert any("step" in f for f in fails)
    assert any("winner" in f for f in fails)


def test_check_expect_player_name_with_dot_and_bad_zone():
    state = _state()
    state["players"]["a.b"] = {"life": 5, "hand": [], "battlefield": [_card("z", "Elf")],
                               "graveyard": [], "exile": []}
    assert check_expect({"zones": {"a.b.battlefield": ["Elf"]}}, state, {}) == []
    assert check_expect({"zones": {"a.b.nowhere": ["Elf"]}}, state, {})
    assert check_expect({"zones": {"ghost.hand": []}}, state, {})
    assert check_expect({"zones": {"alice.hand": ["@nope"]}}, state, {})
