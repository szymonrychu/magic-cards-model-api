"""`ui/render/decision_window.js`: the floating, draggable decision window.

Every COMPLEX decision - scry, surveil, a look-at-the-top-and-split, every
`choose_cards`, `order_triggers`, and the live seat's own pregame mulligan -
is answered in one persistent `#decision-window` built once in
`ui/index.html`, with real card images from `card_details` (or the seat's own
hand). Small decisions stay in `#prompt-box`.

Run under Node against a fake DOM, the `tests/test_ui_ribbon.py` pattern,
extended with what dragging needs: listeners that are lists and bubble,
`closest`, `elementFromPoint`, and per-element rectangles.

What the posted answers must look like is fixed by the controls that already
existed (`ui/decisions.js`'s view models, `POST .../decide` and
`POST .../pregame/mulligan`): these tests hold the window to exactly those
shapes rather than inventing new ones.
"""

from __future__ import annotations

import re

from ui_node import UI, requires_node, run_module

pytestmark = requires_node

FAKE_DOM = r"""
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.listeners = {};
    this.parentNode = null;
    const self = this;
    this.classList = {
      set: new Set(),
      add: (c) => self.classList.set.add(c),
      remove: (c) => self.classList.set.delete(c),
      toggle: (c, on) => {
        const want = on === undefined ? !self.classList.set.has(c) : Boolean(on);
        if (want) self.classList.set.add(c);
        else self.classList.set.delete(c);
        return want;
      },
      contains: (c) => self.classList.set.has(c),
    };
  }
  get className() { return [...this.classList.set].join(" "); }
  set className(v) { this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
  appendChild(c) {
    if (c.parentNode && c.parentNode.children) c.parentNode.removeChild(c);
    this.children.push(c);
    c.parentNode = this;
    return c;
  }
  insertBefore(c, ref) {
    if (c.parentNode && c.parentNode.children) c.parentNode.removeChild(c);
    const i = ref ? this.children.indexOf(ref) : -1;
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    c.parentNode = this;
    return c;
  }
  removeChild(c) {
    this.children = this.children.filter((x) => x !== c);
    if (c.parentNode === this) c.parentNode = null;
    return c;
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    if (k === "id") this.id = String(v);
    if (k === "class") this.className = v;
  }
  getAttribute(k) {
    return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
  }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  contains(other) {
    if (other === this) return true;
    return this.children.some((c) => (c.contains ? c.contains(other) : false));
  }
  matches(selector) {
    return selector.split(",").some((one) => {
      const m = one.trim().match(/^([a-zA-Z0-9]*)(#[\w-]+)?((?:\.[\w-]+)*)((?:\[[\w-]+\])*)$/);
      if (!m) return false;
      const [, tag, id, classes, attrs] = m;
      if (tag && String(this.tagName).toLowerCase() !== tag.toLowerCase()) return false;
      if (id && this.id !== id.slice(1)) return false;
      for (const c of classes.split(".").filter(Boolean)) {
        if (!this.classList.contains(c)) return false;
      }
      for (const a of attrs.match(/[\w-]+/g) || []) {
        if (a.startsWith("data-")) {
          const key = a.slice(5).replace(/-(\w)/g, (_, ch) => ch.toUpperCase());
          if (this.dataset[key] === undefined) return false;
        } else if (this.getAttribute(a) === null) return false;
      }
      return true;
    });
  }
  closest(selector) {
    for (let node = this; node && node.matches; node = node.parentNode) {
      if (node.matches(selector)) return node;
    }
    return null;
  }
  focus() { document.activeElement = this; }
  getBoundingClientRect() {
    if (this.size) {
      const left = parseFloat(this.style.left) || 0;
      const top = parseFloat(this.style.top) || 0;
      return { left, top, width: this.size.width, height: this.size.height,
               right: left + this.size.width, bottom: top + this.size.height };
    }
    return this.rect || { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
  }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  get textContent() {
    return this.children.map((c) => (c.textContent === undefined ? "" : c.textContent)).join("");
  }
  set textContent(s) {
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.appendChild({ nodeType: 3, textContent: String(s) });
  }
  querySelector(sel) {
    if (!sel.startsWith("#")) return null;
    const id = sel.slice(1);
    const walk = (node) => {
      if (!node || !node.children) return null;
      if (node.id === id) return node;
      for (const c of node.children) {
        const found = walk(c);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
}

function walkAll(node, pred, out) {
  out = out || [];
  if (!node) return out;
  if (pred(node)) out.push(node);
  for (const c of node.children || []) walkAll(c, pred, out);
  return out;
}

// `findRenderedCard` (`render/decision_popover.js`) needs this - the same
// addition `test_ui_decision_popover.py` makes to this same fake DOM.
El.prototype.querySelectorAll = function (sel) {
  return walkAll(this, (n) => n !== this && typeof n.matches === "function" && n.matches(sel));
};

const hasClass = (cls) => (n) => Boolean(n.classList && n.classList.contains(cls));
const clone = (x) => JSON.parse(JSON.stringify(x));

function attached(node) {
  let at = node;
  while (at.parentNode) {
    if (!at.parentNode.children.includes(at)) return false;
    at = at.parentNode;
  }
  return at === globalThis.document;
}

// Dispatch `type` at `el` and bubble it up to the document, honouring
// `stopPropagation` - the order a browser's bubbling listeners see it in.
function fire(el, type, props) {
  let stopped = false;
  const event = {
    type,
    target: el,
    defaultPrevented: false,
    stopPropagation() { stopped = true; },
    preventDefault() { this.defaultPrevented = true; },
    ...(props || {}),
  };
  for (let node = el; node && !stopped; node = node.parentNode) {
    event.currentTarget = node;
    for (const fn of [...((node.listeners && node.listeners[type]) || [])]) fn(event);
  }
  return event;
}

// A click the way a browser delivers one: nothing for a disabled button or a
// node a re-render has already detached.
function click(el) {
  if (!el || el.disabled || !attached(el)) return false;
  fire(el, "click", {});
  return true;
}

function buttonNamed(root, label) {
  return walkAll(root, (n) => n.tagName === "button" && n.textContent.trim() === label)[0] || null;
}

globalThis.window = {
  innerWidth: 1200,
  innerHeight: 800,
  listeners: {},
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  },
};
globalThis.document = new El("#document");
document.activeElement = null;
document.createElement = (t) => new El(t);
document.createTextNode = (s) => ({ nodeType: 3, textContent: String(s) });
document.createDocumentFragment = () => new El("#fragment");
document.elementFromPoint = () => null;

function boxed(tag, id, parent) {
  const el = document.createElement(tag);
  el.setAttribute("id", id);
  (parent || document).appendChild(el);
  return el;
}
for (const id of ["prompt-box", "priority-box", "stops-box", "mtg-board", "modal"]) {
  boxed("div", id);
}
boxed("div", "casting-strip").hidden = true;
boxed("div", "popover").hidden = true;

// The same structure `ui/index.html` ships (`test_index_html_ships_the_window_structure`
// below pins that the ids agree).
const WIN = boxed("div", "decision-window");
WIN.className = "decision-window";
WIN.hidden = true;
WIN.size = { width: 480, height: 360 };
const HANDLE = boxed("div", "decision-window-handle", WIN);
HANDLE.className = "dw-titlebar";
const TITLE = boxed("h2", "decision-window-title", HANDLE);
const MINIMIZE = boxed("button", "decision-window-minimize", HANDLE);
MINIMIZE.setAttribute("type", "button");
MINIMIZE.textContent = "Minimize";
const BODY = boxed("div", "decision-window-body", WIN);

// A rendered battlefield permanent (`findRenderedCard`'s `.card[data-card-id]`
// anchor), laid out (non-zero rect) so `findRenderedCard` accepts it.
function boardCard(id) {
  const el = document.createElement("div");
  el.className = "card card-md";
  el.dataset.key = id;
  el.dataset.cardId = id;
  el.rect = { left: 0, top: 0, right: 100, bottom: 140, width: 100, height: 140 };
  document.querySelector("#mtg-board").appendChild(el);
  return el;
}

function memoryStorage(seed) {
  const data = { ...(seed || {}) };
  return {
    data,
    getItem: (k) => (Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    removeItem: (k) => { delete data[k]; },
  };
}

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
    graveyards: { alice: [], bob: [] },
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
    busy: 0,
    ...overrides,
  };
}

// The handlers `app.js` passes: the draft merges into the store exactly the
// way `app.js`'s `onDecisionDraft` does, and the two posts are recorded.
function handlersFor(store, log) {
  return {
    onDecisionDraft: (patch) => {
      log.push(["draft", patch]);
      store.set((s) => ({ decisionDraft: { ...s.decisionDraft, ...patch } }));
    },
    onDecide: (answer) => log.push(["decide", answer]),
    onPregame: (path, body) => log.push(["pregame", path, body]),
    nameOf: (id) => id,
  };
}

function mount(store, log, storage) {
  const opts = { storage: storage || memoryStorage() };
  return m.mountDecisionWindow(document, store, handlersFor(store, log), opts);
}
function poll(store, overrides) {
  store.set({ state: baseState(overrides) });
}
function decided(log) {
  return log.filter((e) => e[0] === "decide").map((e) => e[1]);
}
function pos() {
  return { left: parseFloat(WIN.style.left), top: parseFloat(WIN.style.top) };
}
function ptr(el, type, x, y, pointerType, pointerId) {
  return fire(el, type, { clientX: x, clientY: y, pointerId, pointerType, button: 0 });
}
function hoverEnter(el) {
  fire(el, "pointerenter", {});
}
function hoverLeave(el) {
  fire(el, "pointerleave", {});
}
function focusSlot(el) {
  el.focus();
  fire(el, "focusin", {});
}
function blurSlot(el) {
  fire(el, "focusout", {});
}
function sourceClassed() {
  return walkAll(document, hasClass("decision-source"));
}

function laneOf(lane) {
  return walkAll(WIN, (n) => hasClass("dw-lane")(n) && n.dataset.lane === lane)[0] || null;
}
function laneCards(lane) {
  const el = laneOf(lane);
  return el ? el.children.find(hasClass("dw-lane-cards")) : null;
}
function laneLabel(lane) {
  const el = laneOf(lane);
  const label = el ? el.children.find(hasClass("dw-lane-label")) : null;
  return label ? label.textContent : null;
}
function laneIds(lane) {
  const cards = laneCards(lane);
  return cards ? cards.children.map((c) => c.dataset.key) : null;
}
function slotOf(id) {
  const slotLike = (n) => hasClass("dw-slot")(n) || hasClass("dw-pick")(n);
  return walkAll(WIN, (n) => slotLike(n) && n.dataset.key === id)[0] || null;
}
function picked() {
  const on = (n) => hasClass("dw-pick")(n) && n.getAttribute("aria-pressed") === "true";
  return walkAll(WIN, on).map((n) => n.dataset.key);
}
function rectRow(container) {
  container.children.forEach((slot, i) => {
    const left = i * 120;
    slot.rect = { left, right: left + 108, top: 0, bottom: 150, width: 108, height: 150 };
  });
}
function dragTo(slot, dropOn, x, y, pointerType) {
  const t = pointerType || "mouse";
  const grab = slot.children[0] || slot;
  fire(grab, "pointerdown", { clientX: 1, clientY: 1, pointerId: 7, pointerType: t, button: 0 });
  document.elementFromPoint = () => dropOn;
  fire(document, "pointermove", { clientX: x, clientY: y, pointerId: 7, pointerType: t });
  fire(document, "pointerup", { clientX: x, clientY: y, pointerId: 7, pointerType: t });
}
function imagesIn(root) {
  return walkAll(root, (n) => n.tagName === "img").map((n) => n.getAttribute("src"));
}
"""


