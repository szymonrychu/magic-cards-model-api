"""The pregame panel's repaint rule, run against the real `ui/app.js`.

**The bug.** CR 103.5 makes a keeping player "put a number of those cards
equal to the number of times that player has taken a mulligan on the bottom of
their library", so after a mulligan the panel shows a checkbox per card in
hand and only enables "Keep" once exactly that many are ticked
(`canKeep = pregameBottom.length === needed`). Ticking a box re-renders the
panel - that is the only thing that can move the button from disabled to
enabled.

`setHtml` used to refuse to repaint any panel containing the focused INPUT.
Ticking the checkbox focuses it, so the repaint that would enable "Keep" was
the exact repaint that got skipped, and every later poll skipped it too while
the box kept focus. The button stayed disabled forever: from the player's
chair, the mulligan did not work.

A checkbox or radio holds nothing typed, so there is nothing for a repaint to
destroy; the guard exists for half-typed text, and it now says so.

This loads the shipped `ui/app.js` under Node (the file the browser is served,
no build step) rather than re-describing its logic in Python, the same way
`tests/test_ui_autopilot.py` does.

CR 103.5 was verified with `knowledge_search(type="rules")` in the session
that wrote this file.
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


def repaint_with_focus_on(*, tag: str, type_: str | None) -> dict[str, Any]:
    """Call the real `setHtml` with `document.activeElement` being a control
    of this shape inside the panel, and report what the panel ended up with
    and whether focus was restored."""
    script = f"""
    const focused = {{
      tagName: {json.dumps(tag)},
      type: {json.dumps(type_)},
      value: "card-1",
      focused: false,
      focus() {{ this.focused = true; }},
    }};
    globalThis.document = {{
      activeElement: focused,
      body: {{}},
      addEventListener() {{}},
      querySelector: () => null,
      getElementById: () => null,
    }};
    const el = {{
      innerHTML: "<p>before</p>",
      contains: () => true,
      querySelector: () => focused,
    }};
    const {{ setHtml }} = require({json.dumps(str(APP_JS))});
    setHtml(el, "<p>after</p>");
    process.stdout.write(JSON.stringify({{ html: el.innerHTML, refocused: focused.focused }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_ticking_a_bottoming_checkbox_repaints_the_pregame_panel() -> None:
    """The repaint that enables "Keep" is not skipped by the checkbox that
    asked for it (CR 103.5's bottoming picker)."""
    result = repaint_with_focus_on(tag="INPUT", type_="checkbox")

    assert result["html"] == "<p>after</p>", (
        "the panel refused to repaint while the just-ticked checkbox held focus, "
        "which leaves the Keep button disabled forever"
    )
    assert result["refocused"] is True, "focus should return to the control that was ticked"


def test_a_radio_button_does_not_block_a_repaint_either() -> None:
    """Same reasoning: a radio holds no typed text."""
    assert repaint_with_focus_on(tag="INPUT", type_="radio")["html"] == "<p>after</p>"


def test_a_half_typed_text_field_still_blocks_the_repaint() -> None:
    """The guard's real purpose survives: a poll must not wipe what the player
    is in the middle of typing (an X value, a life amount)."""
    assert repaint_with_focus_on(tag="INPUT", type_="number")["html"] == "<p>before</p>"
    assert repaint_with_focus_on(tag="TEXTAREA", type_=None)["html"] == "<p>before</p>"
