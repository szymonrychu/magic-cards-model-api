// The controls a small decision's `viewModel` (`decisions.js`) can ask for in
// `#prompt-box`, split out of `ribbon.js` once that file passed ~500 lines.
// One function per `control.type`, plus `renderDecision`, which wraps them
// with the title, prompt, `remaining` countdown, and submit/secondary
// buttons. The complex kinds (`split`, `order` and the multi-select
// `options` of `choose_cards`) are answered with real cards in
// `render/decision_window.js` instead, so they have no renderer here.
//
// Every control reports the player's choice back through
// `handlers.onDecisionDraft(patch)`, using the exact draft field name each
// decision kind's own `viewModel` reads it back from - `choice` (a single
// pick or free text), `card_ids` (a multi-select), `amount` (a number). Getting a field name wrong here does not fail loudly: it would
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
