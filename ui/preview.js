// The card preview: a large, readable copy of a card, in `#card-zoom`.
//
// Two gestures open it, one per kind of pointer:
//
// * A mouse hovering a `.card` on any card surface gets a floating enlarged
//   copy centred over that card and clamped inside the viewport. It is
//   `position: fixed` and `pointer-events: none` (`style.css`), so nothing on
//   the board moves and every click still lands on the real card underneath.
//   It goes away when the mouse leaves the card, when the card leaves the
//   DOM, on any scroll, and on a press (the click that press becomes opens an
//   anchored confirm `#popover`, which the preview must not cover).
//
// * A finger (or pen) held on a card for `LONG_PRESS_MS` without moving more
//   than `LONG_PRESS_SLOP_PX` gets a large centred overlay instead. Lifting
//   the finger closes it at once and swallows the click that release would
//   have become, so reading a card never also acts on it. A shorter tap is an
//   ordinary click, untouched. While the overlay is open the store's fan-out
//   is held (`store.holdStart`/`holdEnd`, `ui/store.js`) for as long as the
//   finger stays down, not just the press guard's one-second cap, so the card
//   under the finger is not rebuilt mid-read.
//
// Everything is delegated from `document` (capturing, so a handler that
// stops propagation cannot hide a gesture from this) and keyed on the card
// element's id (`cardIdOf`), looked up fresh against the store's state on
// every paint - so a card element `patchChildren` keeps alive and
// `updateCardNode` rewrites in place previews whatever it shows now.

import { h, clear } from "./dom.js";
import { withSymbols } from "./symbols.js";
import { decisionCards } from "./decisions.js";

/** How long a finger must stay down on a card before the preview opens. */
export const LONG_PRESS_MS = 350;
/** How far a finger may drift before the press counts as a scroll or a drag. */
export const LONG_PRESS_SLOP_PX = 10;
/** The hover preview is this wide at least, or `HOVER_SCALE` times the card. */
export const HOVER_MIN_WIDTH_PX = 260;
export const HOVER_SCALE = 2.2;
/** The surfaces whose cards preview. A card anywhere else (the lobby, the
 * review) does not. */
export const PREVIEW_SOURCES =
  "#mtg-board, #zone-ribbon, #zone-viewer, #stack-row, #my-hand, #opp-hand, #permitted-row, #decision-window, #modal, #popover";

/** Every element that stands for a card: a `cardNode` anywhere, or an object
 * on the stack, which `render/board.js` wraps (card plus caption) in a
 * `.stack-object` keyed by the object's id through `data-key` (for a spell,
 * the card's own id) -
 * the same pair `render/decision_popover.js` anchors to. */
export const CARD_SELECTOR = ".card[data-card-id], .stack-object[data-key]";

/** The id of the card a `CARD_SELECTOR` element stands for. */
const cardIdOf = (el) => (el.dataset.cardId !== undefined ? el.dataset.cardId : el.dataset.key);

const HOVER_MARGIN_PX = 8;
const OVERLAY_MARGIN_PX = 16;
// Height over width of a printed card (63mm by 88mm).
const CARD_ASPECT = 88 / 63;
// How long after a long press ends its click may still arrive. Touch clicks
// follow the release by up to ~300ms (`ui/store.js`'s `TOUCH_CLICK_MS`).
const CLICK_SUPPRESS_MS = 700;

/** Every card a live seat's view can ever put a `CARD_SELECTOR` node on
 * screen for - battlefield, stack, exile, your own hand, every
 * graveyard, (CR 601.3) any card a play permission names, and the cards of
 * this seat's own pending decision (a library card a scry or a search shows
 * exists nowhere else on the view) - keyed by id, fresh on each paint rather
 * than kept live, so a card that only exists because of a permission still
 * previews correctly. The decision's cards go in first, so a card that is
 * also on the board previews as its board view. The opponent's hand cards
 * this seat knows (`known_cards[you].hand`) are drawn face up on the board,
 * so they preview like any other visible card. */
