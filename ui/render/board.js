// The board itself: opponent hand/battlefield, the shared stack, your
// battlefield and your hand, with each seat's player badge at the left edge
// of its land row - plus the zone ribbon (`#zone-ribbon`), the column of
// graveyards and exiles standing between the battlefield and the control
// ribbon on a desktop. One `mountBoard(root, store, handlers)` subscribes to
// the store and keeps `#opp-hand`/`#opp-badge`/`#opp-lands-cards`/
// `#opp-battlefield`/`#stack-row`/`#zone-ribbon`/`#my-battlefield`/
// `#my-badge`/`#my-lands-cards`/`#my-hand` patched via `dom.js`'s keyed
// `patchChildren`, so a card kept alive across a poll tick does not lose a
// hover preview or a mid-flight transition. Attackers and blockers are marked
// where they stand (`.attacking`/`.blocking`); there is no separate red zone.
//
// Every card on the seven board rows, and on `#permitted-row` under them, is
// `card-fit`: its size is the row's `--card-w`, which `fitBoardRow` sets from
// `ui/fit.js` after every render and whenever the board is resized, so a
// row's cards always fit its width and height and a crowded row scrolls
// sideways once they reach the floor. `#permitted-row` is fitted the same way
// from its own flex share while it has cards, and is `hidden` (no box, no
// share) while it has none.
//
// Which permanent belongs in which row, and which cards are drawn tucked
// under another card, is decided by `ui/board_layout.js` - pure functions
// over the state view, tested directly. This file draws that answer and
// makes no layout decisions of its own.
//
// Per-card options are derived from the one in-flight `action` (`actions.js`)
// or, when a `choose_targets` decision (CR 601.2c-style targeting, but for a
// decision rather than a cast) is the thing being answered, from
// `decisionTargets` instead - never both at once, since only one of the two
// is ever live for this seat.

import { controlledHand, decisionIsMine } from "../control.js";
import { cardNode, updateCardNode, cardBackNode } from "../cards.js";
import { boardLayout, seatRows, zoneRibbon } from "../board_layout.js";
import { h, patchChildren, clear, setText } from "../dom.js";
import { activeSlotIndex, candidateIds } from "../targeting.js";
import { targetSlotIndex } from "../actions.js";
import { manaPoolNodes } from "../symbols.js";
import { opponentOf } from "../combat.js";
import { fitCardWidth, fitFloor } from "../fit.js";

/** The size class of every card on the seven board rows - sized from the
 * row's `--card-w` in `style.css`, not a fixed pixel width. */
const FIT = "card-fit";

/** Height of `.stack-caption` under a stack card (`style.css`: 14px line plus
 * a 2px margin), taken off `#stack-row`'s height before its cards are fitted. */
const STACK_CAPTION_PX = 16;

// Exported (Task 17's addition to this render module) so `render/panels.js`'s
// zones popover can highlight a legal graveyard/exile target with the exact
// same targetable/chosen/dimmed logic the board itself uses, rather than a
// second copy of it.
export function targetingOpts(app, id) {
  const action = app.action;
  const decision =
    app.decisionDraft && decisionIsMine(app.state, app.state.pending_decision)
      ? app.decisionTargets
      : null;
  const slots = decision ? decision.slots : action ? action.slots : [];
  const aff = decision ? decision.affordance : action ? action.affordance : null;
  const picking = Boolean(aff && aff.targets_anything && slots.length);
  // A `choose_targets` decision has no `focusSlot` of its own (it is not an
  // `actions.js` action) - only an in-flight cast/activate reads through
  // `targetSlotIndex`, so the outlined slot there matches exactly what
  // `app.js`'s click handler will actually fill.
  const active = picking ? (decision ? activeSlotIndex(slots) : targetSlotIndex(action)) : -1;
  const legal = picking && active >= 0 ? candidateIds(aff, slots[active]) : new Set();
  const held = slots.filter((s) => s.targets.includes(id)).length;
  return {
    picking,
    targetable: legal.has(id),
    chosen: held > 0,
    heldCount: held,
    dimmed: picking && !legal.has(id) && held === 0,
  };
}

