// The right-hand ribbon: everything the game ever asks of the player that
// is not "click something on the board" - `#prompt-box` (what is being
// asked right now, in priority order: game over, pregame, a live decision,
// the in-flight action's one-line summary, the combat prompt, or the click
// hint), `#casting-strip` (the in-flight action's own controls, staged above
// the hand), `#priority-box` (the pass-priority verbs), and `#stops-box`
// (the autopilot stop conditions). Plus the one floating `#popover` every
// inline "pick one of these" menu shares.
//
// Every button here fires a request the API already documents; nothing in
// this file invents a rule. `decisions.js`'s `viewModel` is the single
// source of truth for what a pending decision asks and how a draft becomes
// an answer - the actual per-`control.type` rendering lives in
// `decision_controls.js` (split out once this file passed ~500 lines), which
// only turns `controls` into DOM and reports the player's choices back
// through `onDecisionDraft`, never re-deriving them.
//
// `onDecisionDraft`'s draft field names are the ones `decisions.js` itself
// reads per kind: `choice` (a single pick or free text), `card_ids` (a
// multi-select), `amount` (a number), `targets` (a `choose_targets`
// decision's own slot, separate from the in-flight action's `action.slots`),
// `moved`/`order` (a scry/surveil split), `order` alone (CR 603.3b's trigger
// ordering). The pregame mulligan panel reuses the same draft and handler
// for its "tick which cards go to the bottom" checkboxes, under the key
// `bottom` - it is not a `pending_decision` at all (CR 103.5/103.6 run before
// any decision machinery exists), but the shape of the problem ("remember a
// set of ids until the player submits") is identical, and inventing a
// second store field for it would only be able to disagree with this one.
//
// CR numbers here (verified with `knowledge_search` in the session that
// wrote this file): 103.5 (mulligans), 103.6 (opening-hand actions), 508.1
// (declare attackers), 509.1 (declare blockers), 118.9 (alternative costs),
// 117.3c (holding priority), 700.2 (modal spells).

import { clear, h } from "../dom.js";
import { flatten } from "../targeting.js";
import { colorOptions } from "../mana.js";
import { withSymbols } from "../symbols.js";
import { needsExplicitFire, readyToFire, targetSlotIndex } from "../actions.js";
import { VERB_LABELS, STEPS, passButtonsFor, advanceOffered } from "../priority.js";
import { renderDecision } from "./decision_controls.js";

// --------------------------------------------------------------- pregame ---

function opponentSeatNote(status) {
  if (!status) return null;
  if (status.mechanism === "fallback" || status.state === "not_started") {
    return h(
      "div",
      { class: "hint" },
      `Its agent is not running (${status.reason || "not started"}) - the server will make its pregame moves shortly.`
    );
  }
  if (status.state === "error") {
    return h("div", { class: "hint" }, "Its agent stopped with an error - the server will make its pregame moves shortly.");
  }
  if (status.state === "stopped") {
    return h("div", { class: "hint" }, "Its agent has exited - the server will make its pregame moves shortly.");
  }
  return h("div", { class: "hint" }, `Its agent is ${status.state}.`);
}

