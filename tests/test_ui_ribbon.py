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
  onTogglePitch: NOOP, onToggleConvoke: NOOP, onToggleDelve: NOOP,
  onToggleImprovise: NOOP, onFocusSlot: NOOP,
  onUnfill: NOOP, onSetShare: NOOP, onDone: NOOP, onCast: NOOP, onCancel: NOOP,
  onHoldPriority: NOOP, onDecisionDraft: NOOP, onDecide: NOOP, onVerb: NOOP,
  onStopRun: NOOP, onToggleStop: NOOP, onToggleStackStop: NOOP,
  onToggleKeepPriorityByDefault: NOOP,
  onToggleAskAttackers: NOOP, onToggleAskBlockers: NOOP,
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


# -- combatConfirmKind: a paused declare_attackers/declare_blockers --------
#
# The dedicated "Declare attackers (CR 508.1)"/"Declare blockers (CR 509.1)"
# prompt boxes are gone - replaced by the "Ask me to declare attackers"/"Ask
# me to declare blockers" stops-box ticks (below) plus a Pass button that
# confirms the board's own combat draft. `state.step` and
# `state.priority_holder` are both frozen at whatever they were *before* a
# combat-declaration pause (`app.py`'s `advance` docstring), never the step
# the pause is trying to enter - so `combatConfirmKind` reads
# `state.pending_decision` instead, which is stamped the moment the question
# is actually asked.

ATTACKER = {
    "id": "a1",
    "name": "Grizzly Bears",
    "controller": "alice",
    "tapped": False,
    "summoning_sick": False,
    "type_line": "Creature",
    "zone": "battlefield",
}

BLOCKER = {
    "id": "b1",
    "name": "Grizzly Bears",
    "controller": "alice",
    "tapped": False,
    "summoning_sick": False,
    "type_line": "Creature",
    "zone": "battlefield",
}


def test_no_prompt_box_for_a_paused_declare_attackers_decision() -> None:
    """No dedicated box - and no "Declare attackers"/"508.1" text anywhere -
    while the declaration is still possible and ticked; the generic "click a
    card to play it" hint covers it instead, same as any other moment with
    nothing else to say."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({
            step: "beginning_of_combat",
            battlefield: [ATTACKER],
            pending_decision: { player: "alice", kind: "declare_attackers" },
          });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return document.querySelector("#prompt-box").textContent;
        })()
        """,
        ATTACKER=ATTACKER,
    )
    assert "Declare attackers" not in result
    assert "508.1" not in result
    assert "click a card to play it" in result


def test_ticked_and_possible_declare_attackers_shows_a_confirming_pass() -> None:
    """Ticked (the default - `HANDLERS`'s `baseApp` carries no `askAttackers`
    at all, which reads as on) with a possible declaration (`ATTACKER`:
    alice's own, untapped, not summoning sick): `#priority-box` swaps its
    ordinary Pass verb for one that submits `app.combatDraft` through the
    same `onAttackConfirm` the old dedicated box's own "Attack" button
    called - the label says plainly that this is a confirm, not a plain
    pass."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const calls = [];
          const state = baseState({
            step: "beginning_of_combat",
            battlefield: [ATTACKER],
            pending_decision: { player: "alice", kind: "declare_attackers" },
          });
          const handlers = { ...HANDLERS, onAttackConfirm: () => calls.push("confirm-attack") };
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, handlers);
          store.set({});
          const box = document.querySelector("#priority-box");
          const btn = findByText(box, "button", "Pass");
          click(btn);
          return { text: box.textContent, calls };
        })()
        """,
        ATTACKER=ATTACKER,
    )
    assert "confirm attackers" in result["text"]
    assert result["calls"] == ["confirm-attack"]


def test_unticked_declare_attackers_offers_no_confirming_pass() -> None:
    """Unticked, `combatConfirmKind` returns `null` even with a possible
    declaration - `app.js`'s own `maybeAutoAnswerCombatDeclaration` is what
    answers "none" for this case, not a render decision, so the ordinary
    priority box (empty here: `priority_holder` is stale during the freeze,
    same as it always was) is what shows instead."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({
            step: "beginning_of_combat",
            battlefield: [ATTACKER],
            pending_decision: { player: "alice", kind: "declare_attackers" },
          });
          const stops = { mine: [], theirs: [], askAttackers: false };
          const store = makeStore(baseApp(state, { stops }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return document.querySelector("#priority-box").textContent;
        })()
        """,
        ATTACKER=ATTACKER,
    )
    assert "confirm" not in result


