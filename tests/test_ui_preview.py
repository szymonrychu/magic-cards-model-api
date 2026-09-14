"""`ui/preview.js`: the card preview.

A mouse hovering a card gets a floating enlarged copy of it centred over the
card; a finger held on a card for `LONG_PRESS_MS` gets a large centred
overlay that closes the moment the finger lifts, and the click that release
would have become is swallowed. The real module is driven under Node against
a small fake DOM with real capture/target/bubble dispatch (so a click stopped
at `window` really never reaches the card's `onclick`) and fake timers (so
"held for 350ms" and "past the 1000ms press cap" are exact, not slept).
The store is the real `ui/store.js`, press guard included.
"""

from __future__ import annotations

import re

from ui_node import UI, requires_node, run_module

pytestmark = requires_node

STORE_URL = (UI / "store.js").as_uri()

FAKE_DOM = """
function matchOne(node, sel) {
  const tokens = sel.match(/[#.]?[\\w-]+|\\[[\\w-]+\\]/g) || [];
  if (tokens.join("") !== sel) return false;
  return tokens.every((t) => {
    if (t[0] === "#") return node.attrs.id === t.slice(1);
    if (t[0] === ".") return node._classes.has(t.slice(1));
    if (t[0] === "[") {
      const name = t.slice(1, -1);
      if (name.startsWith("data-")) {
        const key = name.slice(5).replace(/-(\\w)/g, (_, c) => c.toUpperCase());
        return node.dataset[key] !== undefined;
      }
      return name in node.attrs;
    }
    return node.tagName === t.toLowerCase();
  });
}

class El {
  constructor(tag) {
    this.tagName = tag.toLowerCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.attrs = {};
    this.style = {};
    this.hidden = false;
    this.listeners = [];
    this._classes = new Set();
    this.rect = null;
  }
  get className() { return [...this._classes].join(" "); }
  set className(v) { this._classes = new Set(String(v).split(/\\s+/).filter(Boolean)); }
  get classList() {
    const s = this._classes;
    return {
      add: (...cs) => cs.forEach((c) => s.add(c)),
      remove: (...cs) => cs.forEach((c) => s.delete(c)),
      toggle: (c, on) => {
        const want = on === undefined ? !s.has(c) : Boolean(on);
        if (want) s.add(c); else s.delete(c);
        return want;
      },
      contains: (c) => s.has(c),
    };
  }
  get id() { return this.attrs.id; }
  set id(v) { this.attrs.id = String(v); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) {
    if (c.tagName === "#fragment") {
      for (const x of [...c.children]) this.appendChild(x);
      return c;
    }
    if (c.parentNode) c.parentNode.removeChild(c);
    this.children.push(c);
    c.parentNode = this;
    return c;
  }
  insertBefore(c, ref) {
    if (c.parentNode) c.parentNode.removeChild(c);
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    c.parentNode = this;
    return c;
  }
  removeChild(c) {
    this.children = this.children.filter((x) => x !== c);
    c.parentNode = null;
    return c;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  get isConnected() {
    let n = this;
    while (n.parentNode) n = n.parentNode;
    return n === globalThis.document;
  }
  get textContent() { return this.children.map((c) => c.textContent || "").join(""); }
  set textContent(v) {
    for (const c of [...this.children]) this.removeChild(c);
    if (v) this.appendChild(document.createTextNode(v));
  }
  addEventListener(type, fn, opts) {
    const capture = opts === true || Boolean(opts && opts.capture);
    this.listeners.push({ type, fn, capture });
  }
  removeEventListener(type, fn, opts) {
    const capture = opts === true || Boolean(opts && opts.capture);
    const same = (l) => l.type === type && l.fn === fn && l.capture === capture;
    this.listeners = this.listeners.filter((l) => !same(l));
  }
  matches(sel) { return sel.split(",").some((s) => matchOne(this, s.trim())); }
  closest(sel) {
    for (let n = this; n && n.matches; n = n.parentNode) if (n.matches(sel)) return n;
    return null;
  }
  contains(other) {
    for (let n = other; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  querySelector(sel) {
    const walk = (node) => {
      for (const c of node.children || []) {
        if (c.matches && c.matches(sel)) return c;
        const found = c.children ? walk(c) : null;
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
  getBoundingClientRect() {
    const r = this.rect || { left: 0, top: 0, width: 0, height: 0 };
    return { ...r, right: r.left + r.width, bottom: r.top + r.height };
  }
}

globalThis.document = new El("#document");
document.body = new El("body");
document.appendChild(document.body);
document.createElement = (t) => new El(t);
document.createTextNode = (s) => ({ nodeType: 3, textContent: String(s), parentNode: null });
document.createDocumentFragment = () => new El("#fragment");
globalThis.window = new El("#window");
window.innerWidth = 1280;
window.innerHeight = 800;
window.matchMedia = (q) => ({ matches: q === "(hover: hover)" });

// A browser's dispatch: capture from `window` down, the target (listeners
// and its `on<type>` property), then bubble back up. `stopPropagation` at
// any node ends it there.
function dispatch(target, type, props) {
  const ev = {
    type, target, defaultPrevented: false, _stop: false, _stopImmediate: false,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { this._stop = true; },
    stopImmediatePropagation() { this._stop = true; this._stopImmediate = true; },
    ...(props || {}),
  };
  const path = [];
  for (let n = target; n; n = n.parentNode) path.push(n);
  if (target.isConnected) path.push(window);
  const run = (node, phase) => {
    ev.currentTarget = node;
    for (const l of [...node.listeners]) {
      if (l.type !== type) continue;
      if (phase !== "target" && l.capture !== (phase === "capture")) continue;
      l.fn.call(node, ev);
      if (ev._stopImmediate) return;
    }
    if (phase === "target" && typeof node["on" + type] === "function") node["on" + type](ev);
  };
  for (let i = path.length - 1; i > 0; i--) { run(path[i], "capture"); if (ev._stop) return ev; }
  run(path[0], "target");
  if (ev._stop) return ev;
  for (let i = 1; i < path.length; i++) { run(path[i], "bubble"); if (ev._stop) return ev; }
  return ev;
}

// Fake timers: nothing runs until `advance` says that much time has passed.
const clock = { now: 0, seq: 0, timers: [] };
globalThis.setTimeout = (fn, ms) => {
  const id = ++clock.seq;
  clock.timers.push({ id, at: clock.now + (ms || 0), fn });
  return id;
};
globalThis.clearTimeout = (id) => { clock.timers = clock.timers.filter((t) => t.id !== id); };
function advance(ms) {
  const end = clock.now + ms;
  for (;;) {
    const due = clock.timers
      .filter((t) => t.at <= end)
      .sort((a, b) => a.at - b.at || a.id - b.id)[0];
    if (!due) break;
    clock.timers = clock.timers.filter((t) => t !== due);
    clock.now = due.at;
    due.fn();
  }
  clock.now = end;
}

function box(id, parent) {
  const el = document.createElement("div");
  el.setAttribute("id", id);
  (parent || document.body).appendChild(el);
  return el;
}
const BOARD = box("mtg-board");
const HAND = box("my-hand", BOARD);
const STACK = box("stack-row", BOARD);
const RIBBON = box("zone-ribbon");
const WIN = box("decision-window");
const MODAL = box("modal");
const POPOVER = box("popover");
const LOBBY = box("lobby");

// A card element shaped the way `ui/cards.js`'s `cardNode` builds one: a
// `.card` carrying `data-card-id`, with the small art as its visual.
function cardEl(id, rect, parent) {
  const c = document.createElement("div");
  c.className = "card card-md";
  c.dataset.cardId = id;
  c.dataset.key = id;
  c.rect = rect;
  const img = document.createElement("img");
  img.className = "card-visual card-md";
  img.setAttribute("src", "/small/" + id);
  c.appendChild(img);
  (parent || HAND).appendChild(c);
  return c;
}

const CARD = (id, extra) => ({
  id, name: "Card " + id, image: "/large/" + id, small_image: "/small/" + id, ...(extra || {}),
});
const STATE = (extra) => ({
  you: "alice", hands: { alice: [] }, battlefield: [], ...(extra || {}),
});

const zoom = () => document.querySelector("#card-zoom");
const shown = () => Boolean(zoom()) && !zoom().hidden;
const zoomImage = () => {
  const img = zoom() && zoom().querySelector("img");
  return img ? img.getAttribute("src") : null;
};
const zoomBox = () => {
  const { left, top, width, height } = zoom().style;
  return { left, top, width, height };
};

async function mounted(state) {
  const S = await import(STORE_URL);
  const store = S.createStore({ state: state || STATE(), symbols: {} });
  const unmount = m.mountCardPreview(document, store, window);
  return { S, store, unmount };
}

const mouse = (el, type, extra) =>
  dispatch(el, type, { pointerType: "mouse", pointerId: 1, ...(extra || {}) });
const touch = (el, type, extra) =>
  dispatch(el, type, {
    pointerType: "touch", pointerId: 7, clientX: 100, clientY: 100, ...(extra || {}),
  });
"""


