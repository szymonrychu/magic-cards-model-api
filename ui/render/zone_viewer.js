// The zone viewer: one seat's graveyard, exile or command zone as cards, in
// an overlay over the board. A player badge's zone counts open it
// (`render/board.js`'s `onOpenZone`); in mobile mode, where `#zone-ribbon` is
// hidden, it is the only way into a zone.
//
// Which cards a zone holds is `board_layout.js`'s `zoneRibbon`, the same rows
// the ribbon draws, keyed `opp-graveyard`, `you-exile` and so on - so the
// viewer and the ribbon can never disagree, and a card exiled with a
// permanent is under that permanent here too, not in the list. The cards are
// `card-fit`, sized by the viewer's own `--card-w`, and behave like the
// ribbon's (`render/board.js`'s `zoneCardDecorator`): a click targets the
// card or begins a CR 601.3 permitted cast. `#zone-viewer` is a preview
// surface (`ui/preview.js`), so hover and long press read a card here too.
//
// While open it is re-patched on every store update, so a card milled or
// exiled mid-read appears without reopening. It closes on its close button,
// on Escape and on a click on the backdrop around the panel.

import { h, clear, patchChildren, setText } from "../dom.js";
import { cardNode } from "../cards.js";
import { zoneRibbon } from "../board_layout.js";
import { zoneCardDecorator } from "./board.js";

function build(el) {
  const title = h("h2", { id: "zone-viewer-title", class: "zone-viewer-title" });
  const close = h("button", { type: "button", class: "zone-viewer-close" }, "Close");
  const cards = h("div", { class: "zone-viewer-cards" });
  const empty = h("p", { class: "zone-viewer-empty" }, "No cards in this zone.");
  const panel = h(
    "div",
    { class: "zone-viewer-panel" },
    h("div", { class: "zone-viewer-head" }, title, close),
    cards,
    empty
  );
  clear(el);
  el.appendChild(panel);
  return { title, close, cards, empty };
}

/**
 * Wire `#zone-viewer` under `root` to `store`. `handlers = {onPermanent(card,
 * event), onPermission(permission, event)}`, the board's own. Returns
 * `{open(key), close(), isOpen(), unmount()}`; `key` is a `zoneRibbon` row
 * key. A page without `#zone-viewer` gets a viewer that never opens.
 */
export function mountZoneViewer(root, store, handlers) {
  const el = root.querySelector("#zone-viewer");
  if (!el) return { open() {}, close() {}, isOpen: () => false, unmount() {} };
  const doc = typeof root.addEventListener === "function" ? root : typeof document !== "undefined" ? document : null;
  const parts = build(el);
  let openKey = null;

  const close = () => {
    openKey = null;
    el.hidden = true;
    clear(parts.cards);
  };

  const render = (app) => {
    if (openKey === null || !app || !app.state) return;
    const row = zoneRibbon(app.state).find((r) => r.key === openKey);
    if (!row) return close();
    setText(parts.title, `${row.seatLabel} - ${row.zone} (${row.cards.length})`);
    patchChildren(
      parts.cards,
      row.cards,
      (card) => card.id,
      (card) => cardNode(card, "card-fit", {}),
      zoneCardDecorator(app, handlers)
    );
    parts.empty.hidden = row.cards.length > 0;
  };

  const open = (key) => {
    const changed = key !== openKey;
    openKey = key;
    if (changed) clear(parts.cards);
    el.dataset.zone = key;
    el.hidden = false;
    render(store.get());
    if (typeof parts.close.focus === "function") parts.close.focus();
  };

  parts.close.addEventListener("click", close);
  // Only the backdrop itself: a click on the panel or a card inside it keeps
  // the viewer open.
  const onBackdrop = (event) => {
    if (event.target === el) close();
  };
  el.addEventListener("click", onBackdrop);
  // Capturing, and stopped here, so `ui/app.js`'s own Escape (which cancels
  // the action in flight) does not also fire for the key that closed this.
  // A report form open over the viewer gets its Escape first.
  const onKey = (event) => {
    if (openKey === null || event.key !== "Escape") return;
    const modal = root.querySelector("#modal");
    if (modal && !modal.hidden) return;
    close();
    if (typeof event.preventDefault === "function") event.preventDefault();
    if (typeof event.stopImmediatePropagation === "function") event.stopImmediatePropagation();
    if (typeof event.stopPropagation === "function") event.stopPropagation();
  };
  if (doc) doc.addEventListener("keydown", onKey, true);
  const unsubscribe = store.subscribe(render);

  return {
    open,
    close,
    isOpen: () => openKey !== null,
    unmount() {
      parts.close.removeEventListener("click", close);
      el.removeEventListener("click", onBackdrop);
      if (doc) doc.removeEventListener("keydown", onKey, true);
      if (typeof unsubscribe === "function") unsubscribe();
      close();
    },
  };
}