def _card(cid: str, name: str, zone: str = "library") -> dict:
    return {
        "id": cid,
        "name": name,
        "zone": zone,
        "type_line": "Instant",
        "oracle_text": f"{name} rules text",
        "image": f"/api/card-art?name={name}&size=normal",
        "small_image": f"/api/card-art?name={name}&size=small",
    }


BASE = {
    "generation": 1,
    "player": "alice",
    "kind": "may",
    "prompt": "",
    "source": None,
    "amount": None,
    "cards": [],
    "targets": [],
    "target_minimum": 0,
    "target_maximum": None,
    "affirmative_is_legal": True,
    "options": [],
    "moved_label": "",
    "kept_label": "",
    "card_labels": [],
    "exact": True,
    "default_cards": None,
}
SCRY = {
    **BASE,
    "kind": "scry",
    "source": "Preordain",
    "prompt": "Scry 3.",
    "amount": 3,
    # A real scry is CR 107.1c's "any number of them" (`exact=False`), never
    # `BASE`'s default `True` - L1a's split-count check would otherwise read
    # this fixture as a mandatory "exactly 3" and refuse the one- and
    # two-card answers the drag tests below post.
    "exact": False,
    "cards": ["a", "b", "c"],
    "card_labels": ["Opt", "Ponder", "Island"],
    "card_details": [_card("a", "Opt"), _card("b", "Ponder"), _card("c", "Island")],
}
SURVEIL = {**SCRY, "kind": "surveil", "source": "Consider", "prompt": "Surveil 3."}
SPLIT = {
    **SCRY,
    "source": "Devourer of Destiny",
    "prompt": "look at the top three cards; you may put one back on top",
    "moved_label": "top of your library",
    "kept_label": "exiled",
}
CHOOSE_HAND = {
    **BASE,
    "kind": "choose_cards",
    "source": "Cleanup",
    "prompt": "discard 2 cards",
    "amount": 2,
    "exact": True,
    "cards": ["c1", "c2", "c3"],
    "card_labels": ["Opt", "Ponder", "Island"],
    "card_details": [
        _card("c1", "Opt", "hand"),
        _card("c2", "Ponder", "hand"),
        _card("c3", "Island", "hand"),
    ],
}
SEARCH = {
    **CHOOSE_HAND,
    "source": "Wooded Foothills",
    "prompt": "choose up to 1 card(s) to find while searching your library",
    "amount": 1,
    "exact": False,
    "card_details": [_card("c1", "Opt"), _card("c2", "Ponder"), _card("c3", "Island")],
}
SACRIFICE = {
    **SEARCH,
    "source": "Liliana",
    "prompt": "sacrifice up to 1 creature",
    "card_details": [
        _card("c1", "Opt", "battlefield"),
        _card("c2", "Ponder", "battlefield"),
        _card("c3", "Island", "battlefield"),
    ],
}
TRIGGERS = {
    **BASE,
    "kind": "order_triggers",
    "prompt": "order your triggered abilities on the stack, bottom of the stack first",
    "amount": 0,
    "cards": ["t1", "t2", "t3"],
    "card_labels": [
        "Delver of Secrets - At the beginning of your upkeep, look at the top card.",
        "Delver of Secrets - At the beginning of your upkeep, look at the top card.",
        "Bob - At the beginning of your upkeep, reveal the top card.",
    ],
}
# CR 603.3b named by source (T2's `card_details` shape): `t1` and `t2` are two
# different permanents' triggers with distinct `source_card_id`s (T3's own
# board-highlight needs two rendered permanents to tell apart), `t3` is a
# trigger `card_details` names but with no `source_card_id` of its own (the
# unnameable-first-batch case, still possible once other kinds' abilities are
# mixed in a batch) - hovering or focusing it must be a no-op.
TRIGGERS_SOURCED = {
    **TRIGGERS,
    "cards": ["t1", "t2", "t3"],
    "card_details": [
        {
            "id": "t1",
            "kind": "ability",
            "name": "Delver of Secrets",
            "text": "At the beginning of your upkeep, look at the top card of your library.",
            "controller": "alice",
            "source_card_id": "perm1",
        },
        {
            "id": "t2",
            "kind": "ability",
            "name": "Mind Stone",
            "text": "{1}, {T}, Sacrifice this artifact: Draw a card.",
            "controller": "alice",
            "source_card_id": "perm2",
        },
        {
            "id": "t3",
            "kind": "ability",
            "name": "Bob's Delver of Secrets",
            "text": "At the beginning of your upkeep, reveal the top card.",
            "controller": "bob",
            "source_card_id": None,
        },
    ],
}
HAND = [
    _card("h1", "Forest", "hand"),
    _card("h2", "Llanowar Elves", "hand"),
    _card("h3", "Giant Growth", "hand"),
]