def run(expression: str, **bindings):
    return run_module("preview.js", FAKE_DOM + expression, STORE_URL=STORE_URL, **bindings)


# ------------------------------------------------------------------ hover --


def test_hovering_a_card_shows_an_enlarged_copy_centred_over_it() -> None:
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          const wide = cardEl("b", { left: 500, top: 300, width: 150, height: 210 });
          await mounted(STATE({ battlefield: [CARD("a"), CARD("b")] }));
          const before = shown();
          mouse(card, "pointerover");
          const small = {
            shown: shown(), box: zoomBox(), image: zoomImage(),
            cardInside: Boolean(zoom().querySelector(".card")),
          };
          mouse(card, "pointerout", { relatedTarget: document.body });
          mouse(wide, "pointerover");
          return { before, small, wide: zoomBox() };
        })()
        """
    )
    assert result["before"] is False
    # 260px is larger than 2.2 x 100px, so it wins; 88/63 is a card's aspect.
    assert result["small"] == {
        "shown": True,
        "box": {"left": "420px", "top": "188px", "width": "260px", "height": "363px"},
        "image": "/large/a",
        "cardInside": False,
    }
    # 2.2 x 150px = 330px beats 260px.
    assert result["wide"] == {"left": "410px", "top": "175px", "width": "330px", "height": "461px"}


def test_the_hover_preview_is_clamped_fully_inside_the_viewport() -> None:
    result = run(
        """
        (async () => {
          const corner = cardEl("a", { left: 1230, top: 700, width: 100, height: 140 });
          const origin = cardEl("b", { left: -20, top: -30, width: 100, height: 140 });
          await mounted(STATE({ battlefield: [CARD("a"), CARD("b")] }));
          mouse(corner, "pointerover");
          const bottomRight = zoomBox();
          mouse(origin, "pointerover");
          const topLeft = zoomBox();
          window.innerHeight = 300;
          window.innerWidth = 200;
          mouse(corner, "pointerover");
          const tiny = zoomBox();
          const rect = m.hoverRect({ left: 0, top: 0, width: 100, height: 140 }, 1280, 800);
          return { bottomRight, topLeft, tiny, rect };
        })()
        """
    )
    assert result["bottomRight"] == {
        "left": "1012px",
        "top": "429px",
        "width": "260px",
        "height": "363px",
    }
    assert result["topLeft"] == {"left": "8px", "top": "8px", "width": "260px", "height": "363px"}
    # Too big for the viewport either way: scaled down to fit, aspect kept.
    assert result["tiny"] == {"left": "8px", "top": "35px", "width": "184px", "height": "257px"}
    assert result["rect"] == {"left": 8, "top": 8, "width": 260, "height": 363}


def test_the_hover_preview_goes_away_on_mouseout_but_not_moving_within_the_card() -> None:
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          await mounted(STATE({ battlefield: [CARD("a")] }));
          mouse(card, "pointerover");
          mouse(card, "pointerout", { relatedTarget: card.children[0] });
          const inside = shown();
          mouse(card, "pointerout", { relatedTarget: null });
          const left = shown();
          return { inside, left };
        })()
        """
    )
    assert result == {"inside": True, "left": False}


