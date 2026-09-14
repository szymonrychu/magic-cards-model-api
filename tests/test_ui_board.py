"""`ui/render/board.js`'s `mountBoard` against a fake DOM (the pattern from
`tests/test_ui_ribbon.py`) - `board.js` had no tests of its own before this
whole-branch review.

The critical fix this pins: the opponent's info row's click handler (now
`#opp-badge`, the player badge in the opponent's land row) used to read
`store.get().opponent`, a store field that has never existed (the store
never carries an `opponent` key at all - see `ui/app.js`'s own
`createStore(...)` call) - so clicking the opponent's info row always called
`handlers.onPlayer(undefined)`. `ui/combat.js`'s `opponentOf(state)` is the
real (and already-used-elsewhere, e.g. `ui/app.js`'s `toggleAttacker`) way to
find the other seat's username; `board.js` now reads it off `store.get().state`
instead of a field that was never there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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
    // `setProperty`/`getPropertyValue` because the fit pass writes each
    // row's `--card-w` through them, the way a browser's CSSStyleDeclaration
    // takes a custom property.
    this.style = {
      props: {},
      setProperty: (k, v) => { this.style.props[k] = String(v); },
      getPropertyValue: (k) => this.style.props[k] || "",
    };
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
// A land row holds the seat's player badge in its own left column, then the
// cards, the way `ui/index.html` nests them.
function badgeRow(side) {
  const row = boxed(`${side}-lands`);
  document.removeChild(row);
  for (const id of [`${side}-badge`, `${side}-lands-cards`]) {
    const el = document.createElement("div");
    el.setAttribute("id", id);
    row.appendChild(el);
  }
  document.appendChild(row);
  return row;
}
boxed("mtg-board");
boxed("opp-hand");
badgeRow("opp");
boxed("opp-battlefield");
boxed("stack-row");
boxed("zone-ribbon");
boxed("my-battlefield");
badgeRow("my");
boxed("my-hand");

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
# folded into the opp-badge click test so it also proves the badge is lit
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


def test_clicking_the_opp_badge_calls_on_player_with_the_opponents_username() -> None:
    """The regression itself: `store.get().opponent` never existed, so the
    old handler always called `onPlayer(undefined)`. `alice` is `state.you`;
    `bob` is the only other seat, and must be exactly what `onPlayer` sees -
    `ui/app.js` hands that straight to `tryTarget`, as the info row did."""
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
          document.querySelector("#opp-badge").onclick({ target: null });
          const oppTargetable =
            document.querySelector("#opp-badge").classList.contains("targetable");
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


# ---------------------- the player badge is the only mana-pool render site ----
#
# `patchBadge` is where a seat's pool reaches the board, so it is where the
# CR 106.6-restricted slice has to become visible. `manaPoolNodes`'s own
# structure is pinned in `tests/test_ui_mana_pool.py`; this only proves the
# board actually passes the whole pool through, restricted entries included,
# rather than the `floating`/`snow` pair it used to be given.


def test_the_badge_renders_a_seats_restricted_mana_as_its_own_group() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + """
        (() => {
          const only = "Spend this mana only to cast Eldrazi spells.";
          const state = baseState();
          state.players[0].mana_pool = {
            floating: { C: 3 },
            snow: {},
            restricted: [{ restriction: { text: only, understood: true }, amounts: { C: 2 } }],
          };
          const store = makeStore(baseApp(state));
          m.mountBoard(document, store, {
            onHandCard: NOOP, onPlayer: NOOP, onStackObject: NOOP,
            onPermission: NOOP, onPermanent: NOOP,
          });
          store.set({});
          const pool = document.querySelector("#my-badge").querySelector(".pool");
          return pool.children.map((g) => ({
            classes: [...g.classList.set].sort(),
            title: g.attrs.title,
          }));
        })()
        """,
    )
    assert result == [
        {"classes": ["pool-group"], "title": "1 C"},
        {
            "classes": ["pool-group", "restricted"],
            "title": "2 C - Spend this mana only to cast Eldrazi spells.",
        },
    ]


# ------------------------------------------------ the scalable board layout --
#
# Seven rows, each a fixed share of the board's height whether or not it has
# a card in it, and every card on them sized from the row's own `--card-w`
# (`ui/fit.js` decides the number, `board.js` measures and applies it). The
# red zone row is gone: attackers and blockers stay marked where they stand.

UI = Path(__file__).resolve().parents[1] / "ui"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "payloads"

BOARD_ROWS = [
    ("opp-hand", "0.6"),
    ("opp-lands", "1"),
    ("opp-battlefield", "1.2"),
    ("stack-row", "0.8"),
    ("my-battlefield", "1.2"),
    ("my-lands", "1"),
    ("my-hand", "1.2"),
]


def _kozilek_stack_object() -> dict:
    payload = json.loads((FIXTURES / "kozilek_command_zone_flow.json").read_text())
    state = payload.get("state", payload)
    return state["stack"][0]


def test_the_board_rows_run_in_the_seven_row_order() -> None:
    page = (UI / "index.html").read_text()
    board = page.split('<div id="mtg-board">', 1)[1].split('<aside id="zone-ribbon">', 1)[0]
    fit_rows = re.findall(r'<div id="([a-z-]+)" class="[^"]*\bfit-row\b[^"]*"', board)
    assert fit_rows == [row for row, _ in BOARD_ROWS]


def test_each_board_row_has_its_fixed_height_weight() -> None:
    css = (UI / "style.css").read_text()
    for row, weight in BOARD_ROWS:
        rule = re.search(r"\n#" + re.escape(row) + r" \{([^}]*)\}", css)
        assert rule is not None, f"no #{row} rule"
        assert re.search(r"flex:\s*" + re.escape(weight) + r" 1 0;", rule.group(1)), (
            row,
            rule.group(1),
        )


def test_the_red_zone_row_is_gone_from_page_script_and_style() -> None:
    for name in ("index.html", "render/board.js", "style.css"):
        text = (UI / name).read_text()
        assert "red-zone" not in text and "redZone" not in text, name


def test_an_attacker_is_marked_where_it_stands_without_a_red_zone() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({
            step: "declare_blockers",
            battlefield: [creature("g1", "alice"), creature("b1", "bob")],
            combat: { attacks: [{ attacker: "g1", blockers: ["b1"] }] },
          });
          const store = makeStore(baseApp(state));
          m.mountBoard(document, store, {
            onHandCard: NOOP, onPlayer: NOOP, onStackObject: NOOP, onPermission: NOOP,
            onPermanent: NOOP,
          });
          store.set({});
          const host = (id) => document.querySelector(id).children[0].children[0];
          return {
            redZone: document.querySelector("#red-zone"),
            attacker: [...host("#my-battlefield").classList.set].includes("attacking"),
            blocker: [...host("#opp-battlefield").classList.set].includes("blocking"),
          };
        })()
        """,
    )
    assert result == {"redZone": None, "attacker": True, "blocker": True}


