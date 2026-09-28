// Priority automation: two verbs, and a persisted stops panel that says when
// the second one hands the board back. Two key bindings, MTGO's own: F2 is
// `pass` and F6 is `pass_until` (`shortcutVerb` below), live only while
// `passButtonsFor` offers that verb.
//
// Two verbs because the rules give priority exactly two things to do with it.
// CR 117.1: "The player with priority may cast spells, activate abilities, and
// take special actions." CR 117.3d: "If a player has priority and chooses not
// to take any actions, that player passes." That is `pass`. `pass_until` is
// the same verb run automatically until a stop fires - not a different move,
// just the one move repeated. Everything that *follows* a pass is the game's
// own doing rather than a button: CR 117.4 resolves the top of the stack, or
// ends the step, once all players have passed in succession.
//
// This replaces five verbs (`pass`, `pass_turn`, `pass_turn_regardless`,
// `until_my_turn`, `until_castable`) that were five spellings of the same
// pass with different stop conditions baked into each. The stop conditions
// are the stops panel's job, so they moved there: "even with a stack" is the
// `onStack` tick unticked, and the three turn-scoped destinations are step
// ticks like any other.
//
// A stop is one of three kinds, and only the first is a tick:
//
//   1. a *checked stop* - a step on `stops.mine`/`stops.theirs`, or the
//      `stops.onStack` tick;
//   2. an *unconditional rule* - any pending decision addressed to this seat
//      (a `may`, a `declare_blockers` pause, and now a `declare_attackers`
//      pause too - see below), the game being over. There is no tick for
//      these;
//   3. the `pass` verb's own "one priority, then stop".
//
// `declare_attackers`/`declare_blockers` (CR 508.1, CR 509.1) used to split
// across both of the first two kinds - `declare_attackers` was a checked stop
// on `stops.mine`, `declare_blockers` was unconditional - because unticking
// `stops.mine`'s `declare_attackers` entry was the only way to skip the
// question while a `pass`/`pass_until` run was under way, and there was no
// such skip for `declare_blockers` at all. The owner replaced both with
// `render/ribbon.js`'s "Ask me to declare attackers"/"Ask me to declare
// blockers" stops-box ticks (`stops.askAttackers`/`askBlockers`, beside
// `onStack`), which `app.js`'s own auto-answer (`maybeAutoAnswerCombatDeclaration`)
// consults on *every* poll/refresh tick, not only while a run is executing -
// a player who is not running anything still has the pause answered "none"
// for them the instant it is impossible or unticked. Because that happens
// before `stopReason` is ever asked about the same pause, both declarations
// are uniformly kind 2 here now: whichever one is still outstanding by the
// time `stopReason` sees it is one the auto-answer left alone (ticked, and a
// real declaration is possible), and it stops the run unconditionally, the
// same as any other decision addressed to this seat - `render/ribbon.js`'s
// own combat-confirm mode is what takes over from there, not a resumed run.
//
// The `onStack` tick is deliberately NOT a destination. The owner's case for
// it is "I'm passing until my pre-combat-main, but opponent casts a spell in
// my upkeep - I need to have a way to respond to it", which wants both at
// once: a destination to run to AND a reason to stop short of it. So it sits
// beside the two step lists rather than inside either, and both are live on
// the same run.

import { decidesFor, decisionIsMine, holdsPriority } from "./control.js";

// CR 510.4/506.1: the engine keeps one `Step.COMBAT_DAMAGE` for both
// occurrences of the combat damage step (`serialize.step_position`'s own
// docstring on the server explains why - there is no separate `Step` of its
// own), so `state.step` alone reads `"combat_damage"` in both. This module's
// own name for the first occurrence, and the flag the server sends alongside
// `state.step` to say which one a board is sitting in.
export const FIRST_STRIKE_DAMAGE = "first_strike_damage";

/** `state`'s own step *position* - `FIRST_STRIKE_DAMAGE` while
 * `state.first_strike_damage_step` is true, `state.step` otherwise. Every
 * comparison, rank, stamp or record of "the current step" in this module
 * reads this, never `state.step` directly, so the two occurrences of CR
 * 510.4's combat damage step are never read as the same position. */
export function stepPosition(state) {
  return state.first_strike_damage_step ? FIRST_STRIKE_DAMAGE : state.step;
}

export const STEPS = [
  "untap", "upkeep", "draw", "precombat_main", "beginning_of_combat",
  "declare_attackers", "declare_blockers", FIRST_STRIKE_DAMAGE, "combat_damage",
  "end_of_combat", "postcombat_main", "end_step", "cleanup",
];