def test_the_hover_preview_goes_away_when_its_card_leaves_the_dom() -> None:
    result = run(
        """
        (async () => {
          // A MutationObserver the test can drive: a render that detaches the
          // card without going through the store still takes the preview away.
          const observers = [];
          window.MutationObserver = class {
            constructor(cb) { this.cb = cb; this.on = false; observers.push(this); }
            observe() { this.on = true; }
            disconnect() { this.on = false; }
          };
          const a = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          const b = cardEl("b", { left: 700, top: 300, width: 100, height: 140 });
          const { store } = await mounted(STATE({ battlefield: [CARD("a"), CARD("b")] }));
          mouse(a, "pointerover");
          HAND.removeChild(a);
          store.set({ state: STATE({ battlefield: [CARD("b")] }) });
          const afterRender = shown();
          mouse(b, "pointerover");
          const watching = observers.some((o) => o.on);
          HAND.removeChild(b);
          observers.forEach((o) => o.on && o.cb([]));
          const afterMutation = shown();
          const stillWatching = observers.some((o) => o.on);
          return { afterRender, watching, afterMutation, stillWatching };
        })()
        """
    )
    assert result == {
        "afterRender": False,
        "watching": True,
        "afterMutation": False,
        "stillWatching": False,
    }


def test_the_hover_preview_goes_away_on_scroll_and_on_a_mouse_press() -> None:
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          let clicked = 0;
          card.onclick = () => { clicked += 1; };
          await mounted(STATE({ battlefield: [CARD("a")] }));
          mouse(card, "pointerover");
          dispatch(HAND, "scroll", {});
          const scrolled = shown();
          mouse(card, "pointerover");
          mouse(card, "pointerdown", { button: 0 });
          const pressed = shown();
          mouse(card, "pointerup");
          dispatch(card, "click", {});
          return { scrolled, pressed, clicked };
        })()
        """
    )
    # A press hides it, so the confirm popover the click opens is not covered.
    assert result == {"scrolled": False, "pressed": False, "clicked": 1}


def test_a_scroll_elsewhere_leaves_the_hover_preview_open_where_it_was() -> None:
    """`render/log.js` scrolls the log to its newest line on every poll, and a
    row next to the card may scroll too. Neither moves the hovered card, so
    the preview stays open and in place; only a scroll of something holding
    the card (its row, the board) or of the page itself closes it."""
    result = run(
        """
        (async () => {
          const log = box("log-box");
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          await mounted(STATE({ battlefield: [CARD("a")] }));
          mouse(card, "pointerover");
          const before = zoomBox();
          dispatch(log, "scroll", {});
          dispatch(STACK, "scroll", {});
          const afterUnrelated = { shown: shown(), box: zoomBox() };
          dispatch(BOARD, "scroll", {});
          const afterAncestor = shown();
          mouse(card, "pointerout", { relatedTarget: document.body });
          mouse(card, "pointerover");
          const reopened = shown();
          dispatch(document, "scroll", {});
          const afterPage = shown();
          return { before, afterUnrelated, afterAncestor, reopened, afterPage };
        })()
        """
    )
    assert result["afterUnrelated"] == {"shown": True, "box": result["before"]}
    assert result["afterAncestor"] is False
    assert result["reopened"] is True
    assert result["afterPage"] is False


def test_the_hover_preview_follows_a_card_updated_in_place() -> None:
    """`patchChildren` keeps a keyed card element alive across a poll and
    `updateCardNode` rewrites it in place, so the same element can come to
    show a different face (a transform) or a different card entirely."""
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          const { store } = await mounted(STATE({ battlefield: [CARD("a")] }));
          mouse(card, "pointerover");
          store.set({ state: STATE({ battlefield: [CARD("a", { image: "/large/a-back" })] }) });
          const flipped = { shown: shown(), image: zoomImage() };
          card.rect = { left: 600, top: 300, width: 100, height: 140 };
          card.dataset.cardId = "z";
          store.set({ state: STATE({ battlefield: [CARD("z")] }) });
          const replaced = { shown: shown(), image: zoomImage(), left: zoom().style.left };
          return { flipped, replaced };
        })()
        """
    )
    assert result["flipped"] == {"shown": True, "image": "/large/a-back"}
    assert result["replaced"] == {"shown": True, "image": "/large/z", "left": "520px"}


