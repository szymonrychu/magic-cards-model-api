// The one piece of mutable state the rest of the UI reads from and writes
// through, instead of each panel keeping its own copy of the world. `get()`
// always returns the current object; `set(patch)` merges a partial object -
// or the result of `(state) => partial` when the patch needs to read the
// state it's changing - and notifies every subscriber; `subscribe(fn)`
// returns its own unsubscribe. The one wrinkle is `installPressGuard` below:
// while the player is holding a button down, `set()` still merges but holds
// the notification back until the click has landed.
//
// The state shape: `{gameId, uid, aiUid, you, state, log: {entries,
// lastSeq}, action, pendingRelease, decisionDraft, combatDraft, run, stops,
// symbols, aiStatus, connection}`.

export function createStore(initial) {
  let state = { ...initial };
  const listeners = new Set();
  // The press guard - see `installPressGuard` below for the whole story and
  // the DOM wiring. `pressing` is true from the moment a pointer or key goes
  // down until the guard flushes; `dirty` records that at least one `set()`
  // landed in that window and still owes the listeners a fan-out.
  // `holding` is the long-press preview's (`ui/preview.js`) hold: a press
  // meant to outlast `PRESS_CAP_MS`, released only by `holdEnd`.
  let pressing = false;
  let holding = false;
  let dirty = false;
  const notify = () => {
    for (const fn of listeners) fn(state);
  };
  return {
    get: () => state,
    /**
     * Merge `patch` (an object, or `(state) => object`) and notify.
     *
     * THE MERGE IS ALWAYS SYNCHRONOUS, AND MUST STAY THAT WAY. Only the
     * listener fan-out is ever deferred. `poll()` (`ui/app.js`) reads
     * `store.get()` straight back after its own `set()` to decide whether
     * the board it just read predates one of this seat's own actions, and
     * `send()` bumps `mutations` through this same `set()`; deferring the
     * merge would put a stale board back on screen and let `tickRun()` act
     * twice - the exact defect `MEMORY.md`'s 2026-09-08 `pass_until` entry
     * records, which has its own regression tests in `tests/test_ui_app.py`.
     * Defer the notification if you must. Never the merge.
     */
    set(patch) {
      state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
      if (pressing || holding) {
        dirty = true;
        return;
      }
      notify();
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    /** A press has begun: hold the fan-out. `installPressGuard`'s only way
     * in, and idempotent, so a second pointer or a key auto-repeating inside
     * an existing press changes nothing. */
    pressStart() {
      pressing = true;
    },
    /** The press is over: resume fan-out, and pay off whatever `set()` calls
     * happened during it as a single notification carrying the state as it
     * finally stands. A press nothing arrived during notifies nobody - this
     * settles a debt, it is not a heartbeat. */
    pressEnd() {
      if (!pressing) return;
      pressing = false;
      if (holding || !dirty) return;
      dirty = false;
      notify();
    },
    /** A long-press preview has opened under the finger: hold the fan-out
     * for as long as it stays open, past the press cap that bounds an
     * ordinary press. Only `holdEnd` ends it. */
    holdStart() {
      holding = true;
    },
    /** The finger lifted: stop holding, and pay off anything that arrived
     * meanwhile - unless an ordinary press is still in flight, whose own end
     * settles it. */
    holdEnd() {
      if (!holding) return;
      holding = false;
      if (pressing || !dirty) return;
      dirty = false;
      notify();
    },
  };
}

// --------------------------------------------------------- press guard ----
//
// Every renderer under `ui/render/` rebuilds its box from scratch on every
// notification - `clear(el)`, then a fresh `h(...)` tree - so the button
// under the player's finger is a *different node* after any re-render. The
// browser dispatches `mouseup`/`click` to the node the press started on, and
// that node is now detached: the click is silently eaten, and the player sees
// their press do nothing. `poll()` runs every 1.2s, so an unlucky click is
// not a rare race - it is a coin flip on a busy board.
//
// The fix belongs here rather than in the renderers. Making each of them
// reconcile by key would mean getting reconciliation right in every panel,
// forever; holding the fan-out for the ~100ms a press lasts fixes it once,
// in the one place every render is driven from, and costs nothing else: the
// state itself is still merged synchronously (see `set()` above), so nothing
// that *reads* the store is delayed by a press, only what redraws from it.

/** How long a press may hold the fan-out before it is flushed regardless.
 *
 * A hard cap, and not optional. The release events below can genuinely go
 * missing: another capturing handler can swallow a `pointerup`
 * (`ui/render/ribbon.js`'s popover installs one), and switching tabs or
 * apps mid-press can leave the page with a `pointerdown` that never gets its
 * partner. Without the cap, one lost `pointerup` freezes rendering for the
 * rest of the game - a far worse failure than the one this guard exists to
 * prevent. A second is an order of magnitude longer than any real click and
 * still well inside a poll interval. */
export const PRESS_CAP_MS = 1000;

/** How long after a touch or pen release the synthetic `click` for it may
 * still be waited for.
 *
 * A mouse dispatches `pointerup`, `mouseup` and `click` in one task, so the
 * release really is the end of the press there. Touch and pen do not: the
 * click follows by around 300ms (the legacy tap delay, still on the slow
 * path for any page a browser has not decided is fast-tap), in a task of its
 * own - so a flush scheduled on the release fans out first and rebuilds the
 * button under the finger, which is the whole defect this guard exists to
 * prevent. 500ms covers that delay with headroom, and is well under
 * `PRESS_CAP_MS` so a tap that produces no click at all - the browser turned
 * it into a scroll, or it landed on nothing clickable - still gives up
 * sooner than a press with no release does. */
export const TOUCH_CLICK_MS = 500;

/**
 * Wire `store` to the page's presses: while one is in flight, no re-render
 * happens. Called once, from `main()` (`ui/app.js`) - the whole point of it
 * being one function is that listener registration does not get scattered
 * across the render modules. Returns its own uninstall function.
 *
 * `root`/`view` default to the page's `document`/`window` and exist so
 * `tests/test_ui_store.py` can drive the real shipped function against the
 * fake DOM the render tests already use.
 *
 * The listeners capture (`true`) so that a handler stopping propagation
 * cannot hide a press from this, and the flush is a `setTimeout(_, 0)` - a
 * new macrotask, so `pointerup`, `mouseup` and `click` have all dispatched
 * on the original node before anything is allowed to rebuild. That holds for
 * a mouse, which dispatches all three in one task; touch and pen dispatch
 * their `click` a task and ~300ms later, so a release from either waits for
 * the click itself rather than for the next macrotask (`TOUCH_CLICK_MS`).
 * Note that the
 * pressing flag is cleared by the flush itself rather than by the release
 * handler: a `set()` made by the click handler that is about to run would
 * otherwise fan out synchronously, mid-click, which is the very thing being
 * prevented.
 */
export function installPressGuard(store, root, view) {
  const doc = root || (typeof document === "undefined" ? null : document);
  const win = view || (typeof window === "undefined" ? null : window);
  if (!doc || !doc.addEventListener) return () => {};

  let releaseTimer = null;
  let capTimer = null;
  // Set by a touch or pen release, and cleared by the click it is waiting
  // for: the press is over, but the event it becomes has not been dispatched
  // yet, and the button must survive until it has.
  let awaitingClick = false;

  const flush = () => {
    if (releaseTimer !== null) {
      clearTimeout(releaseTimer);
      releaseTimer = null;
    }
    if (capTimer !== null) {
      clearTimeout(capTimer);
      capTimer = null;
    }
    awaitingClick = false;
    store.pressEnd();
  };

  const down = () => {
    // `capTimer` is the record of "a press is already in flight". Not
    // re-arming it is what keeps a key held down (which repeats `keydown`
    // indefinitely) or a second finger from pushing the cap out forever.
    if (capTimer !== null) return;
    store.pressStart();
    capTimer = setTimeout(flush, PRESS_CAP_MS);
  };

  /** A release. `delayed` says the click this press becomes is dispatched in
   * a LATER task than the release itself - true for touch and pen, false for
   * a mouse, and false for every release that produces no click at all
   * (`pointercancel`, a key, the window losing focus). */
  const release = (delayed) => {
    if (capTimer === null || releaseTimer !== null || awaitingClick) return;
    if (!delayed) {
      releaseTimer = setTimeout(flush, 0);
      return;
    }
    // The cap is re-armed rather than left as it was, for two reasons at
    // once: it stays non-null, so this still reads as a press in flight, and
    // it now bounds the wait for a click that may never come - a press held
    // most of a second before release would otherwise have its cap expire
    // before the click it is waiting for arrived.
    awaitingClick = true;
    clearTimeout(capTimer);
    capTimer = setTimeout(flush, TOUCH_CLICK_MS);
  };

  const up = (e) => release(Boolean(e) && (e.pointerType === "touch" || e.pointerType === "pen"));
  // No click follows a cancelled press, a key, or a window that lost focus.
  const ended = () => release(false);
  const clicked = () => {
    if (!awaitingClick) return;
    awaitingClick = false;
    releaseTimer = setTimeout(flush, 0);
  };

  doc.addEventListener("pointerdown", down, true);
  doc.addEventListener("keydown", down, true);
  doc.addEventListener("pointerup", up, true);
  doc.addEventListener("pointercancel", ended, true);
  doc.addEventListener("keyup", ended, true);
  // Only ever consulted while a touch or pen release is waiting for it - a
  // mouse press has flushed long before its own click reaches here.
  doc.addEventListener("click", clicked, true);
  // A window that loses focus mid-press never delivers the release event at
  // all, so this is the third way out, alongside the release events and the
  // cap.
  if (win && win.addEventListener) win.addEventListener("blur", ended);

  return () => {
    flush();
    doc.removeEventListener("pointerdown", down, true);
    doc.removeEventListener("keydown", down, true);
    doc.removeEventListener("pointerup", up, true);
    doc.removeEventListener("pointercancel", ended, true);
    doc.removeEventListener("keyup", ended, true);
    doc.removeEventListener("click", clicked, true);
    if (win && win.removeEventListener) win.removeEventListener("blur", ended);
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
 *
 * A board can sit unchanged for minutes while a hard turn is genuinely being
 * played out - reading cards, thinking, working through a plan - and the
 * same status payload that says "priority hasn't moved" already carries
 * `activity`. Accusing a seat the activity pill is actively showing as
 * working ("AI (claude): making its play") is not merely ugly, it is false,
 * and the data proving it false is already in hand. So: if
 * `aiSeatActivityLabel` would put a live word on the pill right now, that is
 * this seat genuinely being observed to act, and the timer below never even
 * gets to look at `unchangedSeconds` - deliberately the exact same call the
 * pill itself makes, not a second copy of its staleness math, so the two
 * can never say two different things about the same payload. It already
 * reads the payload's own `activity.seconds` rather than a clock of its own
 * (see its own comment for why), so this defers to it rather than
 * re-deriving that.
 *
 * A stale observation does not get this benefit: `aiSeatActivityLabel`
 * stops vouching for one past `AI_ACTIVITY_STALE_SECONDS` (300s), so past
 * that age this falls straight through to the ordinary timer below. That
 * shared threshold was measured against real transcripts, not guessed, and
 * reusing it here rather than picking a second number keeps "is this
 * activity live" answered once. The browser polls this status roughly every
 * 1.2s, so once a seat's last observation actually does cross into
 * staleness, the human sees the truth within about a second either way -
 * the 300s margin exists to cover the agent's own gaps between transcript
 * lines (a single long tool call or model turn), not to cover polling lag,
 * so there is no reason to shrink it for this caller.
 *
 * `last_error` and `state === "unreachable"` still win over all of this: a
 * seat the server already knows is broken is reported immediately, whatever
 * its last transcript line said.
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
  if (aiSeatActivityLabel(state, aiStatus)) return null;
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