def _pregame(
    needed: int = 0, can: bool = True, awaiting: tuple[str, ...] = ("alice", "bob")
) -> dict:
    return {
        "step": "mulligan",
        "awaiting_mulligan": list(awaiting),
        "cards_to_bottom": {"alice": needed, "bob": 0},
        "can_mulligan": {"alice": can, "bob": True},
        "mulligans_taken": {"alice": needed, "bob": 0},
        "hand_kept": {"alice": False, "bob": False},
        "opening_hand_actor": None,
        "opening_hand_options": {"alice": [], "bob": []},
    }


def _run(expression: str, **bindings: object) -> object:
    return run_module("render/decision_window.js", FAKE_DOM + expression, **bindings)


def _answer(pending: dict, draft: dict) -> object:
    """The answer `ui/decisions.js`'s own view model posts for this draft - the
    shape every existing control already sent."""
    return run_module("decisions.js", "m.answerFor(p, d)", p=pending, d=draft)


# ----------------------------------------------------------- persistence ---


def test_the_window_persists_across_a_poll_while_the_decision_is_unchanged() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: SCRY })));
          mount(store, log);
          store.set({});
          const shown = !WIN.hidden;
          const slotA = slotOf("a");
          const done = buttonNamed(WIN, "Done");

          // A poll: the same decision, as brand-new objects, and a changed board.
          poll(store, { pending_decision: clone(SCRY), battlefield: [{ id: "x" }] });
          const samePoll =
            slotOf("a") === slotA && buttonNamed(WIN, "Done") === done && attached(slotA);

          // A drag in flight is not reset by a poll that lands mid-drag.
          ptr(slotA.children[0], "pointerdown", 1, 1, "mouse", 3);
          ptr(document, "pointermove", 80, 60, "mouse", 3);
          const dragging = slotA.classList.contains("dragging");
          poll(store, { pending_decision: clone(SCRY) });
          const dragSurvived =
            slotOf("a") === slotA &&
            slotA.classList.contains("dragging") &&
            Boolean(slotA.style.transform);
          ptr(document, "pointercancel", 80, 60, "mouse", 3);
          const cancelled = !slotA.classList.contains("dragging") && !slotA.style.transform;

          // An arrangement survives a poll.
          click(buttonNamed(slotOf("a"), "Move to bottom"));
          poll(store, { pending_decision: clone(SCRY) });
          const arranged = { kept: laneIds("kept"), moved: laneIds("moved") };

          // A new decision replaces the content.
          const z = { id: "z", name: "Brainstorm", zone: "library" };
          const next = { ...SCRY, generation: 2, cards: ["z"], card_details: [z] };
          store.set({ state: baseState({ pending_decision: next }), decisionDraft: {} });
          return {
            shown, samePoll, dragging, dragSurvived, cancelled, arranged,
            oldDoneDetached: !attached(done),
            replaced: laneIds("kept"),
            title: TITLE.textContent,
          };
        })()
        """,
        SCRY=SCRY,
    )
    assert result["shown"] is True
    assert result["samePoll"] is True, "a poll with the same decision rebuilt the window"
    assert result["dragging"] is True
    assert result["dragSurvived"] is True, "a poll mid-drag reset the dragged card"
    assert result["cancelled"] is True
    assert result["arranged"] == {"kept": ["b", "c"], "moved": ["a"]}
    assert result["oldDoneDetached"] is True, "a new decision must replace the content"
    assert result["replaced"] == ["z"]
    assert result["title"] == "Preordain"


def test_a_choose_cards_selection_survives_a_poll() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          const pick = slotOf("c2");
          click(pick);
          poll(store, { pending_decision: clone(P) });
          return { picked: picked(), same: slotOf("c2") === pick };
        })()
        """,
        P=CHOOSE_HAND,
    )
    assert result == {"picked": ["c2"], "same": True}


# ------------------------------------------------------------ split lanes ---


def test_dragging_scry_cards_between_and_within_lanes_posts_moved_and_top_order() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          const labels = { kept: laneLabel("kept"), moved: laneLabel("moved") };
          const images = imagesIn(laneCards("kept"));
          dragTo(slotOf("a"), laneCards("moved"), 400, 300);
          rectRow(laneCards("kept"));
          dragTo(slotOf("c"), laneCards("kept"), 10, 50, "touch");
          const draft = store.get().decisionDraft;
          click(buttonNamed(WIN, "Done"));
          return { labels, images, lanes: { kept: laneIds("kept"), moved: laneIds("moved") }, draft,
                   posted: decided(log) };
        })()
        """,
        P=SCRY,
    )
    assert result["labels"] == {"kept": "TOP", "moved": "BOTTOM"}
    assert result["images"] == [
        "/api/card-art?name=Opt&size=normal",
        "/api/card-art?name=Ponder&size=normal",
        "/api/card-art?name=Island&size=normal",
    ]
    assert result["lanes"] == {"kept": ["c", "b"], "moved": ["a"]}
    assert result["posted"] == [{"moved": ["a"], "top_order": ["c", "b"]}]
    assert result["posted"][0] == _answer(SCRY, result["draft"])


def test_surveil_lanes_read_top_and_graveyard_and_the_buttons_still_work() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          const labels = { kept: laneLabel("kept"), moved: laneLabel("moved") };
          click(buttonNamed(slotOf("b"), "Move to graveyard"));
          click(buttonNamed(slotOf("a"), "down"));
          click(buttonNamed(slotOf("c"), "Move to graveyard"));
          click(buttonNamed(slotOf("b"), "up"));
          click(buttonNamed(slotOf("c"), "up"));
          const draft = store.get().decisionDraft;
          click(buttonNamed(WIN, "Done"));
          return { labels, draft, lanes: { kept: laneIds("kept"), moved: laneIds("moved") },
                   posted: decided(log) };
        })()
        """,
        P=SURVEIL,
    )
    assert result["labels"] == {"kept": "TOP", "moved": "GRAVEYARD"}
    assert result["lanes"] == {"kept": ["a"], "moved": ["c", "b"]}
    assert result["posted"] == [{"moved": ["c", "b"], "top_order": ["a"]}]
    assert result["posted"][0] == _answer(SURVEIL, result["draft"])


