// The one in-flight action: cast, activate, crew, or a special action. Pure
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

function abilityOf(card, abilityIndex) {
  return abilityIndex === null || abilityIndex === undefined
    ? null
    : ((card && card.abilities) || [])[abilityIndex] || null;
}

export function needsChoices(card, abilityIndex) {
  const ability = abilityOf(card, abilityIndex);
  if (ability) {
    return Boolean(
      (ability.targeting && ability.targeting.targets_anything) ||
        ability.needs_x ||
        (ability.mana_options && ability.mana_options.length > 1)
    );
  }
  const hints = castHints(card);
  const modes = hints.modes && hints.modes.options && hints.modes.options.length;
  if (modes || (hints.targeting && hints.targeting.targets_anything) || hints.needs_x) return true;
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
    // CR 601.3's dynamic play permissions (cascade, suspend's third ability)
    // let a card be cast from a zone other than the hand - `null` here means
    // "hand", `CastRequest.from_zone`'s own default (`schemas.py`).
    fromZone: extra.fromZone || null,
    modes: [],
    xValue: null,
    color: null,
    altCost: null,
    choosing: [],
    convoke: [],
    delve: [],
    crewing: [],
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

export function toggleCrewing(action, id) {
  return { ...action, crewing: toggleIn(action.crewing, id) };
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
  return query;
}

export function modesReady(action) {
  const modes = castHints(action.card).modes;
  if (!modes || !modes.options || !modes.options.length || action.abilityIndex !== null)
    return true;
  return action.modes.length >= modes.minimum && action.modes.length <= modes.maximum;
}

function needsX(action) {
  const ability = abilityOf(action.card, action.abilityIndex);
  return ability ? Boolean(ability.needs_x) : Boolean(castHints(action.card).needs_x);
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
      hasChoosableAltCost(action)
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
  if (action.kind === "special") return `actions/${action.keyword.toLowerCase()}`;
  return "actions/cast";
}

function isManaAbility(action) {
  if (action.kind !== "activate") return false;
  const ability = abilityOf(action.card, action.abilityIndex);
  return Boolean(ability && ability.is_mana_ability);
}

export function bodyFor(action, you, untapped) {
  const body = { player: you, card_id: action.cardId };
  if (action.kind === "crew") return { ...body, crewing: action.crewing };
  if (action.kind === "activate") {
    body.ability_index = action.abilityIndex;
    if (action.color) body.color = action.color;
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
    if (action.modes.length) body.modes = action.modes;
    if (action.xValue !== null) body.x_value = action.xValue;
    if (action.keyword) body.keyword = action.keyword;
    if (action.convoke.length) body.convoke = action.convoke.map((id) => ({ card_id: id }));
    if (action.delve.length) body.delve = action.delve;
    if (action.altCost !== null) body.alternative_cost = action.altCost;
    if (action.choosing.length) body.choosing = action.choosing;
    // CR 601.3's dynamic play permissions (cascade, suspend's own third
    // ability): `CastRequest.from_zone` (`schemas.py`) defaults to "hand",
    // so this is only sent when the cast began from `beginCastFromPermission`.
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
