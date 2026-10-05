// The one in-flight action: cast, activate, crew, station, or a special action. Pure
// state transitions; `render/ribbon.js` draws it and `app.js` fires it.
import {
  activeSlotIndex,
  fill,
  flatten,
  isSatisfied,
  sharesValid,
  slotsFromAffordance,
  unfill,
} from "./targeting.js";
import { manaSources, pin, unpin } from "./mana.js";

function castHints(card) {
  return (card && card.cast) || {};
}

/**
 * CR 601.2b's optional additional costs this card prints - kicker,
 * multikicker, replicate, offspring, gift, blight, and a printed "As an
 * additional cost to cast this spell, you may ..." sentence - as
 * `affordances.cast_hints` publishes them on every hand and exile card.
 * The engine's own single enumeration
 * (`machines/casting.optional_additional_costs`), never a keyword list of
 * this module's own: a mechanic the engine learns arrives here for free.
 */
export function additionalCosts(card) {
  return castHints(card).additional_costs || [];
}

/** The ones a cast can actually announce. An entry with `understood: false`
 * is printed and optional and this engine cannot charge it, so it is drawn
 * as a notice (`render/ribbon.js`) and never named in a request. */
function announceable(card) {
  return additionalCosts(card).filter((option) => option.understood && option.keyword);
}

/** CR 702.51a's convoke, CR 702.66a's delve and CR 702.126a's improvise, as
 * `affordances.cast_hints` reads them off the printing - the *printed* half
 * of the same question `action.affordance.object_costs` answers against a
 * board. This one rides the board poll, so it is in hand before any
 * affordance fetch, which is what `needsChoices` below needs it to be. */
export function objectCosts(card) {
  return castHints(card).object_costs || [];
}

/** Whether mode `n` of `card` is one this engine cannot read into an effect
 * (`ModeOptionView.understood`, from `engine_api.unreadable_modes`).
 * `render/ribbon.js` never draws a chip for one; this is the same answer at
 * the store, so no other surface can announce a mode the cast would only be
 * refused for once the targets had been chosen. */
export function modeUnreadable(card, n) {
  const modes = castHints(card).modes;
  const option = ((modes && modes.options) || []).find((m) => m.number === n);
  return Boolean(option && option.understood === false);
}

function abilityOf(card, abilityIndex) {
  return abilityIndex === null || abilityIndex === undefined
    ? null
    : ((card && card.abilities) || [])[abilityIndex] || null;
}

/** Whether beginning this action has anything to ask the player before it
 * can fire - what `app.js`'s `beginCast` consults instead of posting the
 * cast on the click that opened the menu.
 *
 * `spellFace` is CR 709.3's chosen half, and it always answers true: a
 * half's own targeting is not on the card view at all - `cast.targeting` is
 * `targeting_profile(card)`, which reads the printing's FRONT text, so a
 * Tear cast read Wear's "target artifact" and an Incongruity cast read
 * Incubation's silence. The server can answer it (`GET .../affordances?
 * spell_face=...`, CR 709.3b), and one extra read is the price of never
 * firing a half at the wrong half's announcement. */
export function needsChoices(card, abilityIndex, spellFace) {
  const ability = abilityOf(card, abilityIndex);
  if (ability) {
    return Boolean(
      (ability.targeting && ability.targeting.targets_anything) ||
        ability.needs_x ||
        (ability.mana_options && ability.mana_options.length > 1)
    );
  }
  if (spellFace) return true;
  const hints = castHints(card);
  const modes = hints.modes && hints.modes.options && hints.modes.options.length;
  if (modes || (hints.targeting && hints.targeting.targets_anything) || hints.needs_x) return true;
  // CR 601.2b's optional additional costs. The reported defect: Sowing
  // Mycospawn targets nothing, has no X and no modes, so every check above
  // read false and `beginCast` fired the cast on the spot - at its unkicked
  // price, with the kicker never offered. An entry the engine cannot charge
  // counts too: the player is owed the notice before the cast is committed,
  // not after.
  if (additionalCosts(card).length) return true;
  // CR 702.51a/702.66a/702.126a: a cost that objects can pay instead of mana
  // is an unmade decision of the same shape. Kappa Cannoneer targets nothing,
  // has no X, no modes and no optional additional cost, so every check above
  // read false and the cast fired at its full {5}{U} - artifacts untapped,
  // the step never drawn, the cast refused for mana the player had sitting
  // on the board.
  if (objectCosts(card).length) return true;
  // CR 118.9 alternative costs (evoke, dash, morph, ...) live entirely on an
  // enter-the-battlefield trigger line for a card like Wistfulness or
  // Solitude, which the engine's `_spell_text` strips before it ever builds
  // `cast.targeting` - so the checks above never see them. `card.cast_options`
  // (`serialize._cast_options_of`, present on every hand card in the board
  // poll before any affordance fetch) is the one place a hand-zone
  // alternative cost is visible this early; any entry there means `beginCast`
  // must fetch the affordance rather than firing at the printed mana cost.
  return Boolean(card && card.cast_options && card.cast_options.length);
}