function renderMulliganStep(state, app, handlers) {
  const pregame = state.pregame;
  const you = state.you;
  const waiting = pregame.awaiting_mulligan || [];
  const taken = (pregame.mulligans_taken || {})[you] || 0;
  const takingText = taken ? ` You have taken ${taken} mulligan${taken > 1 ? "s" : ""}.` : "";
  if (!waiting.includes(you)) {
    const others = waiting.filter((n) => n !== you);
    const who = others.length ? others.join(", ") : "the other seat";
    const box = h("div", { class: "pregame-step" }, h("span", {}, `Mulligans (CR 103.5): waiting on ${who}.${takingText}`));
    const note = opponentSeatNote(app.aiStatus);
    if (note) box.appendChild(note);
    return box;
  }
  const needed = (pregame.cards_to_bottom || {})[you] || 0;
  const hand = (state.hands && state.hands[you]) || [];
  const chosenBottom = app.decisionDraft.bottom || [];
  const box = h(
    "div",
    { class: "pregame-step" },
    h("span", {}, `Your opening hand (CR 103.5): keep it, or shuffle back for a new seven.${takingText}`)
  );
  if (needed) {
    box.appendChild(
      h(
        "div",
        { class: "pregame-bottom" },
        h("span", {}, `Keeping costs ${needed} card${needed > 1 ? "s" : ""} on the bottom of your library - tick exactly ${needed}:`),
        ...hand.map((card) =>
          h(
            "label",
            { class: "option" },
            h("input", {
              type: "checkbox",
              checked: chosenBottom.includes(card.id),
              onChange: () => {
                const next = chosenBottom.includes(card.id)
                  ? chosenBottom.filter((id) => id !== card.id)
                  : [...chosenBottom, card.id];
                handlers.onDecisionDraft({ bottom: next });
              },
            }),
            card.name
          )
        )
      )
    );
  }
  const canKeep = chosenBottom.length === needed;
  box.appendChild(
    h(
      "button",
      {
        class: "primary",
        disabled: !canKeep,
        title: canKeep ? null : `tick exactly ${needed} card(s) first`,
        onClick: () => {
          const body = { player: you, take: false };
          if (chosenBottom.length) body.bottom = chosenBottom;
          handlers.onDecisionDraft({ bottom: [] });
          handlers.onPregame("mulligan", body);
        },
      },
      "Keep"
    )
  );
  if (pregame.can_mulligan && pregame.can_mulligan[you]) {
    box.appendChild(
      h(
        "button",
        {
          onClick: () => {
            handlers.onDecisionDraft({ bottom: [] });
            handlers.onPregame("mulligan", { player: you, take: true });
          },
        },
        "Mulligan"
      )
    );
  } else {
    box.appendChild(h("span", { class: "hint" }, "no mulligan left - a hand of nothing is the floor (CR 103.5)"));
  }
  return box;
}

function renderOpeningHandStep(state, app, handlers) {
  const pregame = state.pregame;
  const you = state.you;
  if (pregame.opening_hand_actor !== you) {
    const who = pregame.opening_hand_actor;
    const box = h(
      "div",
      { class: "pregame-step" },
      h("span", {}, `Opening-hand actions (CR 103.6): waiting on ${who || "the other seat"}.`)
    );
    const note = opponentSeatNote(app.aiStatus);
    if (note) box.appendChild(note);
    return box;
  }
  const options = (pregame.opening_hand_options || {})[you] || [];
  const box = h(
    "div",
    { class: "pregame-step" },
    h("span", {}, "Opening-hand actions (CR 103.6) - yours to take, in any order.")
  );
  if (!options.length) box.appendChild(h("span", { class: "hint" }, "nothing in your opening hand offers one."));
  for (const option of options) {
    box.appendChild(
      h(
        "button",
        { onClick: () => handlers.onPregame("opening-hand/action", { player: you, card_id: option.card_id }) },
        `${option.source}: ${option.prompt}`
      )
    );
  }
  box.appendChild(
    h("button", { class: "primary", onClick: () => handlers.onPregame("opening-hand/finish", { player: you }) }, "Done")
  );
  return box;
}

function renderPregame(state, app, handlers) {
  return state.pregame.step === "mulligan"
    ? renderMulliganStep(state, app, handlers)
    : renderOpeningHandStep(state, app, handlers);
}

// ------------------------------------------------------------- combat prompt

function renderDeclareAttackers(app, handlers) {
  const entries = Object.entries(app.combatDraft || {});
  const box = h("div", { class: "combat-prompt" }, h("div", { class: "hint" }, "Declare attackers (CR 508.1)"));
  box.appendChild(
    entries.length
      ? h("ul", { class: "combat-list" }, ...entries.map(([atk, def]) => h("li", {}, `${handlers.nameOf(atk)} -> ${handlers.nameOf(def)}`)))
      : h("div", { class: "hint" }, "click your creatures to attack")
  );
  box.appendChild(
    h(
      "div",
      { class: "chip-row" },
      h("button", { class: "primary", disabled: entries.length === 0, onClick: handlers.onAttackConfirm }, "Attack"),
      h("button", { onClick: handlers.onAttackNone }, "No attack")
    )
  );
  return box;
}

