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

// ------------------------------------------------------------- AI boot ----
//
// Kubernetes gives an AI seat's own pod tens of seconds before it can act at
// all - image pull, two initContainers, the agent container's own `claude`
// booting (measured `boot_ms` around 5000), then the driver's first
// dispatch - and until that resolves the human at the other seat saw
// nothing at all: not stuck, not coming, just silent air. This is the boot
// half of the same problem `aiStallNotice` above answers for a seat that has
// already started and gone quiet mid-game; `renderAiLoadingBanner`
// (`ui/render/panels.js`) is its one caller.
//
// States a seat's own set of `mechanism`s ever report are its shape here -
// see `ai_seat.py` (`subprocess`/`wrapper`) and `k8s_seats.py`
// (`kubernetes`) - nothing here invents a phase none of them observe.
const AI_LOADING_READY_STATES = new Set(["thinking", "idle", "dispatched", "stopped"]);

/**
 * What to tell the human while their opponent's AI seat is still starting
 * up, or `null` once there is nothing left to say: no AI seat at all, the
 * seat can already act, or it never started and already has its own
 * distinct panel (`mechanism: "fallback"` - `renderAiPanel`'s "no agent
 * running" box and "run it by hand" button).
 *
 * A pure function of the seat's own uid and its last-polled status - the
 * same payload `GET .../ai-seat/status` and `GET .../seats` both hand back,
 * verbatim (`decks_api.py`'s `SeatDetail.ai_seat`) - so
 * `tests/test_ui_ai_loading.py` can run the real shipped function under Node
 * across every state without a network.
 *
 * `{failed: true, message}` for a seat that is not coming: an image pull
 * failure, the driver-grace backstop firing, a Job that failed, or a driver
 * the game server itself cannot reach - a failure is never dressed up as
 * still loading. `{failed: false, message}` while it is still booting -
 * `pod` (kubernetes only) gives one more honest bit of detail when the
 * payload carries it; nothing is invented for the mechanisms that do not.
 * `null` once the seat can act, or was never going to be reported here.
 */
export function aiSeatLoadingNotice(aiUid, aiStatus) {
  if (!aiUid) return null;
  const state = aiStatus ? aiStatus.state : undefined;
  const mechanism = aiStatus ? aiStatus.mechanism : undefined;
  if (mechanism === "fallback") return null;
  if (state === "error") {
    return {
      failed: true,
      message: aiStatus.reason || aiStatus.last_error || "the agent failed to start",
    };
  }
  if (state === "unreachable") {
    return { failed: true, message: "the game server cannot reach the agent" };
  }
  if (state === "gone") {
    // `k8s_seats.py`'s `status()` reports BOTH a clean finish and a hard pod
    // failure as `state: "gone"` (`phase in ("Succeeded", "Failed")`) -
    // only `reason`, the pod phase, tells them apart.
    if (aiStatus.reason === "Failed") {
      return { failed: true, message: "its job failed before it was ready to play" };
    }
    return null;
  }
  if (state && AI_LOADING_READY_STATES.has(state)) return null;
  // Everything else - "pending", "starting", "not_started", or no payload
  // at all yet (the poll has not landed, or `pollSeats` swallowed a fetch
  // failure and left `aiStatus` exactly as it was) - is the ordinary boot
  // window this notice exists for.
  if (aiStatus && aiStatus.pod === "Running") {
    return { failed: false, message: "its container is up - waiting for the agent to come online" };
  }
  return { failed: false, message: "starting the agent" };
}