export function begin(kind, card, extra) {
  extra = extra || {};
  return {
    kind,
    cardId: card.id,
    card,
    abilityIndex: extra.abilityIndex === undefined ? null : extra.abilityIndex,
    keyword: extra.keyword || null,
    // CR 601.3's dynamic play permissions (a flashback grant, an exiled card
    // made playable "this turn") let a card be cast from a zone other than
    // the hand - `null` here means "hand", `CastRequest.from_zone`'s own
    // default (`schemas.py`). Cascade's hit and suspend's third ability no
    // longer grant one of these (battle 8): each casts inline while the
    // permitting spell or ability is still resolving, answered as an ordinary
    // `pending_decision` instead.
    fromZone: extra.fromZone || null,
    // CR 709.3/CR 715.3's chosen face: the half of a split card, or an
    // adventurer card's Adventure, this cast is announcing. `null` names no
    // face - `CastRequest.spell_face`'s own default, and what every
    // single-faced card sends. The string is the engine's own
    // (`Action.spell_face`, published on the row and on
    // `cast.spell_faces[].spell_face`), handed back untransformed.
    spellFace: extra.spellFace || null,
    modes: [],
    // CR 601.2b's optional additional costs, keyed by the keyword
    // `CastRequest.optional_costs[].keyword` takes back, valued by how many
    // times it is announced - a COUNT, because CR 702.33c's multikicker and
    // CR 702.56a's replicate are each paid "any number of times". A boolean
    // here would cap either at one.
    optionalCosts: {},
    xValue: null,
    color: null,
    altCost: null,
    choosing: [],
    convoke: [],
    delve: [],
    // CR 702.126a's artifacts, tapped for one generic each - the same shape
    // `delve` has (a flat list of ids), because `CastRequest.improvise` takes
    // the same.
    improvise: [],
    crewing: [],
    // CR 702.184a: the ONE creature a station permanent taps (a single id,
    // not a list - `StationRequest.tapping`), or null before one is picked.
    tapping: null,
    slots: [],
    shares: {},
    pinned: [],
    freeTargets: [],
    pendingTargets: [],
    // A slot chip's own click (`app.js`'s `onFocusSlot`) can pin which slot
    // the next board click fills, overriding `targetSlotIndex`'s own guess
    // below - `null` defers to it entirely.
    focusSlot: null,
    affordance: null,
    error: null,
    // The casting strip's per-action keep-priority tick. `null` until the
    // player touches it, meaning "follow `stops.keepPriorityByDefault`".
    holdPriority: null,
    done: false,
  };
}

export function withAffordance(action, affordance) {
  const fresh = slotsFromAffordance(affordance);
  const kept = fresh.map((slot, i) => {
    const old = action.slots[i];
    if (!old) return slot;
    const legal = new Set(
      (affordance.candidates || [])
        .filter((c) => (c.requirements || []).includes(slot.requirementIndex))
        .map((c) => c.id)
    );
    return { ...slot, targets: old.targets.filter((id) => legal.has(id)) };
  });
  const pending = action.pendingTargets || [];
  let next = {
    ...action,
    affordance,
    slots: kept,
    pendingTargets: [],
    error: affordance && affordance.error ? affordance.error : null,
  };
  // A click that landed before the affordance did (the candidate fetch is
  // asynchronous) is replayed through the normal slot fill now that there
  // are slots to fill it into, in the order it happened; one that can no
  // longer be filled is silently dropped rather than surfaced as an error.
  for (const id of pending) next = chooseTarget(next, id);
  return next;
}

