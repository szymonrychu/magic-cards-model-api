// The decision popover: a SMALL decision (a yes/no, a payment, an amount, a
// quality or mode choice, top-or-bottom, an opening-hand action) opens right
// next to the card asking it, when `pending_decision.source_card_id` names a
// card that is rendered on the page - the battlefield, a hand, the stack, the
// zone ribbon. `ui/decisions.js`'s `usesDecisionPopover` says which kinds.
// When the id is null (no object asks, or the viewer cannot see it) or the
// card is not on screen, `#prompt-box` renders the decision exactly as it
// always has; `render/ribbon.js` asks `popoverSubject` below, so the two can
// never both show it or both miss it.
//
// The content is `render/decision_controls.js`'s `renderDecision`, the same
// controls, draft keys and `handlers.onDecide` answers the prompt box uses,
// so the posted `decide` body cannot differ between the two surfaces.
//
// Built once in `ui/index.html` (`#decision-popover`, a sibling of the card
// menu `#popover`, never the same element: that one closes on an outside
// click and on Escape, and a required decision must do neither). A poll that
// leaves the decision unchanged touches nothing; the content is rebuilt only
// when the question or the player's own draft changes, and the box is moved
// only when its card moved or the window resized or scrolled. No scrim: the
// board stays live around it.

import { decisionIsMine } from "../control.js";
import { clear } from "../dom.js";
import { usesDecisionPopover, viewModel } from "../decisions.js";
import { renderDecision } from "./decision_controls.js";
import { decisionKey } from "./decision_window.js";

/** The class the asking card wears while its popover is open. */
export const SOURCE_CLASS = "decision-source";

/** Every rendered element that can stand for a card: a `cardNode` anywhere,
 * or a spell on the stack (`render/board.js` keys `.stack-object` by the
 * object's id, which for a spell is the card's own id). */
const ANCHOR_SELECTOR = ".card[data-card-id], .stack-object[data-key]";

/** Surfaces whose cards are never an anchor: overlays that paint over the
 * popover (`#modal` at z 60) or that are not the card itself. */
const NOT_AN_ANCHOR = "#modal, #decision-window, #decision-popover, #popover, #card-zoom";

const FOCUSABLE = "input, button";

function clamp(value, low, high) {
  return Math.min(Math.max(value, low), high);
}

/** Where a `size` popover goes beside `anchor` (a DOMRect-like box) inside
 * `viewport`, in viewport coordinates: to the right of the card, flipped to
 * its left when the right has no room, and below or above it only when
 * neither side fits. Side placements are clamped vertically, vertical ones
 * horizontally and capped by `maxHeight`; none of them overlaps the card. */
export function popoverPlacement(anchor, size, viewport, opts) {
  const gap = opts && opts.gap !== undefined ? opts.gap : 8;
  const margin = opts && opts.margin !== undefined ? opts.margin : 8;
  const { width, height } = size;
  const sideMax = Math.max(0, Math.min(height, viewport.height - 2 * margin));
  const sideTop = clamp(anchor.top, margin, Math.max(margin, viewport.height - margin - sideMax));
  const right = anchor.right + gap;
  if (right + width <= viewport.width - margin) {
    return { left: right, top: sideTop, maxHeight: sideMax, placement: "right" };
  }
  const left = anchor.left - gap - width;
  if (left >= margin) {
    return { left, top: sideTop, maxHeight: sideMax, placement: "left" };
  }
  const spaceBelow = viewport.height - margin - (anchor.bottom + gap);
  const spaceAbove = anchor.top - gap - margin;
  const below = spaceBelow >= height || spaceBelow >= spaceAbove;
  const maxHeight = Math.max(0, Math.min(height, below ? spaceBelow : spaceAbove));
  return {
    left: clamp(anchor.left, margin, Math.max(margin, viewport.width - margin - width)),
    top: below ? anchor.bottom + gap : anchor.top - gap - maxHeight,
    maxHeight,
    placement: below ? "below" : "above",
  };
}

/** The element on screen standing for card `id`, or `null`: attached under
 * `root`, outside the overlays in `NOT_AN_ANCHOR`, and laid out (a card in a
 * `hidden` row has an empty rectangle). */
export function findRenderedCard(root, id) {
  if (!root || typeof root.querySelectorAll !== "function" || id === null || id === undefined) return null;
  for (const el of root.querySelectorAll(ANCHOR_SELECTOR)) {
    const data = el.dataset || {};
    const own = data.cardId !== undefined ? data.cardId : data.key;
    if (String(own) !== String(id)) continue;
    if (typeof el.closest === "function" && el.closest(NOT_AN_ANCHOR)) continue;
    const rect = typeof el.getBoundingClientRect === "function" ? el.getBoundingClientRect() : null;
    if (!rect || !(rect.width > 0 || rect.height > 0)) continue;
    return el;
  }
  return null;
}

/** What the popover should show for `app`, or `null` when the decision (if
 * any) belongs in `#prompt-box` or elsewhere: `{pending, anchor, key}`.
 * Mirrors the prompt box's own priority - a finished game and the pregame
 * come before any decision - and only this seat's own question. */
export function popoverSubject(root, app) {
  const state = app && app.state;
  if (!state || state.winner || state.drawn || state.pregame || !state.you) return null;
  const pending = state.pending_decision;
  if (!pending || !decisionIsMine(state, pending) || !usesDecisionPopover(pending)) return null;
  if (pending.source_card_id === null || pending.source_card_id === undefined) return null;
  const anchor = findRenderedCard(root, pending.source_card_id);
  if (!anchor) return null;
  return { pending, anchor, key: `${decisionKey(pending)}|${pending.source_card_id}` };
}

