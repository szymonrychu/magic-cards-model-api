// Small DOM builder and keyed reconciler, so the rest of the UI never builds
// HTML strings or touches `innerHTML`. `h()` is a hyperscript-style element
// builder; `patchChildren` is a minimal keyed diff (React's list
// reconciliation, without the framework) that keeps a child alive across a
// re-render when its key survives, so the player's open dropdown or hovered
// card is not torn down and rebuilt on every poll tick.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") for (const [k, v] of Object.entries(value)) el.dataset[k] = v;
    else if (key.startsWith("on") && typeof value === "function")
      el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(
      typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child
    );
  }
  return el;
}

export function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

/** Reconcile `parent`'s children against `items`, keyed by `keyOf(item)`. A
 * child whose key survives is kept (and updated in place via `update`)
 * rather than destroyed and recreated, so the browser's own state on it -
 * focus, scroll position, a CSS transition mid-flight - is not lost on every
 * poll-driven re-render. Missing children are built with `create`; extra
 * ones are removed; everything is reordered to match `items`. */
export function patchChildren(parent, items, keyOf, create, update) {
  const existing = new Map();
  for (const child of [...parent.childNodes]) {
    if (child.dataset && child.dataset.key !== undefined) existing.set(child.dataset.key, child);
  }
  const wanted = new Set();
  let cursor = parent.firstChild;
  for (const item of items) {
    const key = String(keyOf(item));
    wanted.add(key);
    let el = existing.get(key);
    if (!el) {
      el = create(item);
      el.dataset.key = key;
    }
    update(el, item);
    if (el !== cursor) {
      // A real DOM's `insertBefore` on an already-attached node moves it -
      // removing it from its old spot is implicit. A minimal fake DOM (see
      // `tests/test_ui_dom.py`) need not replicate that, so a reused,
      // already-positioned child is removed explicitly first; on a real DOM
      // this is a harmless no-op before the same move.
      if (el.parentNode === parent) parent.removeChild(el);
      parent.insertBefore(el, cursor);
    } else
      cursor =
        cursor.nextSibling === undefined
          ? parent.childNodes[parent.childNodes.indexOf(cursor) + 1] || null
          : cursor.nextSibling;
  }
  for (const [key, el] of existing) if (!wanted.has(key)) parent.removeChild(el);
}
