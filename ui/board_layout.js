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