// Mirrors the engine's own `Phase` (machines/turn.py) exactly, string for
// string - `precombat_main`/`postcombat_main` included, not display labels.
// `FIRST_STRIKE_DAMAGE` is in the combat phase, the same as `combat_damage`
// itself - it is the same phase's own first occurrence of that step.
export const PHASE_OF = {
  untap: "beginning", upkeep: "beginning", draw: "beginning",
  precombat_main: "precombat_main", beginning_of_combat: "combat",
  declare_attackers: "combat", declare_blockers: "combat",
  [FIRST_STRIKE_DAMAGE]: "combat", combat_damage: "combat",
  end_of_combat: "combat", postcombat_main: "postcombat_main", end_step: "ending",
  cleanup: "ending",
};

export const VERBS = ["pass", "pass_until"];

export const VERB_LABELS = {
  pass: "Pass",
  pass_until: "Pass until",
};

// `onStack` sits alongside the two step lists, not inside them: it is a stop
// that can fire in any step, and it is on by default because CR 117.4 only
// resolves the top of the stack once every player has passed in succession -
// so the pass this run has not made yet is the whole of the chance to respond
// to whatever just landed. Unticking it is the deliberate "I am answering
// nothing in this stretch".
//
// `keepPriorityByDefault` sits beside it for the same structural reason
// (not a step, fires regardless of which one you are in) but is not a
// `stopReason`/`pass_until` concern at all - `app.js`'s `afterStackAction`
// reads it right after a cast, activation, crew, equip or cycle goes on the
// stack, and when one paused on a decision, `decide()` spends the pass once
// that decision's answer completes it. CR 117.3c
// already gives you priority back after you cast a spell or activate an
// ability; this setting decides whether the harness keeps it for you or
// spends it with one auto-pass. On by default: the owner found casting a
// spell without keeping priority far too easy, and the same goes for
// activating an ability. The casting strip's per-action tick starts from
// it and overrides it for that one action. Mana abilities and land plays
// never auto-pass either way.
// `askAttackers`/`askBlockers` sit beside `onStack`/`keepPriorityByDefault`
// for the identical structural reason: neither is a step, and both fire (or
// rather, decide whether NOT to fire) regardless of which one the board is
// in. `render/ribbon.js`'s `renderStopsBox` draws them as "Ask me to declare
// attackers"/"Ask me to declare blockers", and `app.js`'s own
// `maybeAutoAnswerCombatDeclaration` is the only reader - `stopReason` below
// never looks at either, because ticked-off is answered "none" before
// `stopReason` is ever asked about that pause (see this file's own header).
// On by default, the same as every other tick here: absent has to read as
// "ask", never as a silent auto-decline a player did not choose.
// Human-side speed, every one an opt-out and every action it takes recorded
// (a toast, and `reasoning` on the request): `autoForced` asks the server to
// answer a decision with exactly one legal answer (`app.js`'s
// `maybeAutoAnswerForced`); `autoPassNothing` passes when pass is the only
// legal move (`onlyPassLegal`, `autoPassWindow`, `app.js`'s `maybeAutoPass`);
// `tappedToast` names the sources a payment tapped; `yieldTo` is the
// "always yield to this" list, `{controller, name, text}` per entry
// (`isYielded`). Absent reads as the default, the same as every tick here.
export const DEFAULT_STOPS = {
  mine: ["precombat_main", "declare_attackers", "postcombat_main"],
  theirs: ["declare_attackers", "end_step"],
  onStack: true,
  keepPriorityByDefault: true,
  askAttackers: true,
  askBlockers: true,
  autoForced: true,
  autoPassNothing: true,
  tappedToast: true,
  yieldTo: [],
};

// v1 was written while the keep-priority setting defaulted off, so a stored
// `false` there records the old default rather than a choice. The setting
// lives under v2; v1 is read only for the step ticks and the stack tick.
// `askAttackers`/`askBlockers` are newer still than `keepPriorityByDefault`
// and never existed in v1 either, so they get the same absent-means-ON
// treatment `onStack` already has, on both keys.
const STOPS_KEY = "mcm.stops.v2";
const V1_STOPS_KEY = "mcm.stops.v1";