STACK_MOUNT = """
function mountStack(obj, action) {
  const calls = [];
  const state = baseState({ stack: [obj] });
  const store = makeStore(baseApp(state, { action: action || null }));
  m.mountBoard(document, store, {
    onHandCard: NOOP, onPlayer: NOOP, onPermission: NOOP, onPermanent: NOOP,
    onStackObject: (o) => calls.push(o.id),
  });
  store.set({});
  const wrapper = document.querySelector("#stack-row").children[0];
  const card = wrapper.querySelector(".card");
  const caption = wrapper.querySelector(".stack-caption");
  return { store, calls, wrapper, card, caption };
}
"""


def test_a_stack_object_is_drawn_as_a_card_with_a_caption() -> None:
    obj = _kozilek_stack_object()
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + STACK_MOUNT
        + """
        (() => {
          const { store, calls, wrapper, card, caption } = mountStack(obj);
          store.set({});
          const again = document.querySelector("#stack-row").children[0];
          wrapper.onclick();
          return {
            count: document.querySelector("#stack-row").children.length,
            wrapper: [...wrapper.classList.set].sort(),
            wrapperKey: wrapper.dataset.key,
            card: [...card.classList.set].sort(),
            cardId: card.dataset.cardId,
            caption: caption.textContent,
            survives: again === wrapper && again.querySelector(".card") === card,
            calls,
          };
        })()
        """,
        obj=obj,
    )
    assert result["count"] == 1
    assert "stack-object" in result["wrapper"]
    assert result["wrapperKey"] == obj["id"]
    assert result["card"] == ["card", "card-fit"]
    assert result["cardId"] == obj["id"]
    assert result["caption"] == "X=1 -> alice, alice"
    assert result["survives"] is True
    assert result["calls"] == [obj["id"]]