function renderDeclareBlockers(app, handlers) {
  const entries = Object.entries(app.combatDraft || {});
  const box = h("div", { class: "combat-prompt" }, h("div", { class: "hint" }, "Declare blockers (CR 509.1)"));
  box.appendChild(
    entries.length
      ? h("ul", { class: "combat-list" }, ...entries.map(([blocker, attacker]) => h("li", {}, `${handlers.nameOf(blocker)} blocks ${handlers.nameOf(attacker)}`)))
      : h("div", { class: "hint" }, "click one of your creatures, then the attacker it blocks")
  );
  box.appendChild(
    h(
      "div",
      { class: "chip-row" },
      h("button", { class: "primary", disabled: entries.length === 0, onClick: handlers.onBlockConfirm }, "Block"),
      h("button", { onClick: handlers.onBlockNone }, "No blocks")
    )
  );
  return box;
}

// A `declare_attackers`/`declare_blockers` decision paused (CR 508.1, CR
// 509.1) freezes `state.step` at whatever it was *before* the pause, not the
// one it is trying to enter (`app.py`'s `advance` docstring) - so
// `state.step === "declare_attackers"` alone can only ever be true *after*
// the decision is already answered, never while it is genuinely outstanding.
// Reading `state.pending_decision` alongside it is what lets this prompt
// show up for the moment it actually exists to cover: without it, a human
// player has no combat UI at all for as long as the game is paused waiting
// on them, which is every combat now that nothing auto-defaults to "nobody"
// any more.
function renderCombatPrompt(state, app, handlers) {
  const you = state.you;
  const pending = state.pending_decision;
  const pendingKind = pending && pending.player === you ? pending.kind : null;
  const attackersPending =
    (state.step === "declare_attackers" && state.active_player === you) ||
    pendingKind === "declare_attackers";
  const blockersPending =
    (state.step === "declare_blockers" && state.active_player !== you) ||
    pendingKind === "declare_blockers";
  if (attackersPending) return renderDeclareAttackers(app, handlers);
  if (blockersPending) return renderDeclareBlockers(app, handlers);
  return null;
}

// ------------------------------------------------------------------ #prompt-box

/**
 * Whether `document.activeElement` is a real, typed-into text control
 * (`INPUT[type=text/number/...]`, `TEXTAREA`) somewhere inside `el` right
 * now - never a checkbox or radio, which hold nothing typed. `renderPromptBox`
 * below skips its own repaint while this is true, the way the old classic
 * `ui/app.js`'s `setHtml` was meant to (its actual bug: it skipped for
 * *any* focused INPUT, checkbox and radio included, so ticking CR 103.5's
 * bottoming checkbox froze the very repaint that would have enabled "Keep").
 * `#prompt-box` is fully torn down and rebuilt (`clear` + fresh `h()` nodes)
 * on every store update rather than patched node-by-node, so the only way to
 * keep a half-typed free-text decision answer (CR 201.4/205.3e's "name a
 * card"/"name a creature type", `decision_controls.js`'s `renderTextControl`)
 * from losing its cursor on the next ~1.2s poll tick is to not touch the DOM
 * at all while it holds focus - a checkbox tick still repaints normally,
 * since its type is excluded here.
 */
function focusedTextInput(el) {
  const active = typeof document !== "undefined" ? document.activeElement : null;
  if (!active || typeof el.contains !== "function" || !el.contains(active)) return false;
  // A real DOM's `Element.tagName` is always upper-cased, whatever case the
  // element was created with (`document.createElement("input")` still
  // reports "INPUT") - `.toUpperCase()` here makes the check correct there
  // and tolerant of a fake DOM (`tests/test_ui_pregame_panel.py`) that does
  // not bother normalizing it.
  const tag = (active.tagName || "").toUpperCase();
  if (tag === "TEXTAREA") return true;
  if (tag === "INPUT") {
    const type = (active.type || (active.attrs && active.attrs.type) || "text").toLowerCase();
    return type !== "checkbox" && type !== "radio";
  }
  return false;
}

function actionTitle(action) {
  if (action.kind === "land") return "Play land";
  if (action.kind === "crew") return "Crew";
  if (action.kind === "special") return action.keyword;
  const ability = action.abilityIndex !== null ? (action.card.abilities || [])[action.abilityIndex] : null;
  return ability ? "Activate" : "Cast";
}