function readStops(storage, key) {
  const raw = storage.getItem(key);
  if (!raw) return null;
  const parsed = JSON.parse(raw);
  // "[]" and "7" both parse cleanly but are not a {mine, theirs} object -
  // the wrong shape is exactly as unusable as invalid JSON, so it gets the
  // same fallback rather than silently becoming empty stops.
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  return parsed;
}

function yieldEntry(obj) {
  return { controller: obj.controller, name: obj.name, text: obj.text };
}

/** A stored stops object read back with every absent tick at its default.
 * `isV2` is false only for a `mcm.stops.v1` payload, whose
 * `keepPriorityByDefault` recorded the old default rather than a choice. */
export function normalizeStops(parsed, isV2) {
  // Absent-means-ON across the board: a payload written before a given
  // tick existed must not silently take that tick's chance away from a
  // player who never touched it - `onStack`'s own original reasoning,
  // extended to `keepPriorityByDefault`, `askAttackers`/`askBlockers` and
  // the speed ticks.
  const yields = Array.isArray(parsed.yieldTo) ? parsed.yieldTo : [];
  return {
    mine: parsed.mine || [],
    theirs: parsed.theirs || [],
    onStack: parsed.onStack !== false,
    keepPriorityByDefault: isV2 ? parsed.keepPriorityByDefault !== false : true,
    askAttackers: parsed.askAttackers !== false,
    askBlockers: parsed.askBlockers !== false,
    autoForced: parsed.autoForced !== false,
    autoPassNothing: parsed.autoPassNothing !== false,
    tappedToast: parsed.tappedToast !== false,
    yieldTo: yields.filter((y) => y && typeof y === "object").map(yieldEntry),
  };
}

export function loadStops(storage) {
  try {
    const v2 = readStops(storage, STOPS_KEY);
    const parsed = v2 || readStops(storage, V1_STOPS_KEY);
    if (parsed) return normalizeStops(parsed, Boolean(v2));
  } catch {
    // unreadable storage: the defaults below
  }
  return normalizeStops({ mine: [...DEFAULT_STOPS.mine], theirs: [...DEFAULT_STOPS.theirs] }, true);
}

/** The stops remembered for the signed-in user (`GET /api/me/preferences`)
 * over this browser's own: the remote payload wins whole, read with the same
 * absent-means-default rule, so a key it lacks is the default rather than
 * whatever this browser last held. A remote value that is not a stops object
 * leaves `local` as it is. */
export function mergeStops(local, remote) {
  if (!remote || typeof remote !== "object" || Array.isArray(remote)) return local;
  return normalizeStops({ mine: local.mine, theirs: local.theirs, ...remote }, true);
}

/** Whether `rows` (`GET .../legal-actions`'s `actions`) leave this seat
 * nothing to do but pass - the browser's twin of `seatkit.
 * stack_stop_needs_you`: pass/concede/mana-ability rows are not moves, nor
 * is a row the engine reports `affordable: "no"`; `"unknown"` (a modal or X
 * cost) is given the benefit of the doubt and counts as a move, as does any
 * other row (a land play, a special action, a non-mana activation). A
 * malformed row counts as a move too: never pass on a read not understood. */
const NOT_A_MOVE = new Set(["pass_priority", "concede", "activate_mana"]);
export function onlyPassLegal(rows) {
  if (!Array.isArray(rows)) return false;
  for (const row of rows) {
    if (!row || typeof row !== "object") return false;
    if (NOT_A_MOVE.has(row.kind)) continue;
    if (row.affordable === "no") continue;
    return false;
  }
  return true;
}

/** Whether a stack object is on the "always yield" list - controller, name
 * and text all equal, so a same-named object with other text (or someone
 * else's copy) is not swept up with it. */
export function isYielded(obj, stops) {
  if (!obj) return false;
  const list = (stops && Array.isArray(stops.yieldTo) && stops.yieldTo) || [];
  return list.some((y) => y.controller === obj.controller && y.name === obj.name && y.text === obj.text);
}

/** `stops` with `obj` added to, or removed from, the "always yield" list. */
export function toggleYield(stops, obj) {
  const list = Array.isArray(stops.yieldTo) ? stops.yieldTo : [];
  const yieldTo = isYielded(obj, stops)
    ? list.filter((y) => !(y.controller === obj.controller && y.name === obj.name && y.text === obj.text))
    : [...list, yieldEntry(obj)];
  return { ...stops, yieldTo };
}