def test_every_card_surface_previews_and_nothing_else_does() -> None:
    result = run(
        """
        (async () => {
          const rect = { left: 500, top: 300, width: 100, height: 140 };
          const surfaces = { BOARD, HAND, RIBBON, WIN, MODAL, POPOVER, LOBBY };
          const cards = {};
          for (const [name, parent] of Object.entries(surfaces)) {
            cards[name] = cardEl("c-" + name, rect, parent);
          }
          const state = STATE({ battlefield: Object.keys(surfaces).map((n) => CARD("c-" + n)) });
          await mounted(state);
          const out = {};
          for (const [name, el] of Object.entries(cards)) {
            mouse(el, "pointerover");
            out[name] = shown();
            mouse(el, "pointerout", { relatedTarget: null });
          }
          const plain = cardEl("c-touch", rect);
          dispatch(plain, "pointerover", { pointerType: "touch" });
          out.touchHover = shown();
          return out;
        })()
        """
    )
    assert result == {
        "BOARD": True,
        "HAND": True,
        "RIBBON": True,
        "WIN": True,
        "MODAL": True,
        "POPOVER": True,
        "LOBBY": False,
        "touchHover": False,
    }


BOARD_JS_URL = (UI / "render" / "board.js").as_uri()


def test_a_spell_on_the_stack_as_board_js_draws_it_previews() -> None:
    """`render/board.js` draws each stack object as a `div.stack-object`
    wrapper keyed by the object's id through `data-key` (no `data-card-id` of
    its own), holding the object's `cardNode` and a caption. This drives the
    real `mountBoard`, so the elements hovered and pressed are exactly the
    ones the live UI puts in `#stack-row`: the mouse over the card inside,
    the finger on the wrapper."""
    result = run(
        """
        (async () => {
          for (const id of ["opp-hand", "opp-battlefield", "my-battlefield"]) box(id, BOARD);
          for (const side of ["opp", "my"]) {
            const row = box(side + "-lands", BOARD);
            box(side + "-badge", row);
            box(side + "-lands-cards", row);
          }
          const B = await import(BOARD_JS_URL);
          const spell = (name) => ({
            id: "s1", name, kind: "spell", image: "/large/" + name, targets: [],
          });
          const seat = (username) => ({
            username, life: 20, hand_count: 0, library_count: 40, graveyard_count: 0,
            mana_pool: {},
          });
          const boardState = (stack) => STATE({
            active_player: "alice", priority_holder: "alice", step: "precombat_main",
            players: [seat("alice"), seat("bob")],
            stack, combat: { attacks: [] },
          });
          const { store } = await mounted(boardState([]));
          let clicked = 0;
          B.mountBoard(document, store, {
            onPlayer() {}, onPermanent() {}, onHandCard() {}, onPermission() {},
            onStackObject() { clicked += 1; },
          });
          store.set({
            state: boardState([spell("Bolt")]), action: null, decisionDraft: {}, combatDraft: {},
          });
          const el = STACK.children[0];
          el.rect = { left: 500, top: 300, width: 120, height: 40 };
          const card = el.querySelector(".card");
          card.rect = { left: 500, top: 300, width: 120, height: 168 };
          const out = { drawnAs: el.className, cardId: el.dataset.cardId === undefined };

          mouse(card, "pointerover");
          out.hover = { shown: shown(), image: zoomImage() };
          store.set({ state: boardState([spell("Shock")]) });
          out.inPlace = { same: STACK.children[0] === el, shown: shown(), image: zoomImage() };
          mouse(card, "pointerout", { relatedTarget: null });
          out.left = shown();

          touch(el, "pointerdown");
          advance(m.LONG_PRESS_MS);
          out.longPress = { shown: shown(), overlay: zoom().classList.contains("long-press") };
          touch(el, "pointerup");
          const ev = dispatch(el, "click", {});
          out.release = { shown: shown(), clicked, prevented: ev.defaultPrevented };
          advance(2000);

          touch(el, "pointerdown");
          advance(100);
          touch(el, "pointerup");
          dispatch(el, "click", {});
          out.tap = clicked;
          out.menu = dispatch(el, "contextmenu", {}).defaultPrevented;
          return out;
        })()
        """,
        BOARD_JS_URL=BOARD_JS_URL,
    )
    assert result["drawnAs"] == "stack-object" and result["cardId"] is True
    assert result["hover"] == {"shown": True, "image": "/large/Bolt"}
    assert result["inPlace"] == {"same": True, "shown": True, "image": "/large/Shock"}
    assert result["left"] is False
    assert result["longPress"] == {"shown": True, "overlay": True}
    assert result["release"] == {"shown": False, "clicked": 0, "prevented": True}
    assert result["tap"] == 1
    assert result["menu"] is True


