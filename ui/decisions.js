// Decision view models: one `viewModel(pending, draft) -> ViewModel` per
// `DecisionKind` a live seat can answer through `decide` (see
// `magic_cards_model_api.decisions.ANSWERABLE_KINDS`). `DECISION_KINDS`'
// object-literal keys are read by `tests/test_ui_decision_coverage.py` the
// same way `ui/app.js`'s old `DECISION_RENDERERS` was: a new engine
// `DecisionKind` with no entry here cannot silently hang a live browser game.
// Do not rename this constant without updating that test.
//
// `ViewModel = {kind, title, prompt, controls, submit, secondary, remaining}`
// - pure data, no DOM and no `innerHTML`; the screens turn it into markup:
// `render/decision_controls.js`'s `renderDecision` in `#prompt-box` for the
// small kinds (or in `render/decision_popover.js`'s popover beside the asking
// card, for the kinds `usesDecisionPopover` names), `render/decision_window.js`
// for the kinds `usesDecisionWindow` names. All post exactly the answer the
// view model says.
import { fill, flatten, isSatisfied, slotsFromAffordance } from "./targeting.js";

// -- moved verbatim from ui/app.js (chooseCardsOptions, triggerOrderOptions)

/**
 * The candidates a `choose_cards` question offers, as `{id, label}` in the
 * order the engine listed them (zone order, for a search). Pure, and
 * exported, so `tests/test_fetchland_search_choice.py` can hold the shipped
 * labelling to the rule rather than re-describing it.
 */
export function chooseCardsOptions(pending) {
  const ids = (pending && pending.cards) || [];
  const details = {};
  ((pending && pending.card_details) || []).forEach((obj) => {
    details[obj.id] = obj;
  });
  const labels = (pending && pending.card_labels) || [];
  return ids.map((id, index) => {
    const obj = details[id];
    return { id: id, label: (obj && obj.name) || labels[index] || id };
  });
}

/** How much of an ability's own text to show beside its source's name. Enough
 * to tell two different triggers of one permanent apart without turning a row
 * of buttons into a wall of oracle text. */
const TRIGGER_TEXT_CHARS = 80;

/**
 * The options a CR 603.3b question offers, as `{id, label}` in the order they
 * triggered. Pure, and exported, so `tests/test_trigger_order_labels.py` can
 * hold the real shipped labelling to the rule rather than re-describing it.
 */
export function triggerOrderOptions(pending) {
  const ids = (pending && pending.cards) || [];
  const details = {};
  ((pending && pending.card_details) || []).forEach((obj) => {
    details[obj.id] = obj;
  });
  return ids.map((id, index) => {
    const obj = details[id];
    const position = `#${index + 1}`;
    if (!obj) return { id: id, label: `trigger ${position}` };
    const name = obj.name || `trigger ${position}`;
    const text = (obj.text || "").replace(/\s+/g, " ").trim();
    const clipped =
      text.length > TRIGGER_TEXT_CHARS ? `${text.slice(0, TRIGGER_TEXT_CHARS - 1)}…` : text;
    // The position stays on every label even when the name is known: two
    // copies of one card is the normal case here (two Delver of Secrets both
    // read "at the beginning of your upkeep"), so name and text alone can be
    // identical for two genuinely different objects.
    return {
      id: id,
      label: clipped ? `${position} ${name} - ${clipped}` : `${position} ${name}`,
    };
  });
}

// -- shared helpers ----------------------------------------------------------

/**
 * `pending.card_details` travels two shapes across this codebase:
 * `visibility.redact_pending_decision` stamps it as a *list* of
 * `{id, name, ...}` (what `chooseCardsOptions`/`triggerOrderOptions` above
 * already expect - that is the real wire shape), but a caller that already
 * has one indexed by id may hand it over as an object instead. Both are
 * normalized here so `labelOf` reads either.
 */
function detailsById(pending) {
  const raw = (pending && pending.card_details) || {};
  if (!Array.isArray(raw)) return raw;
  const out = {};
  for (const obj of raw) out[obj.id] = obj;
  return out;
}

function labelOf(pending, id) {
  const details = detailsById(pending);
  if (details[id] && details[id].name) return details[id].name;
  const i = (pending.cards || []).indexOf(id);
  if (i >= 0 && pending.card_labels && pending.card_labels[i]) return pending.card_labels[i];
  return id;
}

function yesNo(pending, yes, no) {
  return {
    kind: pending.kind,
    title: pending.source || pending.kind,
    prompt: pending.prompt,
    controls: [],
    submit: {
      label: yes,
      enabled: pending.affirmative_is_legal !== false,
      answer: { accept: true },
    },
    secondary: { label: no, answer: { accept: false } },
    remaining: null,
  };
}

