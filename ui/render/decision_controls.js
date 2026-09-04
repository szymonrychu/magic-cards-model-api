// The controls a live decision's `viewModel` (`decisions.js`) can ask for,
// split out of `ribbon.js` once that file passed ~500 lines. One function
// per `control.type`, plus `renderDecision`, which wraps them with the
// title, prompt, `remaining` countdown, and submit/secondary buttons.
//
// Every control reports the player's choice back through
// `handlers.onDecisionDraft(patch)`, using the exact draft field name each
// decision kind's own `viewModel` reads it back from - `choice` (a single
// pick or free text), `card_ids` (a multi-select), `amount` (a number),
// `moved`/`order` (a scry/surveil split), `order` alone (CR 603.3b's trigger
// ordering, verified with `knowledge_search` in the session that wrote this
// file). Getting a field name wrong here does not fail loudly: it would
// leave `viewModel` reading a key nobody ever writes, so the control would
// look interactive while quietly doing nothing.

import { h } from "../dom.js";
import { viewModel } from "../decisions.js";
import { activeSlotIndex } from "../targeting.js";
import { withSymbols } from "../symbols.js";

function renderOptionsControl(control, handlers) {
  const selectedIds = control.items.filter((i) => i.selected).map((i) => i.id);
  return h(
    "div",
    { class: "chip-row" },
    ...control.items.map((item) =>
      h(
        "button",
        {
          class: `chip ${item.selected ? "on" : ""}`,
          disabled: item.disabled,
          onClick: () => {
            if (control.multi) {
              const next = item.selected ? selectedIds.filter((id) => id !== item.id) : [...selectedIds, item.id];
              handlers.onDecisionDraft({ card_ids: next });
            } else {
              handlers.onDecisionDraft({ choice: item.id });
            }
          },
        },
        item.label
      )
    )
  );
}

function renderNumberControl(control, handlers) {
  return h("input", {
    type: "number",
    min: String(control.min),
    value: String(control.value),
    onChange: (e) => handlers.onDecisionDraft({ amount: Number(e.target.value) }),
  });
}

// A `choose_targets` decision's own slot - separate from the in-flight
// action's `action.slots`. Board clicks are handled by `app.js`'s
// `tryTarget`, which already toggles an id in and out of `draft.targets` -
// this only shows what has been chosen so far, per slot.
function renderTargetsControl(control, handlers) {
  const active = activeSlotIndex(control.slots);
  return h(
    "div",
    { class: "chip-row" },
    ...control.slots.map((slot, i) => {
      const names = slot.targets.map((id) => handlers.nameOf(id));
      return h(
        "div",
        { class: `chip slot-chip ${i === active ? "active-slot" : ""}` },
        names.length ? `${slot.label}: ${names.join(", ")}` : slot.label
      );
    })
  );
}

function swapped(list, i, j) {
  const next = [...list];
  [next[i], next[j]] = [next[j], next[i]];
  return next;
}

function renderSplitControl(control, handlers) {
  const movedIds = () => control.cards.filter((c) => c.moved).map((c) => c.id);
  const byId = new Map(control.cards.map((c) => [c.id, c]));
  const kept = h(
    "div",
    { class: "split-col" },
    h("div", { class: "label" }, control.keptLabel),
    ...control.order.map((id, i) => {
      const card = byId.get(id);
      return h(
        "div",
        { class: "split-row" },
        h("span", {}, card ? card.label : id),
        h("button", { disabled: i === 0, onClick: () => handlers.onDecisionDraft({ order: swapped(control.order, i, i - 1) }) }, "up"),
        h(
          "button",
          { disabled: i === control.order.length - 1, onClick: () => handlers.onDecisionDraft({ order: swapped(control.order, i, i + 1) }) },
          "down"
        ),
        h("button", { onClick: () => handlers.onDecisionDraft({ moved: [...movedIds(), id] }) }, "Move")
      );
    })
  );
  const moved = h(
    "div",
    { class: "split-col" },
    h("div", { class: "label" }, control.movedLabel),
    ...control.cards
      .filter((c) => c.moved)
      .map((card) =>
        h(
          "div",
          { class: "split-row" },
          h("span", {}, card.label),
          h("button", { onClick: () => handlers.onDecisionDraft({ moved: movedIds().filter((id) => id !== card.id) }) }, "Keep")
        )
      )
  );
  return h("div", { class: "split-columns" }, kept, moved);
}

function renderOrderControl(control, handlers) {
  const current = () =>
    control.items
      .filter((i) => i.selected)
      .sort((a, b) => a.position - b.position)
      .map((i) => i.id);
  return h(
    "div",
    {},
    h(
      "div",
      { class: "chip-row" },
      ...control.items.map((item) =>
        h(
          "button",
          {
            class: `chip ${item.selected ? "on" : ""}`,
            disabled: item.selected,
            onClick: () => handlers.onDecisionDraft({ order: [...current(), item.id] }),
          },
          item.position >= 0 ? `#${item.position + 1} ${item.label}` : item.label
        )
      )
    ),
    h("button", { onClick: () => handlers.onDecisionDraft({ order: [] }) }, "Start over")
  );
}

function renderTextControl(control, handlers) {
  return h("input", {
    type: "text",
    value: control.value,
    onInput: (e) => handlers.onDecisionDraft({ choice: e.target.value }),
  });
}

function renderControl(control, handlers) {
  if (control.type === "options") return renderOptionsControl(control, handlers);
  if (control.type === "number") return renderNumberControl(control, handlers);
  if (control.type === "targets") return renderTargetsControl(control, handlers);
  if (control.type === "split") return renderSplitControl(control, handlers);
  if (control.type === "order") return renderOrderControl(control, handlers);
  if (control.type === "text") return renderTextControl(control, handlers);
  return h("div", { class: "error" }, `no ribbon renderer for control type ${control.type}`);
}

/** A pending decision (`pending`, one seat's `state.pending_decision`) as a
 * DOM node: `decisions.js`'s own `viewModel(pending, draft)` is the single
 * source of truth for what it asks and how a draft becomes an answer - this
 * only turns its `controls` into DOM and reports choices back through
 * `handlers.onDecisionDraft`/`handlers.onDecide`. */
export function renderDecision(pending, app, handlers) {
  const vm = viewModel(pending, app.decisionDraft);
  const box = h("div", { class: "decision" }, h("h3", {}, withSymbols(app.symbols, vm.title)));
  if (vm.prompt) box.appendChild(h("div", { class: "hint" }, withSymbols(app.symbols, vm.prompt)));
  if (vm.remaining !== null && vm.remaining !== undefined) {
    box.appendChild(h("div", { class: "hint remaining" }, `${vm.remaining}s left`));
  }
  for (const control of vm.controls) box.appendChild(renderControl(control, handlers));
  const buttons = h("div", { class: "chip-row" });
  if (vm.submit) {
    buttons.appendChild(
      h("button", { class: "primary", disabled: !vm.submit.enabled, onClick: () => handlers.onDecide(vm.submit.answer) }, vm.submit.label)
    );
  }
  if (vm.secondary) {
    buttons.appendChild(h("button", { onClick: () => handlers.onDecide(vm.secondary.answer) }, vm.secondary.label));
  }
  box.appendChild(buttons);
  return box;
}