def test_no_possible_attackers_offers_no_confirming_pass_even_ticked() -> None:
    """No candidate (an empty battlefield): `combatConfirmKind` returns
    `null` regardless of the tick - `maybeAutoAnswerCombatDeclaration` again,
    not a render decision here either."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({
            step: "beginning_of_combat",
            battlefield: [],
            pending_decision: { player: "alice", kind: "declare_attackers" },
          });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return document.querySelector("#priority-box").textContent;
        })()
        """,
    )
    assert "confirm" not in result


def test_ticked_and_possible_declare_blockers_shows_a_confirming_pass() -> None:
    """The blockers sibling: `BLOCKER` is alice's own, untapped, so a block
    is possible while she is being attacked and the tick is on (default)."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const calls = [];
          const state = baseState({
            step: "declare_attackers",
            active_player: "bob",
            battlefield: [BLOCKER],
            combat: { attacks: [{ attacker: "c1", defender: "alice", blockers: [] }] },
            pending_decision: { player: "alice", kind: "declare_blockers" },
          });
          const handlers = { ...HANDLERS, onBlockConfirm: () => calls.push("confirm-block") };
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, handlers);
          store.set({});
          const box = document.querySelector("#priority-box");
          click(findByText(box, "button", "Pass"));
          return { text: box.textContent, calls };
        })()
        """,
        BLOCKER=BLOCKER,
    )
    assert "confirm blockers" in result["text"]
    assert result["calls"] == ["confirm-block"]


def test_the_rail_shows_the_same_confirming_pass_as_the_priority_box() -> None:
    """Owner spec: "Mobile rail pass does the same as the ribbon pass" - the
    rail is not hidden for a live combat declaration the way it is for every
    other pending decision, and clicking it calls the identical handler."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          boxed("rail-pass");
          const calls = [];
          const state = baseState({
            step: "beginning_of_combat",
            battlefield: [ATTACKER],
            pending_decision: { player: "alice", kind: "declare_attackers" },
          });
          const handlers = { ...HANDLERS, onAttackConfirm: () => calls.push("confirm-attack") };
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, handlers);
          store.set({});
          const rail = document.querySelector("#rail-pass");
          click(findByText(rail, "button", "Pass"));
          return { hidden: rail.hidden, calls };
        })()
        """,
        ATTACKER=ATTACKER,
    )
    assert result["hidden"] is False
    assert result["calls"] == ["confirm-attack"]


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


def test_the_stops_box_offers_ask_attackers_and_ask_blockers_and_wires_them_up() -> None:
    """The two ticks that replaced the dedicated "Declare attackers (CR
    508.1)"/"Declare blockers (CR 509.1)" prompt boxes, beside "anything on
    the stack". Both start ticked - absent from the stops object reads as
    ticked, the same `!== false` reading every other tick here gets."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const look = (label, handlerName, stops) => {
            const store = makeStore(baseApp(baseState(), { stops }));
            const toggled = [];
            m.mountRibbon(document, store, { ...HANDLERS, [handlerName]: () => toggled.push(1) });
            store.set({});
            const box = document.querySelector("#stops-box");
            const tick = findByText(box, "label", label);
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
          const empty = { mine: [], theirs: [] };
          return {
            attackersAbsent: look("Ask me to declare attackers", "onToggleAskAttackers", empty),
            attackersOff: look(
              "Ask me to declare attackers", "onToggleAskAttackers",
              { ...empty, askAttackers: false }
            ),
            blockersAbsent: look("Ask me to declare blockers", "onToggleAskBlockers", empty),
            blockersOff: look(
              "Ask me to declare blockers", "onToggleAskBlockers",
              { ...empty, askBlockers: false }
            ),
          };
        })()
        """,
    )
    for key, off_key in (("attackersAbsent", "attackersOff"), ("blockersAbsent", "blockersOff")):
        absent, off = result[key], result[off_key]
        assert absent["offered"] is True, key
        assert absent["checked"] is True, f"{key}: absent must read as ticked"
        assert absent["toggles"] == 1, f"{key}: clicking the tick reaches no handler"
        assert off["offered"] is True, off_key
        assert off["checked"] is False, off_key


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