function amount(pending, draft, field) {
  const value = draft.amount === undefined ? 0 : draft.amount;
  return {
    kind: pending.kind,
    title: pending.source || pending.kind,
    prompt: pending.prompt,
    controls: [{ type: "number", min: 0, max: pending.amount, value }],
    submit: {
      label: "Pay",
      enabled: value >= 0 && value <= pending.amount,
      answer: { [field]: value },
    },
    secondary: null,
    remaining: null,
  };
}

function chooseCards(pending, draft) {
  const chosen = draft.card_ids || [];
  const options = chooseCardsOptions(pending).map((o) => ({
    ...o,
    selected: chosen.includes(o.id),
    disabled: false,
  }));
  const max = pending.amount;
  // `exact` absent (an older server) reads as `true` - the engine's own
  // default for the field (`decisions.py`'s `PendingDecision.exact`) and the
  // shape every `choose_cards` question had before a search became one.
  const exact = pending.exact !== false;
  const ok = exact
    ? chosen.length === max
    : chosen.length <= (max === null || max === undefined ? Infinity : max);
  // Choosing zero is legal for a non-exact choice, and for an exact choice of
  // zero. It is "Find nothing" for a search, "Choose none" when a candidate
  // is known to be somewhere other than a library (a hand, the battlefield,
  // a graveyard). A candidate with no `card_details` says nothing about its
  // zone - a search's often has only the engine's labels - so it does not
  // turn a search into "Choose none".
  const details = detailsById(pending);
  const ids = (pending && pending.cards) || [];
  const searching = !ids.some((id) => details[id] && details[id].zone && details[id].zone !== "library");
  const noneLabel = searching ? "Find nothing" : "Choose none";
  return {
    kind: pending.kind,
    title: pending.source || "Choose cards",
    prompt: pending.prompt,
    controls: [{ type: "options", items: options, multi: true }],
    submit: {
      label: exact ? (max === 0 ? noneLabel : `Choose ${max}`) : "Choose",
      enabled: ok,
      answer: { card_ids: chosen },
    },
    secondary: exact ? null : { label: noneLabel, answer: { card_ids: [] } },
    remaining: null,
  };
}

function targetsAffordance(pending) {
  return {
    targets_anything: true,
    understood: true,
    minimum: pending.target_minimum || 0,
    maximum: pending.target_maximum,
    requirements: [
      {
        phrase: pending.prompt,
        count:
          pending.target_maximum === null || pending.target_maximum === undefined
            ? 1
            : pending.target_maximum,
        minimum: pending.target_minimum || 0,
        unbounded: pending.target_maximum === null || pending.target_maximum === undefined,
        variable_count: false,
        mode: null,
        mode_text: null,
      },
    ],
    division: null,
    candidates: (pending.targets || []).map((t) => ({
      id: t.id,
      kind: t.kind,
      name: labelOf(pending, t.id),
      requirements: [0],
    })),
  };
}

function chooseTargets(pending, draft) {
  const aff = targetsAffordance(pending);
  let slots = slotsFromAffordance(aff);
  for (const id of draft.targets || []) slots = fill(slots, 0, id);
  return {
    kind: pending.kind,
    title: pending.source || "Choose targets",
    prompt: pending.prompt,
    controls: [{ type: "targets", slots, affordance: aff }],
    submit: { label: "Choose", enabled: isSatisfied(slots), answer: { targets: flatten(slots) } },
    secondary: null,
    remaining: null,
  };
}

function split(pending, draft) {
  const moved = draft.moved || [];
  const kept = (pending.cards || []).filter((id) => !moved.includes(id));
  // The drafted order, restricted to still-kept cards, with any kept card
  // `draftOrder` doesn't yet mention appended at the end, rather than
  // dropped. Filtering `draftOrder` down to `kept` alone (this function's
  // previous behaviour) lost exactly that card: once the player had drawn
  // an explicit order (up/down on the split control writes back the
  // *current* `order` this function returned, per-id), a card moved and
  // then kept again was back in `kept` but still absent from that stale
  // `draftOrder`, so it silently vanished from the list the player would
  // submit as `top_order` - never offered a row to reorder, never included.
  const draftOrder = draft.order || [];
  const order = [
    ...draftOrder.filter((id) => kept.includes(id)),
    ...kept.filter((id) => !draftOrder.includes(id)),
  ];
  return {
    kind: pending.kind,
    title: pending.source || pending.kind,
    prompt: pending.prompt,
    controls: [
      {
        type: "split",
        cards: (pending.cards || []).map((id) => ({
          id,
          label: labelOf(pending, id),
          moved: moved.includes(id),
        })),
        movedLabel: pending.moved_label || (pending.kind === "surveil" ? "graveyard" : "bottom"),
        keptLabel: pending.kept_label || "top",
        order,
      },
    ],
    submit: {
      label: "Done",
      enabled: true,
      answer: { moved, top_order: order.length ? order : null },
    },
    secondary: null,
    remaining: null,
  };
}