def test_a_decision_card_and_an_unlisted_card_still_preview() -> None:
    """A library card a scry shows exists only on the pending decision; a
    card the state view does not list at all (a render the lookup has not
    caught up with) previews from its own element's art."""
    result = run(
        """
        (async () => {
          const rect = { left: 500, top: 300, width: 100, height: 140 };
          const lib = cardEl("lib", rect, WIN);
          const stray = cardEl("stray", rect, BOARD);
          await mounted(STATE({
            pending_decision: {
              player: "alice", kind: "scry", cards: ["lib"],
              card_details: [
                { id: "lib", name: "Ponder", image: "/api/card-art?name=Ponder&size=normal" },
              ],
            },
          }));
          mouse(lib, "pointerover");
          const library = zoomImage();
          mouse(stray, "pointerover");
          return { library, stray: zoomImage() };
        })()
        """
    )
    assert result == {"library": "/api/card-art?name=Ponder&size=normal", "stray": "/small/stray"}


def test_a_card_without_art_previews_as_text() -> None:
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          await mounted(STATE({ battlefield: [{
            id: "a", name: "Grizzly Bears", type_line: "Creature - Bear",
            oracle_text: "Just a bear.", power: 2, toughness: 2,
          }] }));
          mouse(card, "pointerover");
          return { image: zoomImage(), text: zoom().textContent };
        })()
        """
    )
    assert result["image"] is None
    for part in ("Grizzly Bears", "Creature - Bear", "Just a bear.", "2/2"):
        assert part in result["text"]


def test_hoverable_cards_cover_every_visible_zone() -> None:
    result = run_module(
        "preview.js",
        """
        Object.keys(m.hoverableCards({
          you: "alice",
          battlefield: [{ id: "b" }],
          stack: [{ id: "s" }],
          graveyards: { bob: [{ id: "g" }] },
          hands: { alice: [{ id: "h" }] },
          play_permissions: { alice: [{ card: { id: "p" } }] },
        })).sort()
        """,
    )
    assert result == ["b", "g", "h", "p", "s"]


# ------------------------------------------------------------------- touch --


def test_a_short_tap_clicks_normally_and_shows_nothing() -> None:
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          let clicked = 0;
          card.onclick = () => { clicked += 1; };
          await mounted(STATE({ battlefield: [CARD("a")] }));
          touch(card, "pointerdown");
          advance(200);
          touch(card, "pointerup");
          const ev = dispatch(card, "click", {});
          advance(1000);
          return { clicked, prevented: ev.defaultPrevented, everShown: shown() };
        })()
        """
    )
    assert result == {"clicked": 1, "prevented": False, "everShown": False}


