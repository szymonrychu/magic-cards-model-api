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

Two more scenarios exercise an actual handler end to end: the fake DOM's
`addEventListener` records the listener `h()` wires up, and `click(el)`
(found via `findByText`) invokes it - a `choose_cards` decision's option
button reports `{card_ids: [...]}` back through `onDecisionDraft`, and a
scry's split control's "Move" button reports `{moved: [...]}`. Everything
else about a control's own correctness (which draft field each kind reads
back, `readyToFire`'s exact gating) belongs to `decisions.js`/`actions.js`/
`targeting.js`'s own direct tests - these two just confirm the wiring
between a rendered button and the handler it is supposed to call is real,
not merely plausible-looking markup.
"""

from __future__ import annotations

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
  removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
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

// Invoke whatever `h()` wired to this element's "click" via
// `addEventListener` - a no-op if nothing is listening (a disabled button,
// or the element wasn't found at all).
function click(el) {
  if (el && el.listeners && el.listeners.click) el.listeners.click({ target: el });
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


def test_clicking_a_choose_cards_option_reports_card_ids() -> None:
    calls = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const calls = [];
          const handlers = { ...HANDLERS, onDecisionDraft: (patch) => calls.push(patch) };
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
          m.mountRibbon(document, store, handlers);
          store.set({});
          click(findByText(document, "button", "Brainstorm"));
          return calls;
        })()
        """,
    )
    assert calls == [{"card_ids": ["c1"]}]


def test_clicking_the_split_controls_move_button_reports_moved() -> None:
    calls = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const calls = [];
          const handlers = { ...HANDLERS, onDecisionDraft: (patch) => calls.push(patch) };
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
          m.mountRibbon(document, store, handlers);
          store.set({});
          click(findByText(document, "button", "Move"));
          return calls;
        })()
        """,
    )
    assert calls == [{"moved": ["a"]}]


# --------------------------------------------------------- conceding safely
#
# From the browser pass. `passButtonsFor` offers no verbs at all while the
# opponent holds priority, so the priority box emptied out and Concede rose
# into the position "Pass" had been in one tick earlier. The next click
# there ended the game. CR 104.3a keeps conceding legal at any time, so the
# button stays - it just stops being a single click, and it goes away
# entirely once somebody has already won.


def test_concede_is_a_plain_button_until_it_is_armed() -> None:
    label = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({ priority_holder: "bob" });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          const box = document.querySelector("#priority-box");
          return box.children.map((c) => c.textContent);
        })()
        """,
    )
    assert label == ["", "Concede"], "with no verbs on offer, Concede is the only button there"


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


def test_a_finished_game_offers_no_concede_at_all() -> None:
    found = run_module(
        "render/ribbon.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({ winner: "bob", priority_holder: "bob" });
          const store = makeStore(baseApp(state));
          m.mountRibbon(document, store, HANDLERS);
          store.set({});
          return findByText(document.querySelector("#priority-box"), "button", "Concede") !== null;
        })()
        """,
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
