from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

FAKE_DOM = """
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.textContent = "";
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
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
}
globalThis.document = {
  createElement: (t) => new El(t),
  createTextNode: (s) => ({ nodeType: 3, textContent: s }),
  createDocumentFragment: () => new El("#fragment"),
};
"""


def test_patch_children_keeps_nodes_with_the_same_key() -> None:
    result = run_module(
        "dom.js",
        FAKE_DOM
        + """
        (() => {
          const parent = document.createElement("div");
          const create = (item) => {
            const el = m.h("div", { dataset: { key: item.id } }, item.id);
            el.made = item.id;
            return el;
          };
          const update = (el, item) => { el.updated = (el.updated || 0) + 1; };
          m.patchChildren(parent, [{ id: "a" }, { id: "b" }], (i) => i.id, create, update);
          const first = parent.children[0];
          m.patchChildren(
            parent,
            [{ id: "b" }, { id: "a" }, { id: "c" }],
            (i) => i.id,
            create,
            update
          );
          return [
            parent.children.map((c) => c.dataset.key),
            parent.children[1] === first,
            first.updated,
          ];
        })()""",
    )
    assert result == [["b", "a", "c"], True, 2]
