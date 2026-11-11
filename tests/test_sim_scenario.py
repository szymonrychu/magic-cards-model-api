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


# -- run_scenario against the real engine ------------------------------------

import json  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from magic_cards_model_api.main import app  # noqa: E402
from magic_cards_model_api.sim.sandbox_game import SandboxGame  # noqa: E402
from magic_cards_model_api.sim.scenario import run_scenario  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
BOLT_SCRIPT = json.loads((FIXTURES / "bolt_bears_script.json").read_text())

CYCLER_SETUP = {
    "players": [{"name": "alice"}, {"name": "bob"}],
    "cards": [
        {"card": "Lórien Revealed", "player": "alice", "zone": "hand", "label": "cycler"},
        {"card": "Forest", "player": "alice", "zone": "battlefield", "label": "forest"},
        {"card": "Mountain", "player": "alice", "zone": "library", "position": "bottom"},
        {"card": "Island", "player": "alice", "zone": "library", "position": "bottom"},
        {"card": "Island", "player": "alice", "zone": "library", "position": "bottom"},
    ],
}
CYCLE = [
    {"act": {"action": "activate", "player": "alice",
             "body": {"card_id": "@cycler", "ability_index": 0, "mana_sources": ["@forest"]}}},
    {"act": {"action": "resolve_top"}},
]


@pytest.fixture
def game():
    sandbox = SandboxGame(TestClient(app))
    yield sandbox
    sandbox.close()


def run(game, data, budget=60.0):
    scenario = validate_scenario(data, 200)
    return run_scenario(game, scenario, time.monotonic() + budget)


def test_script_bolt_kills_bears_and_reports_diff(game):
    out = run(game, BOLT_SCRIPT)
    assert out["stopped_at"] is None and out["stop_reason"] is None
    assert [s["ok"] for s in out["steps"]] == [True, True, True, True]
    assert [s["index"] for s in out["steps"]] == [0, 1, 2, 3]
    # The card keeps its id across the zone move in the real engine, so it is one "moved"
    # entry (not removed + added). The label and the "<player>.<zone>" form are the format
    # pinned by the Task 7 state_diff tests.
    assert out["diff"]["moved"] == [
        {"card": "@bolt", "from": "alice.hand", "to": "alice.graveyard"},
        {"card": "@bears", "from": "bob.battlefield", "to": "bob.graveyard"},
    ]
    assert set(out["diff"]) == {"moved"}
    assert "support_warnings" in out
    assert out["initial_state"]["players"]["bob"]["battlefield"][0]["name"] == "Grizzly Bears"
    assert out["final_state"]["players"]["bob"]["graveyard"][0]["name"] == "Grizzly Bears"


def test_script_stops_at_an_unanswered_decision(game):
    steps = [*CYCLE, {"act": {"action": "pass", "player": "alice"}}]
    out = run(game, {"setup": CYCLER_SETUP, "steps": steps})
    assert out["stopped_at"] == 2
    assert "choose_cards" in out["stop_reason"]
    assert out["final_state"]["pending_decision"]["kind"] == "choose_cards"
    assert len(out["steps"]) == 2


def test_script_baseline_answers_fill_in_decisions(game):
    steps = [*CYCLE, {"act": {"action": "pass", "player": "alice"}}]
    out = run(game, {"setup": CYCLER_SETUP, "steps": steps, "answers": "baseline"})
    assert out["stopped_at"] is None, out["stop_reason"]
    assert [s["kind"] for s in out["steps"]] == ["act", "act", "auto_decide", "act"]
    assert out["final_state"]["pending_decision"] is None


def test_script_stops_on_refusal_with_engine_reason(game):
    steps = [
        {"act": {"action": "cast", "player": "alice", "body": {"card_id": "@bolt"}}},
        {"act": {"action": "pass", "player": "alice"}},
    ]
    out = run(game, {"setup": BOLT_SCRIPT["setup"], "steps": steps})
    assert out["stopped_at"] == 0
    assert out["steps"][0]["ok"] is False
    assert "too few targets" in out["steps"][0]["error"]
    assert "too few targets" in out["stop_reason"]
    assert len(out["steps"]) == 1


def test_failed_expect_is_reported_not_fatal(game):
    steps = [
        {"expect": {"life": {"alice": 5}}},
        {"act": {"action": "pass", "player": "alice"}},
    ]
    out = run(game, {"setup": BOLT_SCRIPT["setup"], "steps": steps})
    assert out["stopped_at"] is None
    assert out["steps"][0]["ok"] is False
    assert out["steps"][0]["expect_failures"] == ["life.alice: expected 5, got 20"]
    assert out["steps"][1]["ok"] is True


def test_script_stops_when_the_time_budget_is_used_up(game):
    steps = [{"note": "a"}, {"act": {"action": "pass", "player": "alice"}}]
    out = run(game, {"setup": BOLT_SCRIPT["setup"], "steps": steps}, budget=-1.0)
    assert out["stopped_at"] == 0
    assert out["stop_reason"].startswith("time budget of ")
    assert out["stop_reason"].endswith(" s used up")


def test_script_creature_keeps_summoning_sickness_like_sim_new(game):
    spec = {
        "players": [{"name": "alice"}, {"name": "bob"}],
        "cards": [
            {"card": "Grizzly Bears", "player": "alice", "zone": "battlefield", "label": "bears"}
        ],
    }
    game.create(spec)
    expected = game.state(full=True)["state"]["battlefield"][0]["summoning_sick"]
    assert expected is True
    run(game, {"setup": spec, "steps": []})
    got = game.state(full=True)["state"]["battlefield"][0]["summoning_sick"]
    assert got == expected


class _StuckGame:
    """A game whose decision never resolves and whose every answer takes 0.05 s."""

    def __init__(self):
        self.decides = 0
        self.pending = {"kind": "choose_cards", "player": "alice", "options": [], "cards": []}

    def create(self, spec):
        return {"state": {"pending_decision": self.pending}, "labels": {}}

    def act(self, action, player=None, body=None, dry_run=False):
        time.sleep(0.05)
        if action == "decide":
            self.decides += 1
        return {"state": {"pending_decision": self.pending}, "labels": {},
                "result": {"accepted": True}}

    def state(self, full=False):
        return {"state": {"pending_decision": self.pending}, "labels": {}}


def test_auto_decide_loop_stops_at_the_deadline():
    stuck = _StuckGame()
    steps = [{"act": {"action": "pass", "player": "alice"}}]
    scenario = validate_scenario(
        {"setup": BOLT_SCRIPT["setup"], "steps": steps, "answers": "baseline"}, 200
    )
    out = run_scenario(stuck, scenario, time.monotonic() + 0.2)
    assert stuck.decides < 50
    assert out["stopped_at"] == 0
    assert out["stop_reason"].startswith("time budget of ")
    assert out["stop_reason"].endswith(" s used up")
    assert all(s["kind"] == "auto_decide" for s in out["steps"]) and out["steps"]
