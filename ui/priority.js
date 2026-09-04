// Priority automation. MTGO's yield tiers as verbs, plus a stops panel; no
// key bindings, those vary by client version. Every verb still stops when
// you are attacked, when a decision is pending, when a new object appears on
// the stack (except `pass_turn_regardless`), and at a checked stop.
//
// This is the port of the old `ui/app.js` autopilot
// (`autopilotStopReason`/`startAutopilot`/`interpretPassOutcome`,
// app.js:1331-1360 and app.js:1486-1645): a named-destination-plus-three-
// toggles picker replaced by five fixed verbs plus a persisted stops panel.
// The picker's `autopilotTarget` (a step to run to) becomes a `stops` object
// containing exactly that one step; the three settings
// (`stopOnStack`/`stopWhenAttacked`/`stopWhenYouCanAttack`) become,
// respectively, the choice of verb, an unconditional rule, and membership of
// `declare_attackers` in `stops.mine`.

export const STEPS = [
  "untap", "upkeep", "draw", "precombat_main", "beginning_of_combat",
  "declare_attackers", "declare_blockers", "combat_damage", "end_of_combat",
  "postcombat_main", "end_step", "cleanup",
];

// Mirrors the engine's own `Phase` (machines/turn.py) exactly, string for
// string - `precombat_main`/`postcombat_main` included, not display labels.
export const PHASE_OF = {
  untap: "beginning", upkeep: "beginning", draw: "beginning",
  precombat_main: "precombat_main", beginning_of_combat: "combat",
  declare_attackers: "combat", declare_blockers: "combat", combat_damage: "combat",
  end_of_combat: "combat", postcombat_main: "postcombat_main", end_step: "ending",
  cleanup: "ending",
};

export const VERBS = ["pass", "pass_turn", "pass_turn_regardless", "until_my_turn", "until_castable"];

export const VERB_LABELS = {
  pass: "Pass",
  pass_turn: "Pass turn",
  pass_turn_regardless: "Pass turn (even with a stack)",
  until_my_turn: "Pass until my next turn",
  until_castable: "Pass until I can cast something",
};

export const DEFAULT_STOPS = {
  mine: ["precombat_main", "declare_attackers", "postcombat_main"],
  theirs: ["declare_attackers", "end_step"],
};

const STOPS_KEY = "mcm.stops.v1";

export function loadStops(storage) {
  const defaults = { mine: [...DEFAULT_STOPS.mine], theirs: [...DEFAULT_STOPS.theirs] };
  try {
    const raw = storage.getItem(STOPS_KEY);
    if (!raw) return defaults;
    const parsed = JSON.parse(raw);
    // "[]" and "7" both parse cleanly but are not a {mine, theirs} object -
    // the wrong shape is exactly as unusable as invalid JSON, so it gets the
    // same fallback rather than silently becoming empty stops.
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return defaults;
    return { mine: parsed.mine || [], theirs: parsed.theirs || [] };
  } catch {
    return defaults;
  }
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
    startedStep: state.step,
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
  const stillThere = state.step === run.startedStep && state.turn_number === run.startedTurn;
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
    state.step === run.startedStep &&
    state.turn_number === run.startedTurn &&
    state.active_player === run.startedActive
  );
}

export function stopReason(state, run, stops, castable) {
  if (state.winner || state.drawn) return "the game is over";
  if (state.pending_decision && state.pending_decision.player === state.you) {
    return "a decision is pending";
  }
  if (attackedNow(state) && !sameOccurrence(state, run)) return "you are being attacked";
  const stackIds = (state.stack || []).map((o) => o.id);
  const newObject = stackIds.some((id) => !run.knownStackIds.includes(id));
  const mine = state.active_player === state.you;
  const side = mine ? stops.mine : stops.theirs;
  const turnChanged = state.turn_number !== run.startedTurn;
  switch (run.verb) {
    case "pass":
      if (run.passes >= 1) return "passed once";
      break;
    case "pass_turn":
      if (newObject) return "something new is on the stack";
      if (turnChanged) return "the turn ended";
      break;
    case "pass_turn_regardless":
      if (turnChanged) return "the turn ended";
      break;
    case "until_my_turn":
      if (newObject) return "something new is on the stack";
      if (mine && turnChanged) return "it is your turn";
      break;
    case "until_castable":
      if (newObject) return "something new is on the stack";
      if (castable) return "you can cast something";
      break;
    default:
      return `unknown verb ${run.verb}`;
  }
  if (side.includes(state.step) && !sameOccurrence(state, run)) {
    return `stop at ${mine ? "your" : "their"} ${state.step.replace(/_/g, " ")}`;
  }
  return null;
}

export function interpretPassOutcome(res) {
  if (!res || res.allowed === false) return { ok: false, reason: (res && res.reason) || "pass refused" };
  return { ok: true, reason: null, outcome: res.outcome || null, settledThrough: res.settled_through || null };
}

export function passButtonsFor(state) {
  if (state.priority_holder !== state.you) return [];
  if (state.pending_decision && state.pending_decision.player === state.you) return ["pass"];
  return [...VERBS];
}
