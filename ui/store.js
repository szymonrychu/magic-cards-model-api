// The one piece of mutable state the rest of the UI reads from and writes
// through, instead of each panel keeping its own copy of the world. `get()`
// always returns the current object; `set(patch)` merges a partial object -
// or the result of `(state) => partial` when the patch needs to read the
// state it's changing - and notifies every subscriber; `subscribe(fn)`
// returns its own unsubscribe.
//
// The state shape: `{gameId, uid, aiUid, you, state, log: {entries,
// lastSeq}, action, decisionDraft, combatDraft, run, stops, symbols,
// aiStatus, connection}`.

export function createStore(initial) {
  let state = { ...initial };
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
      for (const fn of listeners) fn(state);
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
  };
}

// ------------------------------------------------------------ AI stall ----
//
// How long the AI seat may hold priority with nothing changing before this
// says so. Long enough that an agent genuinely thinking through a turn is
// never accused; short enough that a dead one is not mistaken for a hung
// browser tab.
export const AI_STALL_SECONDS = 45;

/**
 * What to tell the human about a silent AI seat, or null when there is
 * nothing to say. A pure function of (state, the seat's name, the seat's
 * server-reported status, how long the board has been unchanged) - no DOM,
 * no fetch, no clock of its own - so `tests/test_ui_ai_stall.py` can run the
 * real shipped function under Node the way the autopilot tests do.
 *
 * An error the server already knows about beats the timer: there is no point
 * waiting 45 seconds to report something the driver reported at second one.
 */
export function aiStallNotice(state, aiPlayer, aiStatus, unchangedSeconds, grace) {
  if (!state || !aiPlayer) return null;
  if (state.winner || state.drawn) return null;
  if (state.priority_holder !== aiPlayer) return null;
  const error = aiStatus && aiStatus.last_error;
  if (error) return `${aiPlayer}'s agent reported an error: ${error}`;
  if (aiStatus && aiStatus.state === "unreachable") {
    return `${aiPlayer}'s agent cannot be reached by the game server`;
  }
  const limit = grace === undefined ? AI_STALL_SECONDS : grace;
  if (!(unchangedSeconds >= limit)) return null;
  return `${aiPlayer} has held priority for ${Math.floor(unchangedSeconds)}s without acting`;
}
