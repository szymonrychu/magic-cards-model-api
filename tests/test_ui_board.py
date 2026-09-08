"""`ui/render/board.js`'s `mountBoard` against a fake DOM (the pattern from
`tests/test_ui_ribbon.py`) - `board.js` had no tests of its own before this
whole-branch review.

The critical fix this pins: `#opp-info`'s click handler used to read
`store.get().opponent`, a store field that has never existed (the store
never carries an `opponent` key at all - see `ui/app.js`'s own
`createStore(...)` call) - so clicking the opponent's info row always called
`handlers.onPlayer(undefined)`. `ui/combat.js`'s `opponentOf(state)` is the
real (and already-used-elsewhere, e.g. `ui/app.js`'s `toggleAttacker`) way to
find the other seat's username; `board.js` now reads it off `store.get().state`
instead of a field that was never there.
"""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

# A fake DOM minimal enough for `dom.js`'s `h`/`clear`/`patchChildren` (the
# `test_ui_dom.py` shape) plus what `board.js` itself needs: `querySelector`
# by id (`mountBoard` looks up its boxes that way) and a working
# `insertBefore`/`removeChild`/`nextSibling` pair for the keyed reconciler -
# copied from `tests/test_ui_ribbon.py`'s own fake DOM rather than
# reinvented, since `patchChildren`'s requirements are identical here.
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
  // A real DOM's `.className` and `.classList` are two views of the same
  // token set - `h()` (`ui/dom.js`) sets `class` through `.className`, while
  // `cards.js`'s `updateCardNode` reads/writes through `.classList` - so
  // this fake element keeps them in sync the way a browser does, rather than
  // treating them as two unrelated properties.
  set className(value) {
    this.classList.set = new Set(String(value || "").split(/\\s+/).filter(Boolean));
  }
  get className() {
    return [...this.classList.set].join(" ");
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
  get firstChild() { return this.children[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const i = this.parentNode.children.indexOf(this);
    return this.parentNode.children[i + 1] || null;
  }
  get childNodes() { return this.children; }
  get textContent() {
    return this.children.map((c) => (c.textContent === undefined ? "" : c.textContent)).join("");
  }
  // A real element's `textContent` is writable and replaces everything under
  // it with one text node - `dom.js`'s `setText` writes it, and a getter-only
  // property here would throw instead of just being a fake with a gap.
  set textContent(value) {
    this.children = [{ nodeType: 3, textContent: String(value) }];
  }
  // `#id` (how `mountBoard` finds its boxes) and `.class` (how the zone
  // ribbon finds the label spans inside a row it just built). Nothing else.
  querySelector(sel) {
    const byId = sel.startsWith("#");
    if (!byId && !sel.startsWith(".")) return null;
    const name = sel.slice(1);
    const hit = (node) =>
      byId ? node.attrs && node.attrs.id === name : node.classList && node.classList.contains(name);
    const walk = (node) => {
      if (!node) return null;
      if (hit(node)) return node;
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
boxed("opp-info");
boxed("opp-hand");
boxed("opp-lands");
boxed("opp-battlefield");
boxed("stack-row");
boxed("zone-ribbon");
boxed("red-zone", true);
boxed("my-battlefield");
boxed("my-lands");
boxed("my-hand");
boxed("you-info");

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

function creature(id, controller, over) {
  return {
    id, name: id, controller, tapped: false, summoning_sick: false,
    type_line: "Creature", zone: "battlefield", abilities: [],
    ...(over || {}),
  };
}

function baseState(overrides) {
  return {
    you: "alice",
    active_player: "alice",
    priority_holder: "alice",
    step: "precombat_main",
    players: [
      { username: "alice", life: 20, hand_count: 0, library_count: 40,
        graveyard_count: 0, mana_pool: {} },
      { username: "bob", life: 20, hand_count: 3, library_count: 40,
        graveyard_count: 0, mana_pool: {} },
    ],
    battlefield: [],
    hands: { alice: [] },
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
    preview: null,
    symbols: {},
    ...overrides,
  };
}
"""

# A one-target, targets-anything action whose affordance already lists the
# opponent (bob) as the sole legal candidate - the "pending action whose
# affordance lists the opponent as a candidate" the task brief calls for,
# folded into the opp-info click test so it also proves the info row is lit
# up as a legal target while the fix is exercised.
ONE_TARGET_ACTION = {
    "kind": "cast",
    "cardId": "b1",
    "card": {"id": "b1", "name": "Lightning Bolt"},
    "abilityIndex": None,
    "keyword": None,
    "modes": [],
    "xValue": None,
    "color": None,
    "altCost": None,
    "choosing": [],
    "convoke": [],
    "delve": [],
    "crewing": [],
    "slots": [
        {
            "index": 0,
            "requirementIndex": 0,
            "label": "any target",
            "count": 1,
            "minimum": 1,
            "mode": None,
            "targets": [],
        }
    ],
    "shares": {},
    "pinned": [],
    "freeTargets": [],
    "pendingTargets": [],
    "focusSlot": None,
    "affordance": {
        "targets_anything": True,
        "understood": True,
        "minimum": 1,
        "maximum": 1,
        "requirements": [
            {
                "phrase": "any target",
                "count": 1,
                "minimum": 1,
                "unbounded": False,
                "variable_count": False,
                "mode": None,
                "mode_text": None,
            }
        ],
        "division": None,
        "candidates": [{"id": "bob", "kind": "player", "name": "bob", "requirements": [0]}],
    },
    "error": None,
    "holdPriority": False,
    "done": False,
}


def test_clicking_opp_info_calls_on_player_with_the_opponents_username() -> None:
    """The regression itself: `store.get().opponent` never existed, so the
    old handler always called `onPlayer(undefined)`. `alice` is `state.you`;
    `bob` is the only other seat, and must be exactly what `onPlayer` sees."""
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + """
        (() => {
          const calls = [];
          const state = baseState();
          const store = makeStore(baseApp(state, { action: action }));
          m.mountBoard(document, store, {
            onHandCard: NOOP, onPermanent: NOOP, onStackObject: NOOP, onPermission: NOOP,
            onPlayer: (u) => calls.push(u),
          });
          store.set({});
          document.querySelector("#opp-info").onclick();
          const oppTargetable =
            document.querySelector("#opp-info").classList.contains("targetable");
          return { calls, oppTargetable };
        })()
        """,
        action=ONE_TARGET_ACTION,
    )
    assert result["calls"] == ["bob"]
    assert result["oppTargetable"] is True


def test_clicking_a_battlefield_card_calls_on_permanent_with_that_card() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + """
        (() => {
          const calls = [];
          const state = baseState({ battlefield: [creature("g1", "alice")] });
          const store = makeStore(baseApp(state));
          m.mountBoard(document, store, {
            onHandCard: NOOP, onPlayer: NOOP, onStackObject: NOOP, onPermission: NOOP,
            onPermanent: (card, event) => calls.push(card.id),
          });
          store.set({});
          // Every permanent is drawn inside its own `.perm-stack` wrapper
          // now - the pile that carries whatever is attached to it - so the
          // card itself is one level down even when nothing is attached.
          const stack = document.querySelector("#my-battlefield").children[0];
          const bfCard = stack.children[0];
          bfCard.onclick({ currentTarget: bfCard });
          return calls;
        })()
        """,
    )
    assert result == ["g1"]


# ----------------------------- item 7: summoning sickness is battlefield-only
#
# `cards.js`'s `toggles()` used to apply `.summoning-sick` off `card.
# summoning_sick` alone - but `cardNode`/`updateCardNode` are shared by every
# zone a card can be rendered in (a hand, a graveyard, the zones popover's
# exile grid), and a card that just left the battlefield can still carry a
# stale `summoning_sick: true` from the state view a moment before the move.
# The swirl and greyed-out look must not follow it there.


def test_summoning_sick_class_only_applies_on_the_battlefield() -> None:
    result = run_module(
        "cards.js",
        FAKE_DOM
        + """
        (() => {
          const onBattlefield = m.cardNode(
            { id: "c1", name: "Sick Bear", zone: "battlefield",
              summoning_sick: true, abilities: [] },
            "card-md", {}
          );
          const inGraveyard = m.cardNode(
            { id: "c2", name: "Sick Bear", zone: "graveyard",
              summoning_sick: true, abilities: [] },
            "card-md", {}
          );
          return {
            battlefield: onBattlefield.classList.contains("summoning-sick"),
            graveyard: inGraveyard.classList.contains("summoning-sick"),
          };
        })()
        """,
    )
    assert result == {"battlefield": True, "graveyard": False}
