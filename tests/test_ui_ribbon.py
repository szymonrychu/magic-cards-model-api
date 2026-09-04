"""`ui/render/ribbon.js`'s `mountRibbon` against a fake DOM (the pattern from
`tests/test_ui_dom.py`, extended with `querySelector` by id and a
`textContent` getter that aggregates the whole subtree, since `ribbon.js`
builds several levels of nested nodes per section).

Two scenarios, matching the task brief: a `may` decision (the simplest
`decisions.js` view model - no controls, just yes/no) and a Kozilek's
Command cast with two target slots (one filled, one open) exercising the
casting strip's mode chips, X input, and slot chips together. Both assert
only that mounting does not throw and that the rendered `#prompt-box`/
`#casting-strip` fakes contain the labels a player would need to see -
`ribbon.js`'s own unit-level correctness (which draft field each control
patches, `readyToFire`'s exact gating) is exercised indirectly through the
modules it composes (`decisions.js`, `actions.js`, `targeting.js`), which
already have their own direct tests.
"""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

# A fake DOM minimal enough for `dom.js`'s `h`/`clear`/`patchChildren` (the
# `test_ui_dom.py` shape) plus what `ribbon.js` itself needs: `querySelector`
# by id (`mountRibbon` looks up its four boxes that way), a `textContent`
# getter that walks the whole subtree (real DOM's own behaviour - `ribbon.js`
# nests several `h()` calls deep per section), and `contains`/
# `getBoundingClientRect`/`style` for the popover code path, even though
# neither scenario below calls it.
FAKE_DOM = """
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.style = {};
    this.hidden = false;
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
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener() {}
  removeEventListener() {}
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
globalThis.window = { scrollX: 0, scrollY: 0 };
globalThis.document = new El("#document");
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

const NOOP = () => {};
const HANDLERS = {
  onToggleMode: NOOP, onSetX: NOOP, onChooseColor: NOOP, onChooseAltCost: NOOP,
  onTogglePitch: NOOP, onToggleConvoke: NOOP, onToggleDelve: NOOP, onFocusSlot: NOOP,
  onUnfill: NOOP, onSetShare: NOOP, onDone: NOOP, onCast: NOOP, onCancel: NOOP,
  onHoldPriority: NOOP, onDecisionDraft: NOOP, onDecide: NOOP, onVerb: NOOP,
  onStopRun: NOOP, onToggleStop: NOOP, onAttackConfirm: NOOP, onAttackNone: NOOP,
  onBlockConfirm: NOOP, onBlockNone: NOOP, onConcede: NOOP, onPregame: NOOP,
  nameOf: (id) => id,
};

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
"""

# The exact Kozilek's Command fixture and affordance `test_ui_actions.py`
# uses for its own two-mode flow test, with a top-level `mana_cost` added -
# `serialize.py`'s real card view carries `mana_cost` both at the top level
# (`state.card.mana_cost`, what a board card shows) and again inside `cast`
# (`cast_hints`' own copy, read when nothing has been announced yet);
# `ribbon.js`'s casting-strip header reads the top-level one.
KOZILEK = {
    "id": "kc",
    "name": "Kozilek's Command",
    "mana_cost": "{X}{C}{C}",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Kindred Instant",
    "cast": {
        "mana_cost": "{X}{C}{C}",
        "needs_x": True,
        "modes": {
            "instruction": "Choose two -",
            "minimum": 2,
            "maximum": 2,
            "repeatable": False,
            "understood": True,
            "options": [
                {"number": 1, "text": "Target player creates X ..."},
                {"number": 2, "text": "Target player scries X, then draws a card."},
                {"number": 3, "text": "Exile target creature with mana value X or less."},
                {"number": 4, "text": "Exile up to X target cards from graveyards."},
            ],
        },
        "targeting": {
            "targets_anything": True,
            "understood": False,
            "minimum": 0,
            "maximum": None,
            "requirements": [],
        },
    },
    "abilities": [],
}
KOZ_AFF = {
    "targets_anything": True,
    "understood": True,
    "minimum": 2,
    "maximum": 2,
    "requirements": [
        {
            "phrase": "target player",
            "count": 1,
            "minimum": 1,
            "unbounded": False,
            "variable_count": False,
            "mode": 1,
            "mode_text": "Target player creates X ...",
        },
        {
            "phrase": "target player",
            "count": 1,
            "minimum": 1,
            "unbounded": False,
            "variable_count": False,
            "mode": 2,
            "mode_text": "Target player scries X, then draws a card.",
        },
    ],
    "division": None,
    "candidates": [
        {"id": "alice", "kind": "player", "name": "alice", "requirements": [0, 1]},
        {"id": "bob", "kind": "player", "name": "bob", "requirements": [0, 1]},
    ],
}


def test_mounting_a_may_decision_renders_prompt_and_yes_no() -> None:
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
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return {
            prompt: document.querySelector("#prompt-box").textContent,
            stripHidden: document.querySelector("#casting-strip").hidden,
            priority: document.querySelector("#priority-box").textContent,
            stops: document.querySelector("#stops-box").textContent,
          };
        })()
        """,
    )
    assert "Fog" in result["prompt"]
    assert "Prevent all combat damage this turn?" in result["prompt"]
    assert "Yes" in result["prompt"]
    assert "No" in result["prompt"]
    assert result["stripHidden"] is True
    assert "Pass" in result["priority"]
    assert "declare attackers" in result["stops"]
    assert "declare blockers" in result["stops"]
    # `untap`/`cleanup` are excluded from the stops grid.
    assert "untap" not in result["stops"]
    assert "cleanup" not in result["stops"]


def test_mounting_a_kozilek_command_action_renders_strip_with_two_slots() -> None:
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const action = {
            kind: "cast", cardId: koz.id, card: koz, abilityIndex: null, keyword: null,
            modes: [1, 2], xValue: 1, color: null, altCost: null,
            choosing: [], convoke: [], delve: [], crewing: [],
            slots: [
              {
                index: 0, requirementIndex: 0, label: "Mode 1: target player",
                count: 1, minimum: 1, mode: 1, targets: ["alice"],
              },
              {
                index: 1, requirementIndex: 1, label: "Mode 2: target player",
                count: 1, minimum: 1, mode: 2, targets: [],
              },
            ],
            shares: {}, pinned: [], freeTargets: [], pendingTargets: [],
            affordance: aff, error: null, holdPriority: false, done: false,
          };
          const state = baseState({ hands: { alice: [koz] } });
          const store = makeStore(baseApp(state, { action }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return {
            prompt: document.querySelector("#prompt-box").textContent,
            strip: document.querySelector("#casting-strip").textContent,
            stripHidden: document.querySelector("#casting-strip").hidden,
          };
        })()
        """,
        koz=KOZILEK,
        aff=KOZ_AFF,
    )
    assert "Kozilek's Command" in result["prompt"]
    assert "Cancel" in result["prompt"]
    assert result["stripHidden"] is False
    strip = result["strip"]
    assert "Kozilek's Command" in strip
    # Both printed modes are offered as chips, chosen or not (CR 700.2).
    assert "Target player creates X" in strip
    assert "Exile target creature with mana value X or less." in strip
    # The filled slot shows its target's name; the open one shows its label
    # with no target yet - the two-slot case the task brief calls for.
    assert "Mode 1: target player" in strip
    assert "alice" in strip
    assert "Mode 2: target player" in strip
    assert "Announce X" in strip
    assert "keep priority" in strip