function renderPromptBox(el, app, handlers) {
  if (focusedTextInput(el)) return;
  clear(el);
  const state = app.state;
  if (!state) return;
  if (state.winner || state.drawn) {
    el.appendChild(
      h("div", { class: "prompt-gameover" }, state.winner ? `Game over - ${state.winner} won.` : "Game over - drawn.")
    );
    return;
  }
  if (state.pregame) {
    el.appendChild(renderPregame(state, app, handlers));
    return;
  }
  const pending = state.pending_decision;
  // `declare_attackers`/`declare_blockers` reach `pending_decision` now
  // (pausing put both on the one tape every other kind already answers
  // through - `decisions.py`'s module docstring), but `ui/decisions.js`'s
  // `DECISION_KINDS` deliberately carries no entry for either: the dedicated
  // combat screen below already exists (`renderCombatPrompt`), driven by
  // `attackBody`/`blockBody` in `ui/combat.js` - see
  // `tests/test_ui_decision_coverage.py`'s own note on this split. Routing
  // one of these two through `renderDecision` anyway would fall through
  // `viewModel`'s "no renderer for ..." placeholder instead: this seat's own
  // paused attack/block decision has to keep falling through to
  // `renderCombatPrompt` below, not stop here.
  const isCombatDeclaration = pending && (pending.kind === "declare_attackers" || pending.kind === "declare_blockers");
  if (pending && pending.player === state.you && !isCombatDeclaration) {
    el.appendChild(renderDecision(pending, app, handlers));
    return;
  }
  if (app.action) {
    el.appendChild(
      h(
        "div",
        { class: "prompt-action-summary" },
        h("span", {}, `${actionTitle(app.action)}: ${app.action.card.name}`),
        h("button", { onClick: handlers.onCancel }, "Cancel")
      )
    );
    return;
  }
  const combat = renderCombatPrompt(state, app, handlers);
  if (combat) {
    el.appendChild(combat);
    return;
  }
  el.appendChild(h("div", { class: "hint" }, "click a card to play it"));
}

// --------------------------------------------------------------- #casting-strip

function slotIsOpen(slot) {
  const cap = slot.count === null ? Infinity : slot.count;
  return (slot.count === null || slot.minimum < (slot.count || 0)) && slot.targets.length < cap;
}

function renderModeChips(card, action, handlers, symbols) {
  const modes = card.cast && card.cast.modes;
  if (!modes || action.kind !== "cast") return null;
  const range = modes.minimum === modes.maximum ? String(modes.minimum) : `${modes.minimum}-${modes.maximum}`;
  return h(
    "div",
    { class: "tray-step" },
    h("div", { class: "label" }, `${modes.instruction} (${range})`),
    h(
      "div",
      { class: "chip-row" },
      ...modes.options.map((m) =>
        h(
          "button",
          { class: `chip ${action.modes.includes(m.number) ? "on" : ""}`, onClick: () => handlers.onToggleMode(m.number) },
          `${m.number}. `,
          withSymbols(symbols, m.text)
        )
      )
    )
  );
}

function renderColorChips(card, action, handlers, symbols) {
  if (action.abilityIndex === null) return null;
  const colors = colorOptions(card, action.abilityIndex);
  if (colors.length <= 1) return null;
  return h(
    "div",
    { class: "tray-step" },
    h("div", { class: "label" }, "Which colour"),
    h(
      "div",
      { class: "chip-row" },
      ...colors.map((c) =>
        h("button", { class: `chip ${action.color === c ? "on" : ""}`, onClick: () => handlers.onChooseColor(c) }, withSymbols(symbols, `{${c}}`, true))
      )
    )
  );
}

function renderXInput(card, action, handlers) {
  const ability = action.abilityIndex !== null ? (card.abilities || [])[action.abilityIndex] : null;
  const needsX = ability ? Boolean(ability.needs_x) : Boolean(card.cast && card.cast.needs_x);
  if (!needsX) return null;
  return h(
    "div",
    { class: "tray-step" },
    h("div", { class: "label" }, "Announce X"),
    h("input", {
      type: "number",
      min: "0",
      value: action.xValue === null ? "" : String(action.xValue),
      placeholder: "X",
      // `castingStripEditInProgress` (below) keys off this to tell a live,
      // uncommitted X value apart from a share stepper's own input.
      dataset: { field: "x" },
      onChange: (e) => handlers.onSetX(e.target.value === "" ? null : Number(e.target.value)),
    })
  );
}

