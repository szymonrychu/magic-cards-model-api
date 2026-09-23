// The right-hand ribbon: everything the game ever asks of the player that
// is not "click something on the board" - `#prompt-box` (what is being
// asked right now, in priority order: game over, pregame, a live decision,
// or the click hint - and nothing at all while an announcement is in
// flight, because `#casting-strip` directly below it is already saying so),
// `#casting-strip` (the in-flight action's own controls, a panel in this
// ribbon rather than a row of the board), `#priority-box` (the two pass
// verbs, plus the untap/cleanup escape hatch `priority.js`'s `advanceOffered`
// explains - or, while a combat declaration is standing and possible, one
// Pass button that confirms it instead of passing priority), and
// `#stops-box` (the step ticks, `anything on the stack`, and the "Ask me to
// declare attackers"/"Ask me to declare blockers" ticks beside it). Mobile
// mode repeats the same Pass control (plain or combat-confirming) on the
// right rail (`#rail-pass`, `renderRailPass`), through the same handlers, so
// priority can be passed - or a declaration confirmed - with the drawer
// closed. Plus the one floating
// `#popover` every card-scoped "pick one of these" menu shares - anchored
// over the card it is about, drawn on top of the board rather than into it,
// so a click never changes the board's geometry (`ui/menus.js`).
//
// Concede (CR 104.3a) is rendered from here too (`renderConcedeButton`), but
// not into this ribbon: it patches `#concede-slot` in the pinned top-right
// toolbar (`ui/index.html`), beside Report and Lobby - the owner asked for
// the three to live together rather than Concede staying in `#priority-box`.
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
// ordering).
//
// The complex decisions - scry, surveil, every `choose_cards`,
// `order_triggers`, and this seat's own mulligan - are not answered here at
// all: `render/decision_window.js` answers them in a floating window with
// the real cards, and this box only says where the question went (with a
// button to bring a minimized window back). `decision_window.js`'s
// `windowSubject` is the one test of which questions those are, so the two
// surfaces can never both claim one.
//
// CR numbers here (verified with `knowledge_search` in the session that
// wrote this file): 103.5 (mulligans), 103.6 (opening-hand actions), 508.1
// (declare attackers), 509.1 (declare blockers), 118.9 (alternative costs),
// 117.3c (holding priority), 700.2 (modal spells).

import { decisionIsMine } from "../control.js";
import { attackCandidates, blockCandidates } from "../combat.js";
import { clear, h, setText } from "../dom.js";
import { flatten } from "../targeting.js";
import { colorOptions } from "../mana.js";
import { anchoredPosition } from "../menus.js";
import { withSymbols, restrictedManaRows } from "../symbols.js";
import {
  additionalCosts,
  clampX,
  faceBeingCast,
  needsExplicitFire,
  needsX,
  optionalCostOffer,
  optionalCostTimes,
  readyToFire,
  targetSlotIndex,
  xRange,
} from "../actions.js";
import { VERB_LABELS, STEPS, passButtonsFor, advanceOffered } from "../priority.js";
import { renderDecision } from "./decision_controls.js";
import { windowSubject } from "./decision_window.js";
import { popoverSubject } from "./decision_popover.js";
import { viewModel } from "../decisions.js";

// --------------------------------------------------------------- pregame ---

/** The server's own notice for a pregame move it found owed by a seat
 * nothing is playing and refused to answer, or null when there is none.
 *
 * It outranks `opponentSeatNote` below, which only ever knew what the seat's
 * process looked like: this knows what the *game* is actually held on, and
 * carries the ways out. See `ai_pregame.stall_text`. */
function pregameStallNote(pregame, you) {
  const stalled = (pregame && pregame.stalled) || [];
  const held = stalled.find((entry) => entry && entry.player !== you);
  return held ? h("div", { class: "card-warning" }, held.text) : null;
}

/** Deliberately does NOT promise that the server will make the seat's moves.
 * It moves a gone seat along only where the move has exactly one legal
 * answer (`ai_pregame.is_forced`); a real choice is held open for the seat
 * and reported by `pregameStallNote` above, because a kept hand cannot be
 * given back. */
function opponentSeatNote(status) {
  if (!status) return null;
  if (status.mechanism === "fallback" || status.state === "not_started") {
    return h("div", { class: "hint" }, `Its agent is not running (${status.reason || "not started"}).`);
  }
  if (status.state === "error") {
    return h("div", { class: "hint" }, "Its agent stopped with an error.");
  }
  if (status.state === "stopped") {
    return h("div", { class: "hint" }, "Its agent has exited.");
  }
  return h("div", { class: "hint" }, `Its agent is ${status.state}.`);
}

/** The mulligan step while the other seat is still deciding. This seat's own
 * keep-or-mulligan is `render/decision_window.js`'s, with the hand shown as
 * cards, and never reaches here (`renderPromptBox` hands it over first). */
function renderMulliganStep(state, app) {
  const pregame = state.pregame;
  const you = state.you;
  const waiting = pregame.awaiting_mulligan || [];
  const taken = (pregame.mulligans_taken || {})[you] || 0;
  const takingText = taken ? ` You have taken ${taken} mulligan${taken > 1 ? "s" : ""}.` : "";
  const others = waiting.filter((n) => n !== you);
  const who = others.length ? others.join(", ") : "the other seat";
  const box = h("div", { class: "pregame-step" }, h("span", {}, `Mulligans (CR 103.5): waiting on ${who}.${takingText}`));
  const note = pregameStallNote(pregame, you) || opponentSeatNote(app.aiStatus);
  if (note) box.appendChild(note);
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
    const note = pregameStallNote(pregame, you) || opponentSeatNote(app.aiStatus);
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
    ? renderMulliganStep(state, app)
    : renderOpeningHandStep(state, app, handlers);
}

