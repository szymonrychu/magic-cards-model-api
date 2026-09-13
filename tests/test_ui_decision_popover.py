"""`ui/render/decision_popover.js`: small decisions open next to the card asking.

Every SMALL decision kind (a yes/no, a payment, an amount, a quality or mode
choice, top-or-bottom, an opening-hand action) pending for this seat opens a
small popover anchored to `pending_decision.source_card_id`'s card when that
card is rendered anywhere on the page (board, hand, stack, zone ribbon). When
the id is null or the card is not rendered, the decision stays in
`#prompt-box` exactly as before. Complex kinds keep the decision window;
`choose_targets` and the combat declarations keep their board flows.

Run under Node against the fake DOM `tests/test_ui_decision_window.py`
already ships (listeners that bubble, `closest`, per-element rectangles),
extended with `querySelectorAll`, which the anchor lookup needs.
"""

from __future__ import annotations

import re

from test_ui_decision_window import FAKE_DOM as WINDOW_FAKE_DOM
from ui_node import UI, requires_node, run_module

pytestmark = requires_node

FAKE_DOM = (
    WINDOW_FAKE_DOM
    + r"""
El.prototype.querySelectorAll = function (sel) {
  return walkAll(this, (n) => n !== this && typeof n.matches === "function" && n.matches(sel));
};
El.prototype.remove = function () {
  if (this.parentNode) this.parentNode.removeChild(this);
};

const POP = boxed("div", "decision-popover");
POP.className = "decision-popover";
POP.hidden = true;
POP.size = { width: 240, height: 120 };
const PROMPT = document.querySelector("#prompt-box");
const BOARD = document.querySelector("#mtg-board");
const MODAL = document.querySelector("#modal");
const HAND_ROW = boxed("div", "my-hand", BOARD);
const STACK_ROW = boxed("div", "stack-row", BOARD);

function rectOf(r) {
  return { ...r, right: r.left + r.width, bottom: r.top + r.height };
}
function cardEl(id, r, parent, cls) {
  const el = document.createElement("div");
  el.className = cls || "card card-md";
  el.dataset.key = id;
  el.dataset.cardId = id;
  el.rect = rectOf(r);
  (parent || BOARD).appendChild(el);
  return el;
}
function stackEl(id, r) {
  const el = document.createElement("div");
  el.className = "stack-object";
  el.dataset.key = id;
  el.rect = rectOf(r);
  STACK_ROW.appendChild(el);
  return el;
}
function popRect() {
  return { left: parseFloat(POP.style.left), top: parseFloat(POP.style.top) };
}
function buttonsIn(root) {
  return walkAll(root, (n) => n.tagName === "button").map((n) => n.textContent.trim());
}
function resize() {
  for (const fn of [...(window.listeners.resize || [])]) fn({});
}

async function setup(app, opts) {
  const R = await import(pathToFileURL(UI_DIR + "/render/ribbon.js").href);
  const NOOP = () => {};
  const log = [];
  const store = (opts && opts.store) || makeStore(app);
  const base = {
    ...handlersFor(store, log),
    onDecide: (answer) => {
      log.push(["decide", answer]);
      if (opts && opts.onDecide) opts.onDecide(store, answer);
    },
  };
  const handlers = new Proxy(base, { get: (t, k) => (k in t ? t[k] : NOOP) });
  R.mountRibbon(document, store, handlers);
  if (!(opts && opts.ribbonOnly)) m.mountDecisionPopover(document, store, handlers);
  return { R, store, log };
}

// Answer `p` through whatever `root` holds right now, the way a player would:
// set the control, then press the buttons. Every kind's full set of answers.
function answerThrough(root, p) {
  const press = (label) => click(buttonNamed(root, label));
  const input = () => walkAll(root, (n) => n.tagName === "input")[0];
  switch (p.kind) {
    case "may": press("Yes"); press("No"); break;
    case "pay_life": case "pay_energy": case "pay_mana": press("Pay"); press("Don't pay"); break;
    case "opening_hand_action": press("Do it"); press("Skip"); break;
    case "put_on_top_or_bottom": press("Top"); press("Bottom"); break;
    case "pay_energy_amount": case "pay_life_amount":
      fire(input(), "change", { target: { value: "2" } });
      press("Pay");
      break;
    case "choose_a_mode": case "choose_a_quality":
      if ((p.options || []).length) click(buttonNamed(root, p.options[1]));
      else fire(input(), "input", { target: { value: "Goblin" } });
      press("Choose");
      break;
  }
}
"""
)

