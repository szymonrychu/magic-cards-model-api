"""What the Priority group in the action bar offers a player, run against the
real `ui/app.js`.

"Pass priority" and "Resolve top" both used to sit at the top of this group in
every game. Removing both of them stranded a player: a triggered ability on the
stack in their own precombat main, priority held, no decision pending, and the
turn buttons render only in untap and cleanup - so nothing on the panel could
move the game.

So "Pass priority" is back, but only where it is contextually needed: you hold
priority AND the stack is non-empty. That is the state CR 117.4 describes, in
which all players passing in succession is what resolves the top of the stack.
Passing on an empty stack ends the step instead, which is the "Auto-pass until…"
picker's job, so the button stays hidden there and the panel stays clean.

"Resolve top" stays gone in every state. Under CR 117.4 the top of the stack
resolving is a consequence of everyone passing, not an action a player takes.

`passPriority()` also still runs on the cast path (`confirmAction`), so this is
one more caller of a live function, not a resurrection.

This loads the shipped `ui/app.js` under Node - the exact file the browser is
served, no build step - the way `tests/test_ui_pregame_panel.py` does.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "ui" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def board(**overrides: Any) -> dict[str, Any]:
    """A board state shaped like the one `GET /api/games/{id}` returns, with
    only the fields the action bar reads."""
    state = {
        "you": "alice",
        "priority_holder": "alice",
        "active_player": "alice",
        "turn_number": 1,
        "step": "precombat_main",
        "stack": [],
        "hands": {"alice": []},
        "play_permissions": {"alice": []},
        "battlefield": [],
        "pending_decision": None,
        "winner": None,
        "drawn": None,
    }
    state.update(overrides)
    return state


def action_bar_html(state: dict[str, Any]) -> str:
    """The HTML `renderActionBar` really writes into `#action-bar`."""
    script = f"""
    const bar = {{ innerHTML: "", contains: () => false, querySelector: () => null }};
    globalThis.document = {{
      activeElement: null,
      body: {{}},
      addEventListener() {{}},
      querySelector: () => bar,
      querySelectorAll: () => [],
    }};
    const {{ renderActionBar }} = require({json.dumps(str(APP_JS))});
    renderActionBar({json.dumps(state)}, "alice");
    process.stdout.write(JSON.stringify(bar.innerHTML));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


STACK = [{"id": "ability-1", "name": "Devourer of Destiny", "kind": "ability"}]

BOARDS = {
    # You hold priority but there is nothing to resolve: passing here would end
    # the step, which is the picker's job, so no button.
    "yours-empty-stack": board(),
    # Something is on the stack but it is not your priority, so you have nothing
    # to pass.
    "theirs-full-stack": board(priority_holder="bob", active_player="bob", stack=list(STACK)),
    # Both halves true - the stranded state the button exists for.
    "yours-full-stack": board(stack=list(STACK)),
}


def test_no_pass_button_on_an_empty_stack_even_though_you_hold_priority() -> None:
    html = action_bar_html(BOARDS["yours-empty-stack"])

    assert "Pass priority" not in html, html
    assert "passPriority()" not in html, html


def test_no_pass_button_when_the_opponent_holds_priority_over_a_full_stack() -> None:
    """It used to render disabled rather than absent, so not-your-priority is
    where a half-done condition would still show it."""
    html = action_bar_html(BOARDS["theirs-full-stack"])

    assert "Pass priority" not in html, html
    assert "passPriority()" not in html, html


def test_the_pass_button_is_offered_when_you_hold_priority_over_a_full_stack() -> None:
    """The regression this file exists for: a triggered ability in your own
    precombat main, priority yours, no decision pending. Nothing else on the
    panel can move the game from here."""
    html = action_bar_html(BOARDS["yours-full-stack"])

    assert "Pass priority" in html, html
    assert 'onclick="passPriority()"' in html, html
    # And it is offered live - there is no state in which it shows disabled.
    assert "disabled" not in html.split("Pass priority")[0].rsplit("<button", 1)[1], html


@pytest.mark.parametrize("name", sorted(BOARDS))
def test_resolve_top_is_never_offered(name: str) -> None:
    """Priority held or not, stack full or empty - it is gone for good. Under
    CR 117.4 the top of the stack resolves because everyone passed, so it was
    never a player action to begin with."""
    html = action_bar_html(BOARDS[name])

    assert "Resolve top" not in html, html
    assert "resolveTop()" not in html, html


@pytest.mark.parametrize("name", sorted(BOARDS))
def test_the_rest_of_the_priority_group_is_untouched(name: str) -> None:
    """The picker, its button, and the three stop settings all stay, in every
    one of the three states above."""
    html = action_bar_html(BOARDS[name])

    assert "Priority" in html
    assert 'id="autopilot-target"' in html, html
    assert "startAutopilotFromPicker()" in html, html
    assert "Auto-pass until" in html, html
    assert "toggleStopOnStackSetting" in html, html
    assert "stop when anything lands on the stack" in html, html
    assert "toggleStopWhenAttackedSetting" in html, html
    assert "stop when you are being attacked" in html, html
    assert "toggleStopWhenYouCanAttackSetting" in html, html
    assert "stop when you could attack" in html, html
    # And the picker still offers both halves of the `who` dimension.
    assert "my main 1" in html
    assert "opponent&#39;s end" in html


def test_the_other_groups_still_render() -> None:
    """Nothing outside the Priority group was disturbed: the step's own button
    (here CR 508's declare-attackers) is still offered."""
    html = action_bar_html(board(step="declare_attackers"))

    assert "Declare attackers" in html, html