export function toggleMode(action, n) {
  // A mode this engine cannot read is not a choice: chosen, it resolves
  // doing nothing (CR 700.2a) and the cast is refused for naming it
  // (`engine_api.unreadable_mode_notice`) - after the targets have been
  // picked, which is too late to have chosen another. `render/ribbon.js`
  // draws it as a notice rather than a chip, and this is the same answer at
  // the store, so it cannot be announced from anywhere.
  if (modeUnreadable(action.card, n)) return action;
  const modes = action.modes.includes(n)
    ? action.modes.filter((x) => x !== n)
    : [...action.modes, n];
  // `pendingTargets` is a click queued while the *previous* affordance fetch
  // was still in flight (see `chooseTarget`'s own comment) - a mode change
  // starts a fresh fetch (`slots`/`affordance` reset below) against a
  // possibly different targeting profile entirely, so a queued click from
  // before it must not survive to be replayed against slots it was never
  // clicked for once the new affordance lands.
  return {
    ...action,
    modes: modes.sort((a, b) => a - b),
    slots: [],
    affordance: null,
    pendingTargets: [],
  };
}

export function setX(action, n) {
  return {
    ...action,
    xValue: n === null || n === undefined || Number.isNaN(n) ? null : Number(n),
    slots: [],
    affordance: null,
    // Same reasoning as `toggleMode`: announcing X starts a fresh affordance
    // fetch, so a click queued against the old one must not be replayed
    // into it.
    pendingTargets: [],
  };
}

/**
 * Announce `keyword` `times` times, or drop it entirely at 0 - CR 601.2b's
 * announcement, made before the cast is fired rather than as a separate
 * round trip. `keyword` is the row's own spelling (`Kicker`, `Replicate`);
 * a `null` keyword names an option this engine cannot charge and is
 * ignored, since there is nothing to hand back for one.
 */
export function setOptionalCost(action, keyword, times) {
  if (!keyword) return action;
  const next = { ...action.optionalCosts };
  const count = Number(times);
  if (!count || count < 0 || Number.isNaN(count)) delete next[keyword];
  else next[keyword] = count;
  return { ...action, optionalCosts: next };
}

/** How many times `keyword` is currently announced - 0 when it is not. */
export function optionalCostTimes(action, keyword) {
  return (action.optionalCosts || {})[keyword] || 0;
}

/**
 * The engine's own `GET .../legal-actions` rows, keyed by the browser's
 * `action.kind`. The engine splits an activation in two (`activate` for an
 * ordinary one, `activate_mana` for a mana ability, each its own entry
 * point); this browser's announcement state has a single `activate` for
 * both, so a match that compared the names directly would find a mana
 * ability's row never. `crew`, `station` and `special` are deliberately absent: neither
 * is an `ActionKind` the enumeration answers with.
 */
const ROW_KINDS = { cast: ["cast"], activate: ["activate", "activate_mana"] };

/**
 * The `GET .../legal-actions` row this in-flight announcement IS, out of the
 * rows `app.js`'s own menu read (`enrichActionMenu`) stashed for this card -
 * matched on the three fields that identify an `Action`, which is
 * `menus.js`'s `matchesAction` rule rather than a second one of this file's
 * own.
 *
 * `null` for an announcement no row describes, and that is the ordinary case
 * as often as not: the rows are a snapshot taken on the click that opened
 * the card's menu, so a strip opened before the read landed - or from a path
 * that opened no menu at all - simply has none. Every reader below answers
 * `null` in turn and the strip falls back to what it drew before, which is
 * why none of this can make a control disappear.
 */
export function rowFor(action, rows) {
  const kinds = action ? ROW_KINDS[action.kind] : null;
  if (!kinds) return null;
  const index = action.abilityIndex === undefined ? null : action.abilityIndex;
  for (const row of rows || []) {
    if (!row || row.card_id !== action.cardId) continue;
    if (!kinds.includes(row.kind)) continue;
    if ((row.ability_index === undefined ? null : row.ability_index) !== index) continue;
    return row;
  }
  return null;
}

/**
 * The legal range for this announcement's X, off the engine's own
 * `Action.x_range` - `[minimum, maximum]`, with `maximum` null for an X
 * nothing caps (`models/action.py`: "`maximum` is `None` for a genuinely
 * open-ended X ..., never for 'not applicable'" - that is the whole field
 * being null, which is why this answers `null` and not `[0, null]` for a row
 * that names no X at all).
 *
 * The browser had no reading of this at all: the strip drew
 * `<input type=number min=0>` and an X outside what the engine would take
 * was announced, refused, and reported back as an error. A bound the player
 * can see before they type is the same fact one round trip earlier.
 */