def test_a_look_at_the_top_split_uses_the_payloads_own_lane_labels() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          dragTo(slotOf("b"), laneCards("moved"), 400, 300);
          const draft = store.get().decisionDraft;
          click(buttonNamed(WIN, "Done"));
          return { labels: { kept: laneLabel("kept"), moved: laneLabel("moved") }, draft,
                   posted: decided(log) };
        })()
        """,
        P=SPLIT,
    )
    assert result["labels"] == {"kept": "EXILED", "moved": "TOP OF YOUR LIBRARY"}
    assert result["posted"] == [{"moved": ["b"], "top_order": ["a", "c"]}]
    assert result["posted"][0] == _answer(SPLIT, result["draft"])


# -- L1a: the window's own status line must state the count the engine
# enforces (`pending.amount`/`.exact`), never leave a seat to guess it -----

SPLIT_EXACT_ONE = {**SCRY, "cards": ["a", "b"], "amount": 1, "exact": True}
SPLIT_UP_TO_ONE = {**SCRY, "amount": 1, "exact": False}


def _status_text(pending: dict) -> str:
    result = _run(
        """
        (() => {
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, []);
          store.set({});
          return walkAll(WIN, hasClass("dw-status"))[0].textContent;
        })()
        """,
        P=pending,
    )
    return str(result)


def test_the_split_window_states_any_number_for_a_plain_scry() -> None:
    assert "any number, including none" in _status_text(SCRY)


def test_the_split_window_states_exactly_one_for_an_exact_split() -> None:
    assert "exactly 1" in _status_text(SPLIT_EXACT_ONE)


def test_the_split_window_states_up_to_one_for_a_ceiling_of_one() -> None:
    assert "up to 1" in _status_text(SPLIT_UP_TO_ONE)


def test_a_click_without_movement_is_not_a_drag() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          const slot = slotOf("a");
          document.elementFromPoint = () => laneCards("moved");
          ptr(slot.children[0], "pointerdown", 5, 5, "mouse", 1);
          ptr(document, "pointerup", 6, 6, "mouse", 1);
          return { drafts: log.filter((e) => e[0] === "draft").length, kept: laneIds("kept") };
        })()
        """,
        P=SCRY,
    )
    assert result == {"drafts": 0, "kept": ["a", "b", "c"]}


# ----------------------------------------------------------- choose_cards ---


def test_choose_cards_selection_honours_an_exact_count() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          const submit = buttonNamed(WIN, "Choose 2");
          const enabledAtStart = !submit.disabled;
          click(slotOf("c1"));
          const enabledAtOne = !submit.disabled;
          click(slotOf("c2"));
          click(slotOf("c3"));
          const afterThird = picked();
          const enabledAtTwo = !buttonNamed(WIN, "Choose 2").disabled;
          click(slotOf("c1"));
          click(slotOf("c3"));
          click(buttonNamed(WIN, "Choose 2"));
          return {
            enabledAtStart, enabledAtOne, afterThird, enabledAtTwo,
            none: Boolean(buttonNamed(WIN, "Choose none") || buttonNamed(WIN, "Find nothing")),
            images: imagesIn(WIN).length,
            buttons: walkAll(WIN, hasClass("dw-pick")).map((n) => n.tagName),
            posted: decided(log),
          };
        })()
        """,
        P=CHOOSE_HAND,
    )
    assert result["enabledAtStart"] is False
    assert result["enabledAtOne"] is False
    assert result["afterThird"] == ["c1", "c2"], "a third pick past the maximum of 2 was accepted"
    assert result["enabledAtTwo"] is True
    assert result["none"] is False, "zero is not a legal answer to an exact choose-2"
    assert result["images"] == 3
    assert result["buttons"] == ["button", "button", "button"]
    assert result["posted"] == [{"card_ids": ["c2", "c3"]}]


def test_a_library_search_offers_find_nothing_and_a_single_pick_replaces() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          click(slotOf("c1"));
          click(slotOf("c2"));
          const afterTwo = picked();
          click(buttonNamed(WIN, "Find nothing"));
          return { afterTwo, posted: decided(log) };
        })()
        """,
        P=SEARCH,
    )
    assert result["afterTwo"] == ["c2"]
    assert result["posted"] == [{"card_ids": []}]


def test_a_choice_from_another_zone_where_zero_is_legal_offers_choose_none() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          click(slotOf("c3"));
          const chooseEnabled = !buttonNamed(WIN, "Choose").disabled;
          click(buttonNamed(WIN, "Choose"));
          click(buttonNamed(WIN, "Choose none"));
          return { chooseEnabled, find: Boolean(buttonNamed(WIN, "Find nothing")),
                   posted: decided(log) };
        })()
        """,
        P=SACRIFICE,
    )
    assert result["chooseEnabled"] is True
    assert result["find"] is False
    assert result["posted"] == [{"card_ids": ["c3"]}, {"card_ids": []}]


# ---------------------------------------------------------- order_triggers ---


def test_dragging_triggers_into_order_posts_the_reversed_order() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          const start = laneIds("order");
          const fallback = walkAll(WIN, hasClass("card-fallback")).length;
          const text = laneCards("order").textContent;
          rectRow(laneCards("order"));
          dragTo(slotOf("t3"), laneCards("order"), 10, 50);
          const afterDrag = laneIds("order");
          const draft = store.get().decisionDraft;
          click(buttonNamed(WIN, "Put on the stack"));
          return { start, fallback, text, afterDrag, draft,
                   posted: decided(log) };
        })()
        """,
        P=TRIGGERS,
    )
    assert result["start"] == ["t1", "t2", "t3"]
    assert result["fallback"] == 3, "a trigger with no card image must fall back to the text frame"
    assert "Delver of Secrets" in result["text"]
    assert result["afterDrag"] == ["t3", "t1", "t2"]
    assert result["posted"] == [{"order": ["t2", "t1", "t3"]}]
    assert result["posted"][0] == _answer(TRIGGERS, result["draft"])


def test_ordering_triggers_with_the_buttons_and_confirming_the_shown_order() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          const enabledUntouched = !buttonNamed(WIN, "Put on the stack").disabled;
          click(buttonNamed(WIN, "Put on the stack"));
          click(buttonNamed(slotOf("t2"), "up"));
          click(buttonNamed(WIN, "Put on the stack"));
          return { enabledUntouched, posted: decided(log) };
        })()
        """,
        P=TRIGGERS,
    )
    assert result["enabledUntouched"] is True
    assert result["posted"] == [{"order": ["t3", "t2", "t1"]}, {"order": ["t3", "t1", "t2"]}]


# -- highlighting each trigger's source permanent while ordering (T3) -------
#
# `card_details`' `source_card_id` (`TRIGGERS_SOURCED`) names the permanent
# that asked each trigger; `orderMode` points at it with `SOURCE_CLASS`
# (`render/decision_popover.js`, the same class the popover itself wears)
# while a player hovers or keyboard-focuses that ability's lane slot - so a
# player ordering CR 603.3b's batch can see which permanent asked what,
# without opening a card preview for each one.


def test_hovering_a_slot_outlines_only_its_own_source_permanent() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          const perm1 = boardCard("perm1");
          const perm2 = boardCard("perm2");
          mount(store, log);
          store.set({});
          hoverEnter(slotOf("t1"));
          return {
            perm1: perm1.classList.contains("decision-source"),
            perm2: perm2.classList.contains("decision-source"),
            total: sourceClassed().length,
          };
        })()
        """,
        P=TRIGGERS_SOURCED,
    )
    assert result == {"perm1": True, "perm2": False, "total": 1}


def test_hovering_a_second_slot_moves_the_outline_to_its_source() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          const perm1 = boardCard("perm1");
          const perm2 = boardCard("perm2");
          mount(store, log);
          store.set({});
          hoverEnter(slotOf("t1"));
          // A fast pointer move to an adjacent slot, exactly like the drag
          // tests' own `dragTo`: never more than one source lit at a time,
          // even without an intervening `pointerleave` reaching slot 1 first.
          hoverEnter(slotOf("t2"));
          return {
            perm1: perm1.classList.contains("decision-source"),
            perm2: perm2.classList.contains("decision-source"),
            total: sourceClassed().length,
          };
        })()
        """,
        P=TRIGGERS_SOURCED,
    )
    assert result == {"perm1": False, "perm2": True, "total": 1}


def test_leaving_a_slot_clears_its_source_outline() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          const perm1 = boardCard("perm1");
          mount(store, log);
          store.set({});
          hoverEnter(slotOf("t1"));
          const duringHover = perm1.classList.contains("decision-source");
          hoverLeave(slotOf("t1"));
          return { duringHover, afterLeave: perm1.classList.contains("decision-source") };
        })()
        """,
        P=TRIGGERS_SOURCED,
    )
    assert result == {"duringHover": True, "afterLeave": False}


