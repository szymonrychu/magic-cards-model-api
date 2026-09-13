"""The pregame panel's repaint rule, run against the real shipped
`ui/render/ribbon.js` (Task 17 replaced the classic `ui/app.js` this file
used to load with `require` - `setHtml` no longer exists anywhere; keyed
`patchChildren` (`ui/dom.js`) replaced whole-panel `innerHTML` rewrites for
lists like the board's cards and the log's lines).

**The bug, as it originally shipped.** CR 103.5 makes a keeping player "put a
number of those cards equal to the number of times that player has taken a
mulligan on the bottom of their library", so after a mulligan the panel shows
a checkbox per card in hand and only enables "Keep" once exactly that many
are ticked. Ticking a box re-renders the panel - that used to be the only
thing that could move the button from disabled to enabled - and the old
`setHtml` refused to repaint any panel containing the focused INPUT at all,
checkbox and radio included. Ticking the checkbox focuses it, so the repaint
that would enable "Keep" was the exact repaint that got skipped, and the
button stayed disabled forever: from the player's chair, the mulligan did
not work.

**The mechanism, now.** `ui/render/ribbon.js`'s `renderPromptBox` still tears
down and rebuilds `#prompt-box` wholesale (`clear` + fresh `h()` nodes) on
every store update rather than patching it node-by-node - the box is one of
five mutually-exclusive things (game over, pregame, a decision, an in-flight
action's summary, the combat prompt), not a keyed list `patchChildren` fits.
So a naive full rebuild would tear a half-typed free-text decision answer
(CR 201.4/205.3e's "name a card"/"name a creature type",
`decision_controls.js`'s `renderTextControl`) out from under the player on
the next ~1.2s poll tick, the same way the checkbox bug worked. The fix
carries the old guard's *actual* intent forward rather than its bug: a
private `focusedTextInput(el)` helper skips the whole repaint only while
`document.activeElement` is a real typed-into control (`INPUT` of a text-like
type, or `TEXTAREA`) inside `el` - never a checkbox or radio, which hold
nothing typed, so there is nothing for a repaint to destroy.

A checkbox or radio holds nothing typed, so there is nothing for a repaint to
destroy; the guard exists for half-typed text, and it now says so.

CR 103.5 was verified with `knowledge_search(type="rules")` in the session
that wrote this file (and again in the session that ported it here).
"""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

# The fake DOM `test_ui_ribbon.py` uses, trimmed to what these three tests
# need: `El` with `dataset`/`classList`/`attrs`/`addEventListener` that
# actually records the listener, `contains` for the guard's own containment
# check, and `document.activeElement` left settable per test (unlike a real
# browser, nothing here moves it on its own - each test sets it explicitly to
# stand in for "the player just focused/clicked this control").
FAKE_DOM = """
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.style = {};
    this.hidden = false;
    this.listeners = {};
    this.classList = {
      set: new Set(),
      add: (c) => this.classList.set.add(c),
      remove: (c) => this.classList.set.delete(c),
      toggle: (c, on) => (on ? this.classList.set.add(c) : this.classList.set.delete(c)),
      contains: (c) => this.classList.set.has(c),
    };
  }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    c.parentNode = this;
    return c;
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
  setAttribute(k, v) {
    this.attrs[k] = v;
    if (k === "id") this.id = v;
    if (k === "type") this.type = v;
  }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  removeEventListener(type, fn) { if (this.listeners[type] === fn) delete this.listeners[type]; }
  contains(other) {
    if (other === this) return true;
    return this.children.some((c) => (c.contains ? c.contains(other) : false));
  }
  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0 }; }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  get textContent() {
    return this.children.map((c) => (c.textContent === undefined ? "" : c.textContent)).join("");
  }
  querySelector(sel) {
    if (!sel.startsWith("#")) return null;
    const id = sel.slice(1);
    const walk = (node) => {
      if (!node) return null;
      if (node.attrs && node.attrs.id === id) return node;
      for (const c of node.children || []) {
        const found = walk(c);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
}

function findByText(node, tag, text) {
  const content = typeof node.textContent === "string" ? node.textContent : "";
  if (node.tagName === tag && content.includes(text)) return node;
  for (const c of node.children || []) {
    const found = findByText(c, tag, text);
    if (found) return found;
  }
  return null;
}

function findByType(node, tag, type) {
  if (node.tagName === tag && node.attrs && node.attrs.type === type) return node;
  for (const c of node.children || []) {
    const found = findByType(c, tag, type);
    if (found) return found;
  }
  return null;
}

globalThis.window = { scrollX: 0, scrollY: 0 };
globalThis.document = new El("#document");
document.activeElement = null;
document.createElement = (t) => new El(t);
document.createTextNode = (s) => ({ nodeType: 3, textContent: s });
document.createDocumentFragment = () => new El("#fragment");

function boxed(id, hidden) {
  const el = document.createElement("div");
  el.setAttribute("id", id);
  el.hidden = Boolean(hidden);
  document.appendChild(el);
  return el;
}
boxed("prompt-box");
boxed("casting-strip", true);
boxed("priority-box");
boxed("stops-box");
boxed("popover", true);

function makeStore(initial) {
  let state = initial;
  const listeners = [];
  return {
    get: () => state,
    set(patch) {
      state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
      for (const fn of listeners) fn(state);
    },
    subscribe(fn) {
      listeners.push(fn);
      return () => {};
    },
  };
}

function baseState(overrides) {
  return {
    you: "alice",
    winner: null,
    drawn: false,
    pregame: null,
    pending_decision: null,
    step: "precombat_main",
    active_player: "alice",
    priority_holder: "alice",
    battlefield: [],
    hands: { alice: [] },
    players: [
      { username: "alice", life: 20 },
      { username: "bob", life: 20 },
    ],
    stack: [],
    combat: { attacks: [] },
    ...overrides,
  };
}

function baseApp(state, overrides) {
  return {
    state,
    action: null,
    decisionDraft: {},
    combatDraft: {},
    run: null,
    stops: { mine: [], theirs: [] },
    symbols: {},
    aiStatus: null,
    ...overrides,
  };
}

const NOOP = () => {};
function handlers(overrides) {
  return {
    onToggleMode: NOOP, onSetX: NOOP, onChooseColor: NOOP, onChooseAltCost: NOOP,
    onTogglePitch: NOOP, onToggleConvoke: NOOP, onToggleDelve: NOOP, onFocusSlot: NOOP,
    onUnfill: NOOP, onSetShare: NOOP, onDone: NOOP, onCast: NOOP, onCancel: NOOP,
    onHoldPriority: NOOP, onDecisionDraft: NOOP, onDecide: NOOP, onVerb: NOOP,
    onStopRun: NOOP, onToggleStop: NOOP, onToggleStackStop: NOOP,
  onAttackConfirm: NOOP, onAttackNone: NOOP,
    onBlockConfirm: NOOP, onBlockNone: NOOP, onConcede: NOOP, onPregame: NOOP,
    nameOf: (id) => id,
    ...overrides,
  };
}

function hasAttr(el, name) {
  return Object.prototype.hasOwnProperty.call(el.attrs || {}, name);
}
"""