export function xRange(action, rows) {
  const row = rowFor(action, rows);
  const range = row && row.x_range;
  if (!Array.isArray(range) || range.length !== 2) return null;
  const low = Number(range[0]);
  const high = range[1] === null || range[1] === undefined ? null : Number(range[1]);
  if (Number.isNaN(low) || (high !== null && Number.isNaN(high))) return null;
  return [low, high];
}

/** `n` brought inside `range` (`xRange`'s own shape, or `null` for an
 * announcement no row bounded - which is left exactly as typed). */
export function clampX(n, range) {
  if (!range || n === null || n === undefined || Number.isNaN(n)) return n;
  const [low, high] = range;
  if (n < low) return low;
  if (high !== null && n > high) return high;
  return n;
}

/**
 * What the engine says about announcing `keyword` on this row - one entry of
 * `ActionRow.optional_costs`, or `null` when no row names it.
 *
 * The strip offers the options themselves from `card.cast.additional_costs`,
 * which is what the browser holds the moment the card is picked up. That
 * enumeration says what the card prints; it does not say what this board can
 * pay. Only the row does: it prices each option with this row's own cost
 * plus that one and reports `affordable` and `unaffordable_reason` per
 * option, in the engine's own words. The two are not the same fact, which is
 * why one does not supersede the other.
 */
export function optionalCostOffer(action, rows, keyword) {
  if (!keyword) return null;
  const row = rowFor(action, rows);
  for (const option of (row && row.optional_costs) || []) {
    if (option && option.keyword === keyword) return option;
  }
  return null;
}

export function chooseColor(action, color) {
  return { ...action, color };
}

export function chooseAltCost(action, index) {
  return { ...action, altCost: index === undefined ? null : index, choosing: [] };
}

function toggleIn(list, id) {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
}

export function togglePitch(action, id) {
  return { ...action, choosing: toggleIn(action.choosing, id) };
}

export function toggleConvoke(action, id) {
  return { ...action, convoke: toggleIn(action.convoke, id) };
}

export function toggleDelve(action, id) {
  return { ...action, delve: toggleIn(action.delve, id) };
}

export function toggleImprovise(action, id) {
  return { ...action, improvise: toggleIn(action.improvise, id) };
}

export function toggleCrewing(action, id) {
  return { ...action, crewing: toggleIn(action.crewing, id) };
}

/** Station takes exactly one creature: clicking another replaces it, clicking
 * the chosen one again clears it. */
export function selectTapping(action, id) {
  return { ...action, tapping: action.tapping === id ? null : id };
}

export function addFreeTarget(action, id) {
  return { ...action, freeTargets: toggleIn(action.freeTargets, id) };
}

function canTake(action, slotIndex, id) {
  const slot = action.slots[slotIndex];
  const candidates = (action.affordance && action.affordance.candidates) || [];
  return candidates.some(
    (c) => c.id === id && (c.requirements || []).includes(slot.requirementIndex)
  );
}

export function chooseTarget(action, id) {
  if (action.slots.length === 0 && !action.affordance) {
    // The affordance hasn't arrived yet (it's fetched asynchronously right
    // after `begin`), so there is nothing to fill. Remember the click and
    // `withAffordance` will replay it once real slots exist.
    return { ...action, pendingTargets: toggleIn(action.pendingTargets, id) };
  }
  const already = action.slots.findIndex((s) => s.targets.includes(id));
  if (already >= 0) {
    // A second click on a chosen object unfills it from the LAST slot it sits
    // in, so a player who put "alice" in mode 1 and mode 2 and clicks once
    // removes only the mode 2 one.
    const last = action.slots.map((s) => s.targets.includes(id)).lastIndexOf(true);
    const active = activeSlotIndex(action.slots);
    if (active >= 0 && action.affordance && canTake(action, active, id)) {
      return { ...action, slots: fill(action.slots, active, id) };
    }
    return { ...action, slots: unfill(action.slots, last, id) };
  }
  const active = activeSlotIndex(action.slots);
  if (active < 0 || !canTake(action, active, id)) return action;
  return { ...action, slots: fill(action.slots, active, id) };
}

export function setShare(action, id, n) {
  return { ...action, shares: { ...action.shares, [id]: Number(n) } };
}

/** `abilityIndex` is `null` for a source with a single mana ability (nothing
 * to choose, so nothing is named and the request keeps its old shape) and
 * the chosen index when the player picked among several - `mana.js`'s `pin`
 * carries the rest. */