def test_a_null_source_card_id_is_a_no_op() -> None:
    """`t3`'s ability entry carries no `source_card_id` (the unnameable-batch
    case) - hovering it never throws and lights nothing, whether or not a
    real source was lit and cleared just before it."""
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          const perm1 = boardCard("perm1");
          mount(store, log);
          store.set({});
          hoverEnter(slotOf("t1"));
          hoverLeave(slotOf("t1"));
          hoverEnter(slotOf("t3"));
          return {
            perm1: perm1.classList.contains("decision-source"),
            total: sourceClassed().length,
          };
        })()
        """,
        P=TRIGGERS_SOURCED,
    )
    assert result == {"perm1": False, "total": 0}


def test_a_source_that_is_not_rendered_is_a_no_op() -> None:
    """`perm1` is not on the page at all here - `findRenderedCard` finds
    nothing, and hovering the slot must still never throw."""
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          hoverEnter(slotOf("t1"));
          return { total: sourceClassed().length };
        })()
        """,
        P=TRIGGERS_SOURCED,
    )
    assert result == {"total": 0}


def test_focusing_a_slot_outlines_its_source_and_blurring_clears_it() -> None:
    """Keyboard and screen-reader users get the same pointer as a mouse
    hover: focus (which bubbles from an up/down button inside the slot, in a
    real browser) lights the source, and losing focus clears it."""
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          const perm2 = boardCard("perm2");
          mount(store, log);
          store.set({});
          const downBtn = buttonNamed(slotOf("t2"), "down");
          downBtn.focus();
          fire(downBtn, "focusin", {});
          const duringFocus = perm2.classList.contains("decision-source");
          fire(downBtn, "focusout", {});
          return { duringFocus, afterBlur: perm2.classList.contains("decision-source") };
        })()
        """,
        P=TRIGGERS_SOURCED,
    )
    assert result == {"duringFocus": True, "afterBlur": False}


def test_the_slots_title_names_the_source() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, log);
          store.set({});
          return slotOf("t1").title;
        })()
        """,
        P=TRIGGERS_SOURCED,
    )
    assert "Delver of Secrets" in result
    assert "look at the top card" in result


def test_replacing_the_decision_clears_the_source_outline() -> None:
    """A fresh `order_triggers` decision (a new key - different `cards`)
    replaces the window's content; nothing from the old one should stay lit."""
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          const perm1 = boardCard("perm1");
          mount(store, log);
          store.set({});
          hoverEnter(slotOf("t1"));
          const duringHover = perm1.classList.contains("decision-source");
          const next = { ...P, cards: ["u1", "u2"], card_details: [] };
          store.set({ state: baseState({ pending_decision: next }) });
          return { duringHover, afterReplace: perm1.classList.contains("decision-source") };
        })()
        """,
        P=TRIGGERS_SOURCED,
    )
    assert result == {"duringHover": True, "afterReplace": False}


def test_hiding_the_window_clears_the_source_outline() -> None:
    """The decision resolving (no `pending_decision` left) hides the window;
    a highlighted permanent must not stay lit once the window is gone."""
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          const perm1 = boardCard("perm1");
          mount(store, log);
          store.set({});
          hoverEnter(slotOf("t1"));
          store.set({ state: baseState({ pending_decision: null }) });
          return perm1.classList.contains("decision-source");
        })()
        """,
        P=TRIGGERS_SOURCED,
    )
    assert result is False


def test_answering_the_decision_clears_the_source_outline_immediately() -> None:
    """Confirming the order posts the answer right away - the highlight must
    not wait for the next poll to disappear."""
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          const perm1 = boardCard("perm1");
          mount(store, log);
          store.set({});
          hoverEnter(slotOf("t1"));
          click(buttonNamed(WIN, "Put on the stack"));
          return { outlined: perm1.classList.contains("decision-source"), posted: decided(log) };
        })()
        """,
        P=TRIGGERS_SOURCED,
    )
    assert result["outlined"] is False
    assert result["posted"] == [{"order": ["t3", "t2", "t1"]}]


# ---------------------------------------------------------------- mulligan ---


def test_the_opening_hand_is_shown_as_cards_with_keep_and_mulligan() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pregame: PG, hands: { alice: HAND } })));
          mount(store, log);
          store.set({});
          const shown = { hidden: WIN.hidden, title: TITLE.textContent, images: imagesIn(WIN) };
          click(buttonNamed(WIN, "Keep"));
          click(buttonNamed(WIN, "Mulligan"));
          return { shown, log };
        })()
        """,
        PG=_pregame(),
        HAND=HAND,
    )
    assert result["shown"]["hidden"] is False
    assert result["shown"]["title"] == "Opening hand"
    assert result["shown"]["images"] == [c["image"] for c in HAND]
    posts = [e for e in result["log"] if e[0] == "pregame"]
    assert posts == [
        ["pregame", "mulligan", {"player": "alice", "take": False}],
        ["pregame", "mulligan", {"player": "alice", "take": True}],
    ]


