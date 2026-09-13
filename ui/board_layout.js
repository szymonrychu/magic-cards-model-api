// Where each permanent goes on the board, as pure functions over one
// serialised state view - no DOM, so `tests/test_ui_board_layout.py` can ask
// it the awkward questions directly (`ui/app.js`'s `bootTarget` is the same
// seam). `render/board.js` does the drawing and nothing else.
//
// Two conventions, and one of them is not a rule:
//
// 1. Each seat's battlefield is two rows - lands in the row nearest that
//    seat, everything else in the row further from them, so the two halves
//    mirror around the stack. The Comprehensive Rules prescribe no physical
//    arrangement of the battlefield at all: it is a shared zone, and this
//    layout comes from play practice and tournament board-state clarity.
//    There is deliberately no CR citation anywhere in this file.
// 2. A card that physically lives *under* another card is drawn under it:
//    an Aura or an Equipment attached to a permanent (`attached_to`), and a
//    card exiled with a permanent - imprint, hideaway - which the state view
//    reports as `exile_pile.linked_permanent_id`. Both relationships come
//    from the engine through `serialize.py`; neither is ever guessed from
//    card text or from the order cards happen to arrive in.

/** Whether this permanent belongs in its controller's land row.
 *
 * Read off `type_line`, which the state view already computes for a
 * permanent rather than copying off the printing - so an activated manland
 * arrives here saying "Land Creature" and an animated land says whatever it
 * has become.
 *
 * A permanent that is *both* a land and a creature (Dryad Arbor, a manland
 * that has been activated) goes with the creatures, not with the lands: it
 * can attack, block, and be targeted as a creature, and the non-land row is
 * the one combat is read off. The physical convention agrees - you slide a
 * manland forward when you animate it. The reverse case (a creature that has
 * become a land and nothing else) falls out of the same test and lands in
 * the mana row, which is where it now belongs.
 */
export function isLandRow(card) {
  const line = (card && card.type_line) || "";
  return /\bland\b/i.test(line) && !/\bcreature\b/i.test(line);
}

/** The battlefield card `card` is ultimately tucked under, or `null` when it
 * stands on its own.
 *
 * Follows a chain (an Aura on an Equipment on a creature) down to the
 * permanent that is on the battlefield in its own right, so a pile is always
 * one card deep to read rather than a tuck inside a tuck. Returns `null` for
 * an attachment naming a player, naming a card that is not on the
 * battlefield, or sitting in a cycle - in every one of those the card is
 * still drawn as an ordinary permanent instead of disappearing.
 */
function rootParentId(card, byId) {
  const seen = new Set([card.id]);
  let current = card;
  let parentId = null;
  for (;;) {
    const ref = current.attached_to;
    if (!ref || ref.kind !== "card") return parentId;
    const parent = byId.get(ref.id);
    if (!parent) return parentId;
    // A cycle has no root to tuck anything under, so every card in it is
    // drawn as an ordinary permanent rather than half of them vanishing
    // under the other half.
    if (seen.has(parent.id)) return null;
    seen.add(parent.id);
    parentId = parent.id;
    current = parent;
  }
}

/** The whole board, split the way it is drawn.
 *
 * `{ seats: { [controller]: { lands: Entry[], main: Entry[] } }, exile:
 * card[] }`, where an `Entry` is `{ card, under: card[] }` - the permanent
 * and the pile of cards physically beneath it. A seat with nothing on the
 * battlefield gets no key; `render/board.js` supplies the empty rows.
 *
 * `exile` is what is left of the shared exile zone once the cards exiled
 * *with* a permanent still on the battlefield have been pulled out to be
 * drawn under it - a card exiled with a permanent that has since left stays
 * in exile, because that is where it still is.
 */