function permanentOpts(app, card) {
  const opts = targetingOpts(app, card.id);
  const attacks = (app.state.combat && app.state.combat.attacks) || [];
  opts.attacking = Boolean(
    (app.combatDraft && app.state.step === "declare_attackers" && app.combatDraft[card.id]) ||
      attacks.some((a) => a.attacker === card.id)
  );
  opts.blocking = Boolean(
    (app.combatDraft &&
      Object.keys(app.combatDraft).includes(card.id) &&
      app.state.step === "declare_blockers") ||
      attacks.some((a) => (a.blockers || []).includes(card.id))
  );
  opts.previewTap = Boolean(
    app.preview && app.preview.tapped && app.preview.tapped.some((t) => t.card_id === card.id)
  );
  opts.pinned = Boolean(app.action && app.action.pinned.some((p) => p.card_id === card.id));
  opts.lifted = Boolean(app.action && app.action.cardId === card.id);
  return opts;
}

/** Bring one already-built `.card` element up to date as a permanent: the
 * classes `cards.js` owns, the two this file owns, the held-count badge, and
 * the click. Shared by every row here, so a card drawn tucked under another
 * one behaves exactly like a card standing on its own. */
function updatePermanentNode(el, card, app, onClick) {
  const opts = permanentOpts(app, card);
  updateCardNode(el, card, opts);
  // `cards.js`'s `updateCardNode` toggles the classes it knows about from
  // its own `toggles()` table; `lifted` (the card mid-drag into a cast)
  // is board-only state, so it is toggled here instead of asking
  // `cards.js` to carry a class it has no other use for.
  el.classList.toggle("lifted", opts.lifted);
  if (opts.heldCount > 1) {
    let badge = el.querySelector(".held-count");
    if (!badge) {
      badge = h("span", { class: "held-count" });
      el.appendChild(badge);
    }
    setText(badge, `x${opts.heldCount}`);
  } else {
    const badge = el.querySelector(".held-count");
    if (badge) badge.remove();
  }
  el.onclick = (event) => onClick(card, event);
}

function patchCards(container, cards, app, size, onClick) {
  patchChildren(
    container,
    cards,
    (c) => c.id,
    (card) => cardNode(card, size, {}),
    (el, card) => updatePermanentNode(el, card, app, onClick)
  );
}

/** One battlefield row, from `board_layout.js`'s entries: each top-level
 * permanent in a `.perm-stack` wrapper, with whatever is attached to it (an
 * Aura, an Equipment) and whatever was exiled with it (an imprint, a
 * hideaway card) drawn underneath it, offset down and to the right the way
 * those cards are physically slid under it on a table.
 *
 * `data-under` on the wrapper is how many cards are in the pile, so the
 * wrapper can reserve the height those offsets need; `data-tuck` on each
 * card is its depth in the pile, and `0` is the permanent itself, on top.
 *
 * `decorate(el, card)` is everything about a card that is not its position
 * in the pile - here, the live board's targeting/combat classes and its
 * click. Exported with that seam because `render/review.js` draws the same
 * rows from a finished game's snapshots and needs the identical geometry
 * (the CSS is keyed on `.perm-stack[data-under]`/`.card[data-tuck]`) with
 * none of the interaction. */
export function patchStacks(container, entries, size, decorate) {
  patchChildren(
    container,
    entries,
    (entry) => entry.card.id,
    () => h("div", { class: "perm-stack" }),
    (el, entry) => {
      el.dataset.under = String(entry.under.length);
      const pile = entry.under.map((card, index) => ({ card, tuck: index + 1 }));
      pile.push({ card: entry.card, tuck: 0 });
      patchChildren(
        el,
        pile,
        (item) => item.card.id,
        (item) => cardNode(item.card, size, {}),
        (child, item) => {
          child.classList.toggle("tucked", item.tuck > 0);
          child.dataset.tuck = String(item.tuck);
          decorate(child, item.card);
        }
      );
      // A pile with a tapped (or about-to-tap) card in it reserves a
      // landscape-wide box (`style.css`'s `.perm-stack[data-tapped]`), and
      // `fitBoardRow` counts it as tapped when sizing the row.
      el.dataset.tapped = String([...el.children].some(isTapped));
    }
  );
}

function isTapped(el) {
  return Boolean(el.classList && (el.classList.contains("tapped") || el.classList.contains("preview-tap")));
}