def test_a_long_press_opens_a_large_centred_preview() -> None:
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          await mounted(STATE({ battlefield: [CARD("a")] }));
          touch(card, "pointerdown");
          advance(m.LONG_PRESS_MS - 1);
          const early = shown();
          touch(card, "pointermove", { clientX: 106, clientY: 106 });
          advance(1);
          const out = {
            early, shown: shown(), box: zoomBox(), image: zoomImage(),
            overlay: zoom().classList.contains("long-press"),
          };
          return { ...out, ms: m.LONG_PRESS_MS, slop: m.LONG_PRESS_SLOP_PX };
        })()
        """
    )
    assert result["ms"] == 350 and result["slop"] == 10
    assert result["early"] is False
    # 800px viewport less a 16px margin each side is 768px tall; 768 x 63/88.
    assert result["shown"] is True and result["overlay"] is True
    assert result["box"] == {"left": "365px", "top": "16px", "width": "550px", "height": "768px"}
    assert result["image"] == "/large/a"


def test_lifting_the_finger_closes_the_preview_and_swallows_its_click() -> None:
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          let clicked = 0;
          card.onclick = () => { clicked += 1; };
          await mounted(STATE({ battlefield: [CARD("a")] }));
          const out = {};
          for (const release of ["pointerup", "pointercancel"]) {
            touch(card, "pointerdown");
            advance(600);
            const open = shown();
            touch(card, release);
            const closed = !shown();
            advance(250);
            const ev = dispatch(card, "click", {});
            out[release] = { open, closed, clicked, prevented: ev.defaultPrevented };
            advance(2000);
          }
          // The next ordinary tap is a click again.
          touch(card, "pointerdown");
          advance(100);
          touch(card, "pointerup");
          dispatch(card, "click", {});
          out.nextTap = clicked;
          return out;
        })()
        """
    )
    for release in ("pointerup", "pointercancel"):
        assert result[release] == {"open": True, "closed": True, "clicked": 0, "prevented": True}, (
            release
        )
    assert result["nextTap"] == 1