def test_a_stack_card_without_x_or_targets_keeps_an_empty_caption() -> None:
    payload = json.loads((FIXTURES / "fetchland_search_choice.json").read_text())
    obj = payload.get("state", payload)["stack"][0]
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + STACK_MOUNT
        + """
        (() => {
          const { card, caption } = mountStack(obj);
          return { caption: caption.textContent, card: [...card.classList.set].sort() };
        })()
        """,
        obj=obj,
    )
    assert result == {"caption": "", "card": ["card", "card-fit"]}


def test_a_stack_card_lights_up_as_a_target_and_as_chosen() -> None:
    obj = _kozilek_stack_object()
    action = json.loads(json.dumps(ONE_TARGET_ACTION))
    action["affordance"]["candidates"] = [
        {"id": obj["id"], "kind": "spell", "name": obj["name"], "requirements": [0]}
    ]
    chosen = json.loads(json.dumps(action))
    chosen["slots"][0]["targets"] = [obj["id"]]
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + STACK_MOUNT
        + """
        (() => {
          // Both mounts draw into the same `#stack-row`, and the keyed card
          // survives the second one - so each is read before the next mount.
          const has = (el, c) => el.classList.contains(c);
          const open = mountStack(obj, action).card;
          const openClasses = { targetable: has(open, "targetable"), chosen: has(open, "chosen") };
          const picked = mountStack(obj, chosen).card;
          return { open: openClasses, picked: { chosen: has(picked, "chosen") } };
        })()
        """,
        obj=obj,
        action=action,
        chosen=chosen,
    )
    assert result == {"open": {"targetable": True, "chosen": False}, "picked": {"chosen": True}}


def test_every_card_on_the_seven_rows_sizes_from_card_w() -> None:
    obj = _kozilek_stack_object()
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + """
        (() => {
          const state = baseState({
            battlefield: [
              creature("g1", "alice"),
              creature("f1", "alice", { type_line: "Basic Land - Forest" }),
              creature("b1", "bob"),
            ],
            hands: { alice: [{ id: "h1", name: "Counterspell", zone: "hand", abilities: [] }] },
            stack: [obj],
            known_cards: { alice: { hand: [{ id: "k1", name: "Bolt", owner: "bob",
              zone: "hand", abilities: [] }], library: [], sideboard: [] } },
          });
          const store = makeStore(baseApp(state));
          m.mountBoard(document, store, {
            onHandCard: NOOP, onPlayer: NOOP, onStackObject: NOOP, onPermission: NOOP,
            onPermanent: NOOP,
          });
          store.set({});
          const cards = [];
          const walk = (node) => {
            if (node.classList && (node.classList.contains("card") ||
                node.classList.contains("card-back"))) cards.push([...node.classList.set]);
            for (const c of node.children || []) walk(c);
          };
          for (const id of ["#opp-hand", "#opp-lands", "#opp-battlefield", "#stack-row",
                            "#my-battlefield", "#my-lands", "#my-hand"]) {
            walk(document.querySelector(id));
          }
          return cards;
        })()
        """,
        obj=obj,
    )
    faces = [c for c in result if "card" in c]
    assert len(faces) == 6, result
    for classes in faces:
        assert "card-fit" in classes, classes
        assert not {"card-sm", "card-md", "card-lg"} & set(classes), classes
    assert sum("card-back" in c for c in result) == 2