function pixels(style, property) {
  return (style && parseFloat(style[property])) || 0;
}

/** What `fit.js` needs to know about one child of a board row: whether it
 * stands in a landscape box, and how many cards are tucked under it. */
function footprint(el) {
  if (el.classList && el.classList.contains("perm-stack")) {
    return { tapped: el.dataset.tapped === "true", depth: Number(el.dataset.under) || 0 };
  }
  return { tapped: isTapped(el), depth: 0 };
}

/** Measure one board row and set its `--card-w` from `ui/fit.js`, toggling
 * `.fit-scroll` when even the floor width does not fit. `reserveY` is height
 * inside the row that is not card (a stack card's caption), and `floor` the
 * board's shared floor from `fitBoardFloor` (44px when omitted). A row with
 * no layout box yet (hidden, or not in a rendering document) is left alone. */
export function fitBoardRow(row, reserveY, floor) {
  if (!row || !row.clientWidth || !row.clientHeight) return null;
  const style = typeof getComputedStyle === "function" ? getComputedStyle(row) : null;
  const prints = [...row.children].map(footprint);
  const fit = fitCardWidth({
    width: row.clientWidth - pixels(style, "paddingLeft") - pixels(style, "paddingRight"),
    height: row.clientHeight - pixels(style, "paddingTop") - pixels(style, "paddingBottom") - (reserveY || 0),
    count: prints.length,
    tapped: prints.filter((p) => p.tapped).length,
    depths: prints.map((p) => p.depth),
    gap: pixels(style, "columnGap"),
    floor,
  });
  if (row.style && typeof row.style.setProperty === "function") {
    row.style.setProperty("--card-w", `${fit.cardWidth}px`);
  }
  row.classList.toggle("fit-scroll", fit.scroll);
  return fit;
}

/** The card floor every row of `board` shares, written as the board's
 * `--card-floor` (the rows' `min-height` in `style.css`). It is the height the
 * board has left once its other children, the gaps between them, and each
 * card row's padding, borders and reserved caption (`fitted`, row to
 * `reserveY`) are taken off, split across the card rows - so on a window too
 * short for seven 44px-wide cards (a phone in landscape) the floor drops and
 * every row still fits rather than the board scrolling. A board with no
 * layout box yet is left alone and the rows keep the 44px default. */
export function fitBoardFloor(board, fitted) {
  if (!board || !board.clientHeight) return undefined;
  const reserves = new Map(fitted);
  const style = typeof getComputedStyle === "function" ? getComputedStyle(board) : null;
  const kids = [...(board.children || [])].filter((kid) => !kid.hidden);
  let height =
    board.clientHeight -
    pixels(style, "paddingTop") -
    pixels(style, "paddingBottom") -
    pixels(style, "rowGap") * Math.max(0, kids.length - 1);
  let rows = 0;
  for (const kid of kids) {
    if (!reserves.has(kid)) {
      height -= kid.offsetHeight || 0;
      continue;
    }
    const own = typeof getComputedStyle === "function" ? getComputedStyle(kid) : null;
    rows += 1;
    height -=
      Math.max(0, (kid.offsetHeight || 0) - (kid.clientHeight || 0)) +
      pixels(own, "paddingTop") +
      pixels(own, "paddingBottom") +
      reserves.get(kid);
  }
  const floor = fitFloor({ height, rows });
  if (board.style && typeof board.style.setProperty === "function") {
    board.style.setProperty("--card-floor", `${floor}px`);
  }
  return floor;
}

/** One object on the stack, drawn as the card it is (a spell's printing, an
 * ability's text card) with a caption under it for the two things the card
 * cannot say: the announced X and what it targets. */
function patchStackObjects(container, stack, app, onClick) {
  patchChildren(
    container,
    stack,
    (obj) => obj.id,
    (obj) => h("div", { class: "stack-object" }, cardNode(obj, FIT, {}), h("span", { class: "stack-caption" })),
    (el, obj) => {
      updateCardNode(el.querySelector(".card"), obj, targetingOpts(app, obj.id));
      // `x_value` (CR 202.3e - only set at all when the spell's cost has
      // an `{X}` to announce, see `serialize._card_view`) is the one thing
      // about a stack object a player cannot re-derive from its name: a
      // Nature's Rhythm harmonized for X=8 looks identical to one cast for
      // X=2 without this (battle-7, B7-13).
      const bits = [];
      if (obj.x_value != null) bits.push(`X=${obj.x_value}`);
      if ((obj.targets || []).length) bits.push(`-> ${obj.targets.join(", ")}`);
      const caption = bits.join(" ");
      setText(el.querySelector(".stack-caption"), caption);
      el.title = `${obj.name}${obj.kind === "ability" ? " (ability)" : ""}${caption ? ` ${caption}` : ""}`;
      el.onclick = () => onClick(obj);
    }
  );
}