def test_keeping_after_a_mulligan_asks_for_the_bottom_cards_by_clicking_them() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const store = makeStore(baseApp(baseState({ pregame: PG, hands: { alice: HAND } })));
          mount(store, log);
          store.set({});
          const picksBefore = walkAll(WIN, hasClass("dw-pick")).length;
          click(buttonNamed(WIN, "Keep"));
          const postedEarly = log.filter((e) => e[0] === "pregame").length;
          const picksAfter = walkAll(WIN, hasClass("dw-pick")).length;
          const keepDisabled = buttonNamed(WIN, "Keep").disabled;
          click(slotOf("h2"));
          click(slotOf("h3"));
          const chosen = picked();
          // a poll does not throw the player back out of the bottoming step
          poll(store, { pregame: clone(PG), hands: { alice: HAND } });
          const stillChoosing = picked();
          click(buttonNamed(WIN, "Keep"));
          return { picksBefore, postedEarly, picksAfter, keepDisabled, chosen, stillChoosing,
                   posts: log.filter((e) => e[0] === "pregame") };
        })()
        """,
        PG=_pregame(needed=1),
        HAND=HAND,
    )
    assert result["picksBefore"] == 0
    assert result["postedEarly"] == 0, "Keep must ask for the bottom card first, not post"
    assert result["picksAfter"] == 3
    assert result["keepDisabled"] is True
    assert result["chosen"] == ["h3"]
    assert result["stillChoosing"] == ["h3"]
    assert result["posts"] == [
        ["pregame", "mulligan", {"player": "alice", "take": False, "bottom": ["h3"]}]
    ]


def test_no_mulligan_left_offers_keep_only() -> None:
    result = _run(
        """
        (() => {
          const store = makeStore(baseApp(baseState({ pregame: PG, hands: { alice: HAND } })));
          mount(store, []);
          store.set({});
          return {
            mulligan: Boolean(buttonNamed(WIN, "Mulligan")),
            keep: Boolean(buttonNamed(WIN, "Keep")),
            text: BODY.textContent,
          };
        })()
        """,
        PG=_pregame(can=False),
        HAND=HAND,
    )
    assert result["mulligan"] is False
    assert result["keep"] is True
    assert "no mulligan left" in result["text"]


def test_a_paused_mulligan_decision_shows_the_hand_and_posts_accept() -> None:
    result = _run(
        """
        (() => {
          const log = [];
          const state = baseState({ pending_decision: P, hands: { alice: HAND } });
          const store = makeStore(baseApp(state));
          mount(store, log);
          store.set({});
          const images = imagesIn(WIN).length;
          click(buttonNamed(WIN, "Mulligan"));
          click(buttonNamed(WIN, "Keep"));
          return { images, posted: decided(log) };
        })()
        """,
        P={**BASE, "kind": "mulligan", "prompt": "take a mulligan?"},
        HAND=HAND,
    )
    assert result["images"] == 3
    assert result["posted"] == [{"accept": True}, {"accept": False}]


# ---------------------------------------------------------------- routing ---

COMPLEX = [
    SCRY,
    SURVEIL,
    SPLIT,
    CHOOSE_HAND,
    SEARCH,
    SACRIFICE,
    TRIGGERS,
    {**BASE, "kind": "mulligan"},
]
SMALL = [
    {**BASE, "kind": "may", "source": "Small may"},
    {**BASE, "kind": "pay_life", "source": "Small pay_life"},
    {**BASE, "kind": "pay_energy", "source": "Small pay_energy"},
    {**BASE, "kind": "pay_mana", "source": "Small pay_mana"},
    {**BASE, "kind": "pay_energy_amount", "amount": 3, "source": "Small pay_energy_amount"},
    {**BASE, "kind": "pay_life_amount", "amount": 3, "source": "Small pay_life_amount"},
    {**BASE, "kind": "choose_an_amount", "amount": 3, "source": "Small choose_an_amount"},
    {
        **BASE,
        "kind": "choose_a_quality",
        "options": ["red", "blue"],
        "source": "Small choose_a_quality",
    },
    {**BASE, "kind": "choose_a_mode", "options": ["one", "two"], "source": "Small choose_a_mode"},
    {**BASE, "kind": "put_on_top_or_bottom", "source": "Small put_on_top_or_bottom"},
    {**BASE, "kind": "put_into_hand_or_graveyard", "source": "Small put_into_hand_or_graveyard"},
    {**BASE, "kind": "choose_pile", "source": "Small choose_pile"},
    {**BASE, "kind": "opening_hand_action", "source": "Small opening_hand_action"},
    {
        **BASE,
        "kind": "choose_targets",
        "source": "Small choose_targets",
        "targets": [{"kind": "player", "id": "bob"}],
        "target_minimum": 1,
        "target_maximum": 1,
    },
]


def test_complex_kinds_go_to_the_window_and_small_kinds_stay_in_the_prompt_box() -> None:
    result = _run(
        """
        (async () => {
          const R = await import(pathToFileURL(UI_DIR + "/render/ribbon.js").href);
          const NOOP = () => {};
          const log = [];
          const store = makeStore(baseApp(baseState({})));
          const base = handlersFor(store, log);
          const handlers = new Proxy(base, { get: (t, k) => (k in t ? t[k] : NOOP) });
          R.mountRibbon(document, store, handlers);
          m.mountDecisionWindow(document, store, handlers, { storage: memoryStorage() });
          const prompt = document.querySelector("#prompt-box");
          const seen = [];
          for (const p of COMPLEX.concat(SMALL)) {
            const state = baseState({ pending_decision: p, hands: { alice: HAND } });
            store.set({ state, decisionDraft: {} });
            const text = prompt.textContent;
            seen.push({ kind: p.kind, source: p.source, window: !WIN.hidden, prompt: text });
          }
          poll(store, { pregame: PG_MINE, hands: { alice: HAND } });
          const pregameMine = { window: !WIN.hidden, prompt: prompt.textContent };
          poll(store, { pregame: PG_THEIRS, hands: { alice: HAND } });
          const pregameTheirs = { window: !WIN.hidden, prompt: prompt.textContent };
          poll(store, { pending_decision: { ...SCRY_P, player: "bob" } });
          const opponents = !WIN.hidden;
          poll(store, { pending_decision: { ...SCRY_P, kind: "declare_attackers" } });
          const combat = { window: !WIN.hidden, prompt: prompt.textContent };
          return { seen, pregameMine, pregameTheirs, opponents, combat };
        })()
        """,
        UI_DIR=str(UI),
        COMPLEX=COMPLEX,
        SMALL=SMALL,
        HAND=HAND,
        PG_MINE=_pregame(),
        PG_THEIRS=_pregame(awaiting=("bob",)),
        SCRY_P=SCRY,
    )
    for entry in result["seen"][: len(COMPLEX)]:
        assert entry["window"] is True, f"{entry['kind']} did not open the decision window"
        assert "decision window" in entry["prompt"], (
            f"{entry['kind']}: the prompt box does not point at the window"
        )
    for entry in result["seen"][len(COMPLEX) :]:
        assert entry["window"] is False, f"{entry['kind']} opened the decision window"
        assert entry["source"] in entry["prompt"], (
            f"{entry['kind']} no longer renders in the prompt box"
        )
        assert "decision window" not in entry["prompt"]
    assert result["pregameMine"]["window"] is True
    assert "decision window" in result["pregameMine"]["prompt"]
    assert result["pregameTheirs"]["window"] is False
    assert "waiting on bob" in result["pregameTheirs"]["prompt"]
    assert result["opponents"] is False
    assert result["combat"]["window"] is False
    # The dedicated "Declare attackers (CR 508.1)" prompt box is gone
    # (`render/ribbon.js`'s `combatConfirmKind`) - with no battlefield
    # override, `attackCandidates` is empty, so this falls all the way
    # through to the plain click hint. The assertion this test actually
    # exists for is the one above: the decision window never opens for it.
    assert "click a card to play it" in result["combat"]["prompt"]


def test_the_prompt_box_pointer_restores_a_minimized_window() -> None:
    result = _run(
        """
        (async () => {
          const R = await import(pathToFileURL(UI_DIR + "/render/ribbon.js").href);
          const NOOP = () => {};
          const store = makeStore(baseApp(baseState({ pending_decision: SCRY_P })));
          const restore = () => m.restoreDecisionWindow(document);
          const base = { ...handlersFor(store, []), onShowDecisionWindow: restore };
          const handlers = new Proxy(base, { get: (t, k) => (k in t ? t[k] : NOOP) });
          R.mountRibbon(document, store, handlers);
          m.mountDecisionWindow(document, store, handlers, { storage: memoryStorage() });
          store.set({});
          click(MINIMIZE);
          const minimized = !m.decisionWindowIsOpen(document);
          click(buttonNamed(document.querySelector("#prompt-box"), "Show decision window"));
          return { minimized, open: m.decisionWindowIsOpen(document), bodyHidden: BODY.hidden };
        })()
        """,
        UI_DIR=str(UI),
        SCRY_P=SCRY,
    )
    assert result == {"minimized": True, "open": True, "bodyHidden": False}


# ------------------------------------------------------- the window itself ---


def test_the_window_drags_by_its_title_bar_clamped_and_remembered() -> None:
    result = _run(
        """
        (() => {
          const storage = memoryStorage();
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          m.mountDecisionWindow(document, store, handlersFor(store, []), { storage });
          store.set({});
          const start = pos();
          const out = { start, moves: [] };
          for (const pointerType of ["mouse", "touch"]) {
            const at = pos();
            ptr(TITLE, "pointerdown", at.left + 20, at.top + 10, pointerType, 2);
            ptr(document, "pointermove", at.left + 20 - 100, at.top + 10 - 50, pointerType, 2);
            const moved = pos();
            ptr(document, "pointermove", -900, -900, pointerType, 2);
            const clampedLow = pos();
            ptr(document, "pointermove", 5000, 5000, pointerType, 2);
            const clampedHigh = pos();
            ptr(document, "pointerup", 5000, 5000, pointerType, 2);
            const saved = storage.data[m.WINDOW_POSITION_KEY];
            out.moves.push({ pointerType, at, moved, clampedLow, clampedHigh, saved });
          }
          // pressing the minimize button is not the start of a move
          const before = WIN.style.left;
          ptr(MINIMIZE, "pointerdown", 10, 10, "mouse", 4);
          ptr(document, "pointermove", 300, 300, "mouse", 4);
          ptr(document, "pointerup", 300, 300, "mouse", 4);
          out.minimizeDidNotMove = WIN.style.left === before;
          // a second mount restores the remembered spot
          WIN.style.left = ""; WIN.style.top = "";
          const again = baseState({ pending_decision: { ...P, generation: 9 } });
          const store2 = makeStore(baseApp(again));
          m.mountDecisionWindow(document, store2, handlersFor(store2, []), { storage });
          store2.set({});
          out.restored = pos();
          return out;
        })()
        """,
        P=SCRY,
    )
    for move in result["moves"]:
        at = move["at"]
        assert move["moved"] == {"left": at["left"] - 100, "top": at["top"] - 50}, move
        assert move["clampedLow"] == {"left": 0, "top": 0}, move
        assert move["clampedHigh"] == {"left": 1200 - 480, "top": 800 - 360}, move
        assert move["saved"] == '{"left":720,"top":440}', move
    assert result["minimizeDidNotMove"] is True
    assert result["restored"] == {"left": 720, "top": 440}


def test_a_remembered_position_off_screen_is_clamped_back_inside() -> None:
    result = _run(
        """
        (() => {
          const remembered = JSON.stringify({ left: 5000, top: -40 });
          const storage = memoryStorage({ [m.WINDOW_POSITION_KEY]: remembered });
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          m.mountDecisionWindow(document, store, handlersFor(store, []), { storage });
          store.set({});
          const clamp = m.clampPosition(
            { left: -5, top: 900 }, { width: 100, height: 100 }, { width: 400, height: 500 }
          );
          return { ...pos(), clamp };
        })()
        """,
        P=SCRY,
    )
    assert result == {"left": 720, "top": 0, "clamp": {"left": 0, "top": 400}}


# The top row (`#game-toolbar`) spans the width above the board and paints
# over the window (z-index 55 over 35). The window moves only by its title
# bar, so a title bar under the row could never be grabbed again - and a
# remembered spot would put it back there on every reload.
TOP_ROW = """
function topRow(top, bottom) {
  const row = boxed("div", "game-toolbar");
  row.rect = { left: 0, right: 1200, top, bottom, width: 1200, height: bottom - top };
  return row;
}
HANDLE.rect = { left: 0, right: 480, top: 0, bottom: 30, width: 480, height: 30 };
"""


def test_the_pure_clamp_keeps_the_title_bar_below_the_row_and_above_the_bottom() -> None:
    result = _run(
        """
        (() => {
          const size = { width: 480, height: 360 };
          const viewport = { width: 844, height: 390 };
          const limits = { top: 48, handle: 30 };
          return {
            atZero: m.clampPosition({ left: 10, top: 0 }, size, viewport, limits),
            above: m.clampPosition({ left: 10, top: -300 }, size, viewport, limits),
            // taller than the room below the row: its title bar still shows
            tallLow: m.clampPosition({ left: 10, top: 5000 }, size, viewport, limits),
            small: m.clampPosition(
              { left: 10, top: 5000 }, { width: 200, height: 30 }, viewport, limits
            ),
            noLimits: m.clampPosition({ left: 10, top: 0 }, size, viewport),
          };
        })()
        """
    )
    assert result["atZero"] == {"left": 10, "top": 48}
    assert result["above"] == {"left": 10, "top": 48}
    assert result["tallLow"] == {"left": 10, "top": 48}
    assert result["small"] == {"left": 10, "top": 390 - 30}
    assert result["noLimits"] == {"left": 10, "top": 0}


def test_dragging_to_the_top_edge_stops_the_title_bar_under_the_row() -> None:
    result = _run(
        TOP_ROW
        + """
        (() => {
          const storage = memoryStorage();
          topRow(20, 48);  // a 28px strip dropped by a 20px safe-area inset
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          m.mountDecisionWindow(document, store, handlersFor(store, []), { storage });
          store.set({});
          const at = pos();
          ptr(TITLE, "pointerdown", at.left + 20, at.top + 10, "touch", 2);
          ptr(document, "pointermove", 300, -900, "touch", 2);
          const dragged = pos();
          ptr(document, "pointerup", 300, -900, "touch", 2);
          return { dragged, saved: JSON.parse(storage.data[m.WINDOW_POSITION_KEY]) };
        })()
        """,
        P=SCRY,
    )
    assert result["dragged"]["top"] == 48, result
    assert result["saved"]["top"] == 48, result


def test_a_remembered_top_of_zero_is_reclamped_below_the_row_on_mount() -> None:
    result = _run(
        TOP_ROW
        + """
        (() => {
          const out = {};
          topRow(0, 44);
          const remembered = JSON.stringify({ left: 40, top: 0 });
          const storage = memoryStorage({ [m.WINDOW_POSITION_KEY]: remembered });
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          m.mountDecisionWindow(document, store, handlersFor(store, []), { storage });
          store.set({});
          out.shown = pos();
          // a window already showing at the top when the page mounts again
          WIN.style.top = "0px";
          m.mountDecisionWindow(document, store, handlersFor(store, []), { storage });
          out.remounted = pos();
          return out;
        })()
        """,
        P=SCRY,
    )
    assert result["shown"] == {"left": 40, "top": 44}, result
    assert result["remounted"] == {"left": 40, "top": 44}, result


def test_without_a_top_row_the_window_may_still_reach_the_top_edge() -> None:
    result = _run(
        """
        (() => {
          const remembered = JSON.stringify({ left: 40, top: 0 });
          const storage = memoryStorage({ [m.WINDOW_POSITION_KEY]: remembered });
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          m.mountDecisionWindow(document, store, handlersFor(store, []), { storage });
          store.set({});
          return pos();
        })()
        """,
        P=SCRY,
    )
    assert result == {"left": 40, "top": 0}


def test_a_resize_reclamps_below_the_row_and_above_the_bottom() -> None:
    result = _run(
        TOP_ROW
        + """
        (() => {
          const out = {};
          const row = topRow(0, 28);
          const remembered = JSON.stringify({ left: 40, top: 28 });
          const storage = memoryStorage({ [m.WINDOW_POSITION_KEY]: remembered });
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          m.mountDecisionWindow(document, store, handlersFor(store, []), { storage });
          store.set({});
          out.before = pos();
          const resize = () => { for (const fn of window.listeners.resize || []) fn({}); };
          // the row grows (a desktop row, or a new safe-area inset)
          row.rect = { ...row.rect, top: 8, bottom: 60, height: 52 };
          resize();
          out.taller = pos();
          // the viewport shrinks below the window: the title bar stays on screen
          WIN.style.top = "700px";
          window.innerHeight = 300;
          resize();
          out.shorter = pos();
          return out;
        })()
        """,
        P=SCRY,
    )
    assert result["before"] == {"left": 40, "top": 28}, result
    assert result["taller"] == {"left": 40, "top": 60}, result
    assert result["shorter"] == {"left": 40, "top": 60}, result


def test_the_window_works_when_storage_throws() -> None:
    result = _run(
        """
        (() => {
          const out = {};
          const throwing = {
            getItem() { throw new Error("SecurityError"); },
            setItem() { throw new Error("QuotaExceededError"); },
            removeItem() { throw new Error("SecurityError"); },
          };
          out.load = m.loadWindowPosition(throwing);
          out.save = m.saveWindowPosition(throwing, { left: 1, top: 2 });
          out.loadNull = m.loadWindowPosition(null);
          const denied = () => { throw new Error("denied"); };
          for (const [name, storage] of [["object", throwing], ["accessor", denied]]) {
            const log = [];
            const state = baseState({ pending_decision: { ...P, generation: name.length } });
            const store = makeStore(baseApp(state));
            m.mountDecisionWindow(document, store, handlersFor(store, log), { storage });
            store.set({});
            ptr(TITLE, "pointerdown", 700, 70, "mouse", 2);
            ptr(document, "pointermove", 600, 90, "mouse", 2);
            ptr(document, "pointerup", 600, 90, "mouse", 2);
            click(buttonNamed(WIN, "Done"));
            const posted = decided(log).length;
            out[name] = { shown: !WIN.hidden, left: WIN.style.left, posted };
          }
          return out;
        })()
        """,
        P=SCRY,
    )
    assert result["load"] is None
    assert result["save"] is False
    assert result["loadNull"] is None
    for name in ("object", "accessor"):
        assert result[name]["shown"] is True
        assert result[name]["left"].endswith("px")
        assert result[name]["posted"] == 1


def test_the_window_minimizes_but_never_closes_and_escape_minimizes() -> None:
    result = _run(
        """
        (() => {
          const store = makeStore(baseApp(baseState({ pending_decision: P })));
          mount(store, []);
          store.set({});
          const heading = { tag: TITLE.tagName, text: TITLE.textContent };
          const openAtStart = m.decisionWindowIsOpen(document);
          click(MINIMIZE);
          const afterMinimize = {
            open: m.decisionWindowIsOpen(document),
            bodyHidden: BODY.hidden,
            winHidden: WIN.hidden,
            expanded: MINIMIZE.getAttribute("aria-expanded"),
            cls: WIN.classList.contains("minimized"),
          };
          // a poll with the decision still pending leaves it minimized
          poll(store, { pending_decision: clone(P) });
          const stillMinimized = !m.decisionWindowIsOpen(document) && !WIN.hidden;
          click(MINIMIZE);
          const restored = m.decisionWindowIsOpen(document);
          let reachedDocument = false;
          document.addEventListener("keydown", () => { reachedDocument = true; });
          const inside = slotOf("a");
          const esc = fire(inside, "keydown", { key: "Escape" });
          const afterEscape = !m.decisionWindowIsOpen(document);
          const isClose = (n) => n.tagName === "button" && /close/i.test(n.textContent);
          const closeButtons = walkAll(WIN, isClose).length;
          m.restoreDecisionWindow(document);
          m.minimizeDecisionWindow(document);
          const viaExport = !m.decisionWindowIsOpen(document);
          // the decision is answered: the window goes away on its own
          poll(store, {});
          return {
            heading, openAtStart, afterMinimize, stillMinimized, restored, afterEscape,
            reachedDocument, closeButtons, viaExport, goneWhenAnswered: WIN.hidden,
          };
        })()
        """,
        P=SCRY,
    )
    assert result["heading"] == {"tag": "h2", "text": "Preordain"}
    assert result["openAtStart"] is True
    assert result["afterMinimize"] == {
        "open": False,
        "bodyHidden": True,
        "winHidden": False,
        "expanded": "false",
        "cls": True,
    }
    assert result["stillMinimized"] is True
    assert result["restored"] is True
    assert result["afterEscape"] is True
    assert result["reachedDocument"] is False, (
        "Escape inside the window must not also cancel through app.js"
    )
    assert result["closeButtons"] == 0
    assert result["viaExport"] is True
    assert result["goneWhenAnswered"] is True


def test_a_confirm_button_survives_a_poll_that_lands_mid_press() -> None:
    """PR #20's press guard defers the fan-out while a press is down; the
    window's confirm button is also persistent, so the click lands on the
    node the press began on and posts through `onDecide` (`app.js`'s
    `decide`, whose `send()` suppresses a duplicate in flight)."""
    result = _run(
        """
        (async () => {
          const S = await import(pathToFileURL(UI_DIR + "/store.js").href);
          const store = S.createStore(baseApp(baseState({ pending_decision: P })));
          S.installPressGuard(store, document, window);
          const log = [];
          mount(store, log);
          store.set({});
          const done = buttonNamed(WIN, "Done");
          fire(done, "pointerdown", { pointerType: "mouse", button: 0, pointerId: 1 });
          poll(store, { pending_decision: clone(P), battlefield: [{ id: "q" }] });
          fire(done, "pointerup", { pointerType: "mouse", pointerId: 1 });
          await new Promise((r) => setTimeout(r, 5));
          const same = buttonNamed(WIN, "Done") === done;
          const landed = click(done);
          return { same, landed, posted: decided(log) };
        })()
        """,
        UI_DIR=str(UI),
        P=SCRY,
    )
    assert result["same"] is True
    assert result["landed"] is True
    assert result["posted"] == [{"moved": [], "top_order": ["a", "b", "c"]}]


# ------------------------------------------------------ markup and wiring ---


def _index() -> str:
    return (UI / "index.html").read_text()


def test_index_html_ships_the_window_structure() -> None:
    html = _index()
    match = re.search(r'<div id="decision-window"([^>]*)>(.*?)\n  </div>', html, re.S)
    assert match, "#decision-window is not in ui/index.html"
    attrs, inner = match.group(1), match.group(2)
    assert "hidden" in attrs
    assert 'aria-labelledby="decision-window-title"' in attrs
    for element_id in (
        "decision-window-handle",
        "decision-window-minimize",
        "decision-window-body",
    ):
        assert f'id="{element_id}"' in inner
    assert re.search(r'<h2 id="decision-window-title"', inner), "the window needs a real heading"
    assert re.search(r'<button[^>]*id="decision-window-minimize"', inner)
    assert "close" not in inner.lower(), "a pending decision can only be minimized, never closed"
    assert html.index('id="decision-window"') < html.index('id="modal"')
    side = re.search(r'<aside id="side-panel">(.*?)</aside>', html, re.S)
    assert side and "decision-window" not in side.group(1), (
        "the window must not live inside a rebuilt panel"
    )


def test_the_window_neither_dims_nor_blocks_the_board() -> None:
    css = (UI / "style.css").read_text()
    rule = re.search(r"^\.decision-window \{(.*?)^\}", css, re.M | re.S)
    assert rule, ".decision-window rule not found"
    body = rule.group(1)
    assert "position: fixed" in body
    assert "inset" not in body, "a full-screen window would block the board"
    z = int(re.search(r"z-index:\s*(\d+)", body).group(1))
    popover = int(re.search(r"^\.popover \{.*?z-index:\s*(\d+)", css, re.M | re.S).group(1))
    preview = int(re.search(r"^\.card-zoom \{.*?z-index:\s*(\d+)", css, re.M | re.S).group(1))
    assert z < popover < preview, "the card menus and the card preview must paint over the window"
    busy = re.findall(r"^(body\.busy[^{]*button[^{]*)\{(.*?)^\}", css, re.M | re.S)
    assert any("#decision-window button" in selector for selector, _ in busy), (
        "the window's confirm buttons must get the in-flight dimming the ribbon gets"
    )


def test_app_js_mounts_the_window_with_the_existing_posts_and_escape() -> None:
    app = (UI / "app.js").read_text()
    call = re.search(r"mountDecisionWindow\(document, store, \{(.*?)\}\);", app, re.S)
    assert call, "app.js does not mount the decision window"
    assert "onDecide: decide" in call.group(1)
    assert "onPregame" in call.group(1)
    assert "onDecisionDraft" in call.group(1)
    escape = re.search(r'if \(e\.key !== "Escape"\) return;(.*?)cancel\(\);', app, re.S)
    assert escape and "minimizeDecisionWindow(document)" in escape.group(1)


def test_no_decorative_punctuation_in_the_windows_text() -> None:
    sources = [(UI / "render" / "decision_window.js").read_text()]
    match = re.search(r'<div id="decision-window".*?\n  </div>', _index(), re.S)
    assert match
    sources.append(match.group(0))
    for text in sources:
        for bad in ("—", "–", "‘", "’", "“", "”", "→", "…"):
            assert bad not in text, f"decorative character {bad!r} in the decision window"