/** Whether this board is one the UI may pass on its own when nothing but
 * pass is legal: this seat holds priority, no decision is pending and the
 * game is on; never over an object this seat's own side put on the stack
 * since it last held priority (`seenStackIds` - its own cast with priority
 * kept, or its own trigger, is a stop); and never in this seat's own main
 * phase with an empty stack, where the turn's own plays happen. */
export function autoPassWindow(state, seenStackIds) {
  if (!state || state.winner || state.drawn) return false;
  if (state.pending_decision) return false;
  if (!holdsPriority(state)) return false;
  const stack = state.stack || [];
  const mineTurn = decidesFor(state).has(state.active_player);
  const main = state.step === "precombat_main" || state.step === "postcombat_main";
  if (mineTurn && main && stack.length === 0) return false;
  const seen = new Set(seenStackIds || []);
  const own = decidesFor(state);
  return !stack.some((obj) => !seen.has(obj.id) && own.has(obj.controller));
}

/** MTGO's keys: F2 passes once, F6 runs `pass_until` - each only while
 * `passButtonsFor` offers that verb; any other key is `null`. */
const SHORTCUTS = { F2: "pass", F6: "pass_until" };
export function shortcutVerb(key, state) {
  const verb = SHORTCUTS[key];
  if (!verb || !state) return null;
  return passButtonsFor(state).includes(verb) ? verb : null;
}

export function saveStops(storage, stops) {
  try {
    storage.setItem(STOPS_KEY, JSON.stringify(stops));
  } catch {
    // storage unavailable: the stops live for this page only
  }
}

export function startRun(verb, state) {
  return {
    verb,
    startedTurn: state.turn_number,
    startedStep: stepPosition(state),
    startedActive: state.active_player,
    knownStackIds: (state.stack || []).map((o) => o.id),
    passes: 0,
    // Whether `state` is still the very occurrence (turn, step, active
    // player) the run began in - see `forgetStartingStep` below. Naming the
    // step you are already standing in has to mean the *next* one, the same
    // reason the old `startAutopilot`/`forgetStartingStep` pair existed: a
    // run that stopped where it started would be a no-op, never what the
    // player meant.
    stillAtStart: true,
  };
}

// Clears the starting-occurrence flag once the board has actually left that
// occurrence, so the *next* time that (turn, step) pair comes round it is a
// real stopping point again. Ported from the old `forgetStartingStep`
// (app.js:1602-1614); its own comment there explains why comparing the whole
// (turn, step) pair rather than the step alone matters: an effect can add a
// phase to a turn (CR 500.8, verified with knowledge_search in the session
// that ported this file), and a second combat phase brings a second
// declare-attackers step with it on the same turn number - a real stopping
// point, not a repeat of the one the run started in.
//
// A pure function, like the rest of this module: it returns a new run
// object rather than mutating the one passed in, so the caller threads the
// result back in on every tick that did not stop (exactly as the old
// `tickAutopilot` called `forgetStartingStep(game.autopilot, state)` once
// per poll tick).
export function forgetStartingStep(run, state) {
  const stillThere = stepPosition(state) === run.startedStep && state.turn_number === run.startedTurn;
  if (stillThere) return run;
  return { ...run, stillAtStart: false };
}

// Records one successful pass against a run: `passes` up (the `pass` verb's
// own stop condition), `knownStackIds` refreshed from the state the pass
// left behind (every verb's stack-arrival check). The caller runs this after
// each `pass-priority` call that actually went through - `interpretPassOutcome`
// says whether it did - and threads the result back in for the next
// `stopReason` call, the same shape `forgetStartingStep` already has.
export function recordPass(run, state) {
  return { ...run, passes: run.passes + 1, knownStackIds: (state.stack || []).map((o) => o.id) };
}

// Declaring blockers is a turn-based action that doesn't use the stack (CR
// 509.1, verified with knowledge_search in the session that wrote this) -
// this is where the defending player actually has something to do about an
// attack, so it is the one step this checks, exactly as the old
// `stopWhenAttacked` did. Without the step gate, an attack declared in your
// opponent's declare-attackers step would keep re-triggering this on every
// later tick of the same combat (combat damage, end of combat, ...), since
// `state.combat.attacks` stays populated well past declare blockers.
//
// `serialize.py` writes an attack's `defender` as `str(attack.defender)`,
// which is your player id for an attack on you, but a card id for an attack
// on a planeswalker or battle you control - so `defender === state.you`
// alone misses those. This checks both, and then falls back to the old
// app.js:1590-1598 disjunct: in a declare-blockers step where you are not
// the active player and at least one attack has been declared, you are the
// defending player, whatever the attack's `defender` field actually names -
// the same assumption the old `stopWhenAttacked` made.
function attackedNow(state) {
  if (state.step !== "declare_blockers") return false;
  const attacks = (state.combat && state.combat.attacks) || [];
  if (attacks.length === 0) return false;
  const yours = new Set(
    (state.battlefield || []).filter((card) => card.controller === state.you).map((card) => card.id)
  );
  if (attacks.some((a) => a.defender === state.you || yours.has(a.defender))) return true;
  return state.active_player !== state.you;
}

