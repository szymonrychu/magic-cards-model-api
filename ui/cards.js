// Card rendering, built as nodes. `cardNode` builds a fresh `.card` element;
// `updateCardNode` toggles its classes and replaces only the badge strip in
// place, so `patchChildren` (`ui/dom.js`) can keep a card element alive
// across a poll-driven re-render instead of tearing it down and rebuilding
// it - the thing that would otherwise interrupt a hover preview, a
// mid-flight CSS transition, or a click the player is mid-way through.

import { h, clear } from "./dom.js";

function fallbackInner(card) {
  const pt = card.power !== null && card.power !== undefined ? `${card.power}/${card.toughness}` : "";
  const typeLine = card.type_line || (card.kind === "ability" ? "Ability" : "");
  const text = card.oracle_text || card.text || "";
  const nodes = [
    h("span", { class: "name" }, card.name || card.card_name || "?"),
    h("span", { class: "type" }, typeLine),
    h("span", { class: "oracle" }, text.slice(0, 200)),
  ];
  if (pt) nodes.push(h("span", { class: "pt" }, pt));
  return nodes;
}

function cardVisual(card, sizeClass) {
  const img = sizeClass === "card-sm" ? card.small_image || card.image : card.image || card.small_image;
  if (!img) {
    return [h("div", { class: `card-fallback ${sizeClass}` }, ...fallbackInner(card))];
  }
  // A card with no image gets a legible text card; a card that *has* an
  // image URL but fails to load falls back the same way via `onerror`,
  // rather than showing a broken-image box.
  const fallback = h("div", { class: `card-fallback ${sizeClass}`, style: "display:none" }, ...fallbackInner(card));
  const image = h("img", {
    class: `card-visual ${sizeClass}`,
    src: img,
    alt: card.name || "",
    onerror: () => {
      image.style.display = "none";
      fallback.style.display = "flex";
    },
  });
  return [image, fallback];
}

/** The badges the art itself cannot carry (feature 5). Loyalty gets the
 * shield a real planeswalker card has, rather than being one row of a
 * counters list; `+1/+1` and `-1/-1` counters are coloured by which they
 * are; time counters (suspend) get their own colour so a suspended card's
 * clock is readable at a glance; summoning sickness greys the card and adds
 * the swirl; and CR 614.12a's chosen name (Pithing Needle, Disruptor Flute,
 * Cavern of Souls) is written across the art, because on those permanents
 * the chosen name *is* the card's function. */
export function badges(card) {
  const bits = [];
  const counters = card.counters || {};
  const loyalty = counters.loyalty;

  const pt = card.power !== null && card.power !== undefined ? `${card.power}/${card.toughness}` : "";
  if (pt) {
    const buffed =
      card.printed_power !== undefined &&
      (card.printed_power !== card.power || card.printed_toughness !== card.toughness);
    const title = buffed ? `printed ${card.printed_power}/${card.printed_toughness}` : "";
    bits.push(h("span", { class: `badge badge-pt ${buffed ? "buffed" : ""}`, title }, pt));
  }
  if (loyalty !== undefined && loyalty !== null) {
    bits.push(h("span", { class: "badge badge-loyalty", title: "loyalty" }, String(loyalty)));
  } else if (card.damage) {
    bits.push(h("span", { class: "badge badge-damage", title: "damage marked" }, String(card.damage)));
  }

  const chips = Object.entries(counters)
    .filter(([kind, n]) => n && kind !== "loyalty")
    .map(([kind, n]) => {
      let cls = "";
      if (kind === "+1/+1") cls = "plus";
      else if (kind === "-1/-1") cls = "minus";
      else if (kind === "time") cls = "time";
      return h("span", { class: `counter-chip ${cls}`, title: `${kind} counters` }, `${kind} ${n}`);
    });
  if (chips.length) bits.push(h("div", { class: "counter-strip" }, ...chips));

  if (card.summoning_sick) {
    bits.push(
      h(
        "span",
        { class: "badge badge-sick", title: "summoning sick: it cannot attack or use {T} abilities (CR 302.6)" },
        "↻"
      )
    );
  } else if ((card.abilities || []).length && card.zone === "battlefield") {
    bits.push(
      h(
        "span",
        { class: "badge badge-ability", title: `${(card.abilities || []).length} activated abilit(ies)` },
        String(card.abilities.length)
      )
    );
  }
  if (card.choice) {
    bits.push(
      h(
        "span",
        { class: "badge badge-named", title: `named as it entered (CR 614.12a): ${card.choice.kind}` },
        card.choice.value
      )
    );
  }
  return bits;
}

/** Each toggleable class name, paired with whether `card`/`opts` calls for
 * it - shared by `cardNode` (built once) and `updateCardNode` (toggled on a
 * kept-alive element), so the two can never drift apart. */
function toggles(card, opts) {
  return {
    tapped: !!card.tapped,
    selected: !!opts.selected,
    attacking: !!opts.attacking,
    blocking: !!opts.blocking,
    "summoning-sick": !!card.summoning_sick,
    targetable: !!opts.targetable,
    chosen: !!opts.chosen,
    dimmed: !!opts.dimmed,
    "preview-tap": !!opts.previewTap,
    pinned: !!opts.pinned,
    "active-slot": !!opts.activeSlot,
  };
}

export function cardNode(card, sizeClass, opts) {
  opts = opts || {};
  const classes = ["card", sizeClass];
  for (const [cls, on] of Object.entries(toggles(card, opts))) if (on) classes.push(cls);
  const badgeStrip = h("div", { class: "badges" }, ...badges(card));
  return h(
    "div",
    { class: classes.join(" "), dataset: { key: card.id, cardId: card.id } },
    ...cardVisual(card, sizeClass),
    badgeStrip
  );
}

/** Toggle `el`'s classes and replace only its badge strip's children, so a
 * card kept alive by `patchChildren` reflects a changed card without losing
 * anything the browser owns about the element itself. */
export function updateCardNode(el, card, opts) {
  opts = opts || {};
  for (const [cls, on] of Object.entries(toggles(card, opts))) el.classList.toggle(cls, on);
  el.dataset.cardId = card.id;
  const badgeStrip = el.querySelector ? el.querySelector(".badges") : el.children[el.children.length - 1];
  if (badgeStrip) {
    clear(badgeStrip);
    for (const badge of badges(card)) badgeStrip.appendChild(badge);
  }
}

export function cardBackNode() {
  return h("div", { class: "card-back" });
}
