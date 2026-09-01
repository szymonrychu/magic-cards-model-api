"""What the Priority group in the action bar offers a player, run against the
real `ui/app.js`.

The two manual buttons are gone. "Pass priority" and "Resolve top" used to sit
at the top of this group in every game; the player asked for both to be
removed, in every game and every state, leaving the group to its "Auto-pass
until…" picker, that picker's button, and the "stop when anything lands on the
stack" setting. This file is the standing check that neither button comes back
and that nothing else in the group left with them.

`passPriority()` itself is untouched and still runs on the cast path
(`confirmAction`), so removing the button removed an offer, not a capability.

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


def test_the_pass_priority_button_is_not_offered_even_when_it_is_your_priority() -> None:
    html = action_bar_html(board())

    assert "Pass priority" not in html, html
    assert "passPriority()" not in html, html


def test_the_resolve_top_button_is_not_offered_even_with_a_full_stack() -> None:
    """It was rendered only when the stack was non-empty, so an empty stack
    would have hidden its absence."""
    html = action_bar_html(
        board(stack=[{"id": "ability-1", "name": "Devourer of Destiny", "kind": "ability"}])
    )

    assert "Resolve top" not in html, html
    assert "resolveTop()" not in html, html


def test_neither_button_returns_when_priority_is_the_opponents() -> None:
    """The pass button used to render disabled rather than absent, so the
    not-your-priority state is where a half-removal would still show it."""
    html = action_bar_html(board(priority_holder="bob", active_player="bob"))

    assert "Pass priority" not in html, html
    assert "Resolve top" not in html, html


def test_the_rest_of_the_priority_group_is_untouched() -> None:
    """The picker, its button, and the stop-on-stack setting all stay."""
    html = action_bar_html(board())

    assert "Priority" in html
    assert 'id="autopilot-target"' in html, html
    assert "startAutopilotFromPicker()" in html, html
    assert "Auto-pass until" in html, html
    assert "toggleStopOnStackSetting" in html, html
    assert "stop when anything lands on the stack" in html, html
    # And the picker still offers both halves of the `who` dimension.
    assert "my main 1" in html
    assert "opponent&#39;s end" in html


def test_the_other_groups_still_render() -> None:
    """Nothing outside the Priority group was disturbed: the step's own button
    (here CR 508's declare-attackers) is still offered."""
    html = action_bar_html(board(step="declare_attackers"))

    assert "Declare attackers" in html, html