export function boardLayout(state) {
  const battlefield = (state && state.battlefield) || [];
  const exiled = (state && state.exile) || [];
  const byId = new Map(battlefield.map((card) => [card.id, card]));

  const entries = new Map();
  const tucked = new Map();
  for (const card of battlefield) {
    const parentId = rootParentId(card, byId);
    if (parentId === null) entries.set(card.id, { card, under: [] });
    else tucked.set(card.id, parentId);
  }
  // Two passes, so a pile is filled only after every top-level permanent
  // exists - the order cards arrive in says nothing about which of an
  // attachment and its host comes first.
  for (const card of battlefield) {
    const parentId = tucked.get(card.id);
    if (parentId !== undefined && entries.has(parentId)) entries.get(parentId).under.push(card);
  }

  const exile = [];
  for (const card of exiled) {
    const link = (card.exile_pile && card.exile_pile.linked_permanent_id) || null;
    const host = link === null ? null : byId.get(link);
    const parentId = host ? rootParentId(host, byId) || host.id : null;
    if (parentId !== null && entries.has(parentId)) entries.get(parentId).under.push(card);
    else exile.push(card);
  }

  const seats = {};
  for (const entry of entries.values()) {
    const seat = (seats[entry.card.controller] = seats[entry.card.controller] || {
      lands: [],
      main: [],
    });
    seat[isLandRow(entry.card) ? "lands" : "main"].push(entry);
  }
  return { seats, exile };
}

/** `boardLayout(state).seats[username]`, with the empty rows filled in. */
export function seatRows(layout, username) {
  return layout.seats[username] || { lands: [], main: [] };
}

/** Whose card this is - the same `controller or owner` fallback `serialize.py`
 * uses to decide who could ever cast a card sitting in a graveyard or exile.
 * `null` when the state view attributes it to nobody. */
function belongsTo(card) {
  return (card && (card.controller || card.owner)) || null;
}

/** The zone ribbon between the battlefield and the control ribbon, top to
 * bottom: the opponent's command zone, the opponent's graveyard, the
 * opponent's exile, your exile, your graveyard, your command zone.
 *
 * That order is the battlefield's own mirroring applied to the zones - the
 * far seat's cards far from you, your own nearest you - not a uniform list.
 * The command zone bookends the graveyard/exile pair the ribbon has always
 * had rather than sitting between them, so a board with nothing in either
 * command zone reads exactly as it always did. Reversing either half would
 * put a seat's zones on the other seat's side of the board.
 *
 * Returns six rows, always all six: `{key, side, zone, seat, seatLabel,
 * cards}`. A zone with nothing in it still gets its row, so the board keeps
 * its geometry from turn one instead of reflowing the first time a card is
 * discarded; `render/board.js` collapses an empty row to its label.
 *
 * Exile is one shared zone and the state view sends it as a single list, so
 * the two exile rows are that list split by whose card each one is.
 * A card the view attributes to nobody is drawn on the far side rather than
 * dropped: exile is a public zone and a card in it must be on screen
 * somewhere. What is drawn is `layout.exile` - a card exiled *with* a
 * permanent is already drawn tucked under it and is not in the ribbon at all.
 * The command zone (CR 408) is split the same way: `state.command` is one
 * shared list, and this is the only place on the board that ever draws it -
 * the "Zones" popover this replaced was the sole way to see a commander or an
 * emblem at all, so it moved into the ribbon itself rather than dropping out
 * of the UI along with the button that used to open it.
 *
 * `layout` defaults to `boardLayout(state)`; `render/board.js` passes the one
 * it already computed for the battlefield rather than paying for a second.
 */
export function zoneRibbon(state, layout) {
  const you = (state && state.you) || null;
  const players = (state && state.players) || [];
  const opponent = players.map((p) => p.username).find((name) => name !== you) || null;
  const exile = (layout || boardLayout(state)).exile;
  const graveyards = (state && state.graveyards) || {};
  const graveyard = (seat) => (seat === null ? [] : graveyards[seat] || []);
  const command = (state && state.command) || [];
  const row = (side, zone, seat, cards) => ({
    key: `${side}-${zone}`,
    side,
    zone,
    seat,
    seatLabel: seat === null ? "opponent" : seat,
    cards,
  });
  return [
    row("opp", "command", opponent, command.filter((c) => belongsTo(c) !== you)),
    row("opp", "graveyard", opponent, graveyard(opponent)),
    row("opp", "exile", opponent, exile.filter((c) => belongsTo(c) !== you)),
    row("you", "exile", you, exile.filter((c) => belongsTo(c) === you)),
    row("you", "graveyard", you, graveyard(you)),
    row("you", "command", you, command.filter((c) => belongsTo(c) === you)),
  ];
}
