// The decision window: one floating, draggable `#decision-window` where every
// COMPLEX decision is answered with the real cards in front of the player -
// scry, surveil and a look-at-the-top split (two lanes), every `choose_cards`
// (a grid to pick from), `order_triggers` (a lane to order), and this seat's
// own mulligan (the opening hand). Small decisions stay in `#prompt-box`
// (`render/ribbon.js`); `ui/decisions.js`'s `usesDecisionWindow` is the one
// place that says which is which.
//
// Built once in `ui/index.html` and only ever shown, hidden and patched,
// never rebuilt on a poll - the lesson `#log-modal` already taught. The
// renderers in `#prompt-box` wipe and rebuild on every store update, and the
// press guard (`ui/store.js`) stops holding those back after
// `PRESS_CAP_MS`, so a card mid-drag or a half-made arrangement there would be
// torn out from under the pointer every poll. Here a poll that leaves the
// decision unchanged (same player, kind, generation and cards) patches the
// existing nodes in place, and one that lands mid-drag waits for the drop. A
// different decision replaces the content.
//
// It never dims or blocks the board: no scrim, just the window, below the
// hover preview and the card menus in the z-order. It can be minimized to its
// title bar (the button, or Escape) but not closed while the decision is
// pending, and it goes away by itself once the decision is answered.
//
// Every answer is the one the view models already define: the window only
// writes the draft keys `decisions.js` reads (`moved`/`order`, `card_ids`,
// `order`, `bottom`) through `handlers.onDecisionDraft`, and posts through
// `handlers.onDecide` / `handlers.onPregame` - the same `decide` and
// `pregame/mulligan` paths, with the same bodies, that the old controls used.

import { clear, h, patchChildren, setText } from "../dom.js";
import { cardNode, updateCardNode } from "../cards.js";
import {
  answerFor,
  decisionCards,
  placeInList,
  placeInSplit,
  shownTriggerOrder,
  toggleChoice,
  usesDecisionWindow,
  viewModel,
} from "../decisions.js";
import { withSymbols } from "../symbols.js";

/** Where this viewer last left the window, as `{left, top}` JSON. */
export const WINDOW_POSITION_KEY = "mcm.decisionWindow.v1";

/** How far a press must travel before it is a drag and not a click. */
const DRAG_THRESHOLD_PX = 6;
/** Used when the window cannot be measured (not laid out yet). */
const FALLBACK_SIZE = { width: 480, height: 360 };
/** The control ribbon's width (`style.css`'s `#game` grid): the default spot
 * is just left of it, over the board's right edge, near the prompt box the
 * question used to live in. */
const RIBBON_WIDTH_PX = 340;
const EDGE_GAP_PX = 24;
const DEFAULT_TOP_PX = 72;

// ------------------------------------------------------------- position ---

function resolveStorage(source) {
  try {
    const storage = typeof source === "function" ? source() : source;
    return storage || null;
  } catch {
    return null;
  }
}

/** The remembered position, or `null`. Storage that is missing, blocked or
 * throws on access reads as nothing remembered. `storage` may be the storage
 * object or a function returning it (so reaching `localStorage` itself, which
 * can throw, happens inside the guard). */
export function loadWindowPosition(storage) {
  try {
    const store = resolveStorage(storage);
    if (!store) return null;
    const raw = store.getItem(WINDOW_POSITION_KEY);
    if (!raw) return null;
    const pos = JSON.parse(raw);
    if (pos && Number.isFinite(pos.left) && Number.isFinite(pos.top)) return { left: pos.left, top: pos.top };
  } catch {
    // unreadable, blocked or corrupt storage is no worse than none
  }
  return null;
}

/** Remember `pos`; `false` when storage refused. Never throws. */
export function saveWindowPosition(storage, pos) {
  try {
    const store = resolveStorage(storage);
    if (!store) return false;
    store.setItem(WINDOW_POSITION_KEY, JSON.stringify({ left: Math.round(pos.left), top: Math.round(pos.top) }));
    return true;
  } catch {
    return false;
  }
}

/** `pos` moved so a `size` box sits wholly inside `viewport` (or at its
 * top-left corner when it is bigger than the viewport). */
export function clampPosition(pos, size, viewport) {
  const maxLeft = Math.max(0, viewport.width - size.width);
  const maxTop = Math.max(0, viewport.height - size.height);
  return {
    left: Math.round(Math.min(Math.max(0, pos.left), maxLeft)),
    top: Math.round(Math.min(Math.max(0, pos.top), maxTop)),
  };
}

