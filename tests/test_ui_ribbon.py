"""`ui/render/ribbon.js`'s `mountRibbon` against a fake DOM (the pattern from
`tests/test_ui_dom.py`, extended with `querySelector` by id and a
`textContent` getter that aggregates the whole subtree, since `ribbon.js`
builds several levels of nested nodes per section).

Two structural scenarios, matching the task brief: a `may` decision (the
simplest `decisions.js` view model - no controls, just yes/no) and a
Kozilek's Command cast with two target slots (one filled, one open)
exercising the casting strip's mode chips, X input, and slot chips together.
Both assert only that mounting does not throw and that the rendered
`#prompt-box`/`#casting-strip` fakes contain the labels a player would need
to see.

One more scenario exercises an actual handler end to end: the fake DOM's
`addEventListener` records the listener `h()` wires up, and `click(el)`
(found via `findByText`) invokes it - a `choose_a_mode` decision's option
button reports `{choice: ...}` back through `onDecisionDraft`. The complex
kinds (scry, surveil, `choose_cards`, `order_triggers`) are answered in
`render/decision_window.js` now (`tests/test_ui_decision_window.py`); here
the prompt box only points at it. Everything
else about a control's own correctness (which draft field each kind reads
back, `readyToFire`'s exact gating) belongs to `decisions.js`/`actions.js`/
`targeting.js`'s own direct tests - these just confirm the wiring
between a rendered button and the handler it is supposed to call is real,
not merely plausible-looking markup.
"""

from __future__ import annotations

import pytest
from ui_node import requires_node, run_module

pytestmark = requires_node

# A fake DOM minimal enough for `dom.js`'s `h`/`clear`/`patchChildren` (the
# `test_ui_dom.py` shape) plus what `ribbon.js` itself needs: `querySelector`
# by id (`mountRibbon` looks up its four boxes that way), a `textContent`
# getter that walks the whole subtree (real DOM's own behaviour - `ribbon.js`
# nests several `h()` calls deep per section), `contains`/
# `getBoundingClientRect`/`style` for the popover code path, and an
# `addEventListener` that actually records the listener (keyed by event
# type) instead of discarding it, so a test can find a rendered button and
# invoke the handler `h()` wired to it - `click(el)` below does exactly that.
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
  // A real `removeChild` un-parents the child, which is the difference
  // between a node still in the page and one a click has nothing to land on.
  removeChild(c) {
    this.children = this.children.filter((x) => x !== c);
    if (c.parentNode === this) c.parentNode = null;
    return c;
  }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
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

// The first `tag` element (depth-first, document order) whose own
// `textContent` includes `text` - enough to find "the button labelled
// Brainstorm" or "the first Move button" without a class/id to key off.
function findByText(node, tag, text) {
  const content = typeof node.textContent === "string" ? node.textContent : "";
  if (node.tagName === tag && content.includes(text)) return node;
  for (const c of node.children || []) {
    const found = findByText(c, tag, text);
    if (found) return found;
  }
  return null;
}

// Whether `node` is still reachable from `document` - a child its parent has
// dropped is not, even though it still remembers its own listeners.
function attached(node) {
  let at = node;
  while (at.parentNode) {
    if (!at.parentNode.children.includes(at)) return false;
    at = at.parentNode;
  }
  return at === globalThis.document;
}

// Dispatch a click the way a browser does, rather than calling this node's
// handler: the event goes to the nearest inclusive ancestor still in the
// tree - to nothing at all when there is none - and bubbles from there. A
// fake that just invoked the listener could not tell a live button from one
// a re-render had already replaced, which is exactly the defect the popover
// and the press guard exist to prevent. A no-op if nothing is listening (a
// disabled button, or an element that was not found at all).
function click(el) {
  if (!el) return;
  let target = el;
  while (target && !attached(target)) target = target.parentNode || null;
  if (!target) return;
  for (let node = target; node; node = node.parentNode) {
    if (node.listeners && node.listeners.click) {
      node.listeners.click({ target, currentTarget: node });
    }
  }
}