def test_permitted_cards_size_from_their_own_fitted_card_w() -> None:
    """`#permitted-row` used to draw fixed 108px `card-md` cards in a
    content-sized row, so on a short board it pushed the seven rows under
    their floor and scrolled. Its cards are `card-fit` now, sized by the
    `--card-w` the fit pass writes on the row itself, and the row is hidden
    while no permission exists."""
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + FIT_MOUNT
        + """
        (() => {
          const permitted = boxed("permitted-row", true);
          permitted.clientWidth = 600;
          permitted.clientHeight = 130;
          const exiled = (id) => ({
            id, name: id, zone: "exile", controller: "alice", abilities: [],
          });
          const grant = (id) => ({ card: exiled(id), granted_by: "Bloodbraid Elf",
            from_zone: "exile", usable_now: true, why_not: null });
          const state = baseState();
          const store = makeStore(baseApp(state));
          const clicked = [];
          m.mountBoard(document, store, {
            onHandCard: NOOP, onPlayer: NOOP, onStackObject: NOOP, onPermanent: NOOP,
            onPermission: (p) => clicked.push(p.card.id),
          });
          store.set({});
          const empty = { hidden: permitted.hidden, cards: permitted.children.length };
          const permissions = { alice: [grant("x1"), grant("x2")] };
          store.set({ state: { ...state, play_permissions: permissions } });
          permitted.children[1].onclick({});
          const observed = observers.flatMap((o) => o.targets.map((t) => t.attrs.id));
          return {
            empty,
            hidden: permitted.hidden,
            classes: permitted.children.map((c) => [...c.classList.set]),
            cardW: cardW("#permitted-row"),
            clicked,
            observed: observed.includes("permitted-row"),
          };
        })()
        """,
    )
    assert result["empty"] == {"hidden": True, "cards": 0}
    assert result["hidden"] is False
    assert len(result["classes"]) == 2
    for classes in result["classes"]:
        assert "card-fit" in classes, classes
        assert not {"card-sm", "card-md", "card-lg"} & set(classes), classes
    # 130px high: floor((130) / (88/63)) = 93px, well inside 600px for two.
    assert result["cardW"] == "93px"
    assert result["clicked"] == ["x2"]
    assert result["observed"] is True


def test_the_permitted_row_is_a_fitted_share_of_the_board_not_content_sized() -> None:
    css = (UI / "style.css").read_text()
    found = re.search(r"\n#mtg-board > #permitted-row \{([^}]*)\}", css)
    assert found is not None, "no #mtg-board > #permitted-row rule"
    rule = found.group(1)
    assert re.search(r"flex:\s*[0-9.]+ 1 0;", rule), rule
    assert "flex: 0 0 auto" not in rule
    assert "overflow-x: auto;" in rule and "overflow-y: hidden;" in rule
    assert "flex-wrap: nowrap;" in rule
    assert "--card-h: calc(var(--card-w, 76px) * 88 / 63);" in rule


FIT_MOUNT = """
const observers = [];
globalThis.ResizeObserver = class {
  constructor(fn) { this.fn = fn; this.targets = []; observers.push(this); }
  observe(el) { this.targets.push(el); }
  disconnect() { this.targets = []; }
};
function sized(id, width, height) {
  const el = document.querySelector(id);
  el.clientWidth = width;
  el.clientHeight = height;
  return el;
}
const cardW = (id) => document.querySelector(id).style.getPropertyValue("--card-w");
"""


def test_the_fit_pass_writes_card_w_per_row_after_render_and_on_resize() -> None:
    obj = _kozilek_stack_object()
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + FIT_MOUNT
        + """
        (() => {
          sized("#opp-hand", 1000, 100);
          sized("#opp-lands-cards", 300, 10000);
          sized("#opp-battlefield", 1000, 1000);
          sized("#stack-row", 1000, 156);
          sized("#my-battlefield", 200, 10000);
          sized("#my-lands", 1000, 1000);
          const hand = sized("#my-hand", 1000, 200);
          const many = [];
          for (let i = 0; i < 10; i++) many.push(creature(`c${i}`, "alice"));
          const state = baseState({
            battlefield: [
              ...many,
              creature("l1", "bob", { type_line: "Land", tapped: true }),
              creature("l2", "bob", { type_line: "Land" }),
            ],
            hands: { alice: [
              { id: "h1", name: "A", zone: "hand", abilities: [] },
              { id: "h2", name: "B", zone: "hand", abilities: [] },
            ] },
            stack: [obj],
          });
          const store = makeStore(baseApp(state));
          m.mountBoard(document, store, {
            onHandCard: NOOP, onPlayer: NOOP, onStackObject: NOOP, onPermission: NOOP,
            onPermanent: NOOP,
          });
          store.set({});
          const rendered = {
            myHand: cardW("#my-hand"),
            oppLands: cardW("#opp-lands-cards"),
            stack: cardW("#stack-row"),
            myBattlefield: cardW("#my-battlefield"),
            scrolls: document.querySelector("#my-battlefield").classList.contains("fit-scroll"),
            handScrolls: hand.classList.contains("fit-scroll"),
            tappedPile: document.querySelector("#opp-lands-cards").children[0].dataset.tapped,
            untappedPile: document.querySelector("#opp-lands-cards").children[1].dataset.tapped,
          };
          const observed = observers.flatMap((o) => o.targets.map((t) => t.attrs.id));
          hand.clientWidth = 100;
          for (const o of observers) o.fn([]);
          return { rendered, observed, resized: cardW("#my-hand") };
        })()
        """,
        obj=obj,
    )
    assert result["rendered"] == {
        "myHand": "143px",
        "oppLands": "125px",
        "stack": "100px",
        "myBattlefield": "44px",
        "scrolls": True,
        "handScrolls": False,
        "tappedPile": "true",
        "untappedPile": "false",
    }
    assert "mtg-board" in result["observed"]
    assert result["resized"] == "50px"


