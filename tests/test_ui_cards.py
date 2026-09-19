"""`ui/cards.js`'s `badges()` - CR 722.3a's prepared designation.

The Reality Fracture bump (engine 03a891c) added `CardState.prepared` -
`serialize._card_view` surfaces it on the battlefield as `card.prepared`, and
this is the render half: a `badge-prepared` chip so a seat can see which
permanent already spent its prepare spell, the same way `badge-named`
already lets one see a chosen name. `FAKE_DOM` is `test_ui_dom.py`'s own
minimal `document` stand-in, copied rather than imported: `h()` (`ui/dom.js`)
needs `document.createElement` to build the badge nodes `badges()` returns,
and there is no real DOM under plain Node.
"""

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
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
}
globalThis.document = {
  createElement: (t) => new El(t),
  createTextNode: (s) => ({ nodeType: 3, textContent: s }),
};
"""


def test_a_prepared_card_gets_the_prepared_badge() -> None:
    classes = run_module(
        "cards.js",
        FAKE_DOM + "m.badges(card).map((b) => b.className)",
        card={"name": "Test Card", "prepared": True},
    )
    assert any("badge-prepared" in cls for cls in classes), classes


def test_an_unprepared_card_gets_no_prepared_badge() -> None:
    classes = run_module(
        "cards.js",
        FAKE_DOM + "m.badges(card).map((b) => b.className)",
        card={"name": "Test Card"},
    )
    assert not any("badge-prepared" in cls for cls in classes), classes