// The first `tag` element (depth-first) whose `dataset.field` equals `field`
// - how the casting-strip focus-guard test below finds the X input among
// several inputs without a class or id to key off.
function findByField(node, tag, field) {
  if (node.tagName === tag && node.dataset && node.dataset.field === field) return node;
  for (const c of node.children || []) {
    const found = findByField(c, tag, field);
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
boxed("concede-slot");
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
  onStopRun: NOOP, onToggleStop: NOOP, onToggleStackStop: NOOP,
  onToggleKeepPriorityByDefault: NOOP,
  onAttackConfirm: NOOP, onAttackNone: NOOP,
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


# -- renderCombatPrompt: a paused declare_attackers/declare_blockers -------
#
# `state.step` and `state.priority_holder` are both frozen at whatever they
# were *before* a combat-declaration pause (`app.py`'s `advance` docstring),
# never the step the pause is trying to enter - so `renderCombatPrompt` reads
# `state.pending_decision` instead, which is stamped the moment the question
# is actually asked. These two are the first tests this file has ever had for
# that branch, or for `renderCombatPrompt`/`renderDeclareAttackers`/
# `renderDeclareBlockers` at all.


def test_a_paused_declare_attackers_decision_renders_the_attack_prompt() -> None:
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const calls = [];
          const state = baseState({
            step: "beginning_of_combat",
            pending_decision: { player: "alice", kind: "declare_attackers" },
          });
          const handlers = { ...HANDLERS, onAttackNone: () => calls.push("no-attack") };
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, handlers);
          store.set({});
          const prompt = document.querySelector("#prompt-box");
          click(findByText(prompt, "button", "No attack"));
          return { text: prompt.textContent, calls };
        })()
        """,
    )
    assert "Declare attackers" in result["text"]
    assert "508.1" in result["text"]
    assert result["calls"] == ["no-attack"]


def test_a_paused_declare_blockers_decision_renders_the_block_prompt() -> None:
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const calls = [];
          const state = baseState({
            step: "declare_attackers",
            active_player: "bob",
            pending_decision: { player: "alice", kind: "declare_blockers" },
          });
          const handlers = { ...HANDLERS, onBlockNone: () => calls.push("no-blocks") };
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, handlers);
          store.set({});
          const prompt = document.querySelector("#prompt-box");
          click(findByText(prompt, "button", "No blocks"));
          return { text: prompt.textContent, calls };
        })()
        """,
    )
    assert "Declare blockers" in result["text"]
    assert "509.1" in result["text"]
    assert result["calls"] == ["no-blocks"]


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
            promptHidden: document.querySelector("#prompt-box").hidden,
            strip: document.querySelector("#casting-strip").textContent,
            stripHidden: document.querySelector("#casting-strip").hidden,
          };
        })()
        """,
        koz=KOZILEK,
        aff=KOZ_AFF,
    )
    # `#casting-strip` is a panel in the ribbon now, directly under
    # `#prompt-box` rather than down at the board - so the prompt box naming
    # the in-flight action, and offering its own Cancel, printed the same two
    # things twice in a row, an inch apart. It stands down entirely while an
    # announcement is in flight: the strip below is the one place asking.
    assert result["promptHidden"] is True
    assert result["prompt"] == ""
    assert result["stripHidden"] is False
    assert "Cancel" in result["strip"]
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


# -- renderAltCosts: only the affordance's own candidates get offered ------
#
# The second problem the task brief names: `renderAltCosts`'s `requires`
# branch used to list the player's *entire* hand as pitch chips, with no
# filter for which ones actually pay the cost - Solitude's evoke is "Exile a
# white card from your hand", and offering a blue card got a server refusal
# on click. The affordance's own `alternative_costs[i].candidates`
# (`schemas.AlternativeCostView`) is the engine's own answer to "which of
# these are legal"; this is the one place `renderAltCosts` should read it
# from, never the raw hand.
SOLITUDE = {
    "id": "s1",
    "name": "Solitude",
    "mana_cost": "{3}{W}{W}",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Creature",
    "cast": {
        "mana_cost": "{3}{W}{W}",
        "needs_x": False,
        "modes": None,
        "targeting": {
            "targets_anything": False,
            "understood": True,
            "minimum": 0,
            "maximum": 0,
            "requirements": [],
        },
    },
    "abilities": [],
}
SOLITUDE_AFF = {
    "targets_anything": False,
    "understood": True,
    "minimum": 0,
    "maximum": 0,
    "requirements": [],
    "division": None,
    "candidates": [],
    "alternative_costs": [
        {
            "index": 0,
            "source": "keyword",
            "keyword": "Evoke",
            "source_name": "Solitude",
            "rule": "CR 702.74a",
            "description": "Exile a white card from your hand",
            "cost": None,
            "requires": 1,
            "candidates": [
                {
                    "id": "w1",
                    "kind": "card",
                    "name": "Savannah Lions",
                    "zone": "hand",
                    "controller": "alice",
                    "pays": "exile",
                },
            ],
            "available": True,
            "unavailable": None,
        }
    ],
}


def _solitude_action(overrides: dict | None = None) -> dict:
    return {
        "kind": "cast",
        "cardId": SOLITUDE["id"],
        "card": SOLITUDE,
        "abilityIndex": None,
        "keyword": None,
        "modes": [],
        "xValue": None,
        "color": None,
        "altCost": 0,
        "choosing": [],
        "convoke": [],
        "delve": [],
        "crewing": [],
        "slots": [],
        "shares": {},
        "pinned": [],
        "freeTargets": [],
        "pendingTargets": [],
        "affordance": SOLITUDE_AFF,
        "error": None,
        "holdPriority": False,
        "done": False,
        **(overrides or {}),
    }


def test_alt_cost_pitch_tray_offers_only_the_affordances_own_candidate() -> None:
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const hand = [
            SOLITUDE,
            { id: "w1", name: "Savannah Lions" },
            { id: "b1", name: "Faithful Mending" },
          ];
          const state = baseState({ hands: { alice: hand } });
          const action = solitudeAction;
          const store = makeStore(baseApp(state, { action }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return document.querySelector("#casting-strip").textContent;
        })()
        """,
        SOLITUDE=SOLITUDE,
        solitudeAction=_solitude_action(),
    )
    assert "Savannah Lions" in result
    assert "Faithful Mending" not in result, (
        "the whole hand used to be offered as pitch chips - only the "
        "affordance's own legal candidate must show"
    )