def test_moving_before_the_long_press_cancels_it() -> None:
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          let clicked = 0;
          card.onclick = () => { clicked += 1; };
          await mounted(STATE({ battlefield: [CARD("a")] }));
          touch(card, "pointerdown", { clientX: 100, clientY: 100 });
          advance(100);
          touch(card, "pointermove", { clientX: 108, clientY: 107 });
          advance(1000);
          const moved = shown();
          touch(card, "pointerup");
          const ev = dispatch(card, "click", {});
          return { moved, clicked, prevented: ev.defaultPrevented };
        })()
        """
    )
    # sqrt(8^2 + 7^2) is about 10.6px: past the slop, so no preview and no swallowing.
    assert result == {"moved": False, "clicked": 1, "prevented": False}


def test_the_context_menu_and_text_selection_are_suppressed_on_cards_only() -> None:
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          await mounted(STATE({ battlefield: [CARD("a")] }));
          return {
            menu: dispatch(card.children[0], "contextmenu", {}).defaultPrevented,
            select: dispatch(card, "selectstart", {}).defaultPrevented,
            elsewhere: dispatch(LOBBY, "contextmenu", {}).defaultPrevented,
          };
        })()
        """
    )
    assert result == {"menu": True, "select": True, "elsewhere": False}


def test_the_press_guard_holds_renders_while_the_long_press_preview_is_open() -> None:
    result = run(
        """
        (async () => {
          const card = cardEl("a", { left: 500, top: 300, width: 100, height: 140 });
          const { S, store } = await mounted(STATE({ battlefield: [CARD("a")] }));
          S.installPressGuard(store, document, window);
          const counter = { renders: 0 };
          store.subscribe(() => { counter.renders += 1; });

          touch(card, "pointerdown");
          store.set({ state: STATE({ battlefield: [CARD("a")], turn_number: 2 }) });
          advance(m.LONG_PRESS_MS);
          const opened = shown();
          advance(S.PRESS_CAP_MS * 3);
          const held = counter.renders;
          touch(card, "pointerup");
          advance(0);
          const released = counter.renders;

          // An ordinary press still flushes on the cap.
          dispatch(card, "pointerdown", { pointerType: "mouse", pointerId: 1, button: 0 });
          store.set({ state: STATE({ battlefield: [CARD("a")], turn_number: 3 }) });
          advance(S.PRESS_CAP_MS - 1);
          const ordinaryHeld = counter.renders;
          advance(1);
          return { opened, held, released, ordinaryHeld, ordinaryFlushed: counter.renders };
        })()
        """
    )
    assert result["opened"] is True
    assert result["held"] == 0, "a render landed under the finger while the preview was open"
    assert result["released"] == 1, "lifting the finger did not release the held render"
    assert result["ordinaryHeld"] == 1
    assert result["ordinaryFlushed"] == 2, "the press cap no longer applies to an ordinary press"