// True while `state` is still the very occurrence `run` began in - the
// "declined as a stopping point, exactly once" guard old `autopilotStopReason`
// applied to its destination check and to `stopWhenYouCanAttack`/
// `stopWhenAttacked`. Without it, starting a run while already standing in
// (say) your own attacked declare-blockers step would halt it before it
// moved at all - the exact regression `test_the_same_holds_for_the_blockers_
// condition` (ported into `tests/test_ui_autopilot_attackers.py`) stands
// against.
function sameOccurrence(state, run) {
  return (
    run.stillAtStart &&
    stepPosition(state) === run.startedStep &&
    state.turn_number === run.startedTurn &&
    state.active_player === run.startedActive
  );
}

export function stopReason(state, run, stops) {
  if (state.winner || state.drawn) return "the game is over";
  if (state.pending_decision) {
    const pending = state.pending_decision;
    // CR 723.5: a decision addressed to a player this seat is deciding for
    // is this seat's own (`control.js`). `declare_attackers`/
    // `declare_blockers` (CR 508.1, CR 509.1) are ordinary members of this
    // branch now, the same as a `may` or being attacked: by the time
    // `stopReason` is ever asked about one, `app.js`'s own
    // `maybeAutoAnswerCombatDeclaration` has already answered "none" for
    // every case this run could skip past (no possible declaration, or the
    // matching "Ask me to declare ..." stops-box tick off) - see this file's
    // own header. What is left standing here is always a real question this
    // seat still has to answer by hand, so it stops unconditionally, no
    // tick, no exception - `render/ribbon.js`'s combat-confirm mode is what
    // takes over from there.
    if (decisionIsMine(state, pending)) {
      // Addressed to you - unconditionally this run's business to stop for,
      // no tick, no exception.
      return "a decision is pending";
    } else {
      // Addressed to someone else. A pause rolls the committed game back to
      // before the paused command ran, so `priority_holder` still reads as
      // whoever it was before the pause - a paused command cannot make
      // progress no matter who calls `pass-priority` against it. That used
      // to make this an unconditional stop too, on the reasoning that
      // nothing here can actually move the board. The owner overruled it:
      // "cannot make progress right now" is not "this run is over" - a
      // decision addressed to the opponent is not this seat's business to
      // end its own run over, so this is not a stop at all. `null`, not a
      // reason string: the run stays alive and idle instead, and resumes on
      // its own the moment the decision clears (`interpretPassOutcome`'s and
      // `passButtonsFor`'s own notes cover the rest of this half of the
      // fix). The clobber danger the old unconditional stop also guarded
      // against - passing on the strength of a stale `priority_holder`
      // reproduces the identical pause and discards the question the other
      // seat was about to answer (`app.py`'s `_run_command`) - is guarded a
      // different way now: `app.js`'s `tickRun` does not attempt a pass
      // while any pending decision belongs to someone else, independently
      // of whether this function halts the run for it.
      return null;
    }
  }
  if (attackedNow(state) && !sameOccurrence(state, run)) return "you are being attacked";
  const mine = state.active_player === state.you;
  const side = mine ? stops.mine : stops.theirs;
  switch (run.verb) {
    case "pass":
      // One priority, given up, and then the board comes back. It still falls
      // through to the checked stops below, so a stop on the other side can
      // halt it before it has passed anything at all.
      if (run.passes >= 1) return "passed once";
      break;
    case "pass_until": {
      // `!== false`, not a bare truthiness check: a `stops` object from
      // before this tick existed (see `loadStops`) has no `onStack` key, and
      // absent has to read as on.
      // An arrival on the "always yield" list (`isYielded`) is one the
      // player already said they never respond to.
      if (stops.onStack !== false) {
        const arrivals = (state.stack || []).filter((o) => !run.knownStackIds.includes(o.id));
        if (arrivals.some((o) => !isYielded(o, stops))) {
          return "something new is on the stack";
        }
      }
      break;
    }
    default:
      return `unknown verb ${run.verb}`;
  }
  const position = stepPosition(state);
  if (side.includes(position) && !sameOccurrence(state, run)) {
    return `stop at ${mine ? "your" : "their"} ${position.replace(/_/g, " ")}`;
  }
  return null;
}