def test_alt_cost_pitch_tray_says_so_plainly_when_nothing_is_eligible() -> None:
    """The task brief's own words: if `candidates` is empty but `requires >
    0`, say so plainly rather than rendering an empty tray that looks like a
    bug."""
    affordance = {
        **SOLITUDE_AFF,
        "alternative_costs": [{**SOLITUDE_AFF["alternative_costs"][0], "candidates": []}],
    }
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const hand = [SOLITUDE, { id: "w1", name: "Savannah Lions" }];
          const state = baseState({ hands: { alice: hand } });
          const action = solitudeAction;
          const store = makeStore(baseApp(state, { action }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return document.querySelector("#casting-strip").textContent;
        })()
        """,
        SOLITUDE=SOLITUDE,
        solitudeAction=_solitude_action({"affordance": affordance}),
    )
    assert "Savannah Lions" not in result
    assert "nothing eligible" in result.lower()


def test_a_choose_cards_decision_points_at_the_decision_window() -> None:
    """Every `choose_cards` is answered with real card images in
    `render/decision_window.js`'s floating window now; the prompt box only
    says where the question went, and renders no option chips of its own."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const pending = {
            kind: "choose_cards", player: "alice", source: "Merchant Scroll",
            prompt: "Search your library for an instant card.",
            cards: ["c1", "c2"],
            card_details: [
              { id: "c1", name: "Brainstorm" },
              { id: "c2", name: "Ponder" },
            ],
            amount: 1, exact: true,
          };
          const state = baseState({ pending_decision: pending });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          const box = document.querySelector("#prompt-box");
          return {
            text: box.textContent,
            chip: Boolean(findByText(box, "button", "Brainstorm")),
            show: Boolean(findByText(box, "button", "Show decision window")),
          };
        })()
        """,
    )
    assert "Merchant Scroll" in result["text"]
    assert "decision window" in result["text"]
    assert result["chip"] is False
    assert result["show"] is True


def test_clicking_a_choose_a_mode_option_reports_choice() -> None:
    """A small decision still renders in the prompt box, and its option
    buttons still report through `onDecisionDraft`."""
    calls = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const calls = [];
          const handlers = { ...HANDLERS, onDecisionDraft: (patch) => calls.push(patch) };
          const pending = {
            kind: "choose_a_mode", player: "alice", source: "Charm",
            prompt: "Choose one.", options: ["Counter target spell", "Draw a card"],
          };
          const state = baseState({ pending_decision: pending });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, handlers);
          store.set({});
          click(findByText(document, "button", "Draw a card"));
          return calls;
        })()
        """,
    )
    assert calls == [{"choice": "Draw a card"}]


def test_a_decision_anchored_to_a_rendered_card_leaves_only_a_pointer_in_the_prompt_box() -> None:
    """A small decision whose `source_card_id` names a card on the board is
    answered in `render/decision_popover.js`'s popover by that card; the
    prompt box says where it went and renders none of its controls. The same
    decision with no rendered source still renders its Yes/No here."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const cards = [];
          document.querySelectorAll = (sel) =>
            cards.filter(() => sel.includes(".card[data-card-id]"));
          const pending = {
            kind: "may", player: "alice", source: "Rhystic Study",
            prompt: "Pay one mana?", source_card_id: "rs",
          };
          const store = makeStore(baseApp(baseState({ pending_decision: pending })));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          const box = document.querySelector("#prompt-box");
          const yes = () => Boolean(findByText(box, "button", "Yes"));
          const unrendered = { text: box.textContent, yes: yes() };
          const card = document.createElement("div");
          card.dataset.cardId = "rs";
          card.getBoundingClientRect = () =>
            ({ left: 10, top: 10, right: 118, bottom: 160, width: 108, height: 150 });
          card.closest = () => null;
          document.appendChild(card);
          cards.push(card);
          store.set({});
          return {
            unrendered,
            anchored: { text: box.textContent, yes: yes() },
          };
        })()
        """,
    )
    assert result["unrendered"]["yes"] is True
    assert "Pay one mana?" in result["unrendered"]["text"]
    assert result["anchored"]["yes"] is False
    assert "Rhystic Study" in result["anchored"]["text"]
    assert "at the card" in result["anchored"]["text"]


def test_a_scry_decision_renders_no_split_rows_in_the_prompt_box() -> None:
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const pending = {
            kind: "scry", player: "alice", source: "Scry", prompt: "Scry 2.",
            cards: ["a", "b"],
            card_details: [
              { id: "a", name: "Card A" },
              { id: "b", name: "Card B" },
            ],
          };
          const state = baseState({ pending_decision: pending });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          const box = document.querySelector("#prompt-box");
          return { text: box.textContent, move: Boolean(findByText(box, "button", "Move")) };
        })()
        """,
    )
    assert result["move"] is False
    assert "decision window" in result["text"]


# --------------------------------------------------------- conceding safely
#
# From the browser pass. `passButtonsFor` offers no verbs at all while the
# opponent holds priority, so the priority box emptied out and Concede rose
# into the position "Pass" had been in one tick earlier. The next click
# there ended the game. CR 104.3a keeps conceding legal at any time, so the
# button stays - it just stops being a single click, and it goes away
# entirely once somebody has already won.


def test_concede_is_a_plain_button_until_it_is_armed() -> None:
    """Concede renders into `#concede-slot` (the pinned top-right toolbar),
    not into `#priority-box` - moved there so it sits with Report and Lobby
    at every width rather than in the ribbon."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({ priority_holder: "bob" });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          const slot = document.querySelector("#concede-slot");
          const priorityBox = document.querySelector("#priority-box");
          return {
            slotText: slot.children.map((c) => c.textContent),
            priorityBoxHasConcede: findByText(priorityBox, "button", "Concede") !== null,
          };
        })()
        """,
    )
    assert result["slotText"] == ["Concede"], "Concede should be the only thing in #concede-slot"
    assert result["priorityBoxHasConcede"] is False, (
        "Concede must not also render into #priority-box"
    )


def test_an_armed_concede_says_so_and_asks_for_the_second_click() -> None:
    label = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({ priority_holder: "bob" });
          const store = makeStore(baseApp(state, { concedeArmed: true }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return findByText(document, "button", "Concede").textContent;
        })()
        """,
    )
    assert label == "Concede - click again to confirm"