export function pinSource(action, id, color, abilityIndex) {
  return { ...action, pinned: pin(action.pinned, id, color, abilityIndex) };
}

export function unpinSource(action, id) {
  return { ...action, pinned: unpin(action.pinned, id) };
}

export function markDone(action) {
  return { ...action, done: true };
}

/**
 * Which slot a board click should fill right now: `action.focusSlot` when
 * a slot chip's own click (`app.js`'s `onFocusSlot`) has pinned one,
 * otherwise `targeting.js`'s own `activeSlotIndex` guess. The single place
 * that decides it, used by both `render/board.js`'s `targetingOpts` (which
 * slot gets the "active" highlight and which candidates light up) and
 * `app.js`'s own click handler (which slot a click actually fills) - so the
 * outlined slot and the slot a click lands in can never disagree.
 */
export function targetSlotIndex(action) {
  const focus = action.focusSlot;
  if (focus !== null && focus !== undefined && action.slots[focus]) return focus;
  return activeSlotIndex(action.slots);
}

export function affordanceQuery(action) {
  const query = {};
  if (action.abilityIndex !== null) query.ability_index = action.abilityIndex;
  if (action.modes.length) query.modes = action.modes;
  if (action.xValue !== null) query.x_value = action.xValue;
  // CR 709.3b: the chosen half's own text is what the announcement is read
  // against, so the candidate list has to be asked for per face.
  if (action.spellFace) query.spell_face = action.spellFace;
  return query;
}

export function modesReady(action) {
  const modes = castHints(action.card).modes;
  if (!modes || !modes.options || !modes.options.length || action.abilityIndex !== null)
    return true;
  return action.modes.length >= modes.minimum && action.modes.length <= modes.maximum;
}

/** The `cast.spell_faces` entry this action is announcing, or null - both
 * for an action that named no face and for a card that publishes none.
 * Exported as `faceBeingCast` below for `render/ribbon.js`, which has to
 * print the half rather than the printing. */
function chosenFace(action) {
  const faces = castHints(action.card).spell_faces || [];
  return faces.find((face) => (face.spell_face ?? null) === (action.spellFace ?? null)) || null;
}

/** The face this cast is announcing (CR 709.3's half, CR 715.3's Adventure)
 * as the server published it - `{spell_face, name, mana_cost, type_line,
 * needs_x}` - or null when the cast names no face. What the casting strip
 * prints instead of the printing's own name and combined cost, which are
 * neither half's (CR 709.4). */
export function faceBeingCast(action) {
  return action.spellFace ? chosenFace(action) : null;
}

/** Whether this announcement has an X to announce (CR 107.3a) - exported
 * because `render/ribbon.js`'s X box asks the same question and must never
 * answer it differently from `readyToFire` below.
 *
 * The server's own answer in every case: `AbilityView.needs_x` for an
 * activation, the chosen face's `needs_x` for a cast that named one (CR
 * 709.3a evaluates only that half, so only its own cost announces an X), and
 * `CastHintsView.needs_x` otherwise - which counts a printed additional
 * cost's X (Toxic Deluge's "pay X life") as well as an `{X}` in the mana
 * cost. Nothing here reads a cost string of its own. */
export function needsX(action) {
  const ability = abilityOf(action.card, action.abilityIndex);
  if (ability) return Boolean(ability.needs_x);
  const face = chosenFace(action);
  return Boolean(face ? face.needs_x : castHints(action.card).needs_x);
}

function targeting(action) {
  if (action.affordance) return action.affordance;
  const ability = abilityOf(action.card, action.abilityIndex);
  return ability ? ability.targeting || {} : castHints(action.card).targeting || {};
}

// A slot with no fixed count ("up to N", or an unbounded "any number of") -
// CR 601.2c's "the player announces their choice" never forces every such
// slot to be filled all the way. Shared by `needsExplicitFire` (which cares
// only about the slot's *shape*, filled or not) and `readyToFire` (which
// additionally cares whether it still has room).
function isUnboundedSlot(s) {
  return s.count === null || s.minimum < (s.count || 0);
}

