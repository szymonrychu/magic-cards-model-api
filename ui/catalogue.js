// The batched `GET /api/cards/{name}` printing lookup - every page that
// draws a board from a ledger snapshot's bare card names needs the same
// join-by-name to Scryfall printings for a type line and art. Started life
// inside `render/review.js`; moved out here (R4) so `render/replay.js` uses
// the exact same batching rather than a second copy of it, and so a future
// third caller has somewhere to import it from instead of copying it again.

import { api } from "./api.js";
import { apiBase } from "./config.js";

/** How many printings to look up at once. The lookups are one HTTP call per
 * distinct card name (`GET /api/cards/{name}`, Scryfall behind a cache), and
 * a game touches a few dozen names at most - this only keeps a cold cache
 * from opening fifty sockets at once. */
const LOOKUP_BATCH = 6;

/**
 * The printings `names` need, by name. A name the server cannot answer for
 * is simply absent from the result; the caller's own card renderer
 * (`cards.js`) draws that card as its text fallback rather than this module
 * inventing a placeholder.
 */
export async function loadCatalogue(names) {
  const catalogue = {};
  for (let index = 0; index < names.length; index += LOOKUP_BATCH) {
    const batch = names.slice(index, index + LOOKUP_BATCH);
    const answers = await Promise.all(
      batch.map((name) =>
        api("GET", `${apiBase()}/cards/${encodeURIComponent(name)}`).catch(() => null)
      )
    );
    for (const answer of answers) {
      if (!answer || !answer.requested) continue;
      const card = answer.card || {};
      const images = answer.images || {};
      catalogue[answer.requested] = {
        type_line: card.type_line || "",
        oracle_text: card.oracle_text || "",
        mana_cost: card.mana_cost || "",
        image: images.normal || images.small || null,
        small_image: images.small || images.normal || null,
      };
    }
  }
  return catalogue;
}