function renderAltCosts(action, app, handlers) {
  const alts = (action.affordance && action.affordance.alternative_costs) || [];
  if (!alts.length || action.kind !== "cast") return null;
  const chips = alts.map((alt) => {
    const label = `${alt.keyword || alt.source_name}: ${alt.description}`;
    if (!alt.available) return h("button", { class: "chip", disabled: true, title: alt.unavailable || "" }, label);
    return h(
      "button",
      {
        class: `chip ${action.altCost === alt.index ? "on" : ""}`,
        title: alt.rule,
        onClick: () => handlers.onChooseAltCost(action.altCost === alt.index ? null : alt.index),
      },
      label
    );
  });
  const step = h("div", { class: "tray-step" }, h("div", { class: "label" }, "Alternative cost (CR 118.9) - optional"), h("div", { class: "chip-row" }, ...chips));
  const chosen = alts.find((a) => a.index === action.altCost);
  if (chosen && chosen.requires) {
    const hand = ((app.state.hands || {})[app.state.you] || []).filter((c) => c.id !== action.cardId);
    step.appendChild(h("div", { class: "hint" }, `pay it with ${chosen.requires} card(s) from your hand:`));
    step.appendChild(
      h(
        "div",
        { class: "chip-row" },
        ...hand.map((c) =>
          h("button", { class: `chip ${action.choosing.includes(c.id) ? "on" : ""}`, onClick: () => handlers.onTogglePitch(c.id) }, c.name)
        )
      )
    );
  }
  return step;
}

function renderObjectCostStep(action, offer, field, label, onToggle) {
  const body = offer.candidates.length
    ? h(
        "div",
        { class: "chip-row" },
        ...offer.candidates.map((c) =>
          h("button", { class: `chip ${action[field].includes(c.id) ? "on" : ""}`, onClick: () => onToggle(c.id) }, c.name)
        )
      )
    : h("div", { class: "hint" }, "nothing eligible right now");
  return h(
    "div",
    { class: "tray-step" },
    h("div", { class: "label" }, `${label} (${action[field].length} chosen) - optional`),
    body,
    h("div", { class: "hint" }, offer.rule)
  );
}

function renderCrewProgress(action, offer) {
  const chosen = offer.candidates.filter((c) => action.crewing.includes(c.id));
  const total = chosen.reduce((sum, c) => sum + (c.power || 0), 0);
  return h(
    "div",
    { class: "tray-step" },
    h("div", { class: "label" }, `Crew ${offer.number} - power ${total}/${offer.number}`),
    h("div", { class: "hint" }, `click creatures on the board to crew (${offer.rule})`)
  );
}

function renderSlotChips(action, handlers) {
  if (!action.affordance || !action.affordance.targets_anything || !action.slots.length) return null;
  // A slot chip click (`handlers.onFocusSlot`) sets `action.focusSlot`;
  // `actions.js`'s `targetSlotIndex` is the single place that resolves it
  // against `activeSlotIndex`'s own guess - `app.js`'s click handler reads
  // the exact same helper, so the highlighted slot here is always the slot
  // a board click actually fills.
  const active = targetSlotIndex(action);
  return h(
    "div",
    { class: "tray-step" },
    h("div", { class: "label" }, `Targets (${flatten(action.slots).length})`),
    h(
      "div",
      { class: "chip-row" },
      ...action.slots.map((slot, i) =>
        h(
          "div",
          { class: `chip slot-chip ${i === active ? "active-slot" : ""}`, onClick: () => handlers.onFocusSlot(i) },
          h("span", {}, slot.targets.length ? `${slot.label}: ${slot.targets.map((id) => handlers.nameOf(id)).join(", ")}` : slot.label),
          ...slot.targets.map((id) =>
            h(
              "button",
              {
                class: "unfill",
                onClick: (e) => {
                  e.stopPropagation();
                  handlers.onUnfill(i, id);
                },
              },
              "x"
            )
          )
        )
      )
    )
  );
}