// Whether this cast has a CR 118.9 alternative cost the player has not yet
// chosen, and could: `action.affordance.alternative_costs` is what
// `render/ribbon.js`'s `renderAltCosts` draws its chips from, one `{index,
// available, ...}` entry per option the engine offers - never a hard-coded
// keyword name, since which keywords are alternative costs is the engine's
// to answer. An option the seat cannot currently pay (`available: false`)
// is not worth stopping for; once `action.altCost` names the chosen index
// (`chooseAltCost`), the choice has been made and this reads false again.
function hasChoosableAltCost(action) {
  if (action.altCost !== null && action.altCost !== undefined) return false;
  const alts = (action.affordance && action.affordance.alternative_costs) || [];
  return alts.some((alt) => alt.available);
}

/**
 * Whether firing this action needs an explicit confirmation rather than
 * happening automatically the moment it is legal: pinned mana was chosen by
 * hand, the cost divides across targets (CR 601.2d), any target slot is
 * "up to"/unbounded, or an available alternative cost has not yet been
 * chosen. The "up to" case holds whether or not the slot is currently
 * filled to its cap - a spell offering "up to three targets" that already
 * has three chosen is still one the player might have meant to stop short
 * on, so it is never auto-fired just because `readyToFire` agrees the
 * announcement would already be legal. A non-targeting cast with a real,
 * payable alternative cost is the same shape of unmade decision: the player
 * never asked for anything, so firing it at full price the instant it
 * becomes legal is exactly as wrong as firing an unbounded target slot
 * empty.
 */
export function needsExplicitFire(action) {
  return Boolean(
    action.pinned.length > 0 ||
      (action.affordance && action.affordance.division) ||
      action.slots.some(isUnboundedSlot) ||
      hasChoosableAltCost(action) ||
      // CR 601.2b: a printed optional additional cost is an unmade decision
      // of exactly the same shape as an unchosen alternative cost - firing
      // the spell unkicked the instant it becomes legal is the player never
      // being asked. It holds whether or not a count has already been
      // entered (a kicker already ticked is still one the player might have
      // meant to take back), and whether or not the engine can charge it:
      // an `understood: false` notice is worth reading before the cast.
      (action.kind === "cast" && additionalCosts(action.card).length > 0) ||
      // CR 702.51b/702.66a/702.126b: convoke, delve and improvise apply
      // after the total cost is determined, so a cast that could spend
      // objects instead of mana is legal at the full printed price and
      // firing it there the instant it becomes legal is the player never
      // being asked - the same reasoning as an unchosen alternative cost.
      // The printed keyword is enough; whether anything is currently
      // eligible is `renderObjectCostStep`'s own "nothing eligible right
      // now".
      (action.kind === "cast" && objectCosts(action.card).length > 0)
  );
}

// How many objects the *chosen* alternative cost still needs in
// `action.choosing` before it is actually payable - Solitude's evoke is
// "exile a white card from your hand" (`requires: 1`), Wistfulness's is
// mana-only (`requires: 0`). An alt cost that has not been chosen
// (`action.altCost` still null) imposes nothing here; that is
// `hasChoosableAltCost`'s own job below.
function chosenAltCostRequires(action) {
  if (action.altCost === null || action.altCost === undefined) return 0;
  const alts = (action.affordance && action.affordance.alternative_costs) || [];
  const chosen = alts.find((alt) => alt.index === action.altCost);
  return (chosen && chosen.requires) || 0;
}

export function readyToFire(action) {
  if (action.kind === "crew") return action.crewing.length > 0;
  if (action.kind === "station") return action.tapping !== null;
  // A special action (foretell, plot, suspend, CR 116.2) exiles the card
  // face down without choosing anything about the spell it will become -
  // that happens later, when it's cast from exile - so the underlying
  // card's own cast.targeting (which may well target) never applies here.
  if (action.kind === "special") return true;
  if (!modesReady(action)) return false;
  if (needsX(action) && action.xValue === null) return false;
  // A chosen alternative cost that still needs objects pitched/exiled/
  // sacrificed for it must not fire, or even dry-run, until they are
  // (`onChooseAltCost`'s own click would otherwise post a refused dry run
  // before the player can click a single pitch chip - the reported evoke
  // defect's third half).
  if (action.choosing.length < chosenAltCostRequires(action)) return false;
  const profile = targeting(action);
  if (profile.targets_anything) {
    if (!action.affordance) return false;
    if (!isSatisfied(action.slots)) return false;
    const open = action.slots.some(
      (s) => isUnboundedSlot(s) && s.targets.length < (s.count === null ? Infinity : s.count)
    );
    if (open && !action.done) return false;
    if (action.affordance.division) {
      const targets = flatten(action.slots);
      if (!sharesValid(targets.map((id) => action.shares[id]), action.affordance.division.amount))
        return false;
    }
  }
  return true;
}