/** What a card in a graveyard, exile or the command zone does, wherever it
 * is drawn - the zone ribbon here, or `render/zone_viewer.js`'s overlay: the
 * board's own targeting classes, and a click that begins a cast when a
 * CR 601.3 permission names the card and is an ordinary targeting click
 * otherwise. Returns the `decorate(el, card)` both hand to `patchChildren`. */
export function zoneCardDecorator(app, handlers) {
  const state = app.state;
  const permissions = (state.play_permissions && state.play_permissions[state.you]) || [];
  return (el, card) => {
    updateCardNode(el, card, targetingOpts(app, card.id));
    const permission = permissions.find((p) => p.card.id === card.id);
    el.onclick = (event) =>
      permission ? handlers.onPermission(permission, event) : handlers.onPermanent(card, event);
  };
}

/** The zone ribbon: the opponent's graveyard, the opponent's exile, your
 * exile and your graveyard, in that order - `board_layout.js`'s `zoneRibbon`
 * decides the order and which zone holds which card, this only draws it.
 *
 * The ribbon is always drawn, all four rows, even with every zone empty: it
 * is a column of its own between the battlefield and the control ribbon, and
 * a board that reflowed the first time a card was discarded would be worse
 * than four labelled empty rows. An empty row is marked `.empty` and
 * `style.css` collapses it to its own label, so an empty zone costs a line
 * and the rest of the room goes to whichever zone actually has cards in it.
 *
 * Clicking a card here does what clicking it in the zones popover does: it
 * begins a cast when a CR 601.3 permission names it, and is an ordinary
 * targeting click otherwise. That is the whole reason the graveyards are on
 * the board at all rather than behind a button - a spell that targets a card
 * in a graveyard needs something to point at. */
function patchZoneRibbon(container, rows, app, handlers) {
  const decorate = zoneCardDecorator(app, handlers);
  patchChildren(
    container,
    rows,
    (row) => row.key,
    () =>
      h(
        "section",
        { class: "zone-pile" },
        h(
          "div",
          { class: "zone-pile-label" },
          h("span", { class: "zone-seat" }),
          h("span", { class: "zone-name" }),
          h("span", { class: "zone-count" })
        ),
        h("div", { class: "zone-pile-cards" })
      ),
    (el, row) => {
      // `data-side`/`data-zone` rather than classes per row: the CSS keys the
      // seat colour off the side and the graveyard/exile treatment off the
      // zone, and exile must stay visibly a different zone from the
      // graveyard it is now sitting next to.
      el.dataset.side = row.side;
      el.dataset.zone = row.zone;
      el.classList.toggle("empty", row.cards.length === 0);
      setText(el.querySelector(".zone-seat"), row.seatLabel);
      setText(el.querySelector(".zone-name"), row.zone);
      setText(el.querySelector(".zone-count"), String(row.cards.length));
      patchChildren(
        el.querySelector(".zone-pile-cards"),
        row.cards,
        (c) => c.id,
        (card) => cardNode(card, "card-sm", {}),
        decorate
      );
    }
  );
}

/**
 * CR 601.3's dynamic play permissions - a flashback grant, an exiled card
 * made playable "this turn" (cascade's hit and suspend's own third ability
 * no longer grant one of these, battle 8 - each casts inline instead, as an
 * ordinary `pending_decision`) - `state.play_permissions[you]`, `serialize.py`'s
 * `_play_permission_view`: `{card, from_zone, granted_by, usable_now,
 * why_not, ...}`. Keyed by the permitted card's own id (a permission
 * disappears from the list, rather than changing identity, once its card
 * moves zones - CR 400.7 already makes it dead the instant that happens).
 * Greyed with the engine's own `why_not` as a title when not `usable_now`
 * yet - a hint, not a hard block; the click still begins the cast and the
 * server has the final word, exactly like every other click on this board.
 */