@pytest.mark.parametrize("over", [{"winner": "bob"}, {"drawn": True}])
def test_a_finished_game_offers_no_concede_at_all(over: dict) -> None:
    """`winner || drawn` - a draw ends the game as finally as a win does, and
    it is the pair the rest of the UI (`stopReason`, `renderPromptBox`) reads
    as over."""
    found = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({ ...over, priority_holder: "bob" });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return findByText(document.querySelector("#concede-slot"), "button", "Concede") !== null;
        })()
        """,
        over=over,
    )
    assert found is False


# ------------------------------------------ retrying a refused announcement
#
# From the browser pass. A spell whose only slot is a single required target
# fires the moment that slot is filled, so it never grows a Cast button. When
# the dry run came back refused ("no combination of the available mana
# sources can pay ..."), the strip sat there complete, unfired, and with
# nothing to click: fixing what the refusal named left no way to try again
# short of cancelling the whole announcement.

ONE_TARGET_SPELL = {
    "id": "dd",
    "name": "Demonic Dread",
    "mana_cost": "{1}{B}{R}",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Sorcery",
    "cast": {
        "mana_cost": "{1}{B}{R}",
        "needs_x": False,
        "modes": None,
        "targeting": {"targets_anything": True, "understood": True, "minimum": 1, "maximum": 1},
    },
    "abilities": [],
}

ONE_TARGET_AFF = {
    "targets_anything": True,
    "understood": True,
    "minimum": 1,
    "maximum": 1,
    "requirements": [
        {
            "phrase": "target creature",
            "count": 1,
            "minimum": 1,
            "unbounded": False,
            "variable_count": False,
            "mode": None,
            "mode_text": None,
        }
    ],
    "division": None,
    "candidates": [{"id": "rag", "kind": "card", "name": "Ragavan", "requirements": [0]}],
}


def _one_target_strip(error: str | None) -> str:
    return run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const action = {
            kind: "cast", cardId: spell.id, card: spell, abilityIndex: null, keyword: null,
            modes: [], xValue: null, color: null, altCost: null,
            choosing: [], convoke: [], delve: [], crewing: [],
            slots: [{ index: 0, requirementIndex: 0, label: "target creature",
                      count: 1, minimum: 1, mode: null, targets: ["rag"] }],
            shares: {}, pinned: [], freeTargets: [], pendingTargets: [],
            affordance: aff, error: err, holdPriority: false, done: false,
          };
          const store = makeStore(baseApp(baseState({ hands: { alice: [spell] } }), { action }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return document.querySelector("#casting-strip").textContent;
        })()
        """,
        spell=ONE_TARGET_SPELL,
        aff=ONE_TARGET_AFF,
        err=error,
    )


def test_a_complete_single_target_cast_has_no_cast_button_of_its_own() -> None:
    """The auto-fire case: nothing left to choose, so nothing to confirm."""
    assert "Cast" not in _one_target_strip(None).replace("Cast: ", "")


def test_a_refused_dry_run_puts_the_cast_button_back() -> None:
    strip = _one_target_strip("no combination of the available mana sources can pay {1}{B}{R}")
    assert "no combination of the available mana sources" in strip
    assert "Cast" in strip.replace("Cast: ", ""), "a refusal needs a way to try again"


# --------------------------------------- the casting strip's own focus guard
#
# From the whole-branch review: `renderCastingStrip` used to `clear(el)` on
# every store update with no guard at all, so a poll landing mid-keystroke on
# the X input (or a share stepper) destroyed it and started it over blank -
# `renderPromptBox`'s own `focusedTextInput` protects exactly this failure
# mode for a decision's free-text control, but the casting strip had nothing
# of its own.


