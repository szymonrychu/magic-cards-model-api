"""The play UI while one seat controls another's turn (CR 723).

Live game 0cf08288: the AI seat cast Emrakul, the Promised End and made the
human's decisions for their whole turn 7. The human's page showed their own
priority, offered them a pass the engine then refused ("claude is making
you's decisions this turn (CR 723.5)"), and said nothing about why. These run
`ui/store.js` and `ui/priority.js` under Node, like `test_ui_ai_stall.py`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node


def board(you: str, **overrides: Any) -> dict:
    """The human's own view of their controlled upkeep."""
    return {
        "you": you,
        "winner": None,
        "drawn": False,
        "turn_number": 7,
        "step": "upkeep",
        "active_player": "you",
        "priority_holder": "you",
        "pending_decision": None,
        "stack": [],
        "player_control": {"you": "claude"},
        "players": [
            {"username": "you", "decisions_made_by": "claude"},
            {"username": "claude", "decisions_made_by": None},
        ],
    } | overrides


def notice(state: dict) -> Any:
    return run_module("store.js", f"m.playerControlNotice({json.dumps(state)})")


def test_the_controlled_player_is_told_who_is_deciding_for_them():
    message = notice(board("you"))
    assert message is not None
    assert "claude" in message
    assert "CR 723" in message


def test_the_controller_is_told_whose_decisions_it_is_making():
    message = notice(board("claude"))
    assert message is not None
    assert "you" in message


def test_nothing_is_said_when_nobody_is_controlled():
    state = board(
        "you",
        player_control={},
        players=[
            {"username": "you", "decisions_made_by": None},
            {"username": "claude", "decisions_made_by": None},
        ],
    )
    assert notice(state) is None


def test_the_controlled_player_is_offered_no_pass():
    """CR 723.5: the pass is not theirs to make, and the engine refuses it."""
    buttons = run_module("priority.js", f"m.passButtonsFor({json.dumps(board('you'))})")
    assert buttons == []


def test_the_ai_stall_banner_watches_the_controller_of_the_holder():
    """The AI seat is the one deciding, so the human's page must still notice
    when it goes quiet on the human's own priority."""
    status = {"state": "dispatched", "last_error": None}
    message = run_module(
        "store.js",
        f"m.aiStallNotice({json.dumps(board('you'))}, 'claude', {json.dumps(status)}, 120)",
    )
    assert message is not None and "claude" in message


# -- the human controlling the AI seat's turn (the human cast Emrakul) --------

APP_JS = Path(__file__).resolve().parents[1] / "ui" / "app.js"


def run_app(expression: str, **bindings: Any) -> Any:
    """`ui/app.js` under Node with no network, as `test_ui_app.py` runs it."""
    consts = "\n".join(f"const {name} = {json.dumps(value)};" for name, value in bindings.items())
    script = f"""
    import {{ pathToFileURL }} from "node:url";
    globalThis.localStorage = {{ getItem: () => null, setItem() {{}}, removeItem() {{}} }};
    globalThis.fetch = () => {{ throw new Error("no network here"); }};
    const m = await import(pathToFileURL({json.dumps(str(APP_JS))}).href);
    {consts}
    const __result = await eval({json.dumps(expression)});
    process.stdout.write(JSON.stringify(__result === undefined ? null : __result));
    """
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout or "null")


BEARS = {
    "id": "bears",
    "name": "Grizzly Bears",
    "controller": "claude",
    "tapped": False,
    "summoning_sick": False,
    "type_line": "Creature - Bear",
    "abilities": [],
}
KARN = {
    "id": "karn",
    "name": "Karn's Forest",
    "controller": "claude",
    "tapped": False,
    "type_line": "Basic Land - Forest",
    "abilities": [{"index": 0, "text": "{T}: Add {G}.", "is_mana_ability": True}],
}
MY_ELF = {
    "id": "elf",
    "name": "Llanowar Elves",
    "controller": "you",
    "tapped": False,
    "summoning_sick": False,
    "type_line": "Creature - Elf Druid",
    "abilities": [],
}


def controlling(**overrides: Any) -> dict:
    """The human's own view while they make the AI seat's decisions."""
    return {
        "you": "you",
        "winner": None,
        "drawn": False,
        "turn_number": 7,
        "step": "precombat_main",
        "active_player": "claude",
        "priority_holder": "claude",
        "pending_decision": None,
        "stack": [],
        "battlefield": [BEARS, KARN, MY_ELF],
        "player_control": {"claude": "you"},
        "players": [
            {"username": "you", "decisions_made_by": None},
            {"username": "claude", "decisions_made_by": "you"},
        ],
    } | overrides