export function interpretPassOutcome(res) {
  if (!res || res.allowed === false) {
    return { ok: false, reason: (res && res.reason) || "pass refused", paused: false };
  }
  if (res.paused) {
    // `allowed: null, paused: true`: the command paused on some decision -
    // possibly not even the caller's own - and the committed game rolled
    // back to before this attempt, unchanged (`commands.py`'s own
    // docstring on `RunResult.PAUSED`). `allowed !== false` alone cannot
    // tell a real pass from this: nothing happened, so this is not `ok`,
    // and a caller that treated it as one (the old code did) would replay
    // the identical no-op pass forever - the tick-forever bug this and
    // `stopReason`'s own pending-decision check fix together.
    //
    // `paused: true` here is what lets `tickRun` tell this apart from a
    // genuine refusal: a pass crossing into a turn-based action mid-flight
    // (this seat's own declare_attackers/declare_blockers, CR 508.1/509.1,
    // or - just as easily, since this seat's priority round runs on the
    // opponent's turn too - the OPPONENT's) is not a problem worth ending
    // the run over, only a reason to look at the board fresh next tick. A
    // refusal is.
    return { ok: false, reason: "paused on a pending decision", outcome: null, settledThrough: null, paused: true };
  }
  return {
    ok: true,
    reason: null,
    outcome: res.outcome || null,
    settledThrough: res.settled_through || null,
    paused: false,
  };
}

// The two verbs, or just `pass` while your own decision is pending, or
// nothing at all. `pass_until` is deliberately not offered alongside your own
// pending decision: a run cannot make progress past a question only you can
// answer, so starting one there would spend a tick doing nothing.
export function passButtonsFor(state) {
  // CR 723.5: a priority this seat decides for is its to pass - including a
  // player it controls, and never its own while somebody controls it (the
  // engine refuses that pass).
  if (!holdsPriority(state)) return [];
  if (state.pending_decision) {
    // Your own decision is answered through the decision panel, not a
    // pass, but the pass button has always stayed live alongside it (a
    // resolution mid-way through your own priority). Anyone else's
    // decision is not yours to answer and not something a pass can get
    // past either - `interpretPassOutcome` above turns the attempt into a
    // no-op now, but there is nothing for the button to usefully do, so it
    // is not offered at all.
    return decisionIsMine(state, state.pending_decision) ? ["pass"] : [];
  }
  return [...VERBS];
}

// The one button here that is not a priority verb, and it is kept
// deliberately.
//
// CR 502.4: "No player receives priority during the untap step"; CR 514.3:
// "Normally, no player receives priority during the cleanup step". CR 500.3
// ends both of its own accord ("A step in which no players receive priority
// ends when all specified actions that take place during that step are
// completed"), and the server settles them for every caller
// (`engine_api.settle_priorityless_steps`, run inside the `pass-priority`
// command itself), so a live game should never rest in one. Should. If the
// engine ever declines that advance, the page is left in a step where
// `priority_holder` is null and `passButtonsFor` correctly offers nothing -
// a frozen board with no legal move on it for anybody.
//
// `POST .../advance` is the human's escape from exactly that, and it is why
// this survived the collapse to two verbs while `advance` went from the
// agent's toolset: an agent seat that finds itself there can file a report
// and a person can look, whereas a human staring at a dead page has no other
// button to press. Offered only to the player whose turn it is, in the one
// step it does something in.
export function advanceOffered(state) {
  return decidesFor(state).has(state.active_player) && (state.step === "untap" || state.step === "cleanup");
}

// CR 104.3a: "A player can concede the game at any time. A player who
// concedes leaves the game immediately. That player loses the game." - so
// the button is always live, and that is exactly the problem. Whenever you
// do not hold priority `passButtonsFor` offers no verbs, and Concede slides
// up into the spot "Pass" occupied a moment before; one stray click there
// ended a game during the browser pass, with nothing asked and nothing to
// undo. So a click on it means "arm" until it is already armed, and only
// then "concede". Any pass verb disarms it again.
export function concedeIntent(armed) {
  return armed ? "concede" : "arm";
}