def test_the_board_css_sizes_cards_from_card_w_and_reserves_tapped_boxes() -> None:
    css = (UI / "style.css").read_text()

    def rule(selector: str) -> str:
        found = re.search(r"\n" + re.escape(selector) + r" \{([^}]*)\}", css)
        assert found is not None, f"no {selector} rule"
        return found.group(1)

    card = rule(".card.card-fit")
    assert "width: var(--card-w);" in card and "aspect-ratio: 63 / 88;" in card
    tapped = rule(".card.card-fit.tapped,\n.card.card-fit.preview-tap")
    assert "width: var(--card-h);" in tapped and "aspect-ratio: 88 / 63;" in tapped
    assert "transform: none;" in tapped
    back = rule(".fit-row .card-back")
    assert "width: var(--card-w);" in back and "height: var(--card-h);" in back
    row = rule("#mtg-board > .fit-row")
    assert "overflow-x: auto;" in row and "flex-wrap: nowrap;" in row
    assert "min-height: calc(" in row, "a row never collapses below one floor card"
    assert "--card-h: calc(var(--card-w, 76px) * 88 / 63);" in rule(".fit-row, .perm-stack")
    pile = rule(".perm-stack")
    assert "padding-right: calc(var(--pile) * var(--tuck-x));" in pile
    assert "padding-bottom: calc(var(--pile) * var(--tuck-y));" in pile
    assert "min-width: calc(var(--card-h) + var(--pile) * var(--tuck-x));" in rule(
        '.perm-stack[data-tapped="true"]'
    )
    for fixed in (".card-sm", ".card-md", ".card-lg"):
        assert "var(--card-w" not in rule(fixed)
    board = rule("#mtg-board")
    assert "overflow-x: hidden;" in board


def test_a_short_board_lowers_the_shared_card_floor_so_every_row_fits() -> None:
    """On a landscape phone the board is too short for seven 44px-wide cards
    stacked, so the fit pass derives one floor for every row from the height
    the board has left after its gaps and the stack caption, writes
    it as `#mtg-board`'s `--card-floor` (the rows' `min-height` in
    `style.css`), and fits every row against it rather than the fixed 44px."""
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + FIT_MOUNT
        + """
        (() => {
          const board = document.querySelector("#mtg-board");
          const ids = ["#opp-hand", "#opp-lands", "#opp-battlefield", "#stack-row",
            "#my-battlefield", "#my-lands", "#my-hand"];
          board.children = ids.map((id) => document.querySelector(id));
          board.clientHeight = 7 * 42 + 16;
          for (const id of ids) sized(id, 1000, 42);
          sized("#stack-row", 1000, 58);
          const state = baseState({
            hands: { alice: [{ id: "h1", name: "A", zone: "hand", abilities: [] }] },
          });
          const store = makeStore(baseApp(state));
          m.mountBoard(document, store, {
            onHandCard: NOOP, onPlayer: NOOP, onStackObject: NOOP, onPermission: NOOP,
            onPermanent: NOOP,
          });
          store.set({});
          const floor = () => board.style.getPropertyValue("--card-floor");
          const short = { floor: floor(), hand: cardW("#my-hand") };
          board.clientHeight = 2000;
          for (const o of observers) o.fn([]);
          return { short, tall: floor() };
        })()
        """,
    )
    assert result["short"] == {"floor": "30px", "hand": "30px"}
    assert result["tall"] == "44px"