function patchPermissions(container, permissions, onClick) {
  patchChildren(
    container,
    permissions,
    (p) => p.card.id,
    (p) => cardNode(p.card, FIT, {}),
    (el, p) => {
      updateCardNode(el, p.card, { dimmed: !p.usable_now });
      // `p.rule` already carries its own CR citation (whichever effect
      // actually granted this permission), so this only
      // adds the granting source's name in front of it, not a second CR
      // 601.3 citation on top.
      el.title = p.usable_now ? `${p.granted_by}: ${p.rule || ""}` : p.why_not || "";
      el.onclick = (event) => onClick(p, event);
    }
  );
}

/** This seat's own `known_cards` entry. `serialize.py`'s `_known_cards_view`
 * keys it by the player who knows, and the redaction hands each viewer only
 * its own, so everything known about any player's hidden cards is in here. */
function knownByYou(state) {
  const known = state.known_cards && state.known_cards[state.you];
  return known || { hand: [], library: [], sideboard: [] };
}

/** The opponent's hand as this seat sees it: every card it knows is there,
 * face up, then a card back for each card it does not know. A hand holds only
 * its owner's cards, so the known ones are those `opp` owns. `hand_count` is
 * how many cards the hand really holds, so a known list longer than that is a
 * stale or broken view: it is clamped, with a warning, rather than drawing
 * cards that are not there. */
function opponentHandItems(state, opp) {
  let known = (knownByYou(state).hand || []).filter((card) => card.owner === opp.username);
  if (known.length > opp.hand_count) {
    console.warn(
      `known_cards lists ${known.length} cards in ${opp.username}'s hand, which holds ${opp.hand_count}; drawing only ${opp.hand_count}`
    );
    known = known.slice(0, opp.hand_count);
  }
  const items = known.map((card) => ({ key: card.id, card }));
  for (let i = known.length; i < opp.hand_count; i++) items.push({ key: `back-${i}`, card: null });
  return items;
}

/** The known top card of `username`'s library, as its `known_cards` record
 * (`{index_from_top, source, card}`), or `null` when the top is unknown. */
function knownLibraryTop(state, username) {
  return (
    (knownByYou(state).library || []).find(
      (record) => record.index_from_top === 0 && record.card && record.card.owner === username
    ) || null
  );
}

/** The zones a player badge counts, in the order it lists them - the zone
 * ribbon's own rows for that seat (`board_layout.js`'s `zoneRibbon`). */
const BADGE_ZONES = ["graveyard", "exile", "command"];

/** Life at or below this reads as low on a badge. */
const LOW_LIFE = 5;

/** Build a badge's parts once, into `el`, and hand them back on every later
 * call - so a poll only rewrites text and classes, never the elements a
 * player might be pressing. */
function badgeParts(el) {
  if (el.__badge) return el.__badge;
  const parts = {
    name: h("span", { class: "name" }),
    life: h("span", { class: "life", title: "life" }),
    hand: h("span", { class: "count count-hand" }),
    library: h("span", { class: "count count-library" }),
    pool: h("div", { class: "badge-pool" }),
    poolKey: null,
    top: null,
    zones: {},
  };
  parts.counts = h("div", { class: "badge-counts" }, parts.hand, parts.library);
  const zones = h("div", { class: "badge-zones" });
  for (const zone of BADGE_ZONES) {
    parts.zones[zone] = h("button", { type: "button", class: `zone-count zone-${zone}` });
    zones.appendChild(parts.zones[zone]);
  }
  clear(el);
  el.appendChild(h("div", { class: "badge-head" }, parts.name, parts.life));
  el.appendChild(parts.counts);
  el.appendChild(parts.pool);
  el.appendChild(zones);
  el.__badge = parts;
  return parts;
}

/** One seat's player badge: name, life, hand and library counts, the known
 * top of its library, its mana pool, and a count per zone that opens that
 * zone (`handlers.onOpenZone(row.key)`, the zone ribbon row's own key). The
 * badge itself targets the player (`mountBoard` wires its click); a zone
 * count stops its click there so it opens the zone and targets nobody. */