// ------------------------------------------------------------ combat confirm
//
// The dedicated "Declare attackers (CR 508.1)"/"Declare blockers (CR 509.1)"
// prompt boxes that used to live here are gone: the owner replaced them with
// the "Ask me to declare attackers"/"Ask me to declare blockers" ticks
// `renderStopsBox` draws below. `app.js`'s own `maybeAutoAnswerCombatDeclaration`
// answers "none" the moment a declaration is impossible or the matching tick
// is off, on every poll/refresh tick - so a `declare_attackers`/
// `declare_blockers` pause (CR 508.1, CR 509.1) is only ever still standing
// by the time this file renders it because it is a real question: ticked,
// and a declaration is actually possible. `combatConfirmKind` reads exactly
// that condition, the same way `app.js`'s own check does (`ui/combat.js`'s
// `attackCandidates`/`blockCandidates` are the shared source for both - see
// that module's own note on why `GET .../legal-actions` cannot answer "is a
// declaration possible" while the pause stands: the committed board it reads
// is still frozen one step short, exactly as `baseline_seat.attack_candidates`
// documents server-side for the identical reason). There is no dedicated box
// for it any more: the player declares by clicking creatures on the board,
// same as any other combat, and `#priority-box`/`#rail-pass` swap their
// ordinary Pass verb for one that submits that draft instead
// (`combatConfirmButton` below).
function combatConfirmKind(app) {
  const state = app.state;
  const pending = state && state.pending_decision;
  if (!pending || !decisionIsMine(state, pending)) return null;
  const kind = pending.kind;
  if (kind !== "declare_attackers" && kind !== "declare_blockers") return null;
  const possible =
    kind === "declare_attackers"
      ? attackCandidates(state).length > 0
      : blockCandidates(state).length > 0;
  if (!possible) return null;
  const asked = kind === "declare_attackers" ? app.stops.askAttackers : app.stops.askBlockers;
  if (asked === false) return null;
  return kind;
}

/** The Pass button `#priority-box`/`#rail-pass` show instead of the ordinary
 * pass verbs while `combatConfirmKind` names a live combat declaration - its
 * label says plainly that this click confirms the board's own draft
 * (`app.combatDraft`, built by clicking creatures) rather than passing
 * priority: CR 508.1/CR 509.1's declarations do not use the stack and are
 * not a priority action at all, so the ordinary "Pass" verb has nothing to
 * do here. `onAttackConfirm`/`onBlockConfirm` are the very handlers the old
 * dedicated prompt box's own "Attack"/"Block" buttons called - `confirmAttack`/
 * `confirmBlocks` in `app.js` - which already submit an empty draft as "no
 * attackers"/"no blockers" (CR 508.1a/CR 509.1a's "if any"), so an empty
 * click here needs no separate handling. */