export function hoverableCards(state) {
  const lookup = {};
  const add = (card) => {
    if (card && card.id) lookup[card.id] = card;
  };
  const pending = state.pending_decision;
  if (pending && pending.player === state.you) decisionCards(pending).forEach(add);
  (state.battlefield || []).forEach(add);
  (state.stack || []).forEach(add);
  (state.exile || []).forEach(add);
  (state.exile_pile || []).forEach(add);
  (state.command || []).forEach(add);
  ((state.hands && state.hands[state.you]) || []).forEach(add);
  ((state.known_cards && state.known_cards[state.you] && state.known_cards[state.you].hand) || []).forEach(add);
  Object.values(state.graveyards || {}).forEach((cards) => (cards || []).forEach(add));
  ((state.play_permissions && state.play_permissions[state.you]) || []).forEach((p) => add(p.card));
  return lookup;
}

const clamp = (value, low, high) => Math.max(low, Math.min(value, high));

/** A card-shaped box `width` wide, shrunk (aspect kept) to fit `maxW` x `maxH`. */
function fitCard(width, maxW, maxH) {
  let w = width;
  let ht = w * CARD_ASPECT;
  if (ht > maxH) {
    ht = maxH;
    w = ht / CARD_ASPECT;
  }
  if (w > maxW) {
    w = maxW;
    ht = w * CARD_ASPECT;
  }
  return [w, ht];
}

/** Where the hover preview for a card at `rect` goes in a `vw` x `vh`
 * viewport: centred on the card, `HOVER_MIN_WIDTH_PX` or `HOVER_SCALE`x wide
 * whichever is larger, and clamped fully inside the viewport. */
export function hoverRect(rect, vw, vh) {
  const m = HOVER_MARGIN_PX;
  const [w, ht] = fitCard(Math.max(HOVER_MIN_WIDTH_PX, rect.width * HOVER_SCALE), vw - 2 * m, vh - 2 * m);
  const left = clamp(rect.left + rect.width / 2 - w / 2, m, vw - m - w);
  const top = clamp(rect.top + rect.height / 2 - ht / 2, m, vh - m - ht);
  return { left: Math.round(left), top: Math.round(top), width: Math.round(w), height: Math.round(ht) };
}

/** The long-press overlay: as tall as the viewport allows with a margin,
 * narrower if the viewport is, centred. */
export function overlayRect(vw, vh) {
  const m = OVERLAY_MARGIN_PX;
  const [w, ht] = fitCard((vh - 2 * m) / CARD_ASPECT, vw - 2 * m, vh - 2 * m);
  return {
    left: Math.round((vw - w) / 2),
    top: Math.round((vh - ht) / 2),
    width: Math.round(w),
    height: Math.round(ht),
  };
}

/** What the element itself shows, for a card the state lookup does not have. */
function cardFromElement(el) {
  const img = el.querySelector("img");
  const text = (selector) => {
    const node = el.querySelector(selector);
    return node ? node.textContent : "";
  };
  return {
    id: cardIdOf(el),
    name: (img && img.getAttribute("alt")) || text(".name"),
    type_line: text(".type"),
    oracle_text: text(".oracle"),
    image: img ? img.getAttribute("src") : null,
  };
}

/** The art, with the same legible text card `ui/cards.js` falls back to when
 * there is no image or it fails to load - but the whole oracle text, since
 * this is the copy meant for reading. */
function zoomContent(card, symbols) {
  const img = card.image || card.small_image;
  const pt = card.power !== null && card.power !== undefined ? `${card.power}/${card.toughness}` : "";
  const typeLine = card.type_line || (card.kind === "ability" ? "Ability" : "");
  const text = h(
    "div",
    { class: "card-zoom-text", hidden: Boolean(img) },
    h("span", { class: "name" }, card.name || card.card_name || "?"),
    h("span", { class: "type" }, typeLine),
    h("div", { class: "oracle" }, withSymbols(symbols || {}, card.oracle_text || card.text || "")),
    pt ? h("span", { class: "pt" }, pt) : null
  );
  if (!img) return [text];
  const image = h("img", {
    class: "card-zoom-image",
    src: img,
    alt: card.name || "",
    onerror: () => {
      image.hidden = true;
      text.hidden = false;
    },
  });
  return [image, text];
}

function contentKey(card) {
  return JSON.stringify([
    card.id,
    card.image || card.small_image || "",
    card.name || card.card_name || "",
    card.type_line || "",
    card.oracle_text || card.text || "",
    card.power,
    card.toughness,
  ]);
}

