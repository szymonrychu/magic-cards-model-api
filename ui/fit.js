// How wide a card may be in one board row - pure arithmetic, no DOM, so
// `tests/test_ui_fit.py` can ask it directly. `render/board.js` measures each
// row, asks this, and applies the answer as the row's `--card-w`.
//
// The geometry has to be the same one `style.css` draws:
//
// - a card is 63:88, so a card `w` wide is `w * CARD_ASPECT` tall;
// - a tapped card reserves a landscape box, so across the row it takes the
//   card's height rather than its width;
// - each card tucked under a permanent sits `TUCK_X * w` right of and
//   `TUCK_Y * w` below the one above it, and its pile reserves that room
//   (`.perm-stack`'s `--tuck-x`/`--tuck-y` are these same two ratios);
// - a card never gets narrower than `MIN_CARD_WIDTH`, which is still
//   readable - past that the row scrolls sideways instead of shrinking.

export const CARD_ASPECT = 88 / 63;
export const TUCK_X = 9 / 108;
export const TUCK_Y = 20 / 108;
export const MIN_CARD_WIDTH = 44;

/**
 * `{ cardWidth, scroll }` for one row: `cardWidth` is the largest whole-pixel
 * width at which every card's footprint fits `width` (with `gap` between
 * neighbours) and the tallest card - with the offsets of whatever is tucked
 * under it - fits `height`, but never below `floor`. `scroll` is true when
 * the footprints do not fit even at the floor, so the row has to scroll.
 *
 * `count` cards, `tapped` of them standing in a landscape box, and `depths`
 * the number of cards tucked under each one (missing entries are 0).
 */
export function fitCardWidth({ width, height, count, tapped = 0, depths = [], gap = 0, floor = MIN_CARD_WIDTH }) {
  const tuckedTotal = depths.reduce((sum, depth) => sum + (depth || 0), 0);
  const deepest = depths.reduce((max, depth) => Math.max(max, depth || 0), 0);
  // A row of nothing but tapped cards with nothing tucked under them is only
  // ever `w` tall; anything else has a portrait card in it somewhere.
  const tallest = count > 0 && tapped >= count && deepest === 0 ? 1 : CARD_ASPECT + deepest * TUCK_Y;
  const byHeight = height / tallest;
  if (count <= 0) return { cardWidth: Math.max(floor, whole(byHeight)), scroll: false };
  const across = count - tapped + tapped * CARD_ASPECT + tuckedTotal * TUCK_X;
  const byWidth = (width - gap * (count - 1)) / across;
  const scroll = whole(byWidth) < floor;
  return { cardWidth: Math.max(floor, whole(Math.min(byWidth, byHeight))), scroll };
}

/** The smallest a shared floor may get on a very short window - below this
 * the board scrolls inside itself rather than drawing unreadable slivers. */
export const MIN_FLOOR_WIDTH = 20;

/**
 * The floor width every one of a board's `rows` card rows shares, given the
 * `height` the board has left for cards once its other children, gaps, row
 * padding and captions are taken off: `floor` on any window with room for one card
 * that wide per row, and on a short one (a phone in landscape) the widest
 * card for which one per row still fits `height` - never below `minimum`.
 */
export function fitFloor({ height, rows, floor = MIN_CARD_WIDTH, minimum = MIN_FLOOR_WIDTH }) {
  if (!(rows > 0)) return floor;
  return Math.min(floor, Math.max(minimum, whole(height / rows / CARD_ASPECT)));
}

// Floored to a whole pixel, so rounding can never push the last card over the
// edge - with a hair of tolerance, so an exact fit is not lost to float error.
function whole(value) {
  return Math.floor(value + 1e-9);
}