function patchBadge(el, app, player, side, rows, handlers) {
  const parts = badgeParts(el);
  const opts = targetingOpts(app, player.username);
  el.classList.add("player-badge");
  el.classList.add(side);
  el.classList.toggle("targetable", opts.targetable);
  el.classList.toggle("chosen", opts.chosen);
  el.classList.toggle("dimmed", opts.dimmed);
  el.classList.toggle("active", app.state.active_player === player.username);
  el.classList.toggle("priority", app.state.priority_holder === player.username);
  setText(parts.name, player.username);
  el.title = player.username;
  setText(parts.life, String(player.life));
  parts.life.classList.toggle("low", player.life <= LOW_LIFE);
  setText(parts.hand, `hand ${player.hand_count}`);
  setText(parts.library, `library ${player.library_count}`);

  const top = knownLibraryTop(app.state, player.username);
  if (top) {
    if (!parts.top) {
      parts.top = h("span", { class: "known-top" });
      parts.counts.appendChild(parts.top);
    }
    parts.top.setAttribute("title", top.source ? `${top.card.name} (known from ${top.source})` : top.card.name);
    setText(parts.top, `top: ${top.card.name}`);
  } else if (parts.top) {
    parts.counts.removeChild(parts.top);
    parts.top = null;
  }

  // `manaPoolNodes` builds a fresh `span.pool` (or its `.empty` state), so it
  // is only rebuilt when the pool itself changed.
  const poolKey = JSON.stringify(player.mana_pool || null);
  if (poolKey !== parts.poolKey) {
    clear(parts.pool);
    parts.pool.appendChild(manaPoolNodes(app.symbols, player.mana_pool));
    parts.poolKey = poolKey;
  }

  for (const zone of BADGE_ZONES) {
    const row = rows.find((r) => r.side === side && r.zone === zone);
    const count = row ? row.cards.length : 0;
    const button = parts.zones[zone];
    setText(button, `${zone} ${count}`);
    button.setAttribute("title", `${player.username}'s ${zone}: ${count} card${count === 1 ? "" : "s"}`);
    button.onclick = (event) => {
      if (event && typeof event.stopPropagation === "function") event.stopPropagation();
      if (row && handlers.onOpenZone) handlers.onOpenZone(row.key);
    };
  }

  let held = el.querySelector(".held-count");
  if (opts.heldCount > 1) {
    if (!held) {
      held = h("span", { class: "held-count" });
      el.appendChild(held);
    }
    setText(held, `x${opts.heldCount}`);
  } else if (held) {
    el.removeChild(held);
  }
}

/**
 * Mount the board region into `root` (the element containing the ids listed
 * below - normally `#game`) and return a function that unsubscribes from the
 * store and stops the rows' resize observer. `handlers = {onHandCard(card), onPermanent(card, event),
 * onPlayer(username), onStackObject(obj), onPermission(permission, event),
 * onOpenZone(key), onLand(card)}`; `onOpenZone` gets a zone ribbon row's key
 * (`opp-graveyard`, `you-exile`, ...) when a badge's zone count is clicked,
 * and `onLand` is not called from here (a land click is a
 * `onHandCard`/`onPermanent` click the caller distinguishes by
 * `card.type_line`) but is accepted so callers can pass one handlers object
 * across every render module. `#permitted-row` (CR 601.3's dynamic play
 * permissions, under the hand) and `#zone-ribbon` (the graveyards and exiles
 * beside the board) are optional - a page without either is simply not
 * offered it, the same
 * tolerance every other `$(...)` lookup here has none of only because their
 * ids are load-bearing elsewhere.
 */