function renderShareSteppers(action, handlers) {
  if (!action.affordance || !action.affordance.division) return null;
  const targets = flatten(action.slots);
  if (!targets.length) return null;
  const division = action.affordance.division;
  return h(
    "div",
    { class: "tray-step" },
    h("div", { class: "label" }, `Divide ${division.amount}${division.what ? ` ${division.what}` : ""}`),
    h(
      "div",
      { class: "chip-row" },
      ...targets.map((id) =>
        h(
          "label",
          { class: "share-stepper" },
          handlers.nameOf(id),
          h("input", {
            type: "number",
            min: "1",
            value: String(action.shares[id] || 1),
            // Keyed by the target's own id, not just "a share input", so
            // `castingStripEditInProgress` can tell which target's live edit
            // it is looking at when more than one share stepper is on screen.
            dataset: { field: `share:${id}` },
            onChange: (e) => handlers.onSetShare(id, Number(e.target.value)),
          })
        )
      )
    )
  );
}

/**
 * Whether `#casting-strip` has a live, uncommitted edit sitting in a focused
 * X or share-stepper `<input type=number>` right now - `renderCastingStrip`
 * skips its repaint while this is true, the same shape `renderPromptBox`'s
 * own `focusedTextInput` protects a half-typed free-text decision answer
 * with. `renderCastingStrip` runs on every store update (a ~1.2s poll tick
 * included), and it tears the strip down and rebuilds it wholesale
 * (`clear` + fresh `h()` nodes) same as `renderPromptBox` does - so a poll
 * landing while the player is mid-keystroke on the X input used to destroy
 * that input and start it over blank.
 *
 * Unlike `focusedTextInput` (which blocks on focus alone, because
 * `#prompt-box`'s controls have no notion of "already in sync"), this also
 * checks whether the input's live value has actually diverged from what
 * `action` says it should be: `renderXInput`/`renderShareSteppers` tag their
 * input with `dataset.field` (`"x"`, or `"share:<id>"`) precisely so this can
 * ask "does this field's live text still match the store" rather than
 * "is any input here focused at all". Once an `onChange` has landed and the
 * two agree again, the guard steps aside - continuing to block on focus
 * alone would freeze the rest of the strip (a fresh dry-run preview, a newly
 * legal Cast button) behind an input that simply still happens to hold
 * focus.
 */
function castingStripEditInProgress(el, action) {
  const active = typeof document !== "undefined" ? document.activeElement : null;
  if (!active || typeof el.contains !== "function" || !el.contains(active)) return false;
  const tag = (active.tagName || "").toUpperCase();
  if (tag !== "INPUT") return false;
  const type = (active.type || (active.attrs && active.attrs.type) || "text").toLowerCase();
  if (type !== "number" && type !== "text") return false;
  const field = (active.dataset && active.dataset.field) || "";
  const live = active.value === undefined || active.value === null ? "" : String(active.value);
  if (field === "x") {
    const expected = action && action.xValue !== null && action.xValue !== undefined ? String(action.xValue) : "";
    return live !== expected;
  }
  if (field.startsWith("share:")) {
    const id = field.slice("share:".length);
    const expected = action ? String((action.shares && action.shares[id]) || 1) : "";
    return live !== expected;
  }
  return false;
}

