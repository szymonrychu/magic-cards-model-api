// The side-panel drawer of mobile mode.
//
// On a phone or a narrow window (`style.css`'s mobile media block) the board
// takes the whole width and `#topbar`, `#banners` and `#side-panel` live in
// `#drawer`, which slides in from the right over the board. `#drawer-toggle`,
// a fixed tab in the reserved right rail, opens and closes it. This module
// only owns the `drawer-open` class on `#game` and the toggle's
// `aria-expanded`; on a desktop the class changes nothing, because there the
// drawer is a `display: contents` wrapper and its panels sit in the grid.
//
// The drawer opens by itself when a new question for this seat appears:
// `promptKey` names the question, and a key different from the last one
// opens the drawer. The same key on the next poll does not, so a player who
// closed the drawer on a question keeps it closed until a different one
// comes along.

import { windowSubject, decisionKey } from "./render/decision_window.js";

/**
 * The question this seat is being asked right now, as a string that stays the
 * same across polls for as long as the same question stands, or `null` when
 * nothing waits on this seat. In `#prompt-box`'s own priority order: a
 * finished game asks nothing; the decision window's subject (this seat's
 * mulligan, a scry, a choose-cards); this seat's CR 103.6 opening-hand turn;
 * this seat's pending decision (a popover or `#prompt-box` one, combat
 * declarations included); a cast or activation being announced in
 * `#casting-strip`; and priority held by this seat, unless an autopass run is
 * answering it - keyed by turn, step and the stack, so a new object on the
 * stack is a new question.
 */
export function promptKey(app) {
  const state = app && app.state;
  if (!state || !state.you || state.winner || state.drawn) return null;
  const you = state.you;
  const subject = windowSubject(app);
  if (subject) return `window|${subject.key}`;
  if (state.pregame) {
    const pregame = state.pregame;
    return pregame.step !== "mulligan" && pregame.opening_hand_actor === you ? `pregame|${pregame.step}|${you}` : null;
  }
  const pending = state.pending_decision;
  if (pending) return pending.player === you ? `decision|${decisionKey(pending)}` : null;
  const action = app.action;
  if (action) return `action|${action.kind}|${action.cardId}|${action.abilityIndex}`;
  if (app.run) return null;
  if (state.priority_holder !== you) return null;
  const stack = (state.stack || []).map((obj) => obj.id).join(",");
  return `priority|${state.turn_number}|${state.step}|${stack}`;
}

/**
 * Wire `#drawer-toggle` and `#game` under `root` to `store`. Returns
 * `{open(), close(), toggle(), isOpen(), unmount()}`.
 */
export function mountDrawer(root, store) {
  const game = root.querySelector("#game");
  const toggleEl = root.querySelector("#drawer-toggle");
  let open = false;
  let lastKey = null;

  const set = (value) => {
    open = Boolean(value);
    if (game) game.classList.toggle("drawer-open", open);
    if (toggleEl) {
      toggleEl.setAttribute("aria-expanded", open ? "true" : "false");
      toggleEl.setAttribute("title", open ? "Hide the side panel" : "Show the side panel");
    }
  };
  const toggle = () => set(!open);

  const follow = (app) => {
    const key = promptKey(app);
    if (key !== null && key !== lastKey) set(true);
    lastKey = key;
  };

  if (toggleEl) toggleEl.addEventListener("click", toggle);
  set(false);
  follow(store.get());
  const unsubscribe = store.subscribe(follow);

  return {
    open: () => set(true),
    close: () => set(false),
    toggle,
    isOpen: () => open,
    unmount() {
      if (toggleEl) toggleEl.removeEventListener("click", toggle);
      if (typeof unsubscribe === "function") unsubscribe();
    },
  };
}