BASE = {
    "generation": 1,
    "player": "alice",
    "kind": "may",
    "prompt": "",
    "source": "Rhystic Study",
    "source_card_id": "src",
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

SMALL = [
    {**BASE, "kind": "may", "prompt": "Draw a card?"},
    {**BASE, "kind": "pay_life", "prompt": "Pay 2 life?", "source": "Dismember"},
    {**BASE, "kind": "pay_energy", "prompt": "Pay 1 energy?", "source": "Aether Hub"},
    {**BASE, "kind": "pay_mana", "prompt": "Pay one mana?", "source": "Mana Leak"},
    {**BASE, "kind": "pay_energy_amount", "prompt": "Pay any amount of energy.", "amount": 3},
    {**BASE, "kind": "pay_life_amount", "prompt": "Pay any amount of life.", "amount": 4},
    {
        **BASE,
        "kind": "choose_a_quality",
        "prompt": "Choose a color.",
        "source": "Iona",
        "options": ["white", "blue", "black"],
    },
    {**BASE, "kind": "choose_a_quality", "prompt": "Choose a creature type.", "source": "Cavern"},
    {
        **BASE,
        "kind": "choose_a_mode",
        "prompt": "Choose one.",
        "source": "Charm",
        "options": ["Counter target spell", "Draw a card"],
    },
    {**BASE, "kind": "put_on_top_or_bottom", "prompt": "Top or bottom?", "source": "Brainstone"},
    {**BASE, "kind": "opening_hand_action", "prompt": "Reveal it?", "source": "Chancellor"},
]

EXPECTED_BODIES = {
    "may": [{"accept": True}, {"accept": False}],
    "pay_life": [{"accept": True}, {"accept": False}],
    "pay_energy": [{"accept": True}, {"accept": False}],
    "pay_mana": [{"accept": True}, {"accept": False}],
    "opening_hand_action": [{"accept": True}, {"accept": False}],
    "put_on_top_or_bottom": [{"top": True}, {"top": False}],
    "pay_energy_amount": [{"energy_amount": 2}],
    "pay_life_amount": [{"life_amount": 2}],
    "choose_a_mode": [{"choice": "Draw a card"}],
}

COMPLEX = [
    {**BASE, "kind": "scry", "amount": 1, "cards": ["a"], "card_labels": ["Opt"]},
    {**BASE, "kind": "surveil", "amount": 1, "cards": ["a"], "card_labels": ["Opt"]},
    {**BASE, "kind": "choose_cards", "amount": 1, "cards": ["a"], "card_labels": ["Opt"]},
    {**BASE, "kind": "order_triggers", "cards": ["t1", "t2"], "card_labels": ["x", "y"]},
    {**BASE, "kind": "mulligan", "prompt": "Mulligan?"},
]
TARGETS = {
    **BASE,
    "kind": "choose_targets",
    "prompt": "choose a target",
    "target_maximum": 1,
    "target_minimum": 1,
    "targets": [{"id": "bob", "kind": "player"}],
}


def _run(expression: str, **bindings: object) -> object:
    return run_module(
        "render/decision_popover.js", FAKE_DOM + expression, UI_DIR=str(UI), **bindings
    )


# ---------------------------------------------------------------- anchoring ---


def test_every_small_kind_opens_a_popover_anchored_to_its_rendered_source_card() -> None:
    result = _run(
        """
        (async () => {
          const { store } = await setup(baseApp(baseState({})));
          const src = cardEl("src", { left: 100, top: 300, width: 108, height: 150 });
          const seen = [];
          for (const p of PENDINGS) {
            store.set({ state: baseState({ pending_decision: p }), decisionDraft: {} });
            seen.push({
              kind: p.kind,
              prompt: p.prompt,
              source: p.source,
              open: !POP.hidden,
              text: POP.textContent,
              highlighted: src.classList.contains("decision-source"),
              at: popRect(),
              placement: POP.dataset.placement,
              promptButtons: buttonsIn(PROMPT),
              promptText: PROMPT.textContent,
            });
          }
          return seen;
        })()
        """,
        PENDINGS=SMALL,
    )
    assert len(result) == len(SMALL)
    for entry in result:
        kind = entry["kind"]
        assert entry["open"] is True, f"{kind} did not open the popover"
        assert entry["prompt"] in entry["text"], f"{kind}: the popover lacks the prompt"
        assert entry["source"] in entry["text"], f"{kind}: the popover lacks its title"
        assert entry["highlighted"] is True, f"{kind}: the source card is not highlighted"
        assert entry["placement"] == "right"
        assert entry["at"]["left"] >= 208, f"{kind}: the popover covers its card"
        assert entry["promptButtons"] == [], f"{kind} also rendered its controls in the ribbon"
        assert "at the card" in entry["promptText"]


def test_a_source_on_the_stack_or_in_hand_anchors_too() -> None:
    result = _run(
        """
        (async () => {
          const { store } = await setup(baseApp(baseState({})));
          const spell = stackEl("spell", { left: 500, top: 20, width: 160, height: 30 });
          const handRect = { left: 300, top: 600, width: 150, height: 190 };
          const handCard = cardEl("hc", handRect, HAND_ROW, "card card-lg");
          store.set({ state: baseState({ pending_decision: { ...P, source_card_id: "spell" } }) });
          const onStack = { open: !POP.hidden, lit: spell.classList.contains("decision-source") };
          const fromHand = { ...P, source_card_id: "hc", generation: 2 };
          store.set({ state: baseState({ pending_decision: fromHand }) });
          const r = POP.getBoundingClientRect();
          const inHand = {
            open: !POP.hidden,
            lit: handCard.classList.contains("decision-source"),
            spellLit: spell.classList.contains("decision-source"),
            inside: r.top >= 0 && r.bottom <= 800 && r.left >= 0 && r.right <= 1200,
          };
          return { onStack, inHand };
        })()
        """,
        P=SMALL[0],
    )
    assert result["onStack"] == {"open": True, "lit": True}
    assert result["inHand"] == {"open": True, "lit": True, "spellLit": False, "inside": True}


# ----------------------------------------------------------------- fallback ---


def _ribbon_only(pending: dict) -> object:
    return _run(
        """
        (async () => {
          const app = baseApp(baseState({ pending_decision: P }));
          const { store } = await setup(app, { ribbonOnly: true });
          store.set({});
          return { text: PROMPT.textContent, buttons: buttonsIn(PROMPT) };
        })()
        """,
        P=pending,
    )


def test_a_null_source_card_id_renders_in_the_ribbon_exactly_as_before() -> None:
    for pending in SMALL:
        unanchored = {**pending, "source_card_id": None}
        before = _ribbon_only({k: v for k, v in pending.items() if k != "source_card_id"})
        result = _run(
            """
            (async () => {
              const store = makeStore(baseApp(baseState({ pending_decision: P })));
              await setup(null, { store });
              // the card is on the board, but nothing names it
              cardEl("src", { left: 100, top: 300, width: 108, height: 150 });
              store.set({});
              return { open: !POP.hidden, text: PROMPT.textContent, buttons: buttonsIn(PROMPT) };
            })()
            """,
            P=unanchored,
        )
        assert result["open"] is False, f"{pending['kind']} anchored with no source_card_id"
        assert result["text"] == before["text"]
        assert result["buttons"] == before["buttons"]
        assert result["buttons"], f"{pending['kind']} rendered no controls in the ribbon"


def test_a_source_card_that_is_not_rendered_falls_back_to_the_ribbon() -> None:
    result = _run(
        """
        (async () => {
          const store = makeStore(baseApp(baseState({})));
          await setup(null, { store });
          const out = {};
          const check = (name) => {
            out[name] = { open: !POP.hidden, buttons: buttonsIn(PROMPT) };
          };
          // nothing with that id anywhere
          store.set({ state: baseState({ pending_decision: { ...P, source_card_id: "gone" } }) });
          check("absent");
          // a node with that id that is not laid out (a hidden zone row)
          const hidden = cardEl("hid", { left: 0, top: 0, width: 0, height: 0 });
          store.set({ state: baseState({ pending_decision: { ...P, source_card_id: "hid" } }) });
          check("zeroRect");
          // only inside the zones modal, which paints over any popover
          cardEl("modalonly", { left: 50, top: 50, width: 76, height: 106 }, MODAL, "card card-sm");
          const inModal = { ...P, source_card_id: "modalonly" };
          store.set({ state: baseState({ pending_decision: inModal }) });
          check("inModal");
          // anchored, then the card leaves the board with the question still up
          const src = cardEl("src", { left: 100, top: 300, width: 108, height: 150 });
          store.set({ state: baseState({ pending_decision: P }) });
          check("anchored");
          src.remove();
          store.set({ state: baseState({ pending_decision: clone(P) }) });
          check("afterItMoved");
          out.highlightGone = !src.classList.contains("decision-source");
          return out;
        })()
        """,
        P=SMALL[0],
    )
    for name in ("absent", "zeroRect", "inModal", "afterItMoved"):
        assert result[name] == {"open": False, "buttons": ["Yes", "No"]}, name
    assert result["anchored"] == {"open": True, "buttons": []}
    assert result["highlightGone"] is True


# -------------------------------------------------------------- same answer ---


def test_the_popover_posts_the_same_decide_bodies_as_the_ribbon() -> None:
    for pending in SMALL:
        both = _run(
            """
            (async () => {
              const out = {};
              {
                const store = makeStore(baseApp(baseState({ pending_decision: P })));
                const again = await setup(null, { store });
                cardEl("src", { left: 100, top: 300, width: 108, height: 150 });
                store.set({});
                out.popoverOpen = !POP.hidden;
                answerThrough(POP, P);
                out.popover = decided(again.log);
              }
              {
                const unanchored = { ...P, source_card_id: null };
                const store = makeStore(baseApp(baseState({ pending_decision: unanchored })));
                const again = await setup(null, { store, ribbonOnly: true });
                store.set({});
                answerThrough(PROMPT, unanchored);
                out.ribbon = decided(again.log);
              }
              return out;
            })()
            """,
            P=pending,
        )
        kind = pending["kind"]
        assert both["popoverOpen"] is True, kind
        assert both["popover"], f"{kind}: nothing was posted from the popover"
        assert both["popover"] == both["ribbon"], kind
        if pending["options"]:
            expected = [{"choice": pending["options"][1]}]
        elif kind == "choose_a_quality":
            expected = [{"choice": "Goblin"}]
        else:
            expected = EXPECTED_BODIES[kind]
        assert both["popover"] == expected, kind


# ------------------------------------------------------------------ polling ---


def test_the_popover_is_not_rebuilt_or_moved_by_a_poll_with_the_same_decision() -> None:
    result = _run(
        """
        (async () => {
          const { store } = await setup(baseApp(baseState({})));
          const src = cardEl("src", { left: 100, top: 300, width: 108, height: 150 });
          store.set({ state: baseState({ pending_decision: P }) });
          const content = POP.children[0];
          const at = popRect();
          let writes = 0;
          const style = POP.style;
          POP.style = new Proxy(style, {
            set: (t, k, v) => { writes += 1; t[k] = v; return true; },
          });
          const busier = { pending_decision: clone(P), battlefield: [{ id: "q" }] };
          store.set({ state: baseState(busier) });
          store.set({ state: baseState({ pending_decision: clone(P), battlefield: [] }) });
          const same = POP.children[0] === content;
          const polled = { same, at: popRect(), writes, open: !POP.hidden };
          POP.style = style;
          // the card moves (a row reflowed): the popover follows on the next update
          src.rect = rectOf({ left: 400, top: 200, width: 108, height: 150 });
          store.set({ state: baseState({ pending_decision: clone(P) }) });
          const moved = { same: POP.children[0] === content, at: popRect() };
          // the window resizes: it follows without waiting for a poll
          src.rect = rectOf({ left: 600, top: 100, width: 108, height: 150 });
          resize();
          const resized = { same: POP.children[0] === content, at: popRect() };
          // a different question on the same card replaces the content
          store.set({ state: baseState({ pending_decision: { ...P, generation: 2 } }) });
          const replaced = { same: POP.children[0] === content, open: !POP.hidden };
          return { at, polled, moved, resized, replaced };
        })()
        """,
        P=SMALL[0],
    )
    assert result["polled"]["same"] is True
    assert result["polled"]["open"] is True
    assert result["polled"]["at"] == result["at"]
    assert result["polled"]["writes"] == 0, "an unchanged poll must not touch the popover's style"
    assert result["moved"]["same"] is True
    assert result["moved"]["at"]["left"] >= 508
    assert result["moved"]["at"] != result["at"]
    assert result["resized"]["same"] is True
    assert result["resized"]["at"]["left"] >= 708
    assert result["replaced"] == {"same": False, "open": True}


def test_the_popover_closes_when_the_decision_is_answered_or_replaced() -> None:
    result = _run(
        """
        (async () => {
          const onDecide = (store) => store.set({ state: baseState({}), decisionDraft: {} });
          const store = makeStore(baseApp(baseState({})));
          const { log } = await setup(null, { store, onDecide });
          const src = cardEl("src", { left: 100, top: 300, width: 108, height: 150 });
          const other = cardEl("other", { left: 400, top: 300, width: 108, height: 150 });
          store.set({ state: baseState({ pending_decision: P }) });
          const opened = !POP.hidden;
          click(buttonNamed(POP, "Yes"));
          const answered = {
            posted: decided(log),
            hidden: POP.hidden,
            empty: POP.children.length === 0,
            lit: src.classList.contains("decision-source"),
            promptButtons: buttonsIn(PROMPT),
          };
          store.set({ state: baseState({ pending_decision: P }) });
          const onOther = { ...P, generation: 5, source_card_id: "other" };
          store.set({ state: baseState({ pending_decision: onOther }) });
          const replaced = {
            open: !POP.hidden,
            srcLit: src.classList.contains("decision-source"),
            otherLit: other.classList.contains("decision-source"),
            left: popRect().left,
          };
          return { opened, answered, replaced };
        })()
        """,
        P=SMALL[0],
    )
    assert result["opened"] is True
    assert result["answered"] == {
        "posted": [{"accept": True}],
        "hidden": True,
        "empty": True,
        "lit": False,
        "promptButtons": [],
    }
    assert result["replaced"]["open"] is True
    assert result["replaced"]["srcLit"] is False
    assert result["replaced"]["otherLit"] is True
    assert result["replaced"]["left"] >= 508


def test_a_confirm_button_survives_a_poll_that_lands_mid_press() -> None:
    result = _run(
        """
        (async () => {
          const S = await import(pathToFileURL(UI_DIR + "/store.js").href);
          const store = S.createStore(baseApp(baseState({})));
          S.installPressGuard(store, document, window);
          const { log } = await setup(null, { store });
          cardEl("src", { left: 100, top: 300, width: 108, height: 150 });
          store.set({ state: baseState({ pending_decision: P }) });
          const yes = buttonNamed(POP, "Yes");
          fire(yes, "pointerdown", { pointerType: "mouse", button: 0, pointerId: 1 });
          const busier = { pending_decision: clone(P), battlefield: [{ id: "q" }] };
          store.set({ state: baseState(busier) });
          fire(yes, "pointerup", { pointerType: "mouse", pointerId: 1 });
          const landed = click(yes);
          await new Promise((r) => setTimeout(r, 5));
          return { same: buttonNamed(POP, "Yes") === yes, landed, posted: decided(log) };
        })()
        """,
        P=SMALL[0],
    )
    assert result == {"same": True, "landed": True, "posted": [{"accept": True}]}


# ------------------------------------------------------------ what it skips ---


def test_complex_kinds_targets_combat_and_other_seats_never_anchor() -> None:
    result = _run(
        """
        (async () => {
          const store = makeStore(baseApp(baseState({})));
          await setup(null, { store });
          const src = cardEl("src", { left: 100, top: 300, width: 108, height: 150 });
          const seen = [];
          const look = (label, overrides) => {
            store.set({ state: baseState(overrides), decisionDraft: {} });
            const lit = src.classList.contains("decision-source");
            seen.push({ label, open: !POP.hidden, lit, prompt: PROMPT.textContent });
          };
          for (const p of COMPLEX) look(p.kind, { pending_decision: p, hands: { alice: [] } });
          look("choose_targets", { pending_decision: T });
          look("declare_attackers", { pending_decision: { ...S, kind: "declare_attackers" } });
          look("opponent", { pending_decision: { ...S, player: "bob" } });
          const pregame = { step: "opening_hand", awaiting_mulligan: [] };
          look("pregame", { pending_decision: S, pregame });
          look("over", { pending_decision: S, winner: "bob" });
          return seen;
        })()
        """,
        COMPLEX=COMPLEX,
        T=TARGETS,
        S=SMALL[0],
    )
    for entry in result:
        assert entry["open"] is False, f"{entry['label']} opened the card popover"
        assert entry["lit"] is False, f"{entry['label']} highlighted the source card"
    by_label = {entry["label"]: entry for entry in result}
    assert "choose a target" in by_label["choose_targets"]["prompt"]
    assert "Declare attackers" in by_label["declare_attackers"]["prompt"]


def test_the_classification_is_usesDecisionPopover_in_decisions_js() -> None:
    kinds = [p["kind"] for p in SMALL] + [p["kind"] for p in COMPLEX]
    kinds += ["choose_targets", "declare_attackers", "declare_blockers"]
    routed = run_module(
        "decisions.js",
        "Object.fromEntries(ks.map((k) => [k, m.usesDecisionPopover({ kind: k })]))",
        ks=kinds,
    )
    assert {k for k, v in routed.items() if v} == {p["kind"] for p in SMALL}


# ----------------------------------------------------------------- keyboard ---


def test_focus_moves_in_and_escape_does_not_dismiss_a_required_decision() -> None:
    result = _run(
        """
        (async () => {
          const store = makeStore(baseApp(baseState({})));
          await setup(null, { store });
          const src = cardEl("src", { left: 100, top: 300, width: 108, height: 150 });
          store.set({ state: baseState({ pending_decision: P }) });
          const focusedIn = POP.contains(document.activeElement);
          let reachedDocument = false;
          document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") reachedDocument = true;
          });
          fire(buttonNamed(POP, "Yes"), "keydown", { key: "Escape" });
          const afterInside = !POP.hidden;
          fire(document, "keydown", { key: "Escape" });
          const afterDocument = !POP.hidden;
          // outside clicks do not dismiss it either
          fire(src, "click", {});
          const afterClick = !POP.hidden;
          // a free-text answer keeps focus on its input while typing
          store.set({ state: baseState({ pending_decision: Q }), decisionDraft: {} });
          const input = walkAll(POP, (n) => n.tagName === "input")[0];
          const inputFocused = document.activeElement === input;
          fire(input, "input", { target: { value: "Gob" } });
          const after = walkAll(POP, (n) => n.tagName === "input")[0];
          return {
            focusedIn, afterInside, afterDocument, afterClick, inputFocused,
            typingKeepsFocus: document.activeElement === after,
            chooseEnabled: !buttonNamed(POP, "Choose").disabled,
          };
        })()
        """,
        P=SMALL[0],
        Q=SMALL[7],
    )
    assert result == {
        "focusedIn": True,
        "afterInside": True,
        "afterDocument": True,
        "afterClick": True,
        "inputFocused": True,
        "typingKeepsFocus": True,
        "chooseEnabled": True,
    }


def test_the_card_menu_popover_still_works_beside_an_open_decision_popover() -> None:
    result = _run(
        """
        (async () => {
          const store = makeStore(baseApp(baseState({})));
          const { R } = await setup(null, { store });
          cardEl("src", { left: 100, top: 300, width: 108, height: 150 });
          const other = cardEl("other", { left: 400, top: 300, width: 108, height: 150 });
          store.set({ state: baseState({ pending_decision: P }) });
          const menu = document.querySelector("#popover");
          let picked = 0;
          const items = [{ label: "Tap for mana", onSelect: () => { picked += 1; } }];
          R.showPopover(other, items, { title: "Forest" });
          const both = { menu: !menu.hidden, decision: !POP.hidden };
          click(buttonNamed(menu, "Tap for mana"));
          R.hidePopover();
          return { both, picked, menuHidden: menu.hidden, decision: !POP.hidden };
        })()
        """,
        P=SMALL[0],
    )
    assert result["both"] == {"menu": True, "decision": True}
    assert result["picked"] == 1
    assert result["menuHidden"] is True
    assert result["decision"] is True


# ---------------------------------------------------------------- placement ---


def test_placement_prefers_the_side_flips_and_never_covers_the_card() -> None:
    cases = run_module(
        "render/decision_popover.js",
        """
        (() => {
          const rect = (left, top, width, height) =>
            ({ left, top, width, height, right: left + width, bottom: top + height });
          const size = { width: 240, height: 160 };
          const vp = { width: 1200, height: 800 };
          const out = {};
          const place = (name, anchor, s, v) => {
            out[name] = { anchor, box: m.popoverPlacement(anchor, s || size, v || vp) };
          };
          place("right", rect(100, 300, 108, 150));
          place("flipLeft", rect(1050, 300, 108, 150));
          place("handRow", rect(300, 700, 150, 190));
          place("narrowBelow", rect(40, 100, 108, 150), size, { width: 300, height: 800 });
          place("narrowAbove", rect(40, 600, 108, 150), size, { width: 300, height: 800 });
          place("tooTall", rect(100, 300, 108, 150), { width: 240, height: 2000 });
          return out;
        })()
        """,
    )
    assert cases["right"]["box"]["placement"] == "right"
    assert cases["flipLeft"]["box"]["placement"] == "left"
    assert cases["handRow"]["box"]["placement"] == "right"
    assert cases["narrowBelow"]["box"]["placement"] == "below"
    assert cases["narrowAbove"]["box"]["placement"] == "above"
    viewports = {"narrowBelow": (300, 800), "narrowAbove": (300, 800)}
    for name, case in cases.items():
        anchor, box = case["anchor"], case["box"]
        width = 240
        height = box["maxHeight"]
        vw, vh = viewports.get(name, (1200, 800))
        assert box["left"] >= 8 and box["left"] + width <= vw - 8 or vw < width + 16, name
        assert box["top"] >= 8 and box["top"] + height <= vh - 8, name
        overlaps = (
            box["left"] < anchor["right"]
            and box["left"] + width > anchor["left"]
            and box["top"] < anchor["bottom"]
            and box["top"] + height > anchor["top"]
        )
        assert not overlaps, f"{name}: the popover covers the card it points at"


# ------------------------------------------------------ markup and wiring ---


def _css_z(css: str, selector: str) -> int:
    match = re.search(rf"^{re.escape(selector)} \{{.*?z-index:\s*(\d+)", css, re.M | re.S)
    assert match, f"{selector} has no z-index"
    return int(match.group(1))


def test_index_html_ships_a_persistent_popover_outside_the_rebuilt_panels() -> None:
    html = (UI / "index.html").read_text()
    match = re.search(r'<div id="decision-popover"([^>]*)>', html)
    assert match, "#decision-popover is not in ui/index.html"
    attrs = match.group(1)
    assert "hidden" in attrs
    assert 'role="dialog"' in attrs
    assert 'aria-modal="false"' in attrs
    side = re.search(r'<aside id="side-panel">(.*?)</aside>', html, re.S)
    assert side and "decision-popover" not in side.group(1)
    assert html.index('id="decision-popover"') < html.index('id="modal"')


def test_the_popover_layer_does_not_dim_the_board_and_keeps_the_z_ladder() -> None:
    css = (UI / "style.css").read_text()
    rule = re.search(r"^\.decision-popover \{(.*?)^\}", css, re.M | re.S)
    assert rule, ".decision-popover rule not found"
    body = rule.group(1)
    assert "position: fixed" in body
    assert "inset" not in body
    assert _css_z(css, ".card-preview") == 40
    assert _css_z(css, ".popover") == 50
    assert _css_z(css, ".decision-popover") == 50
    assert _css_z(css, ".modal") == 60
    assert _css_z(css, ".toast") == 70
    assert re.search(r"^\.decision-source \{", css, re.M), "the source card needs a highlight"
    busy = re.findall(r"^(body\.busy[^{]*button[^{]*)\{", css, re.M | re.S)
    assert any("#decision-popover button" in selector for selector in busy)


def test_app_js_mounts_the_popover_with_the_existing_decide_path() -> None:
    app = (UI / "app.js").read_text()
    call = re.search(r"mountDecisionPopover\(document, store, \{(.*?)\}\);", app, re.S)
    assert call, "app.js does not mount the decision popover"
    assert "onDecide: decide" in call.group(1)
    assert "onDecisionDraft" in call.group(1)
    assert "installPressGuard(store)" in app


def test_no_decorative_punctuation_in_the_popover_text() -> None:
    sources = [
        (UI / "render" / "decision_popover.js").read_text(),
        (UI / "render" / "ribbon.js").read_text(),
    ]
    for text in sources:
        for bad in ("—", "–", "‘", "’", "“", "”", "→"):
            assert bad not in text, f"decorative character {bad!r} in the popover UI"