function combatConfirmButton(kind, handlers) {
  const label = kind === "declare_attackers" ? "Pass (confirm attackers)" : "Pass (confirm blockers)";
  const hint =
    kind === "declare_attackers"
      ? "Click your creatures to attack, then Pass to confirm - empty confirms no attackers."
      : "Click your creatures to block, then Pass to confirm - empty confirms no blockers.";
  const onClick = kind === "declare_attackers" ? handlers.onAttackConfirm : handlers.onBlockConfirm;
  return h("button", { type: "button", title: hint, onClick }, label);
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

/** What `#prompt-box` says while the question is up in the decision window:
 * which question, and a button that brings a minimized window back. */
function renderWindowPointer(subject, handlers) {
  let what = "Your opening hand";
  if (subject.pending && !(subject.mode === "hand" && !subject.pending.source)) {
    what = viewModel(subject.pending, {}).title;
  }
  return h(
    "div",
    { class: "decision-pointer" },
    h("div", { class: "hint" }, `${what}: answer it in the decision window.`),
    h(
      "button",
      {
        type: "button",
        onClick: () => {
          if (handlers.onShowDecisionWindow) handlers.onShowDecisionWindow();
        },
      },
      "Show decision window"
    )
  );
}

/** What `#prompt-box` says while a small decision is up in the popover
 * beside its card. */
function renderPopoverPointer(pending) {
  return h(
    "div",
    { class: "decision-pointer" },
    h("div", { class: "hint" }, `${viewModel(pending, {}).title}: answer it at the card.`)
  );
}

function renderPromptBox(el, app, handlers, root) {
  if (focusedTextInput(el)) return;
  clear(el);
  el.hidden = false;
  const state = app.state;
  if (!state) return;
  if (state.winner || state.drawn) {
    el.appendChild(
      h("div", { class: "prompt-gameover" }, state.winner ? `Game over - ${state.winner} won.` : "Game over - drawn.")
    );
    return;
  }
  const subject = windowSubject(app);
  if (subject) {
    el.appendChild(renderWindowPointer(subject, handlers));
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
  // `DECISION_KINDS` deliberately carries no entry for either - see
  // `tests/test_ui_decision_coverage.py`'s own note on this split. Routing
  // one of these two through `renderDecision` anyway would fall through
  // `viewModel`'s "no renderer for ..." placeholder instead: this seat's own
  // paused attack/block decision has to keep falling through past this
  // check rather than stop here. There is no dedicated combat screen below
  // any more (`combatConfirmKind`'s own note above) - a still-outstanding
  // one falls all the way through to the plain "click a card to play it"
  // hint, because the board itself (clicking creatures) and the Pass button
  // (`renderPriorityBox`/`renderRailPass`) are the whole of this UI now.
  const isCombatDeclaration = pending && (pending.kind === "declare_attackers" || pending.kind === "declare_blockers");
  if (pending && decisionIsMine(state, pending) && !isCombatDeclaration) {
    // Anchored beside its rendered source card (`render/decision_popover.js`);
    // otherwise it renders here, exactly as it always has.
    el.appendChild(popoverSubject(root, app) ? renderPopoverPointer(pending) : renderDecision(pending, app, handlers));
    return;
  }
  // An announcement in flight is `#casting-strip`'s, and only its
  // (`ui/menus.js`: `surfaceForChoice("announcement") === "ribbon"`). That
  // panel sits directly under this box now rather than down at the board, so
  // repeating its title and its Cancel here printed the same two things
  // twice, an inch apart - the browser pass on a real Kozilek's Command cast
  // is what made that obvious. This box stands down instead of saying it
  // again; the `return` still matters, because the "click a card to play it"
  // hint below must not speak over a live cast either.
  if (app.action) {
    el.hidden = true;
    return;
  }
  el.appendChild(h("div", { class: "hint" }, "click a card to play it"));
}

// --------------------------------------------------------------- #casting-strip

function slotIsOpen(slot) {
  const cap = slot.count === null ? Infinity : slot.count;
  return (slot.count === null || slot.minimum < (slot.count || 0)) && slot.targets.length < cap;
}

/**
 * CR 700.2's modes, one chip each - except for a mode this engine cannot
 * read (`ModeOptionView.understood: false`, `engine_api.unreadable_modes`).
 *
 * That one is drawn as a notice rather than a chip. CR 700.2a carries each
 * chosen mode out one by one, and a mode the engine reads as nothing
 * resolves doing nothing at all, so a cast naming one is refused
 * (`engine_api.unreadable_mode_notice`) - but that refusal lands *after* the
 * human has chosen targets, quoting a limitation the chip never hinted at.
 * Marking it before the choice is the same failing-closed-out-loud treatment
 * `renderRestrictedMana` gives an unread CR 106.6 restriction and
 * `renderAdditionalCosts` gives a cost the engine cannot charge: the
 * printed sentence in full, the engine's own reason beside it, and no button
 * offering to choose it.
 */
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
        m.understood === false
          ? h(
              "div",
              { class: "restricted-note unusable" },
              h("span", { class: "n" }, `${m.number}.`),
              withSymbols(symbols, m.text),
              h(
                "span",
                { class: "sentence" },
                m.notice ||
                  "this mode cannot be read by this engine, so a cast choosing it is refused"
              )
            )
          : h(
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

/** The CR 106.6 restrictions on mana floating in your own pool, printed in
 * full while a cost is being paid.
 *
 * The board's pool readout marks a restricted group and hangs the sentence on
 * its `title`, which is right for a status line. It is not right here: a
 * tooltip must not be the only place information needed to finish the task
 * lives, and "which of these four mana may actually pay for this spell" is
 * exactly that information at exactly the moment it is needed. So while the
 * strip is up the whole sentence is a step of its own, beside the count and
 * colour it applies to.
 *
 * Not a chip - nothing here is clickable. It reuses the `.tray-step` /
 * `.chip-row` shape the rest of the strip is built from so it reads as one
 * more line of the same panel. */
function renderRestrictedMana(app) {
  const state = app.state || {};
  const seat = (state.players || []).find((p) => p.username === state.you);
  const rows = restrictedManaRows(seat && seat.mana_pool);
  if (!rows.length) return null;
  return h(
    "div",
    { class: "tray-step restricted-mana" },
    h("div", { class: "label" }, "Restricted mana in your pool"),
    h(
      "div",
      { class: "chip-row" },
      ...rows.map((row) =>
        h(
          "div",
          { class: `restricted-note${row.understood ? "" : " unusable"}` },
          h("span", { class: "n" }, String(row.count)),
          withSymbols(app.symbols, `{${row.color}}`, true),
          h("span", { class: "badge badge-restricted", title: row.sentence }, row.marker),
          h("span", { class: "sentence" }, row.sentence)
        )
      )
    )
  );
}

/**
 * CR 107.3a's "Announce X" box, shown exactly when `actions.js` says this
 * announcement has an X to announce - the same predicate `readyToFire` gates
 * on, so the box can never be missing from a strip whose Cast button is
 * waiting for it (or present on one that is not) - and bounded by what the
 * engine has already said it will take.
 *
 * `needsX` reads `card.cast.needs_x`, the chosen face's own `needs_x` (CR
 * 709.3a evaluates only that half, so Toxic Deluge's "pay X life" and a
 * split card's X-less half each get the right answer), or an activated
 * ability's `needs_x` - never a re-derivation of its own.
 *
 * `rows` is the stash `app.js` keeps from the menu read that opened this
 * card's menu; the matching row's `Action.x_range` is the engine's own
 * `(minimum, maximum)` for this announcement, with `maximum` null for an X
 * nothing caps (the engine's own field says so - `models/action.py`, which
 * is also where the rule number above is read from rather than recalled).
 *
 * Without it the box was `<input type=number min=0>` with no ceiling and no
 * floor above zero, so an X the engine would refuse was typed, sent, and
 * came back as an error to read. Three things carry the bound now, because
 * each covers a different way of getting it wrong: `min`/`max` for the
 * stepper, a line of text for anyone typing (the same argument
 * `renderRestrictedMana` makes about not hiding what a task needs in a
 * `title`), and `clampX` on the way out for a value typed past both. A row
 * that says nothing leaves all three off and the box is exactly what it was.
 */
function renderXInput(card, action, handlers, rows) {
  if (!needsX(action)) return null;
  const range = xRange(action, rows);
  const [low, high] = range || [0, null];
  const attrs = {
    type: "number",
    min: String(low),
    value: action.xValue === null ? "" : String(action.xValue),
    placeholder: "X",
    // `castingStripEditInProgress` (below) keys off this to tell a live,
    // uncommitted X value apart from a share stepper's own input.
    dataset: { field: "x" },
    onChange: (e) =>
      handlers.onSetX(e.target.value === "" ? null : clampX(Number(e.target.value), range)),
  };
  if (high !== null) attrs.max = String(high);
  const step = h("div", { class: "tray-step" }, h("div", { class: "label" }, "Announce X"), h("input", attrs));
  if (range) {
    step.appendChild(
      h("div", { class: "hint" }, high === null ? `at least ${low}` : `${low} to ${high}`)
    );
  }
  return step;
}

function renderAltCosts(action, handlers) {
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
    // Which of the caster's own cards actually pay this - the engine's own
    // answer (`chosen.candidates`, `AlternativeCostView.candidates`), never
    // the whole hand: Solitude's evoke is "Exile a white card from your
    // hand", and offering a blue one is a click that comes back refused.
    const candidates = chosen.candidates || [];
    step.appendChild(h("div", { class: "hint" }, `pay it with ${chosen.requires} card(s) from your hand:`));
    step.appendChild(
      candidates.length
        ? h(
            "div",
            { class: "chip-row" },
            ...candidates.map((c) =>
              h("button", { class: `chip ${action.choosing.includes(c.id) ? "on" : ""}`, onClick: () => handlers.onTogglePitch(c.id) }, c.name)
            )
          )
        : h("div", { class: "hint" }, "nothing eligible right now")
    );
  }
  return step;
}

/**
 * CR 601.2b's optional additional costs, announced before the cast is fired -
 * the step the reported defect was missing entirely ("Sowing Mycospawn, cast
 * with kicker didn't work. I simply wasn't presented with option to pay the
 * kicker"). Read off `card.cast.additional_costs`
 * (`affordances.cast_hints`), which is the engine's own enumeration
 * (`machines/casting.optional_additional_costs`) - never a keyword list of
 * this file's own, so a mechanic the engine learns is drawn here for free.
 *
 * Three shapes, each following a pattern already in this strip:
 *
 * - a cost paid once (CR 702.33a's kicker, offspring, gift) is a chip that
 *   toggles, exactly like `renderAltCosts`' own;
 * - a cost paid "any number of times" (CR 702.33c's multikicker,
 *   CR 702.56a's replicate) is the same `<input type=number>` `Announce X`
 *   and the division steppers use, because a toggle would silently cap it
 *   at one;
 * - a cost this engine cannot charge (`understood: false`) is not offered at
 *   all - it is drawn as `renderRestrictedMana`'s own note, the printed
 *   sentence in full rather than a tooltip, so the player learns the card
 *   has an option the program cannot take instead of being shown nothing.
 */
function renderAdditionalCosts(card, action, handlers, rows) {
  const options = action.kind === "cast" ? additionalCosts(card) : [];
  if (!options.length) return null;
  const step = h(
    "div",
    { class: "tray-step additional-costs" },
    h("div", { class: "label" }, "Additional cost (CR 601.2b) - optional")
  );
  const row = h("div", { class: "chip-row" });
  for (const option of options) {
    const printed = option.cost ? `: ${option.cost}` : "";
    if (!option.understood || !option.keyword) {
      row.appendChild(
        h(
          "div",
          { class: "restricted-note unusable" },
          h("span", { class: "n" }, "!"),
          h(
            "span",
            { class: "sentence" },
            `${option.cost} (${option.rule}) - this engine cannot charge this ` +
              "printed optional cost, so it cannot be announced here"
          )
        )
      );
      continue;
    }
    const times = optionalCostTimes(action, option.keyword);
    if (option.repeatable) {
      row.appendChild(
        h(
          "label",
          { class: "share-stepper" },
          `${option.keyword}${printed}`,
          h("input", {
            type: "number",
            min: "0",
            value: String(times),
            title: option.rule,
            // Keyed like the X box and the share steppers, so
            // `castingStripEditInProgress` can tell a live, uncommitted count
            // apart from the rest of the strip's inputs.
            dataset: { field: `optional-cost:${option.keyword}` },
            onChange: (e) =>
              handlers.onSetOptionalCost(option.keyword, Number(e.target.value) || 0),
          })
        )
      );
      continue;
    }
    row.appendChild(
      h(
        "button",
        {
          class: `chip ${times ? "on" : ""}`,
          title: option.rule,
          onClick: () => handlers.onSetOptionalCost(option.keyword, times ? 0 : 1),
        },
        `${option.keyword}${printed}`
      )
    );
  }
  step.appendChild(row);
  // The engine's own verdict on each option, off `ActionRow.optional_costs`.
  // The chips above are the card's printed enumeration
  // (`card.cast.additional_costs`), which says what may be announced and
  // nothing about what this board can pay; the row prices each option with
  // this row's own cost plus that one and names, in the engine's words, why
  // one cannot be. Nothing read it, so a kicker the board could not pay
  // looked exactly like one it could and the player learned the difference
  // from the refusal.
  //
  // Said rather than enforced, deliberately: the option stays clickable,
  // because an unaffordable announcement is still worth making before
  // activating the mana ability that pays for it (`ActionRow.activate_first`
  // is the engine saying so about the cast itself), and the dry run below
  // answers affordability live anyway. This is the answer arriving before
  // the attempt instead of after it.
  for (const option of options) {
    const offer = optionalCostOffer(action, rows, option.keyword);
    if (!offer || offer.affordable !== "no") continue;
    step.appendChild(
      h(
        "div",
        { class: "hint" },
        `${option.keyword}: ${offer.unaffordable_reason || "the engine cannot pay this right now"}`
      )
    );
  }
  return step;
}

/** The `GET .../legal-actions` rows `app.js` stashed for the card this
 * announcement is about, or `[]`.
 *
 * Guarded on the card id because the stash is one card's answer, taken on
 * the click that opened its menu: reading another card's row off it would be
 * a bound, or a price, made up from the wrong board object. An announcement
 * begun some other way simply has no rows and every reader falls back. */
function actionRowsFor(app, action) {
  const stash = app.actionRows;
  if (!stash || !action || stash.cardId !== action.cardId) return [];
  return stash.rows || [];
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
 * input with `dataset.field` (`"x"`, `"share:<id>"`, or
 * `"optional-cost:<keyword>"` for a repeatable CR 601.2b announcement)
 * precisely so this can
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
  if (field.startsWith("optional-cost:")) {
    const keyword = field.slice("optional-cost:".length);
    const expected = action ? String(optionalCostTimes(action, keyword)) : "";
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

  // One header line - what is being cast and what it costs - rather than
  // three separate rows of the ribbon's column. A cast that named a face
  // (CR 709.3's half, CR 715.3's Adventure) is printed as THAT face: the
  // card view's own `name` and `mana_cost` are the printing's, which for a
  // split card is the front half's name and both halves' costs joined, and
  // saying "Cast: Wear / {1}{R} // {W}" while announcing Tear is the strip
  // disagreeing with the request it is about to send.
  const face = faceBeingCast(action);
  const heading = face ? face.name : card.name;
  const cost = face ? face.mana_cost : card.mana_cost;
  const head = h("div", { class: "strip-head" }, h("strong", {}, `${title}: ${heading}`));
  if (cost && !ability) head.appendChild(withSymbols(app.symbols, cost));
  if (ability) head.appendChild(h("span", { class: "hint" }, ability.loyalty ? `${ability.loyalty.label} ` : "", withSymbols(app.symbols, ability.text)));
  el.appendChild(head);

  // Directly under the header, above every choice the strip still wants: a
  // restriction decides whether the mana in the pool can pay for this at all,
  // so it is read before the modes, not after the Cast button.
  const restrictedStep = renderRestrictedMana(app);
  if (restrictedStep) el.appendChild(restrictedStep);

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
    // CR 702.126a, the fourth face of the same `ObjectOffer` the three above
    // come from - published by `affordances.object_costs` since it was
    // written and drawn by nothing, so Kappa Cannoneer's cast went in at its
    // full {5}{U} and came back refused.
    if (objectCosts.improvise) el.appendChild(renderObjectCostStep(action, objectCosts.improvise, "improvise", "Improvise", handlers.onToggleImprovise));
  }

  const altStep = renderAltCosts(action, handlers);
  if (altStep) el.appendChild(altStep);

  const rows = actionRowsFor(app, action);

  const additionalStep = renderAdditionalCosts(card, action, handlers, rows);
  if (additionalStep) el.appendChild(additionalStep);

  const xStep = renderXInput(card, action, handlers, rows);
  if (xStep) el.appendChild(xStep);

  const slotsStep = renderSlotChips(action, handlers);
  if (slotsStep) el.appendChild(slotsStep);

  const sharesStep = renderShareSteppers(action, handlers);
  if (sharesStep) el.appendChild(sharesStep);

  // Done / Cast / keep priority / Cancel share one wrapping line
  // (`.strip-actions`) rather than each taking a row of the 340px ribbon
  // column this panel now lives in.
  const actions = h("div", { class: "strip-actions" });
  if (!action.done && action.slots.some(slotIsOpen)) {
    actions.appendChild(h("button", { onClick: handlers.onDone }, "Done"));
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
    actions.appendChild(h("button", { class: "primary", onClick: handlers.onCast }, title));
  }

  if (action.kind === "cast" || (ability && !ability.is_mana_ability)) {
    actions.appendChild(
      h(
        "label",
        { class: "hint" },
        h("input", {
          type: "checkbox",
          // Untouched (`null`), the tick shows the standing default it follows.
          checked: action.holdPriority ?? app.stops.keepPriorityByDefault !== false,
          onChange: (e) => handlers.onHoldPriority(e.target.checked),
        }),
        " keep priority (CR 117.3c)"
      )
    );
  }

  actions.appendChild(h("button", { onClick: handlers.onCancel }, "Cancel"));
  el.appendChild(actions);
  if (action.error) el.appendChild(h("div", { class: "error" }, action.error));
}

// ------------------------------------------------------------ #priority-box

/** One pass verb's button - the same node for `#priority-box` and the rail,
 * so both make exactly the same `onVerb` call. */
function verbButton(verb, handlers) {
  return h("button", { type: "button", onClick: () => handlers.onVerb(verb) }, VERB_LABELS[verb]);
}

function renderPriorityBox(el, app, handlers) {
  clear(el);
  // A live combat declaration (`combatConfirmKind`'s own note above) takes
  // over the box outright, ahead of the `app.run` check below: `stopReason`
  // always stops a run for a pending decision addressed to this seat, so by
  // the time this is non-null any run has already ended.
  const combatKind = combatConfirmKind(app);
  if (combatKind) {
    el.appendChild(combatConfirmButton(combatKind, handlers));
    return;
  }
  if (app.run) {
    el.appendChild(h("div", { class: "hint" }, `Stop (running: ${VERB_LABELS[app.run.verb] || app.run.verb})`));
    el.appendChild(h("button", { onClick: handlers.onStopRun }, "Stop"));
  } else {
    const verbs = passButtonsFor(app.state);
    el.appendChild(h("div", { class: "chip-row" }, ...verbs.map((v) => verbButton(v, handlers))));
    // CR 502.4/514.3: nobody receives priority during untap or (most)
    // cleanup steps, so `passButtonsFor` offers nothing there at all. The
    // server crosses both for every caller (CR 500.3), so this should be
    // unreachable - it is the escape hatch for when it is not, and
    // `advanceOffered` (with its own note on why it survived the collapse to
    // two priority verbs) is the one place that decides when to show it.
    if (advanceOffered(app.state)) {
      el.appendChild(
        h("button", { class: "primary", onClick: handlers.onAdvance }, `Advance (${app.state.step.replace(/_/g, " ")})`)
      );
    }
  }
}

// ---------------------------------------------------------------- #rail-pass

/** Mobile mode's Pass and Pass until on the right rail (`style.css` shows
 * `#rail-pass` only there), so plain priority can be passed with the drawer
 * closed - `ui/drawer.js` no longer opens the drawer for it. The verbs are
 * the ones `#priority-box` offers, made through the same `onVerb`, and the
 * rail is hidden whenever that box offers none, while an autopass run is
 * answering (the box shows Stop instead), and while any decision is pending:
 * that question is answered in its own surface, and the drawer opens for it.
 *
 * A live combat declaration is the one pending decision that does NOT push
 * the rail into hiding - the owner's own words, "mobile rail pass does the
 * same as the ribbon pass": it shows the identical `combatConfirmButton`
 * `#priority-box` does, through the same `onAttackConfirm`/`onBlockConfirm`. */
export function renderRailPass(el, app, handlers) {
  if (!el) return;
  clear(el);
  const state = app.state;
  const combatKind = combatConfirmKind(app);
  if (combatKind) {
    el.appendChild(combatConfirmButton(combatKind, handlers));
    el.hidden = false;
    return;
  }
  const verbs = state && !app.run && !state.pending_decision ? passButtonsFor(state) : [];
  for (const verb of verbs) el.appendChild(verbButton(verb, handlers));
  el.hidden = verbs.length === 0;
}

// -------------------------------------------------------------- #concede-slot

/** Concede (CR 104.3a lets a player do this at any time), rendered into its
 * own slot in the pinned top-right toolbar (`ui/index.html`'s
 * `#concede-slot`, beside Report and Lobby) rather than appended to
 * `#priority-box` - the owner asked for the three to sit together, reachable
 * at every width, rather than Concede living in the ribbon and the other two
 * in the topbar.
 *
 * It takes two clicks, and it is gone once the game is over. `winner ||
 * drawn` is the same pair `stopReason` and `renderPromptBox` read as "over":
 * a draw ends the game just as finally as a win, and offering a concede
 * afterwards would be offering nothing.
 *
 * The two clicks come from the browser pass: whenever you do not hold
 * priority `passButtonsFor` offers no verbs at all, which used to slide
 * Concede up into the exact spot "Pass" had occupied a moment earlier when
 * both lived in `#priority-box` - one stray click ended a game with nothing
 * asked and nothing to undo. Moving Concede into its own fixed slot removes
 * that hazard outright (nothing else in the toolbar ever changes position
 * under it), and the arming still guards the click itself: the armed button
 * says so and looks like it (`button.danger.armed` in `style.css`), so the
 * second click is never the one you did not mean to make. */
function renderConcedeButton(el, app, handlers) {
  if (!el) return;
  clear(el);
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
  // The stack tick, above the step grid because it is not one of its columns:
  // it can fire in any step and on either player's turn, and it is live
  // alongside whatever steps are checked rather than instead of them (see
  // `priority.js`'s header). CR 117.4 resolves the top of the stack only once
  // every player has passed in succession, so the pass the run has not made
  // yet is the whole of the chance to answer what just landed - which is why
  // it starts ticked.
  el.appendChild(
    h(
      "label",
      { class: "stops-anything" },
      h("input", {
        type: "checkbox",
        checked: app.stops.onStack !== false,
        onChange: () => handlers.onToggleStackStop(),
      }),
      " anything on the stack"
    )
  );
  // Beside it, same reason: neither is a step, and both replace what used to
  // be the dedicated "Declare attackers (CR 508.1)"/"Declare blockers (CR
  // 509.1)" prompt boxes (`combatConfirmKind`'s own note above) - `app.js`'s
  // `maybeAutoAnswerCombatDeclaration` reads these, not `stopReason`, and it
  // reads them on every poll/refresh tick, not only while a run is
  // executing. Ticked (the default) with a possible declaration leaves the
  // pause standing for the board and the Pass button; unticked, or with no
  // possible declaration either way, answers "none" automatically. Absent
  // reads as ticked, like the stack tick (`priority.js`'s `loadStops`).
  el.appendChild(
    h(
      "label",
      { class: "stops-anything" },
      h("input", {
        type: "checkbox",
        checked: app.stops.askAttackers !== false,
        onChange: () => handlers.onToggleAskAttackers(),
      }),
      " Ask me to declare attackers"
    )
  );
  el.appendChild(
    h(
      "label",
      { class: "stops-anything" },
      h("input", {
        type: "checkbox",
        checked: app.stops.askBlockers !== false,
        onChange: () => handlers.onToggleAskBlockers(),
      }),
      " Ask me to declare blockers"
    )
  );
  // Beside it, not inside either step column, for the same reason: it is not
  // a step, it fires (or rather, doesn't) regardless of which one you are
  // in. On by default - casting a spell or activating an ability keeps
  // priority unless this is unticked (`priority.js`'s `DEFAULT_STOPS` comment
  // has the reasoning). Absent reads as ticked, like the stack tick.
  el.appendChild(
    h(
      "label",
      { class: "stops-anything" },
      h("input", {
        type: "checkbox",
        checked: app.stops.keepPriorityByDefault !== false,
        onChange: () => handlers.onToggleKeepPriorityByDefault(),
      }),
      " Keep priority by default"
    )
  );
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
    concedeSlot: root.querySelector("#concede-slot"),
    railPass: root.querySelector("#rail-pass"),
  };
  return store.subscribe((app) => {
    if (!app.state) return;
    renderPromptBox(els.prompt, app, handlers, root);
    renderCastingStrip(els.strip, app, handlers);
    renderPriorityBox(els.priority, app, handlers);
    renderRailPass(els.railPass, app, handlers);
    renderStopsBox(els.stops, app, handlers);
    renderConcedeButton(els.concedeSlot, app, handlers);
  });
}

// ----------------------------------------------------------------- popover
//
// The one anchored menu every card-scoped "pick one of these" shares. It is
// drawn *over* the board, never into it (`ui/menus.js`'s header has the
// card-scoped/turn-scoped split, and `menus.js`'s `anchoredPosition` the
// arithmetic): opening one changes no element's size, so no row reflows, no
// card moves under the cursor, and the page cannot grow a scrollbar.

let popoverEl = null;
let popoverAnchor = null;
let outsideHandler = null;
let keyHandler = null;
let repositionHandler = null;

function removeGlobalHandlers() {
  if (outsideHandler) document.removeEventListener("click", outsideHandler, true);
  if (keyHandler) document.removeEventListener("keydown", keyHandler, true);
  if (repositionHandler && typeof window !== "undefined" && window.removeEventListener) {
    window.removeEventListener("resize", repositionHandler);
    window.removeEventListener("scroll", repositionHandler, true);
  }
  outsideHandler = null;
  keyHandler = null;
  repositionHandler = null;
}

function allOptions(el) {
  return (el.childNodes ? [...el.childNodes] : []).filter(
    (n) => n.classList && n.classList.contains("option")
  );
}

function optionButtons(el) {
  return allOptions(el).filter((n) => !n.disabled);
}

/** Point an option button at `item` - the one place a `{label, sublabel,
 * disabled, title, onSelect}` descriptor becomes what is on screen, used
 * both to build the button and to re-aim one that is already up.
 *
 * The descriptor is held on the node rather than captured in the click
 * handler's closure, so re-aiming really does change what the button does:
 * a handler that closed over the item it was born with would keep firing the
 * stale one after an enrichment. */
function applyItem(el, item) {
  el.__item = item;
  el.disabled = Boolean(item.disabled);
  el.setAttribute("title", item.title || "");
  const children = el.childNodes ? [...el.childNodes] : [];
  const label = children.find((n) => n.nodeType === 3);
  const hint = children.find((n) => n.classList && n.classList.contains("hint"));
  if (label) setText(label, item.label);
  else el.appendChild(document.createTextNode(item.label));
  const sublabel = item.sublabel ? ` ${item.sublabel}` : "";
  if (sublabel && hint) setText(hint, sublabel);
  else if (sublabel) el.appendChild(h("span", { class: "hint" }, sublabel));
  else if (hint) el.removeChild(hint);
}

function optionNode(item) {
  const el = h("button", {
    class: "option",
    type: "button",
    onClick: () => {
      const current = el.__item;
      if (current.disabled) return;
      hidePopover();
      current.onSelect();
    },
  });
  applyItem(el, item);
  return el;
}

/** Move focus `step` options along from whichever one has it, wrapping - so
 * the menu can be driven from the keyboard without becoming a trap: nothing
 * here swallows Tab, and Escape always gets out. */
function moveFocus(el, step) {
  const options = optionButtons(el);
  if (!options.length) return;
  const active = typeof document !== "undefined" ? document.activeElement : null;
  const at = options.indexOf(active);
  const next = options[(((at < 0 ? -1 : at) + step) % options.length + options.length) % options.length];
  if (next && typeof next.focus === "function") next.focus();
}

/** Place the open menu against its anchor, in viewport coordinates. Called
 * on open and again on resize/scroll, so a menu never ends up pointing at
 * where its card used to be. */
function place() {
  if (!popoverEl || !popoverAnchor) return;
  const anchor = popoverAnchor.getBoundingClientRect();
  // Measured with the menu already visible and unconstrained, so `height` is
  // the list's natural height - what `anchoredPosition` needs to decide
  // whether it fits below, and what to cap it at when it does not.
  popoverEl.style.maxHeight = "";
  const menu = popoverEl.getBoundingClientRect();
  const viewport = {
    width: (typeof window !== "undefined" && window.innerWidth) || 1024,
    height: (typeof window !== "undefined" && window.innerHeight) || 768,
  };
  const box = anchoredPosition(anchor, { width: menu.width, height: menu.height }, viewport, {});
  popoverEl.style.left = `${Math.round(box.left)}px`;
  popoverEl.style.top = `${Math.round(box.top)}px`;
  popoverEl.style.maxHeight = `${Math.round(box.maxHeight)}px`;
  popoverEl.dataset.placement = box.placement;
}

/** Anchor a menu over `anchorEl` and list `items` as buttons -
 * `{label, sublabel, disabled, title, onSelect}` each. `opts.title` captions
 * it with the card it is about. Closes on a click outside it, on Escape, or
 * on picking something; arrow keys walk the options. */
export function showPopover(anchorEl, items, opts) {
  const el = document.querySelector("#popover");
  if (!el || !anchorEl) return;
  hidePopover();
  clear(el);
  const title = (opts && opts.title) || "";
  if (title) el.appendChild(h("div", { class: "menu-title" }, title));
  for (const item of items) el.appendChild(optionNode(item));
  el.hidden = false;
  popoverEl = el;
  popoverAnchor = anchorEl;
  if (anchorEl.classList) anchorEl.classList.add("menu-open");
  place();
  removeGlobalHandlers();
  outsideHandler = (e) => {
    if (popoverEl && !popoverEl.contains(e.target)) hidePopover();
  };
  keyHandler = (e) => {
    if (e.key === "Escape") return hidePopover();
    if (e.key === "ArrowDown") {
      e.preventDefault();
      return moveFocus(el, 1);
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      return moveFocus(el, -1);
    }
  };
  // A board row scrolls inside itself (`#mtg-board` and every
  // `.battlefield-row` are `overflow-y: auto`), so the anchor can move
  // without the window moving. `true` catches those inner scrolls too.
  repositionHandler = () => place();
  document.addEventListener("click", outsideHandler, true);
  document.addEventListener("keydown", keyHandler, true);
  if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("resize", repositionHandler);
    window.addEventListener("scroll", repositionHandler, true);
  }
  const first = optionButtons(el)[0];
  if (first && typeof first.focus === "function") first.focus();
}