def test_rerendering_while_the_x_input_is_focused_keeps_the_same_node() -> None:
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
              { index: 0, requirementIndex: 0, label: "Mode 1: target player",
                count: 1, minimum: 1, mode: 1, targets: ["alice"] },
              { index: 1, requirementIndex: 1, label: "Mode 2: target player",
                count: 1, minimum: 1, mode: 2, targets: [] },
            ],
            shares: {}, pinned: [], freeTargets: [], pendingTargets: [],
            affordance: aff, error: null, holdPriority: false, done: false,
          };
          const state = baseState({ hands: { alice: [koz] } });
          const store = makeStore(baseApp(state, { action }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({}); // the initial render
          const xInput = findByField(document.querySelector("#casting-strip"), "input", "x");
          // The player has typed "7" but never committed it (no onChange
          // fired, so the store still holds xValue: 1) - the exact moment a
          // poll tick used to wipe the input out from under them.
          document.activeElement = xInput;
          xInput.value = "7";
          store.set({}); // the poll tick landing mid-edit
          const stillThere = findByField(document.querySelector("#casting-strip"), "input", "x");
          return [stillThere === xInput, stillThere.value];
        })()
        """,
        koz=KOZILEK,
        aff=KOZ_AFF,
    )
    assert result == [True, "7"], "an in-progress X edit must survive a repaint, same node and all"


def test_once_the_x_input_agrees_with_the_store_the_strip_repaints_normally() -> None:
    """The other half of the guard: once the input's live value already
    matches `action.xValue` (nothing uncommitted to protect), staying focused
    must not freeze the whole strip forever - a change elsewhere in the
    action (here, a refused dry run's error message) still has to show up."""
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
              { index: 0, requirementIndex: 0, label: "Mode 1: target player",
                count: 1, minimum: 1, mode: 1, targets: ["alice"] },
              { index: 1, requirementIndex: 1, label: "Mode 2: target player",
                count: 1, minimum: 1, mode: 2, targets: [] },
            ],
            shares: {}, pinned: [], freeTargets: [], pendingTargets: [],
            affordance: aff, error: null, holdPriority: false, done: false,
          };
          const state = baseState({ hands: { alice: [koz] } });
          const store = makeStore(baseApp(state, { action }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({}); // the initial render
          const xInput = findByField(document.querySelector("#casting-strip"), "input", "x");
          document.activeElement = xInput;
          xInput.value = "1"; // already matches action.xValue - nothing in flight
          store.set({ action: { ...action, error: "no combination of sources can pay this" } });
          const repainted = findByField(document.querySelector("#casting-strip"), "input", "x");
          return [
            repainted !== xInput,
            document.querySelector("#casting-strip").textContent,
          ];
        })()
        """,
        koz=KOZILEK,
        aff=KOZ_AFF,
    )
    assert result[0] is True, "a repaint with nothing uncommitted must go through, as a fresh node"
    assert "no combination of sources can pay this" in result[1]


def test_the_stops_box_offers_the_stack_tick_and_wires_it_up() -> None:
    """The tick the owner asked for, rendered and wired.

    It sits above the step grid rather than in one of its two columns,
    because it is not a step: it can fire in any step and on either player's
    turn, and it is live *alongside* whatever steps are checked (the owner's
    own case - "passing until my pre-combat-main" AND "stop if something
    happens"). It starts ticked: CR 117.4 resolves the top of the stack only
    once every player has passed in succession, so the pass a run has not yet
    made is the whole of the chance to answer what just landed.

    `h()` drops a `false` attribute entirely rather than writing it (see
    `ui/dom.js`), so "ticked" is the presence of the `checked` attribute on
    this fake DOM, not its value - which is why both legs are here: an
    assertion that only ever saw the on case would pass on markup that can
    never render the tick off.
    """
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const look = (stops) => {
            const store = makeStore(baseApp(baseState(), { stops }));
            const toggled = [];
            m.mountRibbon(document, store, {
              ...HANDLERS,
              onToggleStackStop: () => toggled.push(1),
            });
            store.set({});
            const box = document.querySelector("#stops-box");
            const tick = findByText(box, "label", "anything on the stack");
            const checkbox = tick && tick.children.find((c) => c.tagName === "input");
            if (checkbox && checkbox.listeners && checkbox.listeners.change) {
              checkbox.listeners.change({ target: checkbox });
            }
            return {
              offered: Boolean(tick),
              checked: checkbox ? "checked" in checkbox.attrs : null,
              toggles: toggled.length,
            };
          };
          return {
            absent: look({ mine: [], theirs: [] }),
            off: look({ mine: [], theirs: [], onStack: false }),
          };
        })()
        """,
    )
    absent, off = result["absent"], result["off"]
    assert absent["offered"] is True, "the stops panel never renders the stack tick"
    assert absent["checked"] is True, "a stops object with no `onStack` must read as ticked"
    assert absent["toggles"] == 1, "clicking the tick reaches no handler"
    assert off["offered"] is True
    assert off["checked"] is False, "`onStack: false` must render the tick unticked"


def test_the_mine_declare_attackers_tick_carries_no_helper_text() -> None:
    """The owner asked for the hint (`test_the_mine_declare_attackers_tick_
    carries_a_consequence_hint`, the test this replaces) and then asked for
    it gone: it broke the stops grid's layout. Removed outright, not
    replaced with a tooltip or an asterisk - `declare_attackers` on `mine`
    renders exactly like every other step tick, bare label included."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const store = makeStore(baseApp(baseState(), { stops: { mine: [], theirs: [] } }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          const box = document.querySelector("#stops-box");
          return box.textContent;
        })()
        """,
    )
    assert "declare attackers" in result
    assert "attack with nothing" not in result, result
    assert "*" not in result, "no asterisk decoration either"


