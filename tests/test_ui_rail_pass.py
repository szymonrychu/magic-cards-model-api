"""Mobile mode's Pass controls on the right rail (`ui/render/ribbon.js`'s
`renderRailPass`, rendered into `#rail-pass` by `mountRibbon`).

On a phone, plain priority no longer slides the drawer over the board
(`ui/drawer.js`'s `promptKey`), so the two pass verbs have to be reachable
with the drawer closed. They are the same verbs `#priority-box` offers,
through the very same `handlers.onVerb` - `app.js` wires one handler set into
`mountRibbon` and both surfaces call it - and they are shown only while the
priority box shows them and nothing else is being asked: never while the
seat does not hold priority, while an autopass run is answering it, or while
a decision is pending.

The fake DOM is `tests/test_ui_ribbon.py`'s. Where the rail sits on screen is
`tests/test_ui_viewport_layout.py`'s business.
"""

from __future__ import annotations

from test_ui_ribbon import FAKE_DOM
from ui_node import UI, requires_node, run_module

pytestmark = requires_node

SETUP = """
boxed("rail-pass");
const calls = [];
const RECORDING = { ...HANDLERS, onVerb: (verb) => calls.push(verb) };

// The button directly under `el` whose whole label is `label`.
function button(el, label) {
  return (el.children || []).flatMap((c) => [c, ...(c.children || [])]).find(
    (n) => n.tagName === "button" && n.textContent.trim() === label
  ) || null;
}
const labels = (el) =>
  (el.children || []).flatMap((c) => [c, ...(c.children || [])])
    .filter((n) => n.tagName === "button").map((n) => n.textContent.trim());

function mount(state, extra) {
  const store = makeStore(baseApp(state, extra));
  m.mountRibbon(document, store, RECORDING);
  store.set({});
  return store;
}
const rail = () => document.querySelector("#rail-pass");
const box = () => document.querySelector("#priority-box");
"""


def run(expression: str):
    return run_module("render/ribbon.js", FAKE_DOM + SETUP + expression)


def test_the_rail_offers_pass_and_pass_until_when_this_seat_holds_priority() -> None:
    result = run(
        """
        (() => {
          mount(baseState());
          return { hidden: rail().hidden, rail: labels(rail()), box: labels(box()) };
        })()
        """
    )
    assert result["hidden"] is False
    assert result["rail"] == ["Pass", "Pass until"]
    assert result["box"] == ["Pass", "Pass until"]


def test_rail_pass_makes_exactly_the_call_the_priority_box_pass_makes() -> None:
    result = run(
        """
        (() => {
          mount(baseState());
          click(button(box(), "Pass"));
          const boxPass = calls.splice(0);
          click(button(rail(), "Pass"));
          const railPass = calls.splice(0);
          click(button(box(), "Pass until"));
          const boxUntil = calls.splice(0);
          click(button(rail(), "Pass until"));
          const railUntil = calls.splice(0);
          return { boxPass, railPass, boxUntil, railUntil };
        })()
        """
    )
    assert result["boxPass"] == ["pass"]
    assert result["railPass"] == result["boxPass"]
    assert result["boxUntil"] == ["pass_until"]
    assert result["railUntil"] == result["boxUntil"]


def test_the_rail_hides_whenever_the_priority_box_offers_no_pass() -> None:
    result = run(
        """
        (() => {
          const seen = {};
          const look = (name, state, extra) => {
            mount(state, extra);
            seen[name] = { hidden: rail().hidden, rail: labels(rail()), box: labels(box()) };
          };
          look("theirs", baseState({ priority_holder: "bob" }));
          look("running", baseState(), { run: { verb: "pass_until" } });
          look("myDecision", baseState({
            pending_decision: { kind: "may", player: "alice", prompt: "Pay {1}?" },
          }));
          look("theirDecision", baseState({
            pending_decision: { kind: "may", player: "bob", prompt: "Pay {1}?" },
          }));
          look("back", baseState());
          return seen;
        })()
        """
    )
    assert result["theirs"]["box"] == [] and result["theirs"]["hidden"] is True
    assert result["running"]["box"] == ["Stop"] and result["running"]["hidden"] is True
    assert result["theirDecision"]["box"] == [] and result["theirDecision"]["hidden"] is True
    # A pending decision is answered in its own surface, and the drawer opens
    # for it, so the rail stands down even though the box keeps its Pass.
    assert result["myDecision"]["hidden"] is True
    for name in ("theirs", "running", "myDecision", "theirDecision"):
        assert result[name]["rail"] == [], name
    assert result["back"] == {
        "hidden": False,
        "rail": ["Pass", "Pass until"],
        "box": ["Pass", "Pass until"],
    }


def test_a_page_without_the_rail_still_mounts_the_ribbon() -> None:
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const store = makeStore(baseApp(baseState()));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return document.querySelector("#priority-box").children.length > 0;
        })()
        """,
    )
    assert result is True


def test_app_js_hands_the_rail_the_ribbons_own_handlers() -> None:
    """One handler set for both surfaces: nothing in `app.js` wires a
    rail-only pass path that could drift from the priority box's."""
    app = (UI / "app.js").read_text()
    assert app.count("mountRibbon(document, store, {") == 1
    assert "onRail" not in app and "rail-pass" not in app