# ------------------------------------------------------------ player badge --
#
# The info rows above and below the board are gone. Each seat's name, life,
# hand and library counts, known library top, mana pool and zone counts sit in
# a compact badge in its own left column of that seat's land row, so the board
# gets the two info rows' height back and the badge never overlaps a card.

BADGE_MOUNT = """
function mountBadges(state, over, extraHandlers) {
  const calls = { player: [], zones: [] };
  const store = makeStore(baseApp(state, over));
  m.mountBoard(document, store, {
    onHandCard: NOOP, onPermanent: NOOP, onStackObject: NOOP, onPermission: NOOP,
    onPlayer: (u) => calls.player.push(u),
    onOpenZone: (key) => calls.zones.push(key),
    ...(extraHandlers || {}),
  });
  store.set({});
  return { store, calls };
}
const badge = (side) => document.querySelector(`#${side}-badge`);
const text = (el, cls) => {
  const found = el.querySelector(cls);
  return found ? found.textContent : null;
};
function readBadge(side) {
  const el = badge(side);
  return {
    name: text(el, ".name"),
    life: text(el, ".life"),
    hand: text(el, ".count-hand"),
    library: text(el, ".count-library"),
    top: el.querySelector(".known-top") ? {
      text: el.querySelector(".known-top").textContent,
      title: el.querySelector(".known-top").attrs.title,
    } : null,
    pool: text(el, ".pool"),
    poolEmpty: Boolean(
      el.querySelector(".pool") && el.querySelector(".pool").classList.contains("empty")
    ),
    graveyard: text(el, ".zone-graveyard"),
    exile: text(el, ".zone-exile"),
    command: text(el, ".zone-command"),
    classes: [...el.classList.set].sort(),
  };
}
"""


def test_the_info_rows_are_gone_and_each_land_row_leads_with_its_badge() -> None:
    page = (UI / "index.html").read_text()
    assert 'id="opp-info"' not in page and 'id="you-info"' not in page
    for side in ("opp", "my"):
        row = re.search(
            r'<div id="' + side + r'-lands" class="[^"]*\bfit-row\b[^"]*">(.*?)\n    </div>',
            page,
            re.S,
        )
        assert row, f"no #{side}-lands row"
        ids = re.findall(r'id="([a-z-]+)"', row.group(1))
        assert ids == [f"{side}-badge", f"{side}-lands-cards"], ids
    for name in ("render/board.js", "style.css"):
        text = (UI / name).read_text()
        assert "opp-info" not in text and "you-info" not in text, name


def test_a_badge_shows_life_hand_library_pool_and_zone_counts() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + BADGE_MOUNT
        + """
        (() => {
          const state = baseState({
            graveyards: { bob: [creature("bg1", "bob", { zone: "graveyard" }),
                                creature("bg2", "bob", { zone: "graveyard" })], alice: [] },
            exile: [creature("be1", "bob", { zone: "exile" }),
                    creature("ae1", "alice", { zone: "exile" }),
                    creature("ae2", "alice", { zone: "exile" })],
            command: [creature("ac1", "alice", { zone: "command" })],
          });
          state.players[1].life = 7;
          state.players[1].library_count = 33;
          state.players[0].mana_pool = { floating: { R: 2 }, snow: {}, restricted: [] };
          mountBadges(state);
          return { opp: readBadge("opp"), me: readBadge("my") };
        })()
        """,
    )
    opp, me = result["opp"], result["me"]
    assert opp["name"] == "bob" and opp["life"] == "7"
    assert opp["hand"] == "hand 3" and opp["library"] == "library 33"
    assert opp["poolEmpty"] is True and opp["pool"] == "empty pool"
    assert (opp["graveyard"], opp["exile"], opp["command"]) == (
        "graveyard 2",
        "exile 1",
        "command 0",
    )
    assert "player-badge" in opp["classes"] and "opp" in opp["classes"]
    assert me["name"] == "alice" and me["life"] == "20"
    assert me["hand"] == "hand 0" and me["library"] == "library 40"
    assert me["poolEmpty"] is False and "2" in me["pool"]
    assert (me["graveyard"], me["exile"], me["command"]) == ("graveyard 0", "exile 2", "command 1")
    assert "you" in me["classes"]
    assert opp["top"] is None and me["top"] is None