function renderCastingStrip(el, app, handlers) {
  const action = app.action;
  if (castingStripEditInProgress(el, action)) return;
  clear(el);
  if (!action) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const card = action.card;
  const ability = action.abilityIndex !== null ? (card.abilities || [])[action.abilityIndex] : null;
  const title = actionTitle(action);

  el.appendChild(h("strong", {}, `${title}: ${card.name}`));
  if (card.mana_cost && !ability) el.appendChild(withSymbols(app.symbols, card.mana_cost));
  if (ability) el.appendChild(h("span", { class: "hint" }, ability.loyalty ? `${ability.loyalty.label} ` : "", withSymbols(app.symbols, ability.text)));

  const modeStep = renderModeChips(card, action, handlers, app.symbols);
  if (modeStep) el.appendChild(modeStep);

  const colorStep = renderColorChips(card, action, handlers, app.symbols);
  if (colorStep) el.appendChild(colorStep);

  const objectCosts = (action.affordance && action.affordance.object_costs) || {};
  if (action.kind === "crew" && objectCosts.crew) {
    el.appendChild(renderCrewProgress(action, objectCosts.crew));
  } else if (action.kind === "cast") {
    if (objectCosts.convoke) el.appendChild(renderObjectCostStep(action, objectCosts.convoke, "convoke", "Convoke", handlers.onToggleConvoke));
    if (objectCosts.delve) el.appendChild(renderObjectCostStep(action, objectCosts.delve, "delve", "Delve", handlers.onToggleDelve));
  }

  const altStep = renderAltCosts(action, app, handlers);
  if (altStep) el.appendChild(altStep);

  const xStep = renderXInput(card, action, handlers);
  if (xStep) el.appendChild(xStep);

  const slotsStep = renderSlotChips(action, handlers);
  if (slotsStep) el.appendChild(slotsStep);

  const sharesStep = renderShareSteppers(action, handlers);
  if (sharesStep) el.appendChild(sharesStep);

  if (!action.done && action.slots.some(slotIsOpen)) {
    el.appendChild(h("button", { onClick: handlers.onDone }, "Done"));
  }

  // `needsExplicitFire` fires whenever pinned mana, a division, or any
  // "up to"/unbounded slot is in play - the last one holds even once that
  // slot is filled to its cap (an "up to three targets" spell sitting at
  // three chosen still waits for Cast, not an automatic fire), which is why
  // this is a separate check from `slotIsOpen`'s "Done" gate above.
  //
  // `action.error` joins it because of the browser pass: a dry run that came
  // back refused leaves the strip complete but unfired, and with nothing to
  // click. Fix what the refusal named - tap a land, add the missing source -
  // and there was no way to try again short of cancelling the whole
  // announcement and rebuilding it. A refusal is exactly the moment the
  // player needs the button.
  if (readyToFire(action) && (needsExplicitFire(action) || action.error)) {
    el.appendChild(h("button", { class: "primary", onClick: handlers.onCast }, title));
  }

  if (action.kind === "cast" || (ability && !ability.is_mana_ability)) {
    el.appendChild(
      h(
        "label",
        { class: "hint" },
        h("input", { type: "checkbox", checked: action.holdPriority, onChange: (e) => handlers.onHoldPriority(e.target.checked) }),
        " keep priority (CR 117.3c)"
      )
    );
  }

  el.appendChild(h("button", { onClick: handlers.onCancel }, "Cancel"));
  if (action.error) el.appendChild(h("div", { class: "error" }, action.error));
}

// ------------------------------------------------------------ #priority-box

function renderPriorityBox(el, app, handlers) {
  clear(el);
  if (app.run) {
    el.appendChild(h("div", { class: "hint" }, `Stop (running: ${VERB_LABELS[app.run.verb] || app.run.verb})`));
    el.appendChild(h("button", { onClick: handlers.onStopRun }, "Stop"));
  } else {
    const verbs = passButtonsFor(app.state);
    el.appendChild(h("div", { class: "chip-row" }, ...verbs.map((v) => h("button", { onClick: () => handlers.onVerb(v) }, VERB_LABELS[v]))));
    // CR 502.4/514.3: nobody receives priority during untap or (most)
    // cleanup steps, so `passButtonsFor` offers nothing there at all - the
    // active player's own way past either is `POST .../advance`
    // (`engine_api.advance`), not a pass. `advanceOffered` is the one place
    // that decides when to show it.
    if (advanceOffered(app.state)) {
      el.appendChild(
        h("button", { class: "primary", onClick: handlers.onAdvance }, `Advance (${app.state.step.replace(/_/g, " ")})`)
      );
    }
  }
  // CR 104.3a lets a player concede at any time, so the button is here for as
  // long as the game is live - but it takes two clicks, and it is gone once
  // the game is over. `winner || drawn` is the same pair `stopReason` and
  // `renderPromptBox` read as "over": a draw ends the game just as finally as
  // a win, and offering a concede afterwards would be offering nothing.
  //
  // The two clicks come from the browser pass: whenever you do not hold
  // priority `passButtonsFor` offers no verbs at all, which slid Concede up
  // into the exact spot "Pass" had occupied a moment earlier. One stray click
  // ended that game with nothing asked and nothing to undo. The armed button
  // says so and looks like it (`button.danger.armed` in `style.css`), so the
  // second click is never the one you did not mean to make.
  if (app.state && (app.state.winner || app.state.drawn)) return;
  el.appendChild(
    h(
      "button",
      { class: app.concedeArmed ? "danger armed" : "ghost danger", onClick: handlers.onConcede },
      app.concedeArmed ? "Concede - click again to confirm" : "Concede"
    )
  );
}