export function mountBoard(root, store, handlers) {
  const $ = (sel) => root.querySelector(sel);
  const els = {
    oppBadge: $("#opp-badge"),
    oppHand: $("#opp-hand"),
    oppLands: $("#opp-lands"),
    oppLandsCards: $("#opp-lands-cards"),
    oppBf: $("#opp-battlefield"),
    stack: $("#stack-row"),
    zoneRibbon: $("#zone-ribbon"),
    myBf: $("#my-battlefield"),
    myLands: $("#my-lands"),
    myLandsCards: $("#my-lands-cards"),
    myHand: $("#my-hand"),
    permittedRow: $("#permitted-row"),
    myBadge: $("#my-badge"),
  };
  // A click anywhere on a badge but its zone counts targets that player, the
  // way the info rows it replaced did.
  const onBadge = (username) => (event) => {
    const target = event && event.target;
    if (target && typeof target.closest === "function" && target.closest(".zone-count")) return;
    handlers.onPlayer(username());
  };
  els.oppBadge.onclick = onBadge(() => opponentOf(store.get().state));
  els.myBadge.onclick = onBadge(() => store.get().state.you);
  // `[row, reserveY, cards]`: a land row is fitted through its cards column,
  // which is the row's width less the badge beside it, while the floor is
  // still worked out from the row itself.
  const fitted = [
    [els.oppHand, 0],
    [els.oppLands, 0, els.oppLandsCards],
    [els.oppBf, 0],
    [els.stack, STACK_CAPTION_PX],
    [els.myBf, 0],
    [els.myLands, 0, els.myLandsCards],
    [els.myHand, 0],
  ];
  if (els.permittedRow) fitted.push([els.permittedRow, 0]);
  const board = $("#mtg-board");
  const fitAll = () => {
    const floor = fitBoardFloor(board, fitted);
    for (const [row, reserveY, cards] of fitted) fitBoardRow(cards || row, reserveY, floor);
  };
  // A window resize, the side panel growing, `#permitted-row` appearing: any
  // of them changes a row's box without a render, so the rows are refitted
  // on the resize itself as well as after every render below.
  let observer = null;
  if (typeof ResizeObserver === "function") {
    observer = new ResizeObserver(fitAll);
    if (board) observer.observe(board);
    for (const [row, , cards] of fitted) {
      observer.observe(row);
      if (cards) observer.observe(cards);
    }
  }
  const unsubscribe = store.subscribe((app) => {
    const state = app.state;
    if (!state) return;
    const you = state.you;
    const me = state.players.find((p) => p.username === you);
    const opp = state.players.find((p) => p.username !== you);
    // CR 723.4/723.5: while this seat decides for the opponent, their hand
    // is shown to it and is its to play from, through the same click a card
    // in its own hand gets (the api takes the action as theirs).
    const controlled = controlledHand(state, opp.username);
    if (controlled.length) {
      patchCards(els.oppHand, controlled, app, FIT, handlers.onHandCard);
    } else {
      patchChildren(
        els.oppHand,
        opponentHandItems(state, opp),
        (item) => item.key,
        (item) => (item.card ? cardNode(item.card, FIT, {}) : cardBackNode()),
        (el, item) => {
          if (!item.card) return;
          updateCardNode(el, item.card, {});
          el.classList.add("card--known");
        }
      );
    }
    // Lands nearest their own controller, everything else in the row further
    // from them - so `#opp-lands` sits directly under the opponent's hand at
    // the top and `#my-lands` directly above your own hand at the bottom,
    // the two halves mirrored around the shared zones in between.
    const layout = boardLayout(state);
    const mine = seatRows(layout, you);
    const theirs = seatRows(layout, opp.username);
    const live = (el, card) => updatePermanentNode(el, card, app, handlers.onPermanent);
    patchStacks(els.oppLandsCards, theirs.lands, FIT, live);
    patchStacks(els.oppBf, theirs.main, FIT, live);
    patchStacks(els.myBf, mine.main, FIT, live);
    patchStacks(els.myLandsCards, mine.lands, FIT, live);
    const zones = zoneRibbon(state, layout);
    patchBadge(els.oppBadge, app, opp, "opp", zones, handlers);
    patchBadge(els.myBadge, app, me, "you", zones, handlers);
    if (els.zoneRibbon) patchZoneRibbon(els.zoneRibbon, zones, app, handlers);
    patchCards(els.myHand, state.hands[you] || [], app, FIT, handlers.onHandCard);
    if (els.permittedRow) {
      const permitted = (state.play_permissions && state.play_permissions[you]) || [];
      els.permittedRow.hidden = permitted.length === 0;
      patchPermissions(els.permittedRow, permitted, handlers.onPermission);
    }
    patchStackObjects(els.stack, state.stack, app, handlers.onStackObject);
    fitAll();
  });
  return () => {
    unsubscribe();
    if (observer) observer.disconnect();
  };
}