def test_the_controller_decides_for_the_controlled_seat():
    state = controlling()
    assert run_module("control.js", "[...m.decidesFor(s)].sort()", s=state) == ["claude", "you"]
    assert run_module("control.js", "m.holdsPriority(s)", s=state) is True
    assert run_module("control.js", "m.actingAs(s)", s=state) == "claude"


def test_the_controller_is_offered_the_controlled_seats_pass():
    buttons = run_module("priority.js", f"m.passButtonsFor({json.dumps(controlling())})")
    assert buttons == ["pass", "pass_until"]


def test_the_controller_is_offered_advance_in_the_controlled_seats_cleanup():
    state = controlling(step="cleanup", priority_holder=None)
    assert run_module("priority.js", "m.advanceOffered(s)", s=state) is True


def test_a_decision_addressed_to_the_controlled_seat_stops_the_controllers_autopilot():
    pending = {"player": "claude", "kind": "choose_cards", "prompt": "discard"}
    state = controlling(pending_decision=pending)
    run = {"verb": "pass_until", "startedStep": None, "startedTurn": None}
    stops = {"mine": [], "theirs": []}
    reason = run_module("priority.js", "m.stopReason(s, r, t)", s=state, r=run, t=stops)
    assert reason == "a decision is pending"
    assert run_module("priority.js", f"m.passButtonsFor({json.dumps(state)})") == ["pass"]


def test_the_controlled_seats_attack_is_the_controllers_to_declare():
    pending = {"player": "claude", "kind": "declare_attackers", "prompt": "attack?"}
    state = controlling(pending_decision=pending)
    stops = {"mine": [], "theirs": []}
    assert run_module("priority.js", "m.ownUnansweredAttackDeclaration(s, t)", s=state, t=stops)
    plan = run_app(
        "m.planPermanentClick(c, {action: null, decisionDraft: {}}, s)", c=BEARS, s=state
    )
    assert plan == {"kind": "attack"}
    assert [c["id"] for c in run_module("combat.js", "m.attackCandidates(s)", s=state)] == ["bears"]
    assert [d["id"] for d in run_module("combat.js", "m.defenders(s)", s=state)] == ["you"]


def test_the_controlled_seats_blockers_are_the_controllers_to_declare():
    pending = {"player": "claude", "kind": "declare_blockers", "prompt": "block?"}
    state = controlling(
        pending_decision=pending,
        active_player="you",
        priority_holder="you",
        combat={"attacks": [{"attacker": "elf", "defender": "claude", "blockers": []}]},
    )
    plan = run_app(
        "m.planPermanentClick(c, {action: null, decisionDraft: {}}, s)", c=BEARS, s=state
    )
    assert plan == {"kind": "block"}
    assert [c["id"] for c in run_module("combat.js", "m.blockCandidates(s)", s=state)] == ["bears"]
    assert len(run_module("combat.js", "m.attacksAgainstYou(s)", s=state)) == 1


def test_the_controller_can_use_the_controlled_seats_permanent():
    plan = run_app(
        "m.planPermanentClick(c, {action: null, decisionDraft: {}}, s)", c=KARN, s=controlling()
    )
    assert plan == {"kind": "activate", "abilityIndex": 0}


def test_the_controlled_seats_decision_opens_the_controllers_decision_window():
    pending = {"player": "claude", "kind": "choose_cards", "prompt": "discard", "cards": []}
    app = {"state": controlling(pending_decision=pending)}
    subject = run_module("render/decision_window.js", "m.windowSubject(a)", a=app)
    assert subject is not None and subject["mode"] == "choose"


def test_a_row_taken_for_another_player_says_so_in_the_menu():
    items = [
        {
            "key": "activate:0",
            "label": "Add {G}",
            "sublabel": "",
            "match": {"card_id": "karn", "kind": "activate_mana", "ability_index": 0},
        },
    ]
    rows = [
        {
            "card_id": "karn",
            "kind": "activate_mana",
            "ability_index": 0,
            "affordable": "yes",
            "player": "claude",
            "acting_for": "claude",
        },
    ]
    enriched = run_module("menus.js", "m.enrichItems(i, r)", i=items, r=rows)
    assert "claude" in enriched[0]["sublabel"]