def test_a_focused_checkbox_does_not_block_a_repaint() -> None:
    """A checkbox holds nothing typed, so focus on one must never freeze the
    repaint - the original bug was CR 103.5's bottoming checkbox freezing
    the repaint that would have enabled "Keep". That picker is clicked cards
    in `render/decision_window.js` now, so a synthetic checkbox stands in
    for it here, the same way the radio test below does."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({
            pending_decision: {
              kind: "may", player: "alice", source: "Fog",
              prompt: "Prevent all combat damage this turn?",
            },
          });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, handlers());
          store.set({});

          const box = document.querySelector("#prompt-box");
          const checkbox = document.createElement("input");
          checkbox.setAttribute("type", "checkbox");
          box.appendChild(checkbox);
          document.activeElement = checkbox;

          store.set({});

          return {
            checkboxSurvived: document.querySelector("#prompt-box").contains(checkbox),
            promptText: document.querySelector("#prompt-box").textContent,
          };
        })()
        """,
    )
    assert result["checkboxSurvived"] is False, (
        "a checkbox-focused repaint must go through - a checkbox holds nothing typed"
    )
    assert "Fog" in result["promptText"]


def test_a_pregame_waiting_on_the_other_seat_still_renders_in_the_prompt_box() -> None:
    """Only this seat's own mulligan moves to the decision window; while the
    other seat is still deciding, the prompt box says so, as before."""
    text = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({
            pregame: {
              step: "mulligan",
              awaiting_mulligan: ["bob"],
              cards_to_bottom: { alice: 0, bob: 0 },
              can_mulligan: { alice: true, bob: true },
              mulligans_taken: { alice: 1 },
            },
          });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, handlers());
          store.set({});
          return document.querySelector("#prompt-box").textContent;
        })()
        """,
    )
    assert "waiting on bob" in text
    assert "You have taken 1 mulligan" in text


def test_a_radio_button_does_not_block_a_repaint_either() -> None:
    """Same reasoning: a radio holds no typed text. `ribbon.js`'s controls
    never render a real `<input type="radio">` today (`choose_a_mode`'s
    options render as chip buttons, not a radio group), so this pins the
    guard's own type check directly with a synthetic one standing in for it -
    a future radio control gets this for free rather than by accident."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({
            pending_decision: {
              kind: "may", player: "alice", source: "Fog",
              prompt: "Prevent all combat damage this turn?",
            },
          });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, handlers());
          store.set({});

          const box = document.querySelector("#prompt-box");
          const radio = document.createElement("input");
          radio.setAttribute("type", "radio");
          box.appendChild(radio);
          document.activeElement = radio;

          store.set({}); // a poll tick with nothing new to say

          return {
            radioSurvived: document.querySelector("#prompt-box").contains(radio),
            promptText: document.querySelector("#prompt-box").textContent,
          };
        })()
        """,
    )
    assert result["radioSurvived"] is False, (
        "a radio-focused repaint must go through like any other - the synthetic marker "
        "node should have been torn down and rebuilt, not preserved"
    )
    assert "Fog" in result["promptText"], "the panel must still show the real content afterward"


def test_a_half_typed_text_field_still_blocks_the_repaint() -> None:
    """The guard's real purpose survives: a poll must not wipe what the
    player is in the middle of typing - here, CR 201.4's "name a card"
    (`choose_a_quality` with no fixed `options`, `decision_controls.js`'s
    `renderTextControl`), the real production text-input path rather than a
    stand-in."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({
            pending_decision: {
              kind: "choose_a_quality", player: "alice", source: "Pithing Needle",
              prompt: "Name a card.", options: [],
            },
          });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, handlers());
          store.set({});

          const before = findByType(document.querySelector("#prompt-box"), "input", "text");
          document.activeElement = before;

          store.set({}); // a poll tick while the player is mid-keystroke

          const after = findByType(document.querySelector("#prompt-box"), "input", "text");
          return { samePromptBox: after === before };
        })()
        """,
    )
    assert result["samePromptBox"] is True, (
        "the free-text control's own INPUT node must survive a repaint while it holds "
        "focus, or the player's cursor (and, in a real browser, their half-typed answer) "
        "is thrown away on the next poll tick"
    )