function choice(pending, draft) {
  const options = pending.options || [];
  // `schemas.py`'s `choice` field also answers `choose_a_quality` for a
  // card-name (CR 201.4) or creature-type (CR 205.3e's "choose a subtype")
  // pick whose universe is too large to enumerate - `pending.options` is
  // empty for that case, unlike `choose_a_mode`'s always-printed, always
  // non-empty modes (CR 700.2a) - so there is nothing to offer as a button
  // list and a free-text control is the only one that can answer it.
  if (options.length === 0) {
    const value = draft.choice || "";
    return {
      kind: pending.kind,
      title: pending.source || pending.kind,
      prompt: pending.prompt,
      controls: [{ type: "text", value }],
      submit: {
        label: "Choose",
        enabled: value.trim().length > 0,
        answer: { choice: value },
      },
      secondary: null,
      remaining: null,
    };
  }
  const items = options.map((o) => ({
    id: o,
    label: o,
    selected: draft.choice === o,
    disabled: false,
  }));
  return {
    kind: pending.kind,
    title: pending.source || pending.kind,
    prompt: pending.prompt,
    controls: [{ type: "options", items, multi: false }],
    submit: {
      label: "Choose",
      enabled: draft.choice !== undefined,
      answer: { choice: draft.choice },
    },
    secondary: null,
    remaining: null,
  };
}

function topOrBottom(pending) {
  return {
    kind: pending.kind,
    title: pending.source || pending.kind,
    prompt: pending.prompt,
    controls: [],
    submit: { label: "Top", enabled: true, answer: { top: true } },
    secondary: { label: "Bottom", answer: { top: false } },
    remaining: null,
  };
}

function isPermutationOf(order, ids) {
  return order.length === ids.length && new Set(order).size === ids.length && ids.every((id) => order.includes(id));
}

function orderTriggers(pending, draft) {
  const order = draft.order || [];
  const items = triggerOrderOptions(pending).map((o) => ({
    ...o,
    selected: order.includes(o.id),
    position: order.indexOf(o.id),
  }));
  // A real answer (CR 603.3b) is a permutation of every offered ability, not
  // merely a same-length list - a duplicate click could otherwise pad
  // `order` out to `items.length` while still dropping one ability and
  // repeating another.
  const isPermutation = isPermutationOf(
    order,
    items.map((o) => o.id)
  );
  return {
    kind: pending.kind,
    title: "Order triggers",
    prompt: pending.prompt,
    controls: [{ type: "order", items }],
    submit: {
      label: "Put on the stack",
      enabled: isPermutation,
      // Reversed: the engine wants bottom of the stack first, and the last
      // thing put on the stack is the first thing to resolve.
      answer: { order: [...order].reverse() },
    },
    secondary: null,
    remaining: null,
  };
}

export const DECISION_KINDS = {
  may: (p) => yesNo(p, "Yes", "No"),
  pay_life: (p) => yesNo(p, "Pay", "Don't pay"),
  pay_energy: (p) => yesNo(p, "Pay", "Don't pay"),
  pay_mana: (p) => yesNo(p, "Pay", "Don't pay"),
  pay_energy_amount: (p, d) => amount(p, d, "energy_amount"),
  pay_life_amount: (p, d) => amount(p, d, "life_amount"),
  scry: split,
  surveil: split,
  // CR 103.5: "True takes a mulligan and False keeps the hand"
  // (`machines/decisions.py`'s `ask_mulligan`) - the opposite polarity of
  // every other `yesNo` kind here, where the submit button's `accept: true`
  // is the affirmative/"yes" branch. `Mulligan`'s submit button must send
  // `accept: true` (it IS the mulligan), and `Keep` is the secondary
  // sending `accept: false`.
  mulligan: (p) => yesNo(p, "Mulligan", "Keep"),
  opening_hand_action: (p) => yesNo(p, "Do it", "Skip"),
  choose_cards: chooseCards,
  choose_targets: chooseTargets,
  choose_a_quality: choice,
  choose_a_mode: choice,
  put_on_top_or_bottom: topOrBottom,
  order_triggers: orderTriggers,
};

export function viewModel(pending, draft) {
  const build = DECISION_KINDS[pending.kind];
  if (!build) {
    return {
      kind: pending.kind,
      title: "Unknown decision",
      prompt: `no renderer for ${pending.kind}`,
      controls: [],
      submit: null,
      secondary: null,
      remaining: null,
    };
  }
  return build(pending, draft || {});
}