function viewportOf(view) {
  return { width: (view && view.innerWidth) || 1024, height: (view && view.innerHeight) || 768 };
}

// ------------------------------------------------------ what is showing ---

/** A decision's identity: a poll carrying the same key is the same question,
 * so the window patches in place instead of starting over. */
export function decisionKey(pending) {
  const generation =
    pending.generation !== undefined && pending.generation !== null ? pending.generation : pending.seq;
  return [
    "decision",
    pending.player,
    pending.kind,
    generation === undefined || generation === null ? "" : generation,
    (pending.cards || []).join(","),
  ].join("|");
}

const MODE_BY_KIND = {
  scry: "split",
  surveil: "split",
  choose_cards: "choose",
  order_triggers: "order",
  mulligan: "hand",
  assign_combat_damage: "damage",
};

/** What the window should be showing for `app`, or `null` when it should be
 * hidden: `{mode, key, pending}`. Mirrors `#prompt-box`'s own priority - a
 * finished game shows nothing, the pregame comes before any decision - and
 * only ever this seat's own question. */
export function windowSubject(app) {
  const state = app && app.state;
  if (!state || state.winner || state.drawn || !state.you) return null;
  const you = state.you;
  if (state.pregame) {
    const pregame = state.pregame;
    if (pregame.step !== "mulligan" || !(pregame.awaiting_mulligan || []).includes(you)) return null;
    const hand = (state.hands && state.hands[you]) || [];
    const key = [
      "pregame",
      you,
      (pregame.mulligans_taken || {})[you] || 0,
      (pregame.cards_to_bottom || {})[you] || 0,
      hand.map((card) => card.id).join(","),
    ].join("|");
    return { mode: "pregame", key, pending: null };
  }
  const pending = state.pending_decision;
  if (!pending || pending.player !== you || !usesDecisionWindow(pending)) return null;
  return { mode: MODE_BY_KIND[pending.kind], key: decisionKey(pending), pending };
}

function windowTitle(subject) {
  if (subject.mode === "pregame") return "Opening hand";
  if (subject.mode === "hand" && !subject.pending.source) return "Opening hand";
  return viewModel(subject.pending, {}).title;
}

// ------------------------------------------------- minimize, from outside ---

function part(root, id) {
  return root.querySelector(`#${id}`);
}

function setMinimized(root, win, on) {
  const body = part(root, "decision-window-body");
  const toggle = part(root, "decision-window-minimize");
  win.classList.toggle("minimized", on);
  if (body) body.hidden = on;
  if (toggle) {
    toggle.setAttribute("aria-expanded", on ? "false" : "true");
    setText(toggle, on ? "Restore" : "Minimize");
    toggle.title = on ? "Show the decision again" : "Shrink to the title bar; the decision stays pending";
  }
}

/** Whether the window is up and not minimized. */
export function decisionWindowIsOpen(root) {
  const win = part(root, "decision-window");
  return Boolean(win && !win.hidden && !win.classList.contains("minimized"));
}

/** Minimize the window to its title bar. It cannot be closed: the decision is
 * still pending. */
export function minimizeDecisionWindow(root) {
  const win = part(root, "decision-window");
  if (!win || win.hidden || win.classList.contains("minimized")) return false;
  setMinimized(root, win, true);
  return true;
}

/** Bring a minimized window back, inside the viewport. */
export function restoreDecisionWindow(root) {
  const win = part(root, "decision-window");
  if (!win || win.hidden) return false;
  setMinimized(root, win, false);
  if (typeof win.__reclamp === "function") win.__reclamp();
  return true;
}

// --------------------------------------------------------- DOM pieces ---

/** A real button whose action is re-aimed on every update rather than
 * captured once, so the node survives a poll and a click always does what
 * the button currently says. */
function commandButton(cls) {
  const el = h("button", {
    type: "button",
    class: cls || null,
    onClick: () => {
      if (!el.disabled && typeof el.__run === "function") el.__run();
    },
  });
  return el;
}

function aim(el, label, enabled, run, title) {
  setText(el, label);
  el.disabled = !enabled;
  el.__run = run;
  if (title !== undefined) el.title = title;
}

/** Make `parent`'s children exactly `nodes`, touching nothing when they
 * already are. */
function syncChildren(parent, nodes) {
  const want = nodes.filter(Boolean);
  const have = parent.childNodes ? [...parent.childNodes] : [];
  if (have.length === want.length && have.every((node, i) => node === want[i])) return;
  clear(parent);
  for (const node of want) parent.appendChild(node);
}