def test_the_stops_box_offers_keep_priority_by_default_and_wires_it_up() -> None:
    """The standing tick beside "anything on the stack": casting a spell or
    activating an ability keeps priority unless it is unticked. Absent from
    the stops object reads as ticked, the same `!== false` reading as the
    stack tick."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const look = (stops) => {
            const store = makeStore(baseApp(baseState(), { stops }));
            const toggled = [];
            m.mountRibbon(document, store, {
              ...HANDLERS,
              onToggleKeepPriorityByDefault: () => toggled.push(1),
            });
            store.set({});
            const box = document.querySelector("#stops-box");
            const tick = findByText(box, "label", "Keep priority by default");
            const checkbox = tick && tick.children.find((c) => c.tagName === "input");
            if (checkbox && checkbox.listeners && checkbox.listeners.change) {
              checkbox.listeners.change({ target: checkbox });
            }
            return {
              offered: Boolean(tick),
              text: box.textContent,
              checked: checkbox ? "checked" in checkbox.attrs : null,
              toggles: toggled.length,
            };
          };
          return {
            absent: look({ mine: [], theirs: [] }),
            off: look({ mine: [], theirs: [], keepPriorityByDefault: false }),
            on: look({ mine: [], theirs: [], keepPriorityByDefault: true }),
          };
        })()
        """,
    )
    absent, off, on = result["absent"], result["off"], result["on"]
    assert off["offered"] is True, "the stops panel never renders the keep-priority tick"
    assert "after casting" not in off["text"], "the old wording must be gone"
    assert off["checked"] is False
    assert off["toggles"] == 1, "clicking the tick reaches no handler"
    assert on["offered"] is True
    assert on["checked"] is True
    assert absent["checked"] is True, "a stops object with no setting must read as ticked"


def test_the_per_action_tick_starts_from_the_standing_default_and_reports_an_untick() -> None:
    """The casting strip's own tick for one action. It starts where the
    standing default is (`holdPriority: null` means "not touched yet"), and
    unticking it reports `false` for that action alone."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const look = (holdPriority, stops) => {
            const action = { ...koz_action, holdPriority };
            const state = baseState({ hands: { alice: [koz] } });
            const store = makeStore(baseApp(state, { action, stops }));
            const reported = [];
            m.mountRibbon(document, store, {
              ...HANDLERS,
              onHoldPriority: (hold) => reported.push(hold),
            });
            store.set({});
            const strip = document.querySelector("#casting-strip");
            const tick = findByText(strip, "label", "keep priority");
            const checkbox = tick && tick.children.find((c) => c.tagName === "input");
            const checked = checkbox ? "checked" in checkbox.attrs : null;
            if (checkbox && checkbox.listeners && checkbox.listeners.change) {
              checkbox.listeners.change({ target: { checked: false } });
            }
            return { offered: Boolean(tick), checked, reported };
          };
          return {
            fresh: look(null, { mine: [], theirs: [] }),
            freshOff: look(null, { mine: [], theirs: [], keepPriorityByDefault: false }),
            unticked: look(false, { mine: [], theirs: [], keepPriorityByDefault: true }),
          };
        })()
        """,
        koz=KOZILEK,
        koz_action={
            "kind": "cast",
            "cardId": KOZILEK["id"],
            "card": KOZILEK,
            "abilityIndex": None,
            "keyword": None,
            "fromZone": None,
            "modes": [],
            "xValue": None,
            "color": None,
            "altCost": None,
            "choosing": [],
            "convoke": [],
            "delve": [],
            "crewing": [],
            "slots": [],
            "shares": {},
            "pinned": [],
            "freeTargets": [],
            "pendingTargets": [],
            "focusSlot": None,
            "affordance": KOZ_AFF,
            "error": None,
            "done": False,
        },
    )
    assert result["fresh"]["offered"] is True
    assert result["fresh"]["checked"] is True, "the per-action tick must start ticked"
    assert result["fresh"]["reported"] == [False]
    assert result["freshOff"]["checked"] is False
    assert result["unticked"]["checked"] is False


# ------------------------------ CR 106.6 restricted mana, mid-payment --------
#
# A restriction sentence on a hover title is no use at the one moment it
# decides anything: NN/g's tooltip guidance is explicit that a tooltip must
# not carry information required to complete the task, and "which of the four
# mana in your pool may actually pay for this" is exactly that. So while an
# announcement is in flight - the casting strip up, a cost being paid - the
# sentence is a `.tray-step` of its own, printed in full, beside the mana it
# is attached to.
#
# `mana_pool.restricted` is the engine's `ManaPool.restricted` as the API
# publishes it (task API1's `ManaPoolView.restricted`): a list of
# `{restriction: {text, understood}, amounts: {<color>: <count>}}`.

ELDRAZI_ONLY = "Spend this mana only to cast Eldrazi spells."
UNREAD_SENTENCE = "Spend this mana only in ways this engine could not parse."


def _seats(pool: object) -> list[dict]:
    return [
        {"username": "alice", "life": 20, "mana_pool": pool},
        {
            "username": "bob",
            "life": 20,
            "mana_pool": {"floating": {}, "snow": {}, "restricted": []},
        },
    ]