function activeElement() {
  return typeof document !== "undefined" ? document.activeElement : null;
}

function focusOn(el) {
  if (el && typeof el.focus === "function") el.focus();
}

function controlsIn(el) {
  return typeof el.querySelectorAll === "function" ? [...el.querySelectorAll(FOCUSABLE)] : [];
}

/** Which control inside `el` has focus, so a rebuild can put it back. */
function rememberFocus(el) {
  const active = activeElement();
  if (!active || !el.contains(active)) return null;
  if (active === el) return { self: true };
  return { index: controlsIn(el).indexOf(active), start: active.selectionStart, end: active.selectionEnd };
}

function restoreFocus(el, memo) {
  if (!memo) return;
  const target = memo.self ? null : controlsIn(el)[memo.index];
  if (!target) return focusOn(el);
  focusOn(target);
  if (typeof memo.start === "number" && typeof target.setSelectionRange === "function") {
    try {
      target.setSelectionRange(memo.start, memo.end);
    } catch {
      // an input type without a caret (number) has nothing to restore
    }
  }
}

/** Focus on open: the answer's own input when it has one, else the popover. */
function focusInto(el) {
  const input = controlsIn(el).find((node) => String(node.tagName).toUpperCase() === "INPUT");
  focusOn(input || el);
}

/**
 * Wire `#decision-popover` to `store`. `handlers` is the prompt box's own
 * set (`onDecisionDraft`, `onDecide`, `nameOf`). `opts.view` (for the
 * viewport, `resize` and `scroll`) defaults to `window`. Returns an unmount
 * function; mounting again on the same element unmounts the previous wiring.
 */
export function mountDecisionPopover(root, store, handlers, opts) {
  const el = root.querySelector("#decision-popover");
  if (!el) return () => {};
  if (typeof el.__unmountDecisionPopover === "function") el.__unmountDecisionPopover();
  const view = (opts && opts.view) || (typeof window !== "undefined" ? window : null);

  let current = null;

  const place = () => {
    if (!current || el.hidden) return;
    const anchor = current.anchor.getBoundingClientRect();
    const rect = el.getBoundingClientRect();
    // `scrollHeight` is the content's natural height even while `max-height`
    // caps the box, so measuring never has to write a style first.
    const frame = (el.offsetHeight || 0) - (el.clientHeight || 0);
    const size = { width: rect.width, height: Math.max(rect.height, (el.scrollHeight || 0) + frame) };
    const viewport = {
      width: (view && view.innerWidth) || 1024,
      height: (view && view.innerHeight) || 768,
    };
    const box = popoverPlacement(anchor, size, viewport);
    const next = {
      left: `${Math.round(box.left)}px`,
      top: `${Math.round(box.top)}px`,
      maxHeight: `${Math.round(box.maxHeight)}px`,
    };
    for (const [key, value] of Object.entries(next)) if (el.style[key] !== value) el.style[key] = value;
    if (el.dataset.placement !== box.placement) el.dataset.placement = box.placement;
  };

  const close = () => {
    if (!current) return;
    const hadFocus = el.contains(activeElement());
    current.anchor.classList.remove(SOURCE_CLASS);
    current = null;
    el.hidden = true;
    clear(el);
    if (hadFocus && typeof document !== "undefined") focusOn(document.body);
  };

  const render = (app) => {
    const subject = popoverSubject(root, app);
    if (!subject) return close();
    const fresh = !current || current.key !== subject.key;
    if (fresh) {
      if (current) current.anchor.classList.remove(SOURCE_CLASS);
      current = { key: subject.key, signature: null, anchor: subject.anchor };
    } else if (current.anchor !== subject.anchor) {
      current.anchor.classList.remove(SOURCE_CLASS);
      current.anchor = subject.anchor;
    }
    if (!subject.anchor.classList.contains(SOURCE_CLASS)) subject.anchor.classList.add(SOURCE_CLASS);
    const signature = JSON.stringify(viewModel(subject.pending, app.decisionDraft));
    const rebuild = current.signature !== signature;
    const memo = rebuild && !fresh ? rememberFocus(el) : null;
    if (rebuild) {
      clear(el);
      el.appendChild(renderDecision(subject.pending, app, handlers));
      current.signature = signature;
    }
    if (el.hidden) el.hidden = false;
    place();
    if (fresh) focusInto(el);
    else if (rebuild) restoreFocus(el, memo);
  };

  // A required decision: Escape answers nothing and closes nothing, and it is
  // not passed on to `app.js`'s document-level Escape either.
  const onKey = (e) => {
    if (e.key !== "Escape") return;
    if (e.preventDefault) e.preventDefault();
    if (e.stopPropagation) e.stopPropagation();
  };

  el.addEventListener("keydown", onKey);
  if (view && view.addEventListener) {
    view.addEventListener("resize", place);
    // Board rows scroll inside themselves, so the card can move without the
    // window moving; `true` catches those inner scrolls too.
    view.addEventListener("scroll", place, true);
  }
  const unsubscribe = store.subscribe(render);

  const unmount = () => {
    el.removeEventListener("keydown", onKey);
    if (view && view.removeEventListener) {
      view.removeEventListener("resize", place);
      view.removeEventListener("scroll", place, true);
    }
    if (typeof unsubscribe === "function") unsubscribe();
    close();
    if (el.__unmountDecisionPopover === unmount) el.__unmountDecisionPopover = null;
  };
  el.__unmountDecisionPopover = unmount;
  return unmount;
}

/** Whether a decision popover is on screen under `root`. */
export function decisionPopoverIsOpen(root) {
  const el = root.querySelector("#decision-popover");
  return Boolean(el && !el.hidden);
}