# -- U1: CR 107.3a's X box appears whenever the server says there is an X ----

#: Toxic Deluge: the X is in the printed additional cost ("pay X life"), not
#: in the mana cost. `cast.needs_x` is the server's own answer for both
#: kinds (`affordances.cast_hints`), so this strip has nothing of its own to
#: decide - which is the whole point of the fix.
TOXIC_DELUGE = {
    "id": "td1",
    "name": "Toxic Deluge",
    "mana_cost": "{2}{B}",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Sorcery",
    "cast": {
        "mana_cost": "{2}{B}",
        "needs_x": True,
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

_PLAIN_CAST_ACTION = """
function castOf(card, extra) {
  return {
    kind: "cast", cardId: card.id, card, abilityIndex: null, keyword: null,
    modes: [], xValue: null, color: null, altCost: null, optionalCosts: {},
    choosing: [], convoke: [], delve: [], crewing: [], slots: [], shares: {},
    pinned: [], freeTargets: [], pendingTargets: [], focusSlot: null,
    affordance: null, error: null, holdPriority: null, done: false,
    spellFace: null, ...(extra || {}),
  };
}
"""


def _strip(card: dict, extra: dict | None = None) -> str:
    return run_module(
        "render/ribbon.js",
        FAKE_DOM
        + _PLAIN_CAST_ACTION
        + """
        (() => {
          const action = castOf(card, extra);
          const store = makeStore(baseApp(baseState({ hands: { alice: [card] } }), { action }));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return document.querySelector("#casting-strip").textContent;
        })()
        """,
        card=card,
        extra=extra or {},
    )


def test_an_additional_cost_x_gets_the_same_announce_box_a_mana_cost_x_does() -> None:
    """The reported defect's surface: no X box appeared for Toxic Deluge, so
    the cast went out with no `x_value` and the wrath resolved as -0/-0 with
    0 life paid."""
    assert "Announce X" in _strip(TOXIC_DELUGE)


def test_a_spell_with_no_x_at_all_still_gets_no_box() -> None:
    no_x = {**TOXIC_DELUGE, "cast": {**TOXIC_DELUGE["cast"], "needs_x": False}}
    assert "Announce X" not in _strip(no_x)


def test_the_x_box_follows_the_chosen_half_of_a_split_card() -> None:
    """CR 709.3: only the chosen half is evaluated, and only its own cost
    announces an X. A printing whose combined cost carries `{X}` must not ask
    for one while the half being cast has none."""
    faces = [
        {
            "spell_face": "Consign",
            "name": "Consign",
            "mana_cost": "{1}{U}",
            "type_line": "Instant",
            "needs_x": False,
        },
        {
            "spell_face": "Oblivion",
            "name": "Oblivion",
            "mana_cost": "{X}{B}{B}",
            "type_line": "Sorcery",
            "needs_x": True,
        },
    ]
    card = {
        **TOXIC_DELUGE,
        "id": "co1",
        "name": "Consign",
        "mana_cost": "{1}{U} // {X}{B}{B}",
        "cast": {**TOXIC_DELUGE["cast"], "needs_x": True, "spell_faces": faces},
    }
    assert "Announce X" not in _strip(card, {"spellFace": "Consign"})
    assert "Announce X" in _strip(card, {"spellFace": "Oblivion"})


def test_the_strip_header_names_the_half_being_cast() -> None:
    """The strip said "Cast: Wear" with the printing's combined cost while
    Tear was the half being announced - the card view's `name` is the FRONT
    face's (CR 709.4's combined characteristics are not either half). What
    is on screen must be the half the player picked."""
    faces = [
        {
            "spell_face": "Wear",
            "name": "Wear",
            "mana_cost": "{1}{R}",
            "type_line": "Instant",
            "needs_x": False,
        },
        {
            "spell_face": "Tear",
            "name": "Tear",
            "mana_cost": "{W}",
            "type_line": "Instant",
            "needs_x": False,
        },
    ]
    card = {
        **TOXIC_DELUGE,
        "id": "wt1",
        "name": "Wear",
        "mana_cost": "{1}{R} // {W}",
        "cast": {**TOXIC_DELUGE["cast"], "needs_x": False, "spell_faces": faces},
    }
    strip = _strip(card, {"spellFace": "Tear"})
    assert "Cast: Tear" in strip
    assert "// " not in strip, "the combined cost is not the cost of either half"


# -- CR 702.126a's improvise in the casting strip ----------------------------
#
# The strip drew an object-cost step for convoke and delve only, so an
# improvise spell offered no artifact-tap step at all and the cast went in at
# its full printed mana and came back refused. Kappa Cannoneer ({5}{U},
# maindeck in affinity) was effectively uncastable in the browser. The step
# follows the shape convoke and delve already have - `renderObjectCostStep`,
# one chip per candidate the *engine* named.

KAPPA = {
    "id": "kappa",
    "name": "Kappa Cannoneer",
    "mana_cost": "{5}{U}",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Artifact Creature - Turtle Warrior",
    "cast": {
        "mana_cost": "{5}{U}",
        "needs_x": False,
        "modes": None,
        "targeting": {
            "targets_anything": False,
            "understood": True,
            "minimum": 0,
            "maximum": 0,
            "requirements": [],
        },
        "object_costs": [{"keyword": "Improvise", "rule": "CR 702.126a"}],
    },
    "abilities": [],
}
KAPPA_AFF = {
    "targets_anything": False,
    "understood": True,
    "minimum": 0,
    "maximum": 0,
    "requirements": [],
    "division": None,
    "candidates": [],
    "object_costs": {
        "improvise": {
            "rule": "CR 702.126a",
            "candidates": [
                {"id": "thopter", "kind": "card", "name": "Ornithopter", "zone": "battlefield"},
                {"id": "drum", "kind": "card", "name": "Springleaf Drum", "zone": "battlefield"},
            ],
        }
    },
}

IMPROVISE_ACTION = """
function kappaAction(overrides) {
  return {
    kind: "cast", cardId: kappa.id, card: kappa, abilityIndex: null, keyword: null,
    modes: [], optionalCosts: {}, xValue: null, color: null, altCost: null,
    choosing: [], convoke: [], delve: [], improvise: [], crewing: [], slots: [],
    shares: {}, pinned: [], freeTargets: [], pendingTargets: [], focusSlot: null,
    affordance: kappaAff, error: null, holdPriority: false, done: false,
    ...(overrides || {}),
  };
}
function kappaStrip(overrides, handlers) {
  const state = baseState({ hands: { alice: [kappa] } });
  const store = makeStore(baseApp(state, { action: kappaAction(overrides) }));
  m.mountRibbon(document, store, handlers || HANDLERS);
  store.set({});
  return document.querySelector("#casting-strip");
}
"""


def test_improvise_offers_the_artifacts_the_engine_named() -> None:
    strip = run_module(
        "render/ribbon.js",
        FAKE_DOM + IMPROVISE_ACTION + "kappaStrip().textContent",
        kappa=KAPPA,
        kappaAff=KAPPA_AFF,
    )
    assert "Improvise" in strip
    assert "Ornithopter" in strip and "Springleaf Drum" in strip
    assert "CR 702.126a" in strip


def test_clicking_an_improvise_artifact_taps_it_into_the_announcement() -> None:
    calls = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + IMPROVISE_ACTION
        + """
        (() => {
          const calls = [];
          const handlers = { ...HANDLERS, onToggleImprovise: (id) => calls.push(id) };
          const strip = kappaStrip({}, handlers);
          click(findByText(strip, "button", "Ornithopter"));
          return calls;
        })()
        """,
        kappa=KAPPA,
        kappaAff=KAPPA_AFF,
    )
    assert calls == ["thopter"]


def test_a_chosen_improvise_artifact_is_counted_and_marked() -> None:
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + IMPROVISE_ACTION
        + """
        (() => {
          const strip = kappaStrip({ improvise: ["thopter"] });
          const chip = findByText(strip, "button", "Ornithopter");
          return { text: strip.textContent, on: (chip.className || "").includes("on") };
        })()
        """,
        kappa=KAPPA,
        kappaAff=KAPPA_AFF,
    )
    assert "(1 chosen)" in result["text"]
    assert result["on"] is True