const isTouch = (e) => e.pointerType === "touch" || e.pointerType === "pen";

/**
 * Wire the preview onto `root` (the page's `document`). `store` supplies the
 * card lookup, the mana-symbol map and the render hold; `view` is the
 * `window` (viewport size, the click suppression, blur and resize). Both
 * default to the page's own so `tests/test_ui_preview.py` can pass fakes.
 * Returns its own uninstall function.
 */
export function mountCardPreview(root, store, view) {
  const doc = root || (typeof document === "undefined" ? null : document);
  const win = view || (typeof window === "undefined" ? null : window);
  if (!doc || !doc.addEventListener) return () => {};

  let hoverAnchor = null; // the card the mouse is over, while its preview shows
  let dismissed = null; // a card pressed with the mouse: no preview until left
  let shownKey = null;
  let observer = null;
  let press = null; // a touch press that may become a long press
  let overlayOpen = false;
  let suppressing = false;
  let suppressTimer = null;
  let releaseTimer = null;

  const zoomEl = () => {
    let el = doc.querySelector("#card-zoom");
    if (!el) {
      el = h("div", { id: "card-zoom", class: "card-zoom", "aria-hidden": "true" });
      el.hidden = true;
      (doc.body || doc).appendChild(el);
    }
    return el;
  };
  const viewport = () => [win ? win.innerWidth : 0, win ? win.innerHeight : 0];

  const cardFor = (el) => {
    const app = store.get() || {};
    const found = app.state ? hoverableCards(app.state)[cardIdOf(el)] : null;
    return found || cardFromElement(el);
  };

  const paint = (el, rect, overlay) => {
    const zoom = zoomEl();
    const card = cardFor(el);
    const key = contentKey(card);
    if (key !== shownKey) {
      clear(zoom);
      for (const node of zoomContent(card, (store.get() || {}).symbols)) zoom.appendChild(node);
      shownKey = key;
    }
    zoom.classList.toggle("long-press", overlay);
    for (const side of ["left", "top", "width", "height"]) {
      const value = `${rect[side]}px`;
      if (zoom.style[side] !== value) zoom.style[side] = value;
    }
    zoom.hidden = false;
  };

  const hideZoom = () => {
    const zoom = doc.querySelector("#card-zoom");
    if (zoom) zoom.hidden = true;
  };

  const unobserve = () => {
    if (observer) observer.disconnect();
    observer = null;
  };

  const hideHover = () => {
    hoverAnchor = null;
    unobserve();
    if (!overlayOpen) hideZoom();
  };

  // Not every render goes through the store (`#modal` rebuilds on open and
  // close), so a card leaving the DOM is also watched directly - only while
  // a hover preview is up, which is the only time it matters.
  const observe = () => {
    const Observer = win && win.MutationObserver;
    if (!Observer || observer) return;
    observer = new Observer(() => {
      if (hoverAnchor && !hoverAnchor.isConnected) hideHover();
    });
    observer.observe(doc.body || doc, { childList: true, subtree: true });
  };

  const showHover = (el) => {
    hoverAnchor = el;
    const [vw, vh] = viewport();
    paint(el, hoverRect(el.getBoundingClientRect(), vw, vh), false);
    observe();
  };

  const cardAt = (target) => {
    if (!target || !target.closest) return null;
    const el = target.closest(CARD_SELECTOR);
    return el && el.closest(PREVIEW_SOURCES) ? el : null;
  };

  const cancelPress = () => {
    if (press && press.timer !== null) clearTimeout(press.timer);
    press = null;
  };

  const openOverlay = () => {
    press.timer = null;
    overlayOpen = true;
    hoverAnchor = null;
    unobserve();
    const [vw, vh] = viewport();
    paint(press.el, overlayRect(vw, vh), true);
    if (releaseTimer !== null) {
      clearTimeout(releaseTimer);
      releaseTimer = null;
    }
    if (store.holdStart) store.holdStart();
  };

  const releaseHold = () => {
    if (!store.holdEnd) return;
    // On the next macrotask, as the press guard flushes: the release event
    // finishes dispatching before anything is allowed to rebuild.
    releaseTimer = setTimeout(() => {
      releaseTimer = null;
      store.holdEnd();
    }, 0);
  };

  const closeOverlay = (swallowClick) => {
    overlayOpen = false;
    press = null;
    hideZoom();
    releaseHold();
    if (!swallowClick) return;
    suppressing = true;
    if (suppressTimer !== null) clearTimeout(suppressTimer);
    suppressTimer = setTimeout(() => {
      suppressTimer = null;
      suppressing = false;
    }, CLICK_SUPPRESS_MS);
  };

  const stopSuppressing = () => {
    suppressing = false;
    if (suppressTimer !== null) clearTimeout(suppressTimer);
    suppressTimer = null;
  };

  // ------------------------------------------------------------ listeners --

  const onOver = (e) => {
    if (e.pointerType !== "mouse" || overlayOpen) return;
    const el = cardAt(e.target);
    if (el && el !== hoverAnchor && el !== dismissed) showHover(el);
  };

  const onOut = (e) => {
    if (e.pointerType !== "mouse") return;
    const el = e.target && e.target.closest ? e.target.closest(CARD_SELECTOR) : null;
    if (!el) return;
    const to = e.relatedTarget;
    if (to && el.contains(to)) return;
    if (el === hoverAnchor) hideHover();
    if (el === dismissed) dismissed = null;
  };

  const onDown = (e) => {
    // A new press: whatever click an earlier long press was waiting to
    // swallow never came.
    stopSuppressing();
    if (!isTouch(e)) {
      if (hoverAnchor) dismissed = hoverAnchor;
      hideHover();
      return;
    }
    if (press || overlayOpen) {
      // A second finger: a pinch or a two-finger scroll, not a long press.
      if (overlayOpen) closeOverlay(true);
      cancelPress();
      return;
    }
    const el = cardAt(e.target);
    if (!el) return;
    press = { id: e.pointerId, x: e.clientX, y: e.clientY, el, timer: null };
    press.timer = setTimeout(openOverlay, LONG_PRESS_MS);
  };

  const onMove = (e) => {
    if (!press || overlayOpen || e.pointerId !== press.id) return;
    if (Math.hypot(e.clientX - press.x, e.clientY - press.y) > LONG_PRESS_SLOP_PX) cancelPress();
  };

  const onRelease = (e) => {
    if (overlayOpen) {
      if (press && e.pointerId !== press.id) return;
      closeOverlay(true);
      return;
    }
    if (press && e.pointerId === press.id) cancelPress();
  };

  // At `window`, capturing: the very first stop on a click's way down, so a
  // swallowed click reaches nothing on the page - not the card's handler,
  // not an outside-click listener that would close an open menu.
  const onClick = (e) => {
    if (!suppressing) return;
    stopSuppressing();
    e.preventDefault();
    e.stopImmediatePropagation();
  };

  const onCardDefault = (e) => {
    if (e.target && e.target.closest && e.target.closest(".card, .stack-object")) e.preventDefault();
  };

  const onBlur = () => {
    if (overlayOpen) closeOverlay(false);
    cancelPress();
    hideHover();
  };

  const onStore = () => {
    if (!hoverAnchor) return;
    if (!hoverAnchor.isConnected || !cardIdOf(hoverAnchor)) {
      hideHover();
      return;
    }
    showHover(hoverAnchor);
  };

  const docListeners = [
    ["pointerover", onOver],
    ["pointerout", onOut],
    ["pointerdown", onDown],
    ["pointermove", onMove],
    ["pointerup", onRelease],
    ["pointercancel", onRelease],
    ["contextmenu", onCardDefault],
    ["selectstart", onCardDefault],
    ["scroll", hideHover],
  ];
  const winListeners = [
    ["click", onClick, true],
    ["blur", onBlur, false],
    ["resize", hideHover, false],
  ];
  for (const [type, fn] of docListeners) doc.addEventListener(type, fn, true);
  if (win && win.addEventListener) for (const [type, fn, capture] of winListeners) win.addEventListener(type, fn, capture);
  const unsubscribe = store.subscribe(onStore);

  return () => {
    if (overlayOpen) closeOverlay(false);
    cancelPress();
    hideHover();
    stopSuppressing();
    for (const [type, fn] of docListeners) doc.removeEventListener(type, fn, true);
    if (win && win.removeEventListener) for (const [type, fn, capture] of winListeners) win.removeEventListener(type, fn, capture);
    if (typeof unsubscribe === "function") unsubscribe();
  };
}