export function endpointFor(action) {
  if (action.kind === "activate") return "actions/activate";
  if (action.kind === "crew") return "actions/crew";
  if (action.kind === "station") return "actions/station";
  if (action.kind === "special") return `actions/${action.keyword.toLowerCase()}`;
  return "actions/cast";
}

/** Whether `action` activates a mana ability - exported for `app.js`'s
 * `fire()`, which never auto-passes after one, paused or not. */
export function isManaAbility(action) {
  if (action.kind !== "activate") return false;
  const ability = abilityOf(action.card, action.abilityIndex);
  return Boolean(ability && ability.is_mana_ability);
}

export function bodyFor(action, you, untapped) {
  const body = { player: you, card_id: action.cardId };
  if (action.kind === "crew") return { ...body, crewing: action.crewing };
  if (action.kind === "station") return { ...body, tapping: action.tapping };
  if (action.kind === "activate") {
    body.ability_index = action.abilityIndex;
    // "U,R,R" is the split of an "any combination" ability: `colors`.
    if (action.color && String(action.color).includes(",")) {
      body.colors = String(action.color).split(",");
    } else if (action.color) body.color = action.color;
  }
  if (action.kind !== "special") {
    // A special action's request has no `targets`/`shares` fields at all
    // (CR 116.2's exile-face-down actions don't choose anything about the
    // spell they'll become) - the underlying card's targeting hint would
    // otherwise leak in through `targeting(action)`'s cast-level fallback.
    const profile = targeting(action);
    if (profile.targets_anything) {
      body.targets = flatten(action.slots);
    } else if (
      profile.understood === false &&
      action.slots.length === 0 &&
      action.freeTargets &&
      action.freeTargets.length
    ) {
      body.targets = [...action.freeTargets];
    }
    if (action.affordance && action.affordance.division) {
      body.shares = flatten(action.slots).map((id) => action.shares[id]);
    }
  }
  if (action.kind === "cast") {
    // CR 709.3's chosen half, as `CastRequest.spell_face` - sent only when
    // the player actually named one, so every cast that named no face sends
    // the same body it always did.
    if (action.spellFace) body.spell_face = action.spellFace;
    if (action.modes.length) body.modes = action.modes;
    if (action.xValue !== null) body.x_value = action.xValue;
    if (action.keyword) body.keyword = action.keyword;
    if (action.convoke.length) body.convoke = action.convoke.map((id) => ({ card_id: id }));
    if (action.delve.length) body.delve = action.delve;
    if (action.improvise.length) body.improvise = action.improvise;
    if (action.altCost !== null) body.alternative_cost = action.altCost;
    if (action.choosing.length) body.choosing = action.choosing;
    // CR 601.2b's announcement, as counts - `CastRequest.optional_costs`.
    // Only what the card actually offers and the engine can actually charge
    // is ever named, and an announcement of 0 is left off entirely rather
    // than sent as "kicked zero times", which is the same body every cast
    // written before this field existed sends.
    const announced = announceable(action.card)
      .map((option) => ({ keyword: option.keyword, times: optionalCostTimes(action, option.keyword) }))
      .filter((entry) => entry.times > 0);
    if (announced.length) body.optional_costs = announced;
    // CR 601.3's dynamic play permissions (a flashback grant, an exiled card
    // made playable "this turn"): `CastRequest.from_zone` (`schemas.py`)
    // defaults to "hand", so this is only sent when the cast began from
    // `beginCastFromPermission`. Cascade and suspend no longer reach here at
    // all (battle 8) - accepting either is answered as an ordinary
    // `pending_decision`, not a permission a seat drives through `cast`.
    if (action.fromZone) body.from_zone = action.fromZone;
  } else if (action.kind === "activate" && action.xValue !== null) {
    body.x_value = action.xValue;
  }
  const sources = manaSources(action.pinned, untapped || []);
  // A mana ability's own source cannot pay for its own activation - drop
  // just that one id/ManaSource rather than the whole list, so a filter
  // land's `{1}, {T}: Add ...` can still be paid by another untapped source.
  const offered = isManaAbility(action)
    ? sources.filter((s) => (typeof s === "string" ? s : s.card_id) !== action.cardId)
    : sources;
  if (offered.length) body.mana_sources = offered;
  return body;
}
