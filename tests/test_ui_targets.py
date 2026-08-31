"""`sendTargets` in `ui/app.js`, exercised for real under Node.

This is the regression guard for the bug that motivated the whole affordances
layer. `POST .../actions/activate` distinguishes three answers:

- no `targets` field at all - "the player has not chosen", which makes the
  engine ask (CR 601.2c reached through CR 602.2b, as a `choose_targets`
  decision);
- `targets: []` - "the player chose none", a real and legal answer for an
  "up to N" requirement (CR 115.6) and a wrong one for anything else;
- `targets: [...]` - these ones.

The old UI collapsed the first into the second: it always sent
`targets: game.selectedTargets.slice()`, which for an untouched picker is
`[]`. Karn, the Great Creator's `+1` ("up to one target noncreature
artifact") therefore went on the stack targeting nothing every single time it
was activated from the browser, and no error was ever shown, because `[]` is
a *legal* answer to that particular ability - just not the one the player
meant. `sendTargets` is the single place in `ui/app.js` allowed to build that
field, and this file pins its three-way behaviour.

Like `test_ui_autopilot.py`, this loads and runs the actual shipped JS rather
than a Python re-description of it. `ui/app.js` has no build step, so this is
the exact file the browser is served at `GET /play/app.js`.
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

#: A sentinel the JS side maps `undefined` onto, since JSON has no way to tell
#: `undefined` from `null` and the whole point of this file is that
#: "no field at all" is its own answer.
ABSENT = "<absent>"


def send_targets(action: dict[str, Any]) -> Any:
    """Call the real `sendTargets` with one `game.action`-shaped object."""
    script = f"""
    const {{ sendTargets }} = require({json.dumps(str(APP_JS))});
    const result = sendTargets({json.dumps(action)});
    process.stdout.write(JSON.stringify(result === undefined ? {json.dumps(ABSENT)} : result));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def action(**overrides: Any) -> dict[str, Any]:
    base = {"targets": [], "touchedTargets": False, "affordance": None}
    base.update(overrides)
    return base


def targeting(minimum: int, maximum: int | None = 1, anything: bool = True) -> dict[str, Any]:
    return {"targets_anything": anything, "minimum": minimum, "maximum": maximum, "candidates": []}


# -- the Karn case: an untouched picker must not answer "none" ---------------


def test_an_untouched_picker_on_an_up_to_one_ability_sends_no_field_at_all() -> None:
    """Karn, the Great Creator's `+1`, exactly as it broke.

    "Up to one target noncreature artifact" has `minimum: 0`, so `[]` would
    be accepted by the engine as a real answer of zero targets. The player
    who has not touched the picker has not given that answer, so no field
    goes at all and the engine asks.
    """
    assert send_targets(action(affordance=targeting(minimum=0))) == ABSENT


def test_an_untouched_picker_on_a_mandatory_target_sends_no_field_at_all() -> None:
    """Lightning Bolt's "any target": `minimum: 1`, nothing chosen yet."""
    assert send_targets(action(affordance=targeting(minimum=1))) == ABSENT


# -- a deliberate "none", which is only ever legal at minimum zero ----------


def test_a_touched_picker_left_empty_on_an_up_to_one_ability_sends_an_empty_list() -> None:
    """CR 115.6's real answer: the player opened the picker and chose none.

    This is the one case where `[]` is what the player actually means, and
    the engine must get it rather than being asked again.
    """
    assert send_targets(action(touchedTargets=True, affordance=targeting(minimum=0))) == []


def test_a_touched_picker_left_empty_on_a_mandatory_target_still_sends_no_field() -> None:
    """Choosing zero is not a legal answer when the minimum is one, so even a
    touched-and-emptied picker falls back to letting the engine ask rather
    than sending an announcement that cannot be legal."""
    assert send_targets(action(touchedTargets=True, affordance=targeting(minimum=1))) == ABSENT


# -- chosen targets always go ------------------------------------------------


def test_chosen_targets_are_sent_verbatim() -> None:
    chosen = ["11111111-1111-1111-1111-111111111111", "alice"]
    assert (
        send_targets(action(targets=chosen, affordance=targeting(minimum=1, maximum=2))) == chosen
    )


def test_chosen_targets_are_sent_even_before_the_affordance_arrives() -> None:
    """The candidate fetch is asynchronous; a player who clicked a target
    before it landed must not have that click dropped."""
    assert send_targets(action(targets=["alice"], affordance=None)) == ["alice"]


# -- something that does not target ------------------------------------------


def test_a_non_targeting_ability_sends_no_field() -> None:
    """Karn's `-2` does not target. Sending `targets: []` for it would be
    harmless but meaningless; sending nothing is the honest announcement."""
    assert send_targets(action(affordance=targeting(0, 0, anything=False))) == ABSENT


def test_a_non_targeting_ability_sends_no_field_even_if_the_picker_was_touched() -> None:
    assert (
        send_targets(action(touchedTargets=True, affordance=targeting(0, 0, anything=False)))
        == ABSENT
    )