function frame(body, symbols, promptText) {
  const prompt = h("div", { class: "dw-prompt" });
  if (promptText) prompt.appendChild(withSymbols(symbols, promptText));
  const status = h("div", { class: "dw-status", "aria-live": "polite" });
  const content = h("div", { class: "dw-content" });
  const actions = h("div", { class: "dw-actions" });
  for (const node of [prompt, status, content, actions]) body.appendChild(node);
  return { prompt, status, content, actions };
}

function cardsById(pending) {
  const out = {};
  for (const card of decisionCards(pending)) out[card.id] = card;
  return out;
}

function laneNode(lane, label) {
  const cards = h("div", { class: "dw-lane-cards" });
  const node = h(
    "section",
    { class: "dw-lane", dataset: { lane }, "aria-label": label },
    h("div", { class: "dw-lane-label" }, label.toUpperCase()),
    cards
  );
  return { lane, label, node, cards };
}

function laneSlot(card, buttons) {
  const cardEl = cardNode(card, "card-md");
  const slot = h(
    "div",
    { class: "dw-slot", dataset: { key: card.id } },
    cardEl,
    h("div", { class: "dw-slot-controls" }, ...buttons)
  );
  slot.__card = cardEl;
  return slot;
}

function handCard(card) {
  const cardEl = cardNode(card, "card-md");
  const el = h("div", { class: "dw-hand-card", dataset: { key: card.id } }, cardEl);
  el.__card = cardEl;
  return el;
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

// ---------------------------------------------------------------- modes ---
//
// Each mode builds its content once for a decision and returns
// `{update(app), drop?(id, lane, index)}`. `update` patches in place;
// `drop` is what a finished card drag calls. Every action reads the store
// fresh when it runs, never a snapshot from when the button was last aimed.

/** Scry, surveil, and `look_at_top_and_split`: a kept lane (TOP, or the
 * payload's `kept_label`) and a moved lane (BOTTOM, GRAVEYARD, or the
 * payload's `moved_label`). */
function splitMode(ctx) {
  const { pending, handlers, store } = ctx;
  const initial = viewModel(pending, ctx.app.decisionDraft || {}).controls[0];
  const f = frame(ctx.body, ctx.app.symbols, pending.prompt);
  const lanes = { kept: laneNode("kept", initial.keptLabel), moved: laneNode("moved", initial.movedLabel) };
  f.content.appendChild(h("div", { class: "dw-lanes" }, lanes.kept.node, lanes.moved.node));
  const done = commandButton("primary");
  f.actions.appendChild(done);
  const cards = cardsById(pending);

  const arrangement = (app) => {
    const vm = viewModel(pending, app.decisionDraft || {});
    return { vm, kept: vm.controls[0].order, moved: vm.submit.answer.moved };
  };
  const drop = (id, lane, index) => {
    if (lane !== "kept" && lane !== "moved") return;
    const now = arrangement(store.get());
    handlers.onDecisionDraft(placeInSplit(now.moved, now.kept, id, lane, index));
  };
  const step = (id, delta) => {
    const now = arrangement(store.get());
    const lane = now.moved.includes(id) ? "moved" : "kept";
    const at = now[lane].indexOf(id);
    if (at >= 0) drop(id, lane, at + delta);
  };
  const across = (id) => {
    const now = arrangement(store.get());
    const to = now.moved.includes(id) ? "kept" : "moved";
    drop(id, to, now[to].length);
  };
  const createSlot = (item) => {
    const buttons = { up: commandButton(), down: commandButton(), move: commandButton() };
    const slot = laneSlot(cards[item.id], [buttons.up, buttons.down, buttons.move]);
    slot.__buttons = buttons;
    return slot;
  };
  const updateSlot = (slot, item) => {
    const card = cards[item.id];
    updateCardNode(slot.__card, card, {});
    const here = lanes[item.lane].label;
    const there = lanes[item.lane === "kept" ? "moved" : "kept"].label;
    const name = card.name || item.id;
    const { up, down, move } = slot.__buttons;
    aim(up, "up", item.index > 0, () => step(item.id, -1), `${name}: one place earlier in ${here}`);
    aim(down, "down", item.index < item.count - 1, () => step(item.id, 1), `${name}: one place later in ${here}`);
    aim(move, `Move to ${there}`, true, () => across(item.id), `${name}: move to ${there}`);
  };

  return {
    lanes,
    drop,
    update(app) {
      const now = arrangement(app);
      for (const lane of ["kept", "moved"]) {
        const ids = now[lane];
        patchChildren(
          lanes[lane].cards,
          ids.map((id, index) => ({ id, lane, index, count: ids.length })),
          (item) => item.id,
          createSlot,
          updateSlot
        );
      }
      // `countHint` (`decisions.js`'s `splitCount`) is the count
      // `validate_answer` actually enforces (`pending.amount`/`.exact`,
      // L1a) - stated here, not only in the Done button's enabled state,
      // so a seat learns why a lane count is wrong instead of only that it
      // is.
      const hint = now.vm.controls[0].countHint;
      const countNote = hint ? ` ${hint} may move to ${lanes.moved.label}.` : "";
      setText(
        f.status,
        `${now.kept.length} to ${lanes.kept.label}, ${now.moved.length} to ${lanes.moved.label}.${countNote} Drag cards between the lanes and along them, or use the buttons.`
      );
      aim(done, now.vm.submit.label, now.vm.submit.enabled, () => {
        const fresh = arrangement(store.get()).vm;
        if (fresh.submit.enabled) handlers.onDecide(fresh.submit.answer);
      });
    },
  };
}

/** Every `choose_cards`, whatever zone the candidates are in: a grid of
 * cards, click to toggle, never past the maximum, with the zero answer
 * offered whenever zero is legal. */
function chooseMode(ctx) {
  const { pending, handlers, store } = ctx;
  const f = frame(ctx.body, ctx.app.symbols, pending.prompt);
  const grid = h("div", { class: "dw-grid", role: "group", "aria-label": "cards to choose from" });
  f.content.appendChild(grid);
  const cards = cardsById(pending);
  const cap = pending.amount === null || pending.amount === undefined ? null : pending.amount;
  const initial = viewModel(pending, ctx.app.decisionDraft || {});
  const submit = commandButton("primary");
  const none = initial.secondary ? commandButton() : null;
  syncChildren(f.actions, [submit, none]);
  const chosenOf = (app) => (app.decisionDraft || {}).card_ids || [];

  const createPick = (item) => {
    const cardEl = cardNode(cards[item.id], "card-md");
    const pick = h(
      "button",
      {
        type: "button",
        class: "dw-pick",
        dataset: { key: item.id },
        onClick: () => handlers.onDecisionDraft({ card_ids: toggleChoice(chosenOf(store.get()), item.id, cap) }),
      },
      cardEl
    );
    pick.__card = cardEl;
    return pick;
  };
  const updatePick = (pick, item) => {
    const card = cards[item.id];
    updateCardNode(pick.__card, card, { selected: item.selected });
    pick.setAttribute("aria-pressed", item.selected ? "true" : "false");
    pick.classList.toggle("at-cap", item.full && !item.selected);
    pick.title = item.selected ? `${card.name || item.id}: chosen, click to put it back` : `${card.name || item.id}: click to choose`;
  };

  return {
    update(app) {
      const chosen = chosenOf(app);
      const vm = viewModel(pending, app.decisionDraft || {});
      const full = cap !== null && cap !== 1 && chosen.length >= cap;
      patchChildren(
        grid,
        (pending.cards || []).map((id) => ({ id, selected: chosen.includes(id), full })),
        (item) => item.id,
        createPick,
        updatePick
      );
      let status;
      if (pending.exact !== false) status = `${chosen.length} of ${cap} chosen.`;
      else if (cap === null) status = `${chosen.length} chosen, any number allowed.`;
      else status = `${chosen.length} chosen, up to ${cap}.`;
      setText(f.status, status);
      aim(submit, vm.submit.label, vm.submit.enabled, () => {
        const fresh = viewModel(pending, store.get().decisionDraft || {});
        if (fresh.submit.enabled) handlers.onDecide(fresh.submit.answer);
      });
      if (none) aim(none, vm.secondary.label, true, () => handlers.onDecide(vm.secondary.answer));
    },
  };
}

/** `order_triggers`: one lane, leftmost resolves first. The shown order is
 * always a full order, so confirming without touching anything posts the
 * offered order. */
function orderMode(ctx) {
  const { pending, handlers, store } = ctx;
  const f = frame(ctx.body, ctx.app.symbols, pending.prompt);
  const lane = laneNode("order", "Resolves first, left to right");
  f.content.appendChild(h("div", { class: "dw-lanes" }, lane.node));
  const confirm = commandButton("primary");
  const reset = commandButton();
  syncChildren(f.actions, [confirm, reset]);
  const cards = cardsById(pending);
  const confirmLabel = viewModel(pending, {}).submit.label;
  const shown = (app) => shownTriggerOrder(pending, app.decisionDraft || {});

  const drop = (id, laneName, index) => {
    if (laneName !== "order") return;
    handlers.onDecisionDraft({ order: placeInList(shown(store.get()), id, index) });
  };
  const step = (id, delta) => {
    const now = shown(store.get());
    const at = now.indexOf(id);
    if (at >= 0) drop(id, "order", at + delta);
  };
  const createSlot = (item) => {
    const buttons = { up: commandButton(), down: commandButton() };
    const slot = laneSlot(cards[item.id], [buttons.up, buttons.down]);
    slot.__buttons = buttons;
    return slot;
  };
  const updateSlot = (slot, item) => {
    const card = cards[item.id];
    updateCardNode(slot.__card, card, {});
    const name = card.text || card.name || item.id;
    aim(slot.__buttons.up, "up", item.index > 0, () => step(item.id, -1), `${name}: resolve one place earlier`);
    aim(slot.__buttons.down, "down", item.index < item.count - 1, () => step(item.id, 1), `${name}: resolve one place later`);
  };

  return {
    lanes: { order: lane },
    drop,
    update(app) {
      const ids = shown(app);
      patchChildren(
        lane.cards,
        ids.map((id, index) => ({ id, index, count: ids.length })),
        (item) => item.id,
        createSlot,
        updateSlot
      );
      setText(f.status, "Drag to order, or use the buttons. The leftmost ability resolves first.");
      aim(confirm, confirmLabel, true, () => {
        const draft = store.get().decisionDraft || {};
        handlers.onDecide(answerFor(pending, { ...draft, order: shownTriggerOrder(pending, draft) }));
      });
      aim(reset, "Reset order", ((app.decisionDraft || {}).order || []).length > 0, () =>
        handlers.onDecisionDraft({ order: [] })
      );
    },
  };
}

/** `assign_combat_damage` (CR 703.4k, 510.1c, 702.19b): one row per
 * recipient - each blocker, a real card, plus the defending player or
 * planeswalker once trample lets damage reach past them - each with a
 * number input for how much of the attacker's power to assign it, and its
 * own `lethal_damage` printed alongside so a seat can see how much is
 * "enough" before trampling past it. No drag: this is a set of amounts, not
 * an arrangement, so it has no `drop` and the window wires none for it. */
function damageMode(ctx) {
  const { pending, handlers, store } = ctx;
  const f = frame(ctx.body, ctx.app.symbols, pending.prompt);
  const grid = h("div", { class: "dw-grid dw-damage-grid", role: "group", "aria-label": "assign combat damage" });
  f.content.appendChild(grid);
  const cards = cardsById(pending);
  const submit = commandButton("primary");
  syncChildren(f.actions, [submit]);
  const rowsOf = (app) => viewModel(pending, app.decisionDraft || {}).controls[0].rows;
  const writeAmount = (row, amount) => {
    const now = (store.get().decisionDraft || {}).damage || {};
    handlers.onDecisionDraft({ damage: { ...now, [row.key]: amount } });
  };

  const createRow = (row) => {
    const input = h("input", {
      type: "number",
      min: "0",
      class: "dw-damage-amount",
      "aria-label": `damage assigned to ${row.name}`,
      onChange: (e) => writeAmount(row, Number(e.target.value) || 0),
    });
    const label =
      row.kind === "player"
        ? h("div", { class: "dw-damage-player" }, row.name)
        : cardNode(cards[row.id], "card-md");
    const el = h(
      "div",
      { class: "dw-damage-row", dataset: { key: row.key } },
      label,
      h("div", { class: "dw-damage-lethal" }, `lethal: ${row.lethal}`),
      input
    );
    el.__input = input;
    el.__card = row.kind === "player" ? null : label;
    return el;
  };
  const updateRow = (el, row) => {
    if (el.__card) updateCardNode(el.__card, cards[row.id], {});
    if (Number(el.__input.value) !== row.amount) el.__input.value = String(row.amount);
  };

  return {
    update(app) {
      const rows = rowsOf(app);
      patchChildren(grid, rows, (row) => row.key, createRow, updateRow);
      const vm = viewModel(pending, app.decisionDraft || {});
      const control = vm.controls[0];
      setText(f.status, `${control.total} of ${control.power} damage assigned.`);
      aim(submit, vm.submit.label, vm.submit.enabled, () => {
        const fresh = viewModel(pending, store.get().decisionDraft || {});
        if (fresh.submit.enabled) handlers.onDecide(fresh.submit.answer);
      });
    },
  };
}

/** A paused `mulligan` decision: the hand it is about, and the view model's
 * own Mulligan / Keep. */
function handMode(ctx) {
  const { pending, handlers } = ctx;
  const f = frame(ctx.body, ctx.app.symbols, pending.prompt);
  const grid = h("div", { class: "dw-grid" });
  f.content.appendChild(grid);
  const vm = viewModel(pending, {});
  const submit = commandButton("primary");
  const secondary = vm.secondary ? commandButton() : null;
  syncChildren(f.actions, [submit, secondary]);
  return {
    update(app) {
      const state = app.state;
      const hand = (state.hands && state.hands[state.you]) || [];
      patchChildren(grid, hand, (card) => card.id, handCard, (el, card) => updateCardNode(el.__card, card, {}));
      aim(submit, vm.submit.label, vm.submit.enabled, () => handlers.onDecide(vm.submit.answer));
      if (secondary) aim(secondary, vm.secondary.label, true, () => handlers.onDecide(vm.secondary.answer));
    },
  };
}

/** This seat's own pregame mulligan (CR 103.5): the opening hand with Keep and
 * Mulligan, then - when keeping costs cards - click the cards that go to the
 * bottom. Posts to `pregame/mulligan` exactly as the old panel did. */
function pregameMode(ctx) {
  const { handlers, store } = ctx;
  const you = ctx.app.state.you;
  const taken = (ctx.app.state.pregame.mulligans_taken || {})[you] || 0;
  const takingText = taken ? ` You have taken ${plural(taken, "mulligan")}.` : "";
  const f = frame(
    ctx.body,
    ctx.app.symbols,
    `Your opening hand (CR 103.5): keep it, or shuffle it away for a new seven.${takingText}`
  );
  const grid = h("div", { class: "dw-grid", role: "group", "aria-label": "your opening hand" });
  f.content.appendChild(grid);
  const keep = commandButton("primary");
  const mulligan = commandButton();
  const back = commandButton();
  const noMulligan = h("span", { class: "hint" }, "no mulligan left - a hand of nothing is the floor (CR 103.5)");
  const local = { phase: "choose", shownPhase: null };

  const facts = (app) => {
    const state = app.state;
    const pregame = state.pregame || {};
    const needed = (pregame.cards_to_bottom || {})[you] || 0;
    const hand = (state.hands && state.hands[you]) || [];
    const ids = hand.map((card) => card.id);
    const chosen = needed ? ((app.decisionDraft || {}).bottom || []).filter((id) => ids.includes(id)) : [];
    return { needed, hand, chosen, canMulligan: Boolean(pregame.can_mulligan && pregame.can_mulligan[you]) };
  };
  const postKeep = () => {
    const now = facts(store.get());
    if (now.chosen.length !== now.needed) return;
    const body = { player: you, take: false };
    if (now.chosen.length) body.bottom = now.chosen;
    handlers.onDecisionDraft({ bottom: [] });
    handlers.onPregame("mulligan", body);
  };
  const createCard = (card) => {
    if (local.phase !== "bottom") return handCard(card);
    const cardEl = cardNode(card, "card-md");
    const pick = h(
      "button",
      {
        type: "button",
        class: "dw-pick",
        dataset: { key: card.id },
        onClick: () => {
          const now = facts(store.get());
          handlers.onDecisionDraft({ bottom: toggleChoice(now.chosen, card.id, now.needed) });
        },
      },
      cardEl
    );
    pick.__card = cardEl;
    return pick;
  };

  return {
    update(app) {
      const now = facts(app);
      if (local.shownPhase !== local.phase) {
        clear(grid);
        local.shownPhase = local.phase;
      }
      patchChildren(grid, now.hand, (card) => card.id, createCard, (el, card) => {
        const selected = now.chosen.includes(card.id);
        updateCardNode(el.__card, card, { selected });
        if (local.phase === "bottom") el.setAttribute("aria-pressed", selected ? "true" : "false");
      });
      if (local.phase === "bottom") {
        setText(
          f.status,
          `Click ${plural(now.needed, "card")} to put on the bottom of your library: ${now.chosen.length} of ${now.needed} chosen.`
        );
        aim(keep, "Keep", now.chosen.length === now.needed, postKeep);
        aim(back, "Back", true, () => {
          local.phase = "choose";
          ctx.rerender();
        });
        syncChildren(f.actions, [keep, back]);
        return;
      }
      setText(f.status, now.needed ? `Keeping costs ${plural(now.needed, "card")} on the bottom of your library.` : "");
      aim(keep, "Keep", true, () => {
        if (facts(store.get()).needed) {
          local.phase = "bottom";
          ctx.rerender();
        } else {
          postKeep();
        }
      });
      if (now.canMulligan) {
        aim(mulligan, "Mulligan", true, () => {
          handlers.onDecisionDraft({ bottom: [] });
          handlers.onPregame("mulligan", { player: you, take: true });
        });
      }
      syncChildren(f.actions, [keep, now.canMulligan ? mulligan : noMulligan]);
    },
  };
}

const MODES = {
  split: splitMode,
  choose: chooseMode,
  order: orderMode,
  hand: handMode,
  pregame: pregameMode,
  damage: damageMode,
};

// ---------------------------------------------------------------- mount ---

/**
 * Wire `#decision-window` to `store`. `handlers` carries `onDecisionDraft`,
 * `onDecide` and `onPregame` (`ui/app.js`'s own). `opts.storage` (the storage
 * object, or a function returning it) and `opts.view` (for the viewport and
 * `resize`) default to the page's `localStorage` and `window`. Returns an
 * unmount function; mounting again on the same element unmounts the previous
 * wiring first.
 */
export function mountDecisionWindow(root, store, handlers, opts) {
  const win = part(root, "decision-window");
  if (!win) return () => {};
  if (typeof win.__unmountDecisionWindow === "function") win.__unmountDecisionWindow();
  const els = {
    handle: part(root, "decision-window-handle"),
    title: part(root, "decision-window-title"),
    minimize: part(root, "decision-window-minimize"),
    body: part(root, "decision-window-body"),
  };
  const options = opts || {};
  const storage = "storage" in options ? options.storage : () => globalThis.localStorage;
  const view = options.view || (typeof window !== "undefined" ? window : null);

  let current = null;
  let cardDrag = null;
  let windowDrag = null;
  let deferred = false;

  const permanent = [];
  const on = (target, type, fn) => {
    if (!target || !target.addEventListener) return;
    target.addEventListener(type, fn);
    permanent.push([target, type, fn]);
  };
  let tracking = [];
  const track = (handlers) => {
    untrack();
    for (const [type, fn] of Object.entries(handlers)) {
      root.addEventListener(type, fn);
      tracking.push([type, fn]);
    }
  };
  const untrack = () => {
    for (const [type, fn] of tracking) root.removeEventListener(type, fn);
    tracking = [];
  };

  // -- position
  const measure = () => {
    const rect = win.getBoundingClientRect ? win.getBoundingClientRect() : null;
    return rect && rect.width ? { width: rect.width, height: rect.height } : FALLBACK_SIZE;
  };
  const positionNow = () => ({ left: parseFloat(win.style.left) || 0, top: parseFloat(win.style.top) || 0 });
  const applyPosition = (pos) => {
    win.style.left = `${pos.left}px`;
    win.style.top = `${pos.top}px`;
  };
  const place = () => {
    const size = measure();
    const viewport = viewportOf(view);
    const wanted = loadWindowPosition(storage) || {
      left: viewport.width - RIBBON_WIDTH_PX - size.width - EDGE_GAP_PX,
      top: DEFAULT_TOP_PX,
    };
    applyPosition(clampPosition(wanted, size, viewport));
  };
  const reclamp = () => {
    if (win.hidden) return;
    applyPosition(clampPosition(positionNow(), measure(), viewportOf(view)));
  };
  win.__reclamp = reclamp;

  // -- rendering
  const render = (app) => {
    if (!app) return;
    if (cardDrag || windowDrag) {
      deferred = true;
      return;
    }
    const subject = windowSubject(app);
    if (!subject) {
      if (current) {
        current = null;
        clear(els.body);
      }
      win.hidden = true;
      return;
    }
    let fresh = false;
    if (!current || current.key !== subject.key) {
      fresh = true;
      clear(els.body);
      current = {
        key: subject.key,
        view: MODES[subject.mode]({
          body: els.body,
          pending: subject.pending,
          app,
          handlers,
          store,
          rerender: () => render(store.get()),
        }),
      };
      setText(els.title, windowTitle(subject));
      setMinimized(root, win, false);
    }
    const wasHidden = win.hidden;
    win.hidden = false;
    current.view.update(app);
    if (fresh && (wasHidden || !win.style.left)) place();
  };
  const flushDeferred = () => {
    if (!deferred) return;
    deferred = false;
    render(store.get());
  };

  // -- moving the window by its title bar
  const onHandleDown = (e) => {
    if (win.hidden) return;
    if (e.button !== undefined && e.button !== 0) return;
    if (e.target && e.target.closest && e.target.closest("button")) return;
    const rect = win.getBoundingClientRect();
    windowDrag = {
      dx: e.clientX - rect.left,
      dy: e.clientY - rect.top,
      size: rect.width ? { width: rect.width, height: rect.height } : FALLBACK_SIZE,
    };
    win.classList.add("moving");
    if (e.preventDefault) e.preventDefault();
    const end = () => {
      if (!windowDrag) return;
      untrack();
      windowDrag = null;
      win.classList.remove("moving");
      saveWindowPosition(storage, positionNow());
      flushDeferred();
    };
    track({
      pointermove: (move) => {
        if (!windowDrag) return;
        const wanted = { left: move.clientX - windowDrag.dx, top: move.clientY - windowDrag.dy };
        applyPosition(clampPosition(wanted, windowDrag.size, viewportOf(view)));
      },
      pointerup: end,
      pointercancel: end,
    });
  };

  // -- dragging cards between and along lanes
  const clearDropTargets = () => {
    const lanes = (current && current.view.lanes) || {};
    for (const lane of Object.values(lanes)) lane.node.classList.remove("drop-target");
  };
  const dropAt = (x, y) => {
    const lanes = (current && current.view.lanes) || {};
    const hit = typeof root.elementFromPoint === "function" ? root.elementFromPoint(x, y) : null;
    const laneEl = hit && hit.closest ? hit.closest(".dw-lane") : null;
    const lane = laneEl ? Object.values(lanes).find((l) => l.node === laneEl) : null;
    if (!lane) return null;
    let index = 0;
    for (const slot of [...lane.cards.childNodes]) {
      if (cardDrag && slot === cardDrag.slot) continue;
      const r = slot.getBoundingClientRect();
      if (y > r.bottom || (y >= r.top && x > r.left + r.width / 2)) index += 1;
    }
    return { lane: lane.lane, index, node: lane.node };
  };
  const endCardDrag = () => {
    untrack();
    if (cardDrag) {
      cardDrag.slot.classList.remove("dragging");
      cardDrag.slot.style.transform = "";
    }
    clearDropTargets();
    cardDrag = null;
  };
  const onCardDown = (e) => {
    if (!current || typeof current.view.drop !== "function") return;
    if (e.button !== undefined && e.button !== 0) return;
    const target = e.target;
    if (!target || !target.closest || target.closest("button")) return;
    const slot = target.closest(".dw-slot");
    if (!slot || !slot.dataset.key || !slot.closest(".dw-lane")) return;
    cardDrag = { id: slot.dataset.key, slot, x: e.clientX, y: e.clientY, started: false, key: current.key };
    if (e.preventDefault) e.preventDefault();
    track({
      pointermove: (move) => {
        if (!cardDrag) return;
        const dx = move.clientX - cardDrag.x;
        const dy = move.clientY - cardDrag.y;
        if (!cardDrag.started) {
          if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
          cardDrag.started = true;
          cardDrag.slot.classList.add("dragging");
        }
        cardDrag.slot.style.transform = `translate(${dx}px, ${dy}px)`;
        const over = dropAt(move.clientX, move.clientY);
        clearDropTargets();
        if (over) over.node.classList.add("drop-target");
      },
      pointerup: (up) => {
        const drag = cardDrag;
        if (!drag) return;
        const target = drag.started ? dropAt(up.clientX, up.clientY) : null;
        endCardDrag();
        flushDeferred();
        if (target && current && current.key === drag.key) current.view.drop(drag.id, target.lane, target.index);
      },
      pointercancel: () => {
        endCardDrag();
        flushDeferred();
      },
    });
  };

  // -- minimize and Escape
  const onMinimizeClick = () => {
    if (win.classList.contains("minimized")) restoreDecisionWindow(root);
    else minimizeDecisionWindow(root);
  };
  const onKey = (e) => {
    if (e.key !== "Escape" || win.hidden || win.classList.contains("minimized")) return;
    minimizeDecisionWindow(root);
    if (els.minimize && typeof els.minimize.focus === "function") els.minimize.focus();
    // Handled here: `app.js`'s document-level Escape would otherwise also
    // cancel an in-flight action.
    if (e.stopPropagation) e.stopPropagation();
  };

  on(els.handle, "pointerdown", onHandleDown);
  on(els.body, "pointerdown", onCardDown);
  on(els.minimize, "click", onMinimizeClick);
  on(win, "keydown", onKey);
  on(view, "resize", reclamp);
  const unsubscribe = store.subscribe(render);

  const unmount = () => {
    untrack();
    for (const [target, type, fn] of permanent) target.removeEventListener(type, fn);
    if (typeof unsubscribe === "function") unsubscribe();
    if (win.__unmountDecisionWindow === unmount) win.__unmountDecisionWindow = null;
  };
  win.__unmountDecisionWindow = unmount;
  return unmount;
}
