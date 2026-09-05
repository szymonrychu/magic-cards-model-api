"""`ui/render/lobby.js`'s `mountLobby` against a fake DOM and a stubbed global
`fetch` - no fixture server, no library, the same "run the real shipped
module under Node" approach every other `test_ui_*.py` file uses.

`mountLobby` had no tests of its own before this whole-branch review. The one
covered here is its most basic promise: `loadBundledDecks` fires a
`GET /api/ui/decks` and appends one `<option>` per name it gets back to
*both* seat pickers (`#you-bundled` and `#ai-bundled`) - the two-select
fan-out is the part worth pinning, since a one-select test would not catch a
regression that only wired one of them.
"""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

# A DOM minimal enough for `lobby.js`'s own needs: it never goes through
# `dom.js`'s `h()` (no build step there either - `document.createElement`
# and direct `.value`/`.textContent` assignment, same as the classic script
# it was ported from), so this fake DOM stays plain rather than reusing the
# heavier `h()`-compatible one `test_ui_ribbon.py` needs - a getter-only
# `textContent` (that shape) would throw the moment `loadBundledDecks` does
# `opt.textContent = name` in strict-mode ES module code.
FAKE_DOM = """
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attrs = {};
    this.listeners = {};
    this.value = "";
    this.textContent = "";
  }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
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

function boxed(id) {
  const el = document.createElement("select");
  el.setAttribute("id", id);
  document.appendChild(el);
  return el;
}
"""


def test_mount_lobby_populates_both_bundled_deck_selects() -> None:
    result = run_module(
        "render/lobby.js",
        FAKE_DOM
        + """
        (async () => {
          const you = boxed("you-bundled");
          const ai = boxed("ai-bundled");

          const fetchCalls = [];
          globalThis.fetch = async (path) => {
            fetchCalls.push(path);
            return {
              ok: true,
              status: 200,
              json: async () => ({ decks: ["Modern Burn", "Modern Affinity"] }),
            };
          };

          const store = { get: () => ({}), set: () => {}, subscribe: () => () => {} };
          m.mountLobby(document, store, { startGame: () => {} });
          // `loadBundledDecks` is fired inside `mountLobby` without being
          // awaited (see that module's own comment on why) - give its
          // promise chain a tick to land before reading the selects back.
          await new Promise((resolve) => setTimeout(resolve, 20));

          return {
            fetchCalls,
            you: you.children.map((o) => ({ value: o.value, text: o.textContent })),
            ai: ai.children.map((o) => ({ value: o.value, text: o.textContent })),
          };
        })()
        """,
    )
    assert result["fetchCalls"] == ["/api/ui/decks"]
    expected = [
        {"value": "Modern Burn", "text": "Modern Burn"},
        {"value": "Modern Affinity", "text": "Modern Affinity"},
    ]
    assert result["you"] == expected
    assert result["ai"] == expected