def test_unmount_removes_every_listener() -> None:
    result = run(
        """
        (async () => {
          const { unmount } = await mounted();
          unmount();
          return { document: document.listeners.length, window: window.listeners.length };
        })()
        """
    )
    assert result == {"document": 0, "window": 0}


# ------------------------------------------------------ markup and wiring --


def test_the_old_fixed_preview_panel_is_gone() -> None:
    html = (UI / "index.html").read_text()
    css = (UI / "style.css").read_text()
    panels = (UI / "render" / "panels.js").read_text()
    assert "card-preview" not in html
    assert "card-preview" not in css
    assert "card-preview" not in panels
    assert "mountHoverPreview" not in panels
    assert re.search(r'<div id="card-zoom" class="card-zoom"[^>]*\bhidden\b', html)


def test_the_preview_paints_over_everything_but_the_toast_and_never_takes_a_click() -> None:
    css = (UI / "style.css").read_text()
    rule = re.search(r"^\.card-zoom \{(.*?)^\}", css, re.M | re.S)
    assert rule, ".card-zoom rule not found"
    body = rule.group(1)
    assert "position: fixed" in body
    assert "pointer-events: none" in body

    def z(selector: str) -> int:
        found = re.search(rf"^{re.escape(selector)} \{{[^}}]*?z-index:\s*(\d+)", css, re.M | re.S)
        assert found, selector
        return int(found.group(1))

    assert z(".modal") < z(".card-zoom") < z(".toast")


def test_cards_suppress_the_callout_and_selection_but_still_scroll() -> None:
    css = (UI / "style.css").read_text()
    section = css[css.index("card preview (ui/preview.js)") :]
    # A stack object stands for a card too (`render/board.js` wraps its card
    # and caption in a `.stack-object`), and a long press on it opens the preview.
    rule = re.search(r"^\.card,\s*\.stack-object \{(.*?)^\}", section, re.M | re.S)
    assert rule, "the preview section has no .card, .stack-object rule"
    body = rule.group(1)
    assert "-webkit-touch-callout: none" in body
    assert "user-select: none" in body
    assert "-webkit-user-select: none" in body
    assert re.search(r"touch-action: (manipulation|pan-x pan-y)", body)


def test_app_js_mounts_the_preview_after_the_press_guard() -> None:
    app = (UI / "app.js").read_text()
    assert 'import { mountCardPreview } from "./preview.js";' in app
    assert "mountHoverPreview" not in app
    assert app.index("installPressGuard(store);") < app.index("mountCardPreview(document, store);")