def _strip_with_pool(pool: object) -> dict:
    return run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const action = {
            kind: "cast", cardId: koz.id, card: koz, abilityIndex: null, keyword: null,
            modes: [1, 2], xValue: 1, color: null, altCost: null,
            choosing: [], convoke: [], delve: [], crewing: [], slots: [],
            shares: {}, pinned: [], freeTargets: [], pendingTargets: [],
            affordance: null, error: null, holdPriority: false, done: false,
          };
          const state = baseState({ hands: { alice: [koz] }, players: seats });
          const store = makeStore(baseApp(state, { action }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          const strip = document.querySelector("#casting-strip");
          // Every `.tray-step` in the strip, as text - so a test can tell a
          // sentence that is actually rendered from one hiding in a `title`.
          // This fake DOM keeps `class` on `.className` (that is what `h()`
          // assigns), so a class test reads the string rather than the
          // `classList` set, which nothing in this file populates.
          const steps = [];
          const walk = (node) => {
            const classes = String(node.className || "").split(/\\s+/);
            if (classes.includes("tray-step")) steps.push(node.textContent);
            for (const c of node.children || []) walk(c);
          };
          walk(strip);
          return { steps, text: strip.textContent, hidden: strip.hidden };
        })()
        """,
        koz=KOZILEK,
        seats=_seats(pool),
    )


def test_the_casting_tray_prints_a_restriction_sentence_in_full() -> None:
    result = _strip_with_pool(
        {
            "floating": {"C": 3},
            "snow": {},
            "restricted": [
                {"restriction": {"text": ELDRAZI_ONLY, "understood": True}, "amounts": {"C": 2}}
            ],
        }
    )
    assert result["hidden"] is False
    step = [s for s in result["steps"] if ELDRAZI_ONLY in s]
    assert step, f"the restriction sentence is not in any tray step: {result['steps']}"
    # The count and the colour it applies to, beside the sentence - "2 of the
    # 3 colourless in your pool", not just "some of it is restricted".
    assert "2" in step[0]
    assert "C" in step[0]


def test_the_casting_tray_says_unusable_when_the_sentence_was_not_understood() -> None:
    """`understood: false` means `ManaRestriction.allows` refuses everything -
    the tray must not imply the mana is merely narrowed."""
    result = _strip_with_pool(
        {
            "floating": {"U": 1},
            "snow": {},
            "restricted": [
                {"restriction": {"text": UNREAD_SENTENCE, "understood": False}, "amounts": {"U": 1}}
            ],
        }
    )
    step = [s for s in result["steps"] if UNREAD_SENTENCE in s]
    assert step, f"the unread sentence is not in any tray step: {result['steps']}"
    assert "unusable" in step[0].lower()
    assert "cannot be spent on anything" in step[0]


def test_the_casting_tray_says_nothing_about_restrictions_when_there_are_none() -> None:
    result = _strip_with_pool({"floating": {"C": 3}, "snow": {}, "restricted": []})
    assert "Kozilek's Command" in result["text"]
    assert "estricted" not in result["text"]
    assert "unusable" not in result["text"].lower()


# -- CR 601.2b's optional additional costs in the casting strip ---------------
#
# The reported defect (2026-09-20): "Sowing Mycospawn, cast with kicker didn't
# work. I simply wasn't presented with option to pay the kicker." The strip is
# the one surface that asks a human anything before a cast is committed, so
# this is where the option has to appear. Fixtures are not the reported card:
# Mold Shambler prints `Kicker {1}{G}`, Consign to Memory the repeatable
# `Replicate {1}`, and March of Otherworldly Light a cost this engine cannot
# charge.

SHAMBLER = {
    "id": "ms",
    "name": "Mold Shambler",
    "mana_cost": "{3}{G}",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Creature - Fungus Beast",
    "cast": {
        "mana_cost": "{3}{G}",
        "needs_x": False,
        "modes": None,
        "targeting": {
            "targets_anything": False,
            "understood": True,
            "minimum": 0,
            "maximum": 0,
            "requirements": [],
        },
        "additional_costs": [
            {
                "keyword": "Kicker",
                "cost": "{1}{G}",
                "rule": "CR 702.33a",
                "repeatable": False,
                "understood": True,
            }
        ],
    },
    "abilities": [],
}
CONSIGN = {
    **SHAMBLER,
    "id": "ctm",
    "name": "Consign to Memory",
    "mana_cost": "{U}",
    "cast": {
        **SHAMBLER["cast"],
        "mana_cost": "{U}",
        "additional_costs": [
            {
                "keyword": "Replicate",
                "cost": "{1}",
                "rule": "CR 702.56a",
                "repeatable": True,
                "understood": True,
            }
        ],
    },
}
MARCH = {
    **SHAMBLER,
    "id": "march",
    "name": "March of Otherworldly Light",
    "mana_cost": "{X}{W}",
    "cast": {
        **SHAMBLER["cast"],
        "mana_cost": "{X}{W}",
        "needs_x": True,
        "additional_costs": [
            {
                "keyword": None,
                "cost": "you may exile any number of white cards from your hand",
                "rule": "CR 118.8b",
                "repeatable": False,
                "understood": False,
            }
        ],
    },
}

OPTIONAL_COST_ACTION = """
function castOf(card, optionalCosts) {
  return {
    kind: "cast", cardId: card.id, card, abilityIndex: null, keyword: null,
    modes: [], xValue: null, color: null, altCost: null,
    optionalCosts: optionalCosts || {},
    choosing: [], convoke: [], delve: [], crewing: [], slots: [], shares: [],
    pinned: [], freeTargets: [], pendingTargets: [], focusSlot: null,
    affordance: null, error: null, holdPriority: false, done: false,
  };
}
function stripFor(card, optionalCosts, handlers) {
  const state = baseState({ hands: { alice: [card] } });
  const store = makeStore(baseApp(state, { action: castOf(card, optionalCosts) }));
  m.mountRibbon(document, store, handlers || HANDLERS);
  store.set({});
  return document.querySelector("#casting-strip");
}
"""


def test_a_kicker_is_offered_before_the_cast_is_committed() -> None:
    strip = run_module(
        "render/ribbon.js",
        FAKE_DOM + OPTIONAL_COST_ACTION + "stripFor(shambler).textContent",
        shambler=SHAMBLER,
    )
    assert "Kicker" in strip
    assert "{1}{G}" in strip
    # The Cast button is there, because `needsExplicitFire` now holds the
    # announcement open - the reported cast fired without one.
    assert "Cast" in strip


def test_clicking_the_kicker_announces_it_once() -> None:
    calls = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + OPTIONAL_COST_ACTION
        + """
        (() => {
          const calls = [];
          const handlers = { ...HANDLERS, onSetOptionalCost: (k, n) => calls.push([k, n]) };
          const strip = stripFor(shambler, {}, handlers);
          click(findByText(strip, "button", "Kicker"));
          return calls;
        })()
        """,
        shambler=SHAMBLER,
    )
    assert calls == [["Kicker", 1]]


def test_clicking_an_announced_kicker_takes_it_back() -> None:
    calls = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + OPTIONAL_COST_ACTION
        + """
        (() => {
          const calls = [];
          const handlers = { ...HANDLERS, onSetOptionalCost: (k, n) => calls.push([k, n]) };
          const strip = stripFor(shambler, { Kicker: 1 }, handlers);
          click(findByText(strip, "button", "Kicker"));
          return calls;
        })()
        """,
        shambler=SHAMBLER,
    )
    assert calls == [["Kicker", 0]]


def test_a_repeatable_cost_gets_a_count_to_choose() -> None:
    """CR 702.56a's replicate is announced "any number of times", so a
    toggle would silently cap it at one - it gets the same number input
    `Announce X` and the division steppers already use."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + OPTIONAL_COST_ACTION
        + """
        (() => {
          const calls = [];
          const handlers = { ...HANDLERS, onSetOptionalCost: (k, n) => calls.push([k, n]) };
          const strip = stripFor(consign, { Replicate: 2 }, handlers);
          const input = findByField(strip, "input", "optional-cost:Replicate");
          const value = input ? input.attrs.value ?? input.value : null;
          if (input && input.listeners.change) input.listeners.change({ target: { value: "3" } });
          return { text: strip.textContent, value: String(value), calls };
        })()
        """,
        consign=CONSIGN,
    )
    assert "Replicate" in result["text"]
    assert result["value"] == "2"
    assert result["calls"] == [["Replicate", 3]]


