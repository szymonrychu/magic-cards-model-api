// The board itself: opponent info/hand/battlefield, the shared stack, your
// battlefield, your hand, and your own info row - plus the zone ribbon
// (`#zone-ribbon`), the column of graveyards and exiles standing between the
// battlefield and the control ribbon. One `mountBoard(root, store, handlers)`
// subscribes to the store and keeps `#opp-info`/`#opp-hand`/`#opp-lands`/
// `#opp-battlefield`/`#stack-row`/`#zone-ribbon`/`#my-battlefield`/
// `#my-lands`/`#my-hand`/`#you-info` patched via `dom.js`'s keyed
// `patchChildren`, so a card kept alive across a poll tick does not lose a
// hover preview or a mid-flight transition. Attackers and blockers are marked
// where they stand (`.attacking`/`.blocking`); there is no separate red zone.
//
// Every card on the seven board rows is `card-fit`: its size is the row's
// `--card-w`, which `fitBoardRow` sets from `ui/fit.js` after every render
// and whenever the board is resized, so a row's cards always fit its width
// and height and a crowded row scrolls sideways once they reach the floor.
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

import { cardNode, updateCardNode, cardBackNode } from "../cards.js";
import { boardLayout, seatRows, zoneRibbon } from "../board_layout.js";
import { h, patchChildren, clear, setText } from "../dom.js";
import { activeSlotIndex, candidateIds } from "../targeting.js";
import { targetSlotIndex } from "../actions.js";
import { manaPoolNodes } from "../symbols.js";
import { opponentOf } from "../combat.js";
import { fitCardWidth } from "../fit.js";

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
    app.decisionDraft && app.state.pending_decision && app.state.pending_decision.player === app.state.you
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
 * inside the row that is not card (a stack card's caption). A row with no
 * layout box yet (hidden, or not in a rendering document) is left alone. */
export function fitBoardRow(row, reserveY) {
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
  });
  if (row.style && typeof row.style.setProperty === "function") {
    row.style.setProperty("--card-w", `${fit.cardWidth}px`);
  }
  row.classList.toggle("fit-scroll", fit.scroll);
  return fit;
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
  const state = app.state;
  const permissions = (state.play_permissions && state.play_permissions[state.you]) || [];
  const decorate = (el, card) => {
    updateCardNode(el, card, targetingOpts(app, card.id));
    const permission = permissions.find((p) => p.card.id === card.id);
    el.onclick = (event) =>
      permission ? handlers.onPermission(permission, event) : handlers.onPermanent(card, event);
  };
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
    (p) => cardNode(p.card, "card-md", {}),
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

function infoRow(el, app, player) {
  const opts = targetingOpts(app, player.username);
  const top = knownLibraryTop(app.state, player.username);
  el.classList.toggle("targetable", opts.targetable);
  el.classList.toggle("chosen", opts.chosen);
  el.classList.toggle("dimmed", opts.dimmed);
  el.classList.toggle("active", app.state.active_player === player.username);
  el.classList.toggle("priority", app.state.priority_holder === player.username);
  clear(el);
  el.appendChild(h("span", { class: "name" }, player.username));
  el.appendChild(h("span", { class: "life", title: "life" }, String(player.life)));
  el.appendChild(
    h(
      "span",
      { class: "counts" },
      `hand ${player.hand_count} / library ${player.library_count} / graveyard ${player.graveyard_count}`
    )
  );
  if (top) {
    const title = top.source ? `${top.card.name} (known from ${top.source})` : top.card.name;
    el.appendChild(h("span", { class: "known-top", title }, `top: ${top.card.name}`));
  }
  // `manaPoolNodes` already returns its own `span.pool` - wrapping it in a
  // second one nested `.pool` inside `.pool` for no reason.
  el.appendChild(manaPoolNodes(app.symbols, player.mana_pool));
  if (opts.heldCount > 1) el.appendChild(h("span", { class: "held-count" }, `x${opts.heldCount}`));
}

/**
 * Mount the board region into `root` (the element containing the ids listed
 * below - normally `#game`) and return a function that unsubscribes from the
 * store and stops the rows' resize observer. `handlers = {onHandCard(card), onPermanent(card, event),
 * onPlayer(username), onStackObject(obj), onPermission(permission, event),
 * onLand(card)}`; `onLand` is not called from here (a land click is a
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
    oppInfo: $("#opp-info"),
    oppHand: $("#opp-hand"),
    oppLands: $("#opp-lands"),
    oppBf: $("#opp-battlefield"),
    stack: $("#stack-row"),
    zoneRibbon: $("#zone-ribbon"),
    myBf: $("#my-battlefield"),
    myLands: $("#my-lands"),
    myHand: $("#my-hand"),
    permittedRow: $("#permitted-row"),
    youInfo: $("#you-info"),
  };
  els.oppInfo.onclick = () => handlers.onPlayer(opponentOf(store.get().state));
  els.youInfo.onclick = () => handlers.onPlayer(store.get().state.you);
  const fitted = [
    [els.oppHand, 0],
    [els.oppLands, 0],
    [els.oppBf, 0],
    [els.stack, STACK_CAPTION_PX],
    [els.myBf, 0],
    [els.myLands, 0],
    [els.myHand, 0],
  ];
  const fitAll = () => {
    for (const [row, reserveY] of fitted) fitBoardRow(row, reserveY);
  };
  // A window resize, the side panel growing, `#permitted-row` appearing: any
  // of them changes a row's box without a render, so the rows are refitted
  // on the resize itself as well as after every render below.
  let observer = null;
  if (typeof ResizeObserver === "function") {
    observer = new ResizeObserver(fitAll);
    const board = $("#mtg-board");
    if (board) observer.observe(board);
    for (const [row] of fitted) observer.observe(row);
  }
  const unsubscribe = store.subscribe((app) => {
    const state = app.state;
    if (!state) return;
    const you = state.you;
    const me = state.players.find((p) => p.username === you);
    const opp = state.players.find((p) => p.username !== you);
    infoRow(els.oppInfo, app, opp);
    infoRow(els.youInfo, app, me);
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
    // Lands nearest their own controller, everything else in the row further
    // from them - so `#opp-lands` sits directly under the opponent's hand at
    // the top and `#my-lands` directly above your own hand at the bottom,
    // the two halves mirrored around the shared zones in between.
    const layout = boardLayout(state);
    const mine = seatRows(layout, you);
    const theirs = seatRows(layout, opp.username);
    const live = (el, card) => updatePermanentNode(el, card, app, handlers.onPermanent);
    patchStacks(els.oppLands, theirs.lands, FIT, live);
    patchStacks(els.oppBf, theirs.main, FIT, live);
    patchStacks(els.myBf, mine.main, FIT, live);
    patchStacks(els.myLands, mine.lands, FIT, live);
    if (els.zoneRibbon) patchZoneRibbon(els.zoneRibbon, zoneRibbon(state, layout), app, handlers);
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
