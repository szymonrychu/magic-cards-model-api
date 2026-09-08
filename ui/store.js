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

// --------------------------------------------------------- AI activity ----
//
// The AI pill used to read "AI (claude): thinking" for a whole match,
// whatever the agent was doing, because the seat status was all the browser
// could see and all it said was "a turn is in flight". The agent container's
// transcript watcher (`mtg_agent/wrapper/activity.py`) now publishes one
// coarse word for what it is actually doing, the driver carries it on its
// `/status`, and it arrives here as `aiStatus.activity` -
// `{state, seconds, seq}`.
//
// Two things this must never do, both of them requirements rather than
// taste. It must not invent: no timer here promotes silence into a state, so
// a quiet agent keeps whatever it was last *observed* doing. And it must not
// leak: the human opponent reads this pill mid-game, so the words come from
// the fixed table below keyed by the state alone - no card name, no tool
// argument and no line of the agent's prose is on this wire in the first
// place (see `observations` in that module), and none is rendered if it ever
// were.

/** Every phrase this pill can show, keyed by the state the wrapper observed.
 * Grouped hard on purpose: several `mtg` read tools are one "reading the
 * board", and every play is one "making its play" - a dozen labels flickering
 * past on a 5s poll would say less than these eight. */
export const AI_ACTIVITY_LABELS = {
  waiting: "waiting for priority",
  reading_board: "reading the board",
  reading_cards: "reading cards",
  reading_rules: "checking the rules",
  deciding: "deciding",
  acting: "making its play",
  thinking: "thinking",
  working: "working",
};

/** The seat states that mean "the agent has the game right now": `thinking`
 * (`k8s_seats.py`, and a `subprocess` runner) and `dispatched` (the `wrapper`
 * mechanism, which never rewrites that word while the seat is healthy - see
 * `ai_seat.py`'s `_live_driver_status`). Every other state - `error`,
 * `unreachable`, `gone`, `stopped`, `idle`, `pending`, `starting` - already
 * says something truer than any activity could, and keeps its own word. */
const AI_ACTIVITY_PLAYING_STATES = new Set(["thinking", "dispatched"]);

/** How old an observation may be before the pill stops reporting it.
 *
 * Not a way of inferring a state - it never produces one - only of retiring
 * a claim this page can no longer stand behind. Silence below the limit
 * changes nothing: the last thing the agent was *observed* doing is still
 * the honest answer, and no timer here gets to invent a different one.
 *
 * Five minutes, and deliberately nowhere near `AI_STALL_SECONDS`. Measured
 * over 65 minutes of real seat transcripts: `wait_for_priority` - the agent
 * parked, correctly, waiting for its moment - holds for a median of 28s and
 * a p90 of 110s with nothing else written to the transcript meanwhile. A
 * limit anywhere near the 45s stall banner retires that live, correct label
 * mid-wait and replaces it with exactly the flat "thinking" this whole
 * feature exists to remove: on that data a 45s limit blanked 49% of polls,
 * almost all of them a legitimate wait. The stall banner is what tells the
 * human a seat has gone quiet; the pill's job is to say what it was doing
 * when it went quiet, which is more useful beside that banner, not less. */
export const AI_ACTIVITY_STALE_SECONDS = 300;

/**
 * The words the AI pill should show for what the agent is doing right now,
 * or `null` when this page cannot honestly say - which is the caller's
 * signal to fall back to the plain seat-state word the pill has always
 * shown (`renderAiPanel`, `ui/render/panels.js`).
 *
 * A pure function of the game state and the seat's last-polled status - no
 * DOM, no fetch, no clock of its own (the age is measured in the agent's own
 * container, where the transcript's timestamps and the clock are the same
 * host's, so no browser skew can reach it) - so `tests/test_ui_ai_activity.py`
 * can run the real shipped function under Node across every state.
 */
export function aiSeatActivityLabel(state, aiStatus) {
  if (!aiStatus) return null;
  if (state && (state.winner || state.drawn)) return null;
  if (aiStatus.mechanism === "fallback") return null;
  if (!AI_ACTIVITY_PLAYING_STATES.has(aiStatus.state)) return null;
  const activity = aiStatus.activity;
  if (!activity || typeof activity !== "object") return null;
  const label = Object.prototype.hasOwnProperty.call(AI_ACTIVITY_LABELS, activity.state)
    ? AI_ACTIVITY_LABELS[activity.state]
    : null;
  if (!label) return null;
  // A missing or unparsable age is not evidence of staleness - only a number
  // past the limit is.
  const age = Number(activity.seconds);
  if (Number.isFinite(age) && age >= AI_ACTIVITY_STALE_SECONDS) return null;
  return label;
}
