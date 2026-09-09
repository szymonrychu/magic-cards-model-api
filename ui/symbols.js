// Mana symbols, built as nodes. Scryfall's symbology (`/api/symbology`) is
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
    const { symbols } = await api("GET", `${base}/symbology`);
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

// A CR 106.6 restriction is marked with a glyph, never with a colour alone:
// WCAG 2.2 SC 1.4.1 (Level A) and the Game Accessibility Guidelines both rule
// out hue as the sole carrier of a distinction, so what tells restricted mana
// apart from the rest of the pool has to survive greyscale. The two marks are
// different from each other for the same reason the wording is: mana narrowed
// to Eldrazi spells and mana that pays for nothing at all are not the same
// news, and one glyph for both would flatten them together.
const RESTRICTED_MARK = "*";
const UNUSABLE_MARK = "!";

/** What a restriction says, in the engine's own words.
 *
 * `understood` is false when `ManaRestriction` parsed the printed sentence
 * into no clauses at all - `allows` then answers False for everything, so the
 * mana exists, is announced (CR 106.4a), empties with the pool, and pays for
 * nothing. The UI has to say that outright: calling it "restricted" would
 * imply it is merely narrowed, and the player would go looking for the spell
 * it can still pay for. The unread sentence is still quoted verbatim, because
 * it is the only thing that says which card put the mana there. */
export function restrictionSentence(restriction) {
  const text = (restriction && restriction.text) || "";
  if (restriction && restriction.understood) return text;
  const quoted = text ? ` "${text}"` : "";
  return `unusable: this engine could not read the restriction${quoted}, so this mana cannot be spent on anything`;
}

/** The restricted slices of a pool, one row per (restriction, colour) pair:
 * `{color, count, understood, marker, text, sentence}`.
 *
 * `ManaPool.floating` carries the whole amount of a colour and
 * `ManaPool.restricted` says how much of that carries a CR 106.6 "Spend this
 * mana only to ..." sentence, so these rows are a *slice* of `floating`, not
 * an addition to it - `manaPoolNodes` subtracts them before drawing what is
 * left as unrestricted.
 *
 * Both the pool renderer here and `render/ribbon.js`'s casting-tray step read
 * a pool through this one function, so the board and the tray can never
 * disagree about what a restriction says or how it is marked. */
export function restrictedManaRows(pool) {
  const rows = [];
  for (const entry of (pool && pool.restricted) || []) {
    const restriction = (entry && entry.restriction) || {};
    const understood = Boolean(restriction.understood);
    for (const [color, count] of Object.entries((entry && entry.amounts) || {})) {
      if (!count) continue;
      rows.push({
        color,
        count,
        understood,
        marker: understood ? RESTRICTED_MARK : UNUSABLE_MARK,
        text: restriction.text || "",
        sentence: restrictionSentence(restriction),
      });
    }
  }
  return rows;
}

/** A mana pool as symbols: three red mana is `3` next to one red pip, not
 * `RRR` and not the string `3R`.
 *
 * Mana under a CR 106.6 restriction is its own group rather than part of the
 * count beside it, because it is not interchangeable with the rest: three
 * colourless of which two may only cast Eldrazi is "1" and "2 restricted",
 * never "3". The restriction's own sentence rides on the group's `title` -
 * which is enough *here*, where the pool is a status readout; while a cost is
 * actually being paid it is printed in full in the casting tray instead
 * (`render/ribbon.js`), since a hover is no use at the moment it decides
 * something. */
export function manaPoolNodes(map, pool) {
  const floating = (pool && pool.floating) || {};
  const snow = (pool && pool.snow) || {};
  const restricted = restrictedManaRows(pool);
  const restrictedByColor = {};
  for (const row of restricted) {
    restrictedByColor[row.color] = (restrictedByColor[row.color] || 0) + row.count;
  }
  const groups = [];
  for (const [color, count] of Object.entries(floating)) {
    const free = count - (restrictedByColor[color] || 0);
    if (free <= 0) continue;
    groups.push(
      h(
        "span",
        { class: "pool-group", title: `${free} ${color}` },
        h("span", { class: "n" }, String(free)),
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
  for (const row of restricted) {
    groups.push(
      h(
        "span",
        {
          class: `pool-group restricted${row.understood ? "" : " unusable"}`,
          title: `${row.count} ${row.color} - ${row.sentence}`,
        },
        h("span", { class: "n" }, String(row.count)),
        symbolNode(map, `{${row.color}}`, true),
        // The `cards.js` badge idiom - a marker that carries its own
        // explanation on `title`, so the glyph never has to be guessed at.
        h("span", { class: "badge badge-restricted", title: row.sentence }, row.marker)
      )
    );
  }
  if (!groups.length) return h("span", { class: "pool empty" }, "empty pool");
  return h("span", { class: "pool" }, ...groups);
}