def test_a_badge_marks_the_active_player_priority_and_low_life() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + BADGE_MOUNT
        + """
        (() => {
          const state = baseState({ active_player: "bob", priority_holder: "alice" });
          state.players[1].life = 3;
          mountBadges(state);
          return {
            opp: readBadge("opp").classes,
            me: readBadge("my").classes,
            oppLow: badge("opp").querySelector(".life").classList.contains("low"),
            meLow: badge("my").querySelector(".life").classList.contains("low"),
          };
        })()
        """,
    )
    assert "active" in result["opp"] and "priority" not in result["opp"]
    assert "priority" in result["me"] and "active" not in result["me"]
    assert result["oppLow"] is True and result["meLow"] is False


def test_clicking_my_badge_targets_me_and_it_lights_up_as_chosen() -> None:
    chosen = json.loads(json.dumps(ONE_TARGET_ACTION))
    chosen["affordance"]["candidates"].append(
        {"id": "alice", "kind": "player", "name": "alice", "requirements": [0]}
    )
    chosen["slots"][0]["targets"] = ["alice"]
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + BADGE_MOUNT
        + """
        (() => {
          const { calls } = mountBadges(baseState(), { action });
          badge("my").onclick({ target: null });
          badge("opp").onclick({ target: null });
          return {
            calls: calls.player, me: readBadge("my").classes, opp: readBadge("opp").classes,
          };
        })()
        """,
        action=chosen,
    )
    assert result["calls"] == ["alice", "bob"]
    assert "chosen" in result["me"]
    # The one slot is full, so nothing else is offered: bob is neither lit nor held.
    assert "targetable" not in result["opp"] and "chosen" not in result["opp"]


def test_a_zone_count_opens_that_zone_and_does_not_target_the_player() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + BADGE_MOUNT
        + """
        (() => {
          const { calls } = mountBadges(baseState());
          const stopped = [];
          const click = (side, zone) => {
            const button = badge(side).querySelector(`.zone-${zone}`);
            const event = { target: button, stopPropagation: () => stopped.push(zone) };
            button.onclick(event);
          };
          click("opp", "graveyard");
          click("my", "exile");
          click("my", "command");
          return { calls, stopped };
        })()
        """,
    )
    assert result["calls"] == {"player": [], "zones": ["opp-graveyard", "you-exile", "you-command"]}
    assert result["stopped"] == ["graveyard", "exile", "command"]


def test_the_badge_is_patched_in_place_across_polls() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + BADGE_MOUNT
        + """
        (() => {
          const { store } = mountBadges(baseState());
          const el = badge("opp");
          const life = el.querySelector(".life");
          const pool = el.querySelector(".pool");
          const zone = el.querySelector(".zone-graveyard");
          const next = baseState({
            graveyards: { bob: [creature("g9", "bob", { zone: "graveyard" })] },
          });
          next.players[1].life = 18;
          store.set({ state: next });
          const midZone = zone.textContent;
          const samePool = el.querySelector(".pool") === pool;
          const floating = baseState();
          floating.players[1].life = 18;
          floating.players[1].mana_pool = { floating: { G: 1 }, snow: {}, restricted: [] };
          store.set({ state: floating });
          return {
            sameLife: el.querySelector(".life") === life,
            lifeText: life.textContent,
            sameZone: el.querySelector(".zone-graveyard") === zone,
            midZone,
            zoneText: zone.textContent,
            samePool,
            newPool: el.querySelector(".pool") !== pool,
            children: el.children.length,
          };
        })()
        """,
    )
    assert result["sameLife"] is True and result["lifeText"] == "18"
    assert result["sameZone"] is True
    assert result["samePool"] is True and result["newPool"] is True
    assert result["midZone"] == "graveyard 1" and result["zoneText"] == "graveyard 0"