# -- a mode this engine cannot read ------------------------------------------
#
# The chips were drawn from `cast.modes.options` unconditionally, so a mode
# the engine cannot read into an effect looked exactly like one it can - and
# picking it was refused only after the human had chosen targets, with a
# message the chip never hinted at. Thraben Charm (boros-aggro,
# orzhov-martyr) prints one: "Exile any number of target players'
# graveyards". Marked before the choice, the same `.restricted-note.unusable`
# an uncharged optional cost and an unread CR 106.6 restriction already get.

THRABEN_NOTICE = (
    "Thraben Charm's mode 3 (\"Exile any number of target players' graveyards.\") "
    "cannot be read by this engine: chosen, it would resolve doing nothing and "
    "leave only a mode notice (CR 700.2a), so a cast choosing it is refused"
)
THRABEN = {
    "id": "tc",
    "name": "Thraben Charm",
    "mana_cost": "{1}{W}",
    "zone": "hand",
    "controller": "alice",
    "type_line": "Instant",
    "cast": {
        "mana_cost": "{1}{W}",
        "needs_x": False,
        "modes": {
            "instruction": "Choose one -",
            "minimum": 1,
            "maximum": 1,
            "repeatable": False,
            "understood": True,
            "options": [
                {
                    "number": 1,
                    "text": "Thraben Charm deals damage equal to twice the number of "
                    "creatures you control to target creature.",
                    "understood": True,
                    "notice": None,
                },
                {
                    "number": 2,
                    "text": "Destroy target enchantment.",
                    "understood": True,
                    "notice": None,
                },
                {
                    "number": 3,
                    "text": "Exile any number of target players' graveyards.",
                    "understood": False,
                    "notice": THRABEN_NOTICE,
                },
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

THRABEN_ACTION = """
function charmStrip(handlers) {
  const state = baseState({ hands: { alice: [charm] } });
  const store = makeStore(baseApp(state, { action: {
    kind: "cast", cardId: charm.id, card: charm, abilityIndex: null, keyword: null,
    modes: [], optionalCosts: {}, xValue: null, color: null, altCost: null,
    choosing: [], convoke: [], delve: [], improvise: [], crewing: [], slots: [],
    shares: {}, pinned: [], freeTargets: [], pendingTargets: [], focusSlot: null,
    affordance: null, error: null, holdPriority: false, done: false,
  } }));
  m.mountRibbon(document, store, handlers || HANDLERS);
  store.set({});
  return document.querySelector("#casting-strip");
}
"""


def test_a_mode_the_engine_cannot_read_is_marked_before_it_is_chosen() -> None:
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + THRABEN_ACTION
        + """
        (() => {
          const strip = charmStrip();
          const find = (node) => {
            if ((node.className || "").includes("unusable")) return node;
            for (const c of node.children || []) { const f = find(c); if (f) return f; }
            return null;
          };
          return { text: strip.textContent, unusable: Boolean(find(strip)) };
        })()
        """,
        charm=THRABEN,
    )
    assert result["unusable"] is True
    assert "Exile any number of target players' graveyards." in result["text"]
    assert "cannot be read by this engine" in result["text"]


def test_a_mode_the_engine_cannot_read_is_not_a_chip_to_click() -> None:
    """No silent refusal path left: there is no button for it, so it cannot
    reach `onToggleMode` and be refused after the targets are picked."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + THRABEN_ACTION
        + """
        (() => {
          const calls = [];
          const handlers = { ...HANDLERS, onToggleMode: (n) => calls.push(n) };
          const strip = charmStrip(handlers);
          const unreadable = findByText(strip, "button", "Exile any number of target players");
          click(findByText(strip, "button", "Destroy target enchantment"));
          return { unreadable: Boolean(unreadable), calls };
        })()
        """,
        charm=THRABEN,
    )
    assert result["unreadable"] is False
    # The readable mode beside it is still an ordinary chip.
    assert result["calls"] == [2]


# ------------------------------------------------ the X box's own bounds ---
#
# The strip drew `<input type=number min=0>` with no maximum and no floor
# above zero, whatever the engine had already said about the X it would take
# (`Action.x_range` on the cast row, published and read by nothing). So an
# announcement outside the range was typed, sent, refused, and reported back
# as an error the player then had to read - with the bound that would have
# prevented it already in the browser's hands.
#
# The rows are the ones `app.js` stashed from the menu read that opened this
# card's menu (`enrichActionMenu`), so the fixture here is that stash.

X_BOUNDED_ROW = {
    "kind": "cast",
    "card_id": "kc",
    "ability_index": None,
    "affordable": "yes",
    "x_range": [1, 3],
}
X_OPEN_ROW = {**X_BOUNDED_ROW, "x_range": [0, None]}

X_STRIP = """
function xStripFor(stash, handlers) {
  const action = {
    kind: "cast", cardId: koz.id, card: koz, abilityIndex: null, keyword: null,
    modes: [1, 2], xValue: null, color: null, altCost: null, optionalCosts: {},
    choosing: [], convoke: [], delve: [], crewing: [], slots: [], shares: {},
    pinned: [], freeTargets: [], pendingTargets: [], focusSlot: null,
    affordance: null, error: null, holdPriority: false, done: false,
  };
  const state = baseState({ hands: { alice: [koz] } });
  const store = makeStore(baseApp(state, { action, actionRows: stash }));
  m.mountRibbon(document, store, handlers || HANDLERS);
  store.set({});
  return document.querySelector("#casting-strip");
}
"""


def test_the_x_box_carries_the_engines_own_floor_and_ceiling() -> None:
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + X_STRIP
        + """
        (() => {
          const input = findByField(xStripFor({ cardId: "kc", rows: [row] }), "input", "x");
          return { min: input.attrs.min, max: input.attrs.max };
        })()
        """,
        koz=KOZILEK,
        row=X_BOUNDED_ROW,
    )
    assert result == {"min": "1", "max": "3"}


def test_an_open_ended_x_gets_a_floor_and_no_ceiling() -> None:
    """`maximum: null` is the engine saying nothing caps this X. A `max` of
    any kind there would be this browser inventing a limit."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + X_STRIP
        + """
        (() => {
          const input = findByField(xStripFor({ cardId: "kc", rows: [row] }), "input", "x");
          const max = input.attrs.max;
          return { min: input.attrs.min, max: max === undefined ? null : max };
        })()
        """,
        koz=KOZILEK,
        row=X_OPEN_ROW,
    )
    assert result == {"min": "0", "max": None}


def test_the_bounded_x_box_says_the_range_in_words_too() -> None:
    """A `min`/`max` pair is enforced by the stepper and invisible to anyone
    typing - the range is written out beside the box for the same reason
    `renderRestrictedMana` writes its sentence out rather than leaving it in
    a `title`."""
    strip = run_module(
        "render/ribbon.js",
        FAKE_DOM + X_STRIP + '(() => xStripFor({ cardId: "kc", rows: [row] }).textContent)()',
        koz=KOZILEK,
        row=X_BOUNDED_ROW,
    )
    assert "1 to 3" in strip


def test_a_typed_x_outside_the_range_is_brought_back_inside_it() -> None:
    """The bound has to hold for a typed value too, which `min`/`max` alone
    do not stop - and the engine's own answer is the one to clamp to."""
    calls = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + X_STRIP
        + """
        (() => {
          const calls = [];
          const handlers = { ...HANDLERS, onSetX: (n) => calls.push(n) };
          const strip = xStripFor({ cardId: "kc", rows: [row] }, handlers);
          const input = findByField(strip, "input", "x");
          input.value = "9";
          input.listeners.change({ target: input });
          input.value = "0";
          input.listeners.change({ target: input });
          input.value = "";
          input.listeners.change({ target: input });
          return calls;
        })()
        """,
        koz=KOZILEK,
        row=X_BOUNDED_ROW,
    )
    assert calls == [3, 1, None]


def test_an_unbounded_x_box_is_exactly_what_it_was_before() -> None:
    """No row, no bound: the strip is opened before the menu read has landed
    and from paths that opened no menu at all, and a missing answer must not
    take the X box away or invent a ceiling for it."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + X_STRIP
        + """
        (() => {
          const strip = xStripFor(null);
          const input = findByField(strip, "input", "x");
          return {
            min: input.attrs.min,
            max: input.attrs.max === undefined ? null : input.attrs.max,
            text: strip.textContent,
          };
        })()
        """,
        koz=KOZILEK,
    )
    assert result["min"] == "0"
    assert result["max"] is None
    assert "Announce X" in result["text"]


def test_a_stash_for_another_card_bounds_nothing() -> None:
    """The stash is one card's rows. Reading another card's `x_range` off it
    would be a bound made up from the wrong board object."""
    result = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + X_STRIP
        + """
        (() => {
          const input = findByField(
            xStripFor({ cardId: "somethingelse", rows: [row] }), "input", "x");
          return input.attrs.max === undefined ? null : input.attrs.max;
        })()
        """,
        koz=KOZILEK,
        row=X_BOUNDED_ROW,
    )
    assert result is None


# -------------------------- the row's own verdict on an optional cost ------
#
# `ActionRow.optional_costs` prices each of CR 601.2b's optional additional
# costs with this row's own cost plus that one, and says in the engine's
# words why one cannot be paid. `card.cast.additional_costs` - which is what
# offers the chips - carries none of that. Nothing read the row's copy, so a
# kicker the board could not pay looked exactly like one it could, and the
# player found out from the refusal.

UNAFFORDABLE_KICKER_ROW = {
    "kind": "cast",
    "card_id": "ms",
    "ability_index": None,
    "affordable": "yes",
    "optional_costs": [
        {
            "keyword": "Kicker",
            "rule": "CR 702.33a",
            "cost": "{1}{G}",
            "repeatable": False,
            "understood": True,
            "affordable": "no",
            "unaffordable_reason": "cannot pay {3}{G} and {1}{G}: 4 mana available",
        }
    ],
}

OPTIONAL_COST_STASH = """
function stashStripFor(card, stash, optionalCosts) {
  const state = baseState({ hands: { alice: [card] } });
  const store = makeStore(
    baseApp(state, { action: castOf(card, optionalCosts), actionRows: stash })
  );
  m.mountRibbon(document, store, HANDLERS);
  store.set({});
  return document.querySelector("#casting-strip");
}
"""


def test_an_unaffordable_kicker_says_so_in_the_engines_own_words() -> None:
    strip = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + OPTIONAL_COST_ACTION
        + OPTIONAL_COST_STASH
        + '(() => stashStripFor(shambler, { cardId: "ms", rows: [row] }).textContent)()',
        shambler=SHAMBLER,
        row=UNAFFORDABLE_KICKER_ROW,
    )
    assert "Kicker" in strip, "the option is still offered - this is a notice, not a block"
    assert "cannot pay {3}{G} and {1}{G}: 4 mana available" in strip


def test_an_affordable_kicker_gets_no_notice_at_all() -> None:
    affordable = {
        **UNAFFORDABLE_KICKER_ROW,
        "optional_costs": [
            {
                **UNAFFORDABLE_KICKER_ROW["optional_costs"][0],
                "affordable": "yes",
                "unaffordable_reason": None,
            }
        ],
    }
    strip = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + OPTIONAL_COST_ACTION
        + OPTIONAL_COST_STASH
        + '(() => stashStripFor(shambler, { cardId: "ms", rows: [row] }).textContent)()',
        shambler=SHAMBLER,
        row=affordable,
    )
    assert "Kicker" in strip
    assert "cannot pay" not in strip


def test_a_kicker_with_no_row_to_price_it_is_offered_exactly_as_before() -> None:
    strip = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + OPTIONAL_COST_ACTION
        + OPTIONAL_COST_STASH
        + "(() => stashStripFor(shambler, null).textContent)()",
        shambler=SHAMBLER,
    )
    assert "Kicker" in strip
    assert "cannot pay" not in strip