def test_a_cost_the_engine_cannot_charge_says_so_in_full() -> None:
    """Failing closed out loud, the same shape an unread CR 106.6 mana
    restriction already gets: the printed sentence is shown in the strip, not
    only in a tooltip, and there is no button offering to announce it."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + OPTIONAL_COST_ACTION
        + """
        (() => {
          const strip = stripFor(march);
          return {
            text: strip.textContent,
            button: Boolean(findByText(strip, "button", "exile any number of white cards")),
          };
        })()
        """,
        march=MARCH,
    )
    assert "you may exile any number of white cards from your hand" in result["text"]
    assert "cannot" in result["text"]
    assert result["button"] is False


def test_a_card_with_no_optional_cost_gets_no_step() -> None:
    strip = run_module(
        "render/ribbon.js",
        FAKE_DOM + OPTIONAL_COST_ACTION + "stripFor(koz).textContent",
        koz=KOZILEK,
    )
    assert "Additional cost" not in strip


def test_a_half_typed_replicate_count_survives_a_poll_tick() -> None:
    """The same guard the X box gets, for the same reason: the strip repaints
    on every store update (a ~1.2s poll tick included) and rebuilds its nodes
    wholesale, so a count typed but not yet committed would be wiped out from
    under the player mid-keystroke."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + OPTIONAL_COST_ACTION
        + """
        (() => {
          const state = baseState({ hands: { alice: [consign] } });
          const store = makeStore(baseApp(state, { action: castOf(consign, { Replicate: 1 }) }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          const strip = document.querySelector("#casting-strip");
          const input = findByField(strip, "input", "optional-cost:Replicate");
          // Typed but never committed - the store still says 1.
          document.activeElement = input;
          input.value = "4";
          store.set({});
          const repainted = document.querySelector("#casting-strip");
          const after = findByField(repainted, "input", "optional-cost:Replicate");
          return [after === input, after.value];
        })()
        """,
        consign=CONSIGN,
    )
    assert result == [True, "4"]


def test_a_committed_replicate_count_lets_the_strip_repaint() -> None:
    """The other half: once the input agrees with the store there is nothing
    uncommitted to protect, and holding focus must not freeze the rest of the
    strip - a refused dry run's message still has to appear."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + OPTIONAL_COST_ACTION
        + """
        (() => {
          const action = castOf(consign, { Replicate: 2 });
          const state = baseState({ hands: { alice: [consign] } });
          const store = makeStore(baseApp(state, { action }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          const strip = document.querySelector("#casting-strip");
          const input = findByField(strip, "input", "optional-cost:Replicate");
          document.activeElement = input;
          input.value = "2";
          store.set({ action: { ...action, error: "no combination of sources can pay this" } });
          return document.querySelector("#casting-strip").textContent;
        })()
        """,
        consign=CONSIGN,
    )
    assert "no combination of sources can pay this" in result