/** Whether the menu currently on screen is the one anchored over
 * `anchorEl`. What an enrichment that arrived from the network asks before
 * redrawing a menu: a dialog the player has already left - Escape, a click
 * away, or picking something - must not be reopened under their cursor, and
 * a menu that has since been opened over a *different* card is not this
 * one's to redraw either. */
export function popoverIsOpenFor(anchorEl) {
  return Boolean(popoverEl && anchorEl && popoverAnchor === anchorEl);
}

/** Re-aim the menu that is ALREADY on screen at `items`, in place - the
 * redraw half of `showPopover`, and deliberately not `showPopover` itself.
 *
 * Reopening the menu would `clear(el)` it, and a browser dispatches `click`
 * to the nearest inclusive ancestor common to the mousedown and the mouseup
 * target: an option detached between the two never sees the press, so an
 * enrichment landing under the player's finger silently eats their click.
 * Rebuilding also re-runs the opening `first.focus()`, which throws a player
 * who has arrow-keyed down the list back to the top - and Enter then
 * activates an ability they never chose. Patching every option node instead
 * leaves both alone, and is the same thing `dom.js`'s keyed reconciler
 * already does for the board.
 *
 * Only ever a redraw of the same options in the same order (`menus.js`'s
 * `enrichItems` maps one item to one item, and `app.js` calls this with the
 * list it opened the menu from), so a different length is not something this
 * can patch: it leaves the menu exactly as it opened - the same silent no-op
 * every other enrichment failure gets - rather than tearing it down. The
 * return value says which happened.
 *
 * Re-placed afterwards because a sublabel arriving can make the menu taller,
 * and a menu that has grown must still be anchored where it belongs. */
export function updatePopoverItems(anchorEl, items) {
  if (!popoverIsOpenFor(anchorEl)) return false;
  const nodes = allOptions(popoverEl);
  if (nodes.length !== items.length) return false;
  items.forEach((item, i) => applyItem(nodes[i], item));
  place();
  return true;
}

export function hidePopover() {
  removeGlobalHandlers();
  if (popoverAnchor && popoverAnchor.classList) popoverAnchor.classList.remove("menu-open");
  // Focus goes back where it came from rather than being left on a node that
  // is about to be removed - a menu that is closed must not leave the page
  // with nothing focused.
  if (popoverAnchor && popoverEl && typeof document !== "undefined" && popoverEl.contains(document.activeElement)) {
    if (typeof popoverAnchor.focus === "function") popoverAnchor.focus();
    else if (document.body && typeof document.body.focus === "function") document.body.focus();
  }
  if (popoverEl) {
    popoverEl.hidden = true;
    popoverEl.style.maxHeight = "";
    clear(popoverEl);
  }
  popoverEl = null;
  popoverAnchor = null;
}