// One implementation. `viewModel` already knows, per kind, exactly how a
// draft becomes an answer (`split`'s `moved`/`top_order` filtered to the
// kept cards, `orderTriggers`'s reversed `order`, every other kind's own
// `submit.answer`) - repeating that logic here let the two disagree, which
// is exactly what happened for `{moved: ["a"], order: ["a", "b"]}`: this
// used to hand back `top_order: ["a", "b"]` unfiltered while `split` itself
// would have sent only `["b"]`, the one card actually still on top.
export function answerFor(pending, draft) {
  const vm = viewModel(pending, draft);
  if (!vm.submit) throw new Error(`no answer for decision kind ${pending.kind}`);
  return vm.submit.answer;
}

// -- the decision window -----------------------------------------------------
//
// The kinds with several cards to see, arrange or pick are answered in
// `render/decision_window.js`'s floating window, with real card images; every
// other kind keeps rendering in `#prompt-box`. The helpers below are the
// window's draft arithmetic - pure, so the answer a drag produces is tested
// without a DOM, and always one of the drafts the view models above already
// read.

const WINDOW_KINDS = new Set(["scry", "surveil", "choose_cards", "order_triggers", "mulligan"]);

/** Whether `pending` is answered in the decision window rather than the
 * prompt box. */
export function usesDecisionWindow(pending) {
  return Boolean(pending && WINDOW_KINDS.has(pending.kind));
}

// The small kinds that open beside their source card
// (`render/decision_popover.js`) when `pending.source_card_id` names a card on
// screen, and render in `#prompt-box` otherwise. `choose_targets` is small but
// answered by clicking the board, so it stays in the prompt box.
const POPOVER_KINDS = new Set([
  "may",
  "pay_life",
  "pay_energy",
  "pay_mana",
  "pay_energy_amount",
  "pay_life_amount",
  "choose_a_quality",
  "choose_a_mode",
  "put_on_top_or_bottom",
  "opening_hand_action",
]);

/** Whether `pending` is a kind the card-anchored popover may answer. Whether
 * it actually does also depends on its source card being rendered. */
export function usesDecisionPopover(pending) {
  return Boolean(pending && POPOVER_KINDS.has(pending.kind));
}

/** The cards a decision is about, one card-like object per `pending.cards`
 * id, in that order: the `card_details` view when there is one (real image
 * URLs included), otherwise a text-only stand-in built from `card_labels`, so
 * `cardNode`'s text frame still has something true to say. A trigger's label
 * is its ability text, not a card name. */
export function decisionCards(pending) {
  const details = detailsById(pending);
  const labels = (pending && pending.card_labels) || [];
  const triggers = Boolean(pending && pending.kind === "order_triggers");
  return ((pending && pending.cards) || []).map((id, index) => {
    if (details[id]) return { ...details[id], id };
    if (triggers) return { id, kind: "ability", name: `trigger #${index + 1}`, text: labels[index] || "" };
    return { id, name: labels[index] || id };
  });
}

function clampIndex(index, length) {
  if (!Number.isFinite(index)) return index > 0 ? length : 0;
  return Math.max(0, Math.min(Math.trunc(index), length));
}

/** `chosen` with `id` toggled, never past `max` (`null`/`undefined` is no
 * limit): picking one more than the limit is refused, except that a
 * single-card choice swaps its pick. */
export function toggleChoice(chosen, id, max) {
  const list = chosen || [];
  if (list.includes(id)) return list.filter((x) => x !== id);
  const cap = max === null || max === undefined ? Infinity : max;
  if (list.length < cap) return [...list, id];
  if (cap === 1) return [id];
  return [...list];
}

/** `list` with `id` moved to position `index` of the list without it. */
export function placeInList(list, id, index) {
  const next = (list || []).filter((x) => x !== id);
  next.splice(clampIndex(index, next.length), 0, id);
  return next;
}

/** The scry/surveil/split draft after `id` is put at position `index` of
 * `lane` ("kept" or "moved"): `{moved, order}`, the two draft keys `split`
 * reads. `order` is the kept lane's order. */
export function placeInSplit(moved, order, id, lane, index) {
  const nextMoved = (moved || []).filter((x) => x !== id);
  const nextOrder = (order || []).filter((x) => x !== id);
  const target = lane === "moved" ? nextMoved : nextOrder;
  target.splice(clampIndex(index, target.length), 0, id);
  return { moved: nextMoved, order: nextOrder };
}

/** The order a trigger-ordering window shows: the drafted order once it is a
 * full permutation of the offered abilities, the offered order until then. */
export function shownTriggerOrder(pending, draft) {
  const offered = (pending && pending.cards) || [];
  const order = (draft && draft.order) || [];
  return isPermutationOf(order, offered) ? [...order] : [...offered];
}
