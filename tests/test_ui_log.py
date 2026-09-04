"""`ui/render/log.js`'s `mountLog` against a fake DOM (the pattern from
`tests/test_ui_ribbon.py`, extended with `scrollTop`/`scrollHeight`/
`clientHeight` so the auto-scroll behaviour has something to read and write).

Three scenarios, matching the task brief:

- mounting on a store with three entries (one refused, one a `report_problem`)
  renders three keyed `.log-line` children, in `seq` order, with the refused
  entry's engine-verbatim `reason` present in a `.refusal` span and the
  `report_problem` entry carrying the `log-problem` bold class;
- a second `store.set` with one more entry appended renders four children
  without discarding the three already on screen - `dom.js`'s keyed
  `patchChildren`, not a rebuild, so the first line's own DOM node survives
  the patch untouched;
- the auto-scroll: a panel already scrolled to its own bottom before a patch
  is pinned to the new bottom after it; one that was scrolled up is left
  alone.
"""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

# The `test_ui_ribbon.py` fake DOM, plus `scrollTop`/`scrollHeight`/
# `clientHeight` (all real-DOM scroll geometry `log.js`'s auto-scroll reads
# and writes) initialized to 0, the same "nothing has scrolled yet" default
# a freshly mounted, empty panel has in a real browser.
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
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
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

globalThis.document = new El("#document");
document.createElement = (t) => new El(t);
document.createTextNode = (s) => ({ nodeType: 3, textContent: s });
document.createDocumentFragment = () => new El("#fragment");

function boxed(id) {
  const el = document.createElement("div");
  el.setAttribute("id", id);
  document.appendChild(el);
  return el;
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

function passEntry(seq, player) {
  return {
    seq, action: "pass_priority", turn: 1, step: "precombat_main",
    detail: { player, allowed: true },
  };
}

const ENTRIES = [
  {
    seq: 1, action: "pass_priority", turn: 1, step: "precombat_main",
    detail: { player: "alice", allowed: true },
  },
  {
    seq: 2, action: "cast", turn: 1, step: "precombat_main",
    detail: { player: "alice", card: "Lightning Bolt", allowed: false, reason: "no legal target" },
  },
  {
    seq: 3, action: "report_problem", turn: 1, step: "precombat_main",
    detail: { player: "bob", text: "the engine let this through" },
  },
];
"""


def test_three_entries_render_as_three_keyed_lines_with_refusal_and_bold() -> None:
    result = run_module(
        "render/log.js",
        FAKE_DOM
        + """
        (() => {
          boxed("log-box");
          const store = makeStore({ log: { entries: ENTRIES, lastSeq: 3 } });
          m.mountLog(document, store);
          store.set({});
          const box = document.querySelector("#log-box");
          const lines = box.children;
          return {
            count: lines.length,
            keys: lines.map((l) => l.dataset.key),
            refusalLineText: lines[1].textContent,
            refusalSpanClasses: lines[1].children.map((c) => c.className || ""),
            problemLineClass: lines[2].className,
            firstLineClass: lines[0].className,
          };
        })()
        """,
    )
    assert result["count"] == 3
    assert result["keys"] == ["1", "2", "3"]
    assert "#2" in result["refusalLineText"]
    assert "no legal target" in result["refusalLineText"]
    assert "refusal" in result["refusalSpanClasses"]
    assert "log-problem" in result["problemLineClass"]
    assert "log-problem" not in result["firstLineClass"]


def test_a_fourth_entry_adds_exactly_one_child_and_keeps_existing_nodes() -> None:
    result = run_module(
        "render/log.js",
        FAKE_DOM
        + """
        (() => {
          boxed("log-box");
          const store = makeStore({ log: { entries: ENTRIES, lastSeq: 3 } });
          m.mountLog(document, store);
          store.set({});
          const box = document.querySelector("#log-box");
          const firstLineBefore = box.children[0];
          store.set({ log: { entries: [...ENTRIES, passEntry(4, "bob")], lastSeq: 4 } });
          return {
            count: box.children.length,
            keys: box.children.map((l) => l.dataset.key),
            firstNodeUnchanged: box.children[0] === firstLineBefore,
          };
        })()
        """,
    )
    assert result["count"] == 4
    assert result["keys"] == ["1", "2", "3", "4"]
    assert result["firstNodeUnchanged"] is True


def test_auto_scroll_pins_the_bottom_only_when_already_there() -> None:
    result = run_module(
        "render/log.js",
        FAKE_DOM
        + """
        (() => {
          boxed("log-box");
          const store = makeStore({ log: { entries: ENTRIES, lastSeq: 3 } });
          m.mountLog(document, store);
          store.set({});
          const box = document.querySelector("#log-box");

          // Scenario A: the panel was scrolled up (not at the bottom) before
          // the patch - a fourth entry must not yank it back down.
          box.scrollHeight = 100;
          box.clientHeight = 20;
          box.scrollTop = 0; // far from bottom (100 - 20 = 80 away)
          store.set({
            log: { entries: [...ENTRIES, passEntry(4, "bob")], lastSeq: 4 },
          });
          const scrollTopAfterScrolledUp = box.scrollTop;

          // Scenario B: the panel was at the bottom before the patch - a
          // fifth entry pins it to the (grown) bottom.
          box.scrollHeight = 100;
          box.clientHeight = 20;
          box.scrollTop = 80; // exactly at the bottom
          store.set({
            log: {
              entries: [...ENTRIES, passEntry(4, "bob"), passEntry(5, "alice")],
              lastSeq: 5,
            },
          });
          box.scrollHeight = 140; // the browser would have grown this on its own
          return {
            scrollTopAfterScrolledUp,
            scrollTopAfterAtBottom: box.scrollTop,
          };
        })()
        """,
    )
    # Scrolled up: the patch must not have moved scrollTop off 0.
    assert result["scrollTopAfterScrolledUp"] == 0
    # At the bottom: mountLog pinned scrollTop to scrollHeight (100) at patch
    # time, before this script bumped scrollHeight again afterwards to stand
    # in for the browser's own post-layout growth.
    assert result["scrollTopAfterAtBottom"] == 100
