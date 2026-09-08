// Mana symbols, built as nodes. Scryfall's symbology (`/api/ui/symbology`) is
// keyed by the exact `{R}` / `{2/U}` / `{T}` token that already appears
// verbatim in a card's mana cost and in an activated ability's printed cost,
// so nothing here needs a symbol table of its own - and hybrid, Phyrexian
// and snow symbols come out right without a single special case. A token
// with no entry (or a cold symbol cache, or no network at first run) falls
// back to the token drawn as a text pip, which is why `symbolNode` never
// returns nothing.

import { h } from "./dom.js";

export async function loadSymbols(api, base = "/api") {
  try {
    const { symbols } = await api("GET", `${base}/ui/symbology`);
    return symbols || {};
  } catch {
    return {};
  }
}

export function symbolNode(map, token, big) {
  const entry = map[token];
  const cls = big ? "ms ms-lg" : "ms";
  if (entry && entry.svg) {
    return h("img", { class: cls, src: entry.svg, alt: token, title: entry.text || token });
  }
  return h("span", { class: "ms-text", title: token }, token.replace(/[{}]/g, ""));
}

/** Any string with `{...}` tokens in it - a mana cost, an ability's cost, a
 * line of oracle text - with the tokens drawn as symbols and everything
 * else as plain text nodes. */
export function withSymbols(map, text, big) {
  const frag = document.createDocumentFragment();
  if (!text) return frag;
  for (const match of String(text).matchAll(/(\{[^}]{1,8}\})|([^{]+)/g)) {
    const [, token, plain] = match;
    frag.appendChild(token ? symbolNode(map, token, big) : document.createTextNode(plain));
  }
  return frag;
}

/** A mana pool as symbols: three red mana is `3` next to one red pip, not
 * `RRR` and not the string `3R`. */
export function manaPoolNodes(map, pool) {
  const floating = (pool && pool.floating) || {};
  const snow = (pool && pool.snow) || {};
  const groups = [];
  for (const [color, count] of Object.entries(floating)) {
    if (!count) continue;
    groups.push(
      h(
        "span",
        { class: "pool-group", title: `${count} ${color}` },
        h("span", { class: "n" }, String(count)),
        symbolNode(map, `{${color}}`, true)
      )
    );
  }
  for (const [color, count] of Object.entries(snow)) {
    if (!count) continue;
    groups.push(
      h(
        "span",
        { class: "pool-group", title: `${count} snow ${color}` },
        h("span", { class: "n" }, String(count)),
        symbolNode(map, "{S}", true)
      )
    );
  }
  if (!groups.length) return h("span", { class: "pool empty" }, "empty pool");
  return h("span", { class: "pool" }, ...groups);
}