// --------------------------------------------------------------- #stops-box

function renderStopsBox(el, app, handlers) {
  clear(el);
  const steps = STEPS.filter((s) => s !== "untap" && s !== "cleanup");
  const grid = h("div", { class: "stops-grid" }, h("div", {}, "my turn"), h("div", {}, "their turn"));
  for (const step of steps) {
    const label = step.replace(/_/g, " ");
    grid.appendChild(
      h(
        "label",
        {},
        h("input", { type: "checkbox", checked: app.stops.mine.includes(step), onChange: () => handlers.onToggleStop("mine", step) }),
        ` ${label}`
      )
    );
    grid.appendChild(
      h(
        "label",
        {},
        h("input", { type: "checkbox", checked: app.stops.theirs.includes(step), onChange: () => handlers.onToggleStop("theirs", step) }),
        ` ${label}`
      )
    );
  }
  el.appendChild(grid);
}

// -------------------------------------------------------------------- mount

/** Mount the ribbon into `root` (normally `document`) and return the
 * `store.subscribe` unsubscribe function. See this file's own header for the
 * full `handlers` shape. */
export function mountRibbon(root, store, handlers) {
  const els = {
    prompt: root.querySelector("#prompt-box"),
    strip: root.querySelector("#casting-strip"),
    priority: root.querySelector("#priority-box"),
    stops: root.querySelector("#stops-box"),
  };
  return store.subscribe((app) => {
    if (!app.state) return;
    renderPromptBox(els.prompt, app, handlers);
    renderCastingStrip(els.strip, app, handlers);
    renderPriorityBox(els.priority, app, handlers);
    renderStopsBox(els.stops, app, handlers);
  });
}

// ----------------------------------------------------------------- popover

let popoverEl = null;
let outsideHandler = null;
let escHandler = null;

function removeGlobalHandlers() {
  if (outsideHandler) document.removeEventListener("click", outsideHandler, true);
  if (escHandler) document.removeEventListener("keydown", escHandler, true);
  outsideHandler = null;
  escHandler = null;
}

/** Position `#popover` under `anchorEl`'s bounding rect and list `items` as
 * buttons - `{label, sublabel, disabled, title, onSelect}` each. Hides on a
 * click outside it or Escape. */
export function showPopover(anchorEl, items) {
  const el = document.querySelector("#popover");
  if (!el) return;
  clear(el);
  for (const item of items) {
    el.appendChild(
      h(
        "button",
        {
          class: "option",
          disabled: Boolean(item.disabled),
          title: item.title || "",
          onClick: () => {
            if (item.disabled) return;
            hidePopover();
            item.onSelect();
          },
        },
        item.label,
        item.sublabel ? h("span", { class: "hint" }, ` ${item.sublabel}`) : null
      )
    );
  }
  const rect = anchorEl.getBoundingClientRect();
  el.style.left = `${rect.left + (window.scrollX || 0)}px`;
  el.style.top = `${rect.bottom + (window.scrollY || 0)}px`;
  el.hidden = false;
  popoverEl = el;
  removeGlobalHandlers();
  outsideHandler = (e) => {
    if (popoverEl && !popoverEl.contains(e.target)) hidePopover();
  };
  escHandler = (e) => {
    if (e.key === "Escape") hidePopover();
  };
  document.addEventListener("click", outsideHandler, true);
  document.addEventListener("keydown", escHandler, true);
}

export function hidePopover() {
  removeGlobalHandlers();
  if (popoverEl) {
    popoverEl.hidden = true;
    clear(popoverEl);
  }
  popoverEl = null;
}
