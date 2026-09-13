// Boot, polling, click flows, and firing - the one place that ties every
// pure module and every render module together into the running page. This
// replaces the old classic-script `ui/app.js` (~2900 lines) entirely; the
// AI panel, the phase track/turn info, the zones popover, the report form,
// the connection pill, and `enterGame`/`leaveGame` are ported here (the
// first five onto `render/panels.js`, split out once porting them here
// would have pushed this file well past ~700 lines).
//
// The click-planning logic (`planHandCardClick`/`planPermanentClick`) is
// factored out as pure functions - no DOM, no fetch - so `tests/test_ui_app.py`
// can exercise "what does clicking this card do" without a network, and so
// this file has exactly one place that decides it rather than the decision
// living twice (once for real, once reimplemented for a test).

import { api, withUid } from "./api.js";
import { createStore, installPressGuard } from "./store.js";
import { loadSymbols } from "./symbols.js";
import * as A from "./actions.js";
import * as P from "./priority.js";
import * as C from "./combat.js";
import { untappedSources, colorOptions, manaAbilities } from "./mana.js";
import * as M from "./menus.js";
import { viewModel } from "./decisions.js";
import { fill, canFill } from "./targeting.js";
import { mountBoard } from "./render/board.js";
import {
  mountRibbon,
  showPopover,
  hidePopover,
  popoverIsOpenFor,
  updatePopoverItems,
} from "./render/ribbon.js";
import { mountLog } from "./render/log.js";
import {
  mountDecisionWindow,
  minimizeDecisionWindow,
  restoreDecisionWindow,
  decisionWindowIsOpen,
} from "./render/decision_window.js";
import { mountDecisionPopover } from "./render/decision_popover.js";
import { mountAdvice } from "./render/advice.js";
import { withoutUid } from "./advice_model.js";
import { mountLobby } from "./render/lobby.js";
import { mountReview } from "./render/review.js";
import { mountTopbar, closeModal } from "./render/panels.js";
import { mountCardPreview } from "./preview.js";
import { loadSavedGames, saveGames, upsertSavedGame, seatsFromRow } from "./lobby_model.js";
import { loadConfig, apiBase, lobbyBase } from "./config.js";

const POLL_MS = 1200;
//: S7 (2b's T8): the AI seat's liveness now comes from the lobby's
//: aggregate `/seats` route (one call for every seat's agent settings and
//: liveness together) rather than a second per-tick `/ai-seat/status` call
//: inside `poll()` itself - and it needs it far less often than the board
//: does, so it runs on its own, slower interval.
const SEAT_POLL_MS = 5000;

function currentStorage() {
  return typeof localStorage !== "undefined" ? localStorage : null;
}

// Set once `main()` mounts the lobby - `leaveGame` (defined ahead of `main()`
// in this file) calls `lobbyApi.refresh()` through it, so the games table
// is current the moment a player returns to the lobby rather than only on
// page load or the game's own Start/End.
let lobbyApi = null;

// Exported for `tests/test_ui_app.py` only - a real (if DOM-less) drive of
// `tryTarget` needs somewhere to set up the scenario's `action`/`state`
// before calling it and somewhere to read the result back from, the same
// reason the old classic script exported its own live `game` object for
// its tests. Not part of any render module's handler contract.
export const store = createStore({
  gameId: null,
  uid: null,
  aiUid: null,
  state: null,
  action: null,
  decisionDraft: {},
  decisionTargets: null,
  combatDraft: {},
  run: null,
  stops: P.loadStops(localStorage),
  symbols: {},
  log: { entries: [], lastSeq: 0 },
  preview: null,
  aiStatus: null,
  connection: "idle",
  polling: false,
  // How many mutating requests this tab has issued. Never displayed - it is
  // the only ordering `poll()` has between its own reads and this seat's own
  // actions, and `send()` (every mutating request in this file goes through
  // it) is the one place it moves. See `poll()` for the defect it exists for.
  mutations: 0,
  // How many of this seat's own mutating requests are outstanding *right
  // now* - `mutations`' live half. Bumped by `send()` before the request and
  // dropped in its `finally`, so it is non-zero for exactly as long as the
  // player is waiting on the server. This is the in-flight state itself: the
  // one thing the page can honestly say the instant a click lands, ~0.4-2.4s
  // before the answer that would otherwise be the first sign the click was
  // heard at all.
  busy: 0,
  concedeArmed: false,
});

/** Whether a request is a SUBMISSION - something the player asked the server
 * to do, and is now waiting on - rather than a read or a dry run this page
 * makes on its own account.
 *
 * The distinction is what `busy` means, and it has to be this one. Both
 * `refreshAffordance()` (a GET) and `previewAndMaybeFire()`'s probe (a POST
 * that changes nothing, `dry_run: true`) go through `send()` and fire on
 * every mode toggle, every pitch/convoke tick and every X change - so a
 * `busy` that counted them dimmed the casting strip those very controls live
 * in, for a whole round trip each, with nothing outstanding that the player
 * had actually submitted. Feedback for a click the player did not make is
 * not feedback.
 *
 * The double-submit guard is `inFlight` below and is deliberately NOT this
 * test: it is keyed on the whole request, so it suppresses the identical
 * resubmission whether or not it is a submission by this definition. */
function isSubmission(method, body) {
  return method !== "GET" && !(body && body.dry_run);
}

/** `path` with this seat's own `uid` on it, unless it already carries one.
 *
 * Idempotent by design, so the six call sites that already wrap themselves
 * in `withUid` keep working untouched and `send()` can apply this to every
 * path without caring which did. A path with no uid available is returned
 * as it came: the server's refusal is a better answer than a `uid=null`
 * query string, and the lobby screen (where there is no seat yet) never
 * calls this. */
function uidOf(path) {
  if (/[?&]uid=/.test(path)) return path;
  const uid = store.get().uid;
  return uid ? withUid(path, uid) : path;
}

/** The in-flight state, on the page rather than only in the store: `body`
 * carries `.busy` (and `aria-busy`) for as long as one of this seat's own
 * submissions is outstanding, and `style.css` turns that into the progress
 * cursor, the sweep, and the ribbon's action buttons dimming. Doing it
 * on `body` is what makes it cost one line in `send()` instead of a spinner
 * hand-wired into each of the nine action paths - and it needs no element
 * that `ui/index.html` does not already have.
 *
 * Defensive about `document` on purpose: `tests/test_ui_app.py` imports this
 * module under Node with no DOM at all, or with a fake that has nothing but
 * `querySelector`. */
function showInFlight(busy) {
  const body = typeof document === "undefined" ? null : document.body;
  if (!body) return;
  if (body.classList) body.classList.toggle("busy", busy);
  if (busy) body.setAttribute("aria-busy", "true");
  else body.removeAttribute("aria-busy");
}

function you() {
  return store.get().state.you;
}
/**
 * The game's own URL, plus `rest`. An empty `rest` means the game itself, and
 * it must not leave a trailing slash behind: `GET /api/games/{id}/` answers
 * 404, not a redirect to `GET /api/games/{id}`, because the router carries
 * sibling routes under that same prefix (`log`, `decide`, `actions/...`) and
 * Starlette's redirect-slashes never fires. The whole game screen stayed
 * blank on that one character, so the poll's path is exported and pinned.
 */
export function gamePath(rest) {
  const base = `${apiBase()}/games/${store.get().gameId}`;
  return rest ? `${base}/${rest}` : base;
}

// T7: a game id is a bare `uuid4().hex` - exactly 32 lowercase hex
// characters (`repository.py`). The canonical `/<uuid>/` address is a single
// unclaimed path segment at the root, the same shape as `/docs`, `/style.css`
// or `/favicon.ico` - this is what tells them apart client-side, mirroring
// the `_GameIdConvertor` regex `app.py` registers server-side so the two
// never drift on what counts as a game id.
const GAME_ID_RE = /^[0-9a-f]{32}$/;

/** The game id a page served at `/<uuid>/` (T7, canonical) or `/g/<uuid>/`
 * (T4, kept working as a redirect-only compatibility form - see `app.py`) is
 * looking at, read straight off `document.baseURI` rather than requiring
 * `store.gameId` to already be set - a fresh load of that URL (a bookmark, a
 * shared link, a browser refresh) has nothing in the store yet. `null` for
 * the plain lobby root, or any other path this deployment might serve, so
 * `main()`'s boot can tell "this page is a game" from "this page is the
 * lobby" without a separate flag. */
export function gameIdFromBaseURI(baseURI) {
  const path = new URL(baseURI).pathname;
  const gPathMatch = path.match(/^\/g\/([^/]+)\/?/);
  if (gPathMatch) return decodeURIComponent(gPathMatch[1]);
  const bareMatch = path.match(/^\/([^/]+)(?:\/.*)?$/);
  if (bareMatch) {
    const candidate = decodeURIComponent(bareMatch[1]);
    if (GAME_ID_RE.test(candidate)) return candidate;
  }
  return null;
}

/** The requests this seat has outstanding, keyed by method+path+body - see
 * `send()`. Module-level rather than in the store because nothing renders it:
 * `busy` is the count the page shows, this is the identity the guard needs. */
const inFlight = new Set();

/**
 * The one place a transport failure - a rejected `fetch` (the network is
 * down, the server is unreachable), or a non-2xx `api()` cannot even parse a
 * body from - is turned into something the player can see, rather than an
 * unhandled promise rejection. Every action path in this file goes through
 * this instead of calling `api()` directly, and tells a transport failure
 * apart from an ordinary refusal (`res.allowed === false`, a normal outcome
 * the caller already toasts on its own terms) by checking for `null` first.
 *
 * `poll()`'s own two reads (state, log) are the one exception: they keep
 * their existing `try/catch`, on purpose - letting *that* throw is what
 * stops a single flaky request from overwriting good board state with
 * `null` (this function would resolve to `null` there instead of leaving
 * the previous `store.set` call unmade).
 */
export async function send(method, path, body) {
  // This seat's own credential, on every request, and the reason it is here
  // rather than at each call site: the server now REFUSES a mutating call on
  // a game that presents no credential at all (`game_access.py`'s positive
  // rule). A deny-list keyed on a uid cannot restrict a caller who omits the
  // uid, so an ownerless game - which is every game in this deployment -
  // used to be writable by anyone who knew its id, which made the mtg-buddy
  // advisor's read-only-ness unenforceable.
  //
  // Nine of this file's own paths sent nothing: both halves of the cast
  // flow (the `dry_run` probe and the real `fire()`), `play-land`, the
  // keyword special actions, `cycle`, both `pass-priority` calls (the
  // player's own and autopass acting for the AI seat), `report` and
  // `concede`. Six others already wrapped themselves in `withUid` by hand,
  // which is exactly the shape that leaves a tenth path to forget. One
  // place, applied to whatever the caller did not already spell out.
  path = uidOf(path);
  // Double-submit protection, and the reason it lives here rather than in
  // nine handlers: a request that is byte-for-byte a request already in
  // flight is the same submission, not a second one. That is exactly the
  // instinctive second click on a control that looked dead - the reported
  // symptom - and until now nothing anywhere in the action path stopped it
  // from reaching the engine twice.
  //
  // Keyed on the whole request, so it never blocks a *different* action
  // taken while one is outstanding (cancelling a cast mid-probe, picking a
  // second target), and it lifts the moment the first answer lands - it is a
  // guard on the flight, never a latch on the button.
  //
  // A suppressed duplicate returns `null`, which every caller in this file
  // already reads as "nothing was decided, say nothing" (the transport-
  // failure case `api()`'s own `catch` below produces). No toast: the player
  // clicked the thing they had already clicked, and the first click is still
  // being answered.
  const key = `${method} ${path} ${body === undefined ? "" : JSON.stringify(body)}`;
  if (inFlight.has(key)) return null;
  inFlight.add(key);
  // Bumped BEFORE the request, not after it: a board read that was answered
  // while this request was still in flight is just as much a picture of the
  // world before it as one answered before it was sent, and `poll()` has to
  // discard both. Every mutating request in this file goes through here, so
  // this is the only place the counter moves.
  //
  // `busy` moves with it for a submission, and this is the whole of the
  // immediate feedback: it is set before `api()` is even called, so the page
  // shows the click as heard on the click, not on the answer. A read or a
  // probe still bumps `mutations` - a board read answered while one was
  // outstanding is just as stale - but says nothing about what the player is
  // waiting for, so it leaves `busy` alone (`isSubmission`).
  const submission = isSubmission(method, body);
  store.set((s) => ({ mutations: s.mutations + 1, busy: submission ? s.busy + 1 : s.busy }));
  if (submission) showInFlight(true);
  try {
    return await api(method, path, body);
  } catch (err) {
    store.set({ connection: `error: ${err.message}` });
    toast(err.message);
    return null;
  } finally {
    inFlight.delete(key);
    if (submission) {
      const busy = store.get().busy - 1;
      store.set({ busy });
      showInFlight(busy > 0);
    }
  }
}

// -------------------------------------------------------- click planning --
//
// Pure: given the card clicked and the store's current snapshot, what should
// happen - never touches the DOM or the network. `onHandCard`/`onPermanent`
// below are thin wrappers that act on the plan; this is the one place that
// decides it.

/** What clicking a card in your own hand should do. `{kind: "cancel"}`
 * (clicking the card already mid-cast again) | `{kind: "target", id}`
 * (an action or a `choose_targets` decision is live, so this click is a
 * target pick) | `{kind: "land"}` | `{kind: "cast"}` |
 * `{kind: "menu", isLand, specials, cycling}` (more than one thing this card
 * could do - a special action, cycling, or, for a land, more than one). */
export function planHandCardClick(card, app) {
  if (app.action && app.action.cardId === card.id) return { kind: "cancel" };
  if (app.action) return { kind: "target", id: card.id };
  const isLand = /land/i.test(card.type_line || "");
  const specials = Object.keys(card.special_actions || {}).filter((k) => k !== "cycling");
  const cycling = (card.special_actions && card.special_actions.cycling) || [];
  if (isLand && !specials.length && !cycling.length) return { kind: "land" };
  if (specials.length || cycling.length) return { kind: "menu", isLand, specials, cycling };
  return { kind: "cast" };
}

/** What clicking a permanent on the battlefield should do. `{kind: "target",
 * id}` (an action or `choose_targets` decision is live) | `{kind: "pin"}`
 * (a mana-ability land, clicked while casting, that isn't already an
 * affordance candidate) | `{kind: "attack"}` | `{kind: "block"}` |
 * `{kind: "activate", abilityIndex}` (exactly one activated ability) |
 * `{kind: "menu", abilities, vehicle}` (more than one thing this permanent
 * could do) | `{kind: "none", reason?}` (not yours, or nothing to do). */
export function planPermanentClick(card, app, state) {
  const seat = state.you;
  const pendingTargets =
    state.pending_decision &&
    state.pending_decision.player === seat &&
    state.pending_decision.kind === "choose_targets";
  if (app.action || pendingTargets) {
    const isCandidate = Boolean(
      app.action &&
        app.action.affordance &&
        app.action.affordance.candidates.some((c) => c.id === card.id)
    );
    if (
      app.action &&
      card.controller === seat &&
      (card.abilities || []).some((a) => a.is_mana_ability) &&
      !isCandidate
    ) {
      return { kind: "pin" };
    }
    return { kind: "target", id: card.id };
  }
  // A `declare_attackers`/`declare_blockers` decision paused (CR 508.1, CR
  // 509.1) freezes `state.step` at whatever it was *before* the pause, not
  // the one it is trying to enter (`app.py`'s `advance` docstring), and
  // `state.priority_holder` is equally stale - CR 508.1/509.1's turn-based
  // action never finished, so neither field ever moved. A plain
  // `state.step === "declare_attackers"` check can therefore never see the
  // pause itself, only the state that results *after* someone answers it -
  // too late to offer this click at all. `pendingDeclareKind` reads the
  // live decision instead, which is stamped the moment the question is
  // asked: a click can attack or block while the game is genuinely waiting
  // on this seat to say so, exactly as it always could once the step
  // actually became real.
  const pendingDeclareKind =
    state.pending_decision && state.pending_decision.player === seat
      ? state.pending_decision.kind
      : null;
  if (
    card.controller === seat &&
    state.active_player === seat &&
    ((state.step === "declare_attackers" && state.priority_holder === seat) ||
      pendingDeclareKind === "declare_attackers")
  ) {
    return { kind: "attack" };
  }
  if (
    state.active_player !== seat &&
    ((state.step === "declare_blockers" && state.priority_holder === seat) ||
      pendingDeclareKind === "declare_blockers")
  ) {
    return { kind: "block" };
  }
  if (card.controller !== seat) return { kind: "none" };
  const abilities = card.abilities || [];
  const vehicle = /vehicle/i.test(card.type_line || "");
  // CR 702.6a: equip has no entry in `card.abilities` (`Game.
  // activated_abilities` never synthesises it - `action_rows.is_equip_row`'s
  // own docstring), so nothing here can say whether it is legal *right now*
  // off the card alone the way a real activated ability can. This only
  // routes an Equipment permanent to the menu rather than "none" or the
  // single-ability auto-activate; `openPermanentMenu` decides whether Equip
  // actually appears in it, from the engine's own legal-actions answer.
  const equipment = /\bequipment\b/i.test(card.type_line || "");
  if (!abilities.length && !vehicle && !equipment) {
    return { kind: "none", reason: `${card.name} has no activated abilities` };
  }
  if (abilities.length === 1 && !vehicle && !equipment) return { kind: "activate", abilityIndex: 0 };
  const menu = { kind: "menu", abilities, vehicle };
  if (equipment) menu.equipment = true;
  return menu;
}

// -------------------------------------------------------------- polling --

/** A cycle's two reads, issued together rather than one after the other.
 * They are independent - `/log` does not depend on the board read and never
 * did - so awaiting them in sequence spent the sum of two latencies where
 * the maximum will do. That is not a micro-optimisation here: the comment
 * above is about a cycle that already outgrows `POLL_MS` late-game with
 * `/log` as the slow half, and every millisecond of cycle is a millisecond
 * the board can be out of date after a click.
 *
 * Both promises are handed back rather than awaited, because the post-action
 * `refresh()` below wants only the board half of the pair to gate on. */
function cycleReads(app) {
  return {
    seq: (boardReads += 1),
    state: api("GET", withUid(gamePath(""), app.uid)),
    log: api("GET", withUid(gamePath(`log?since=${app.log.lastSeq}`), app.uid)),
  };
}

// Ordering the board reads against each other.
//
// `refresh()` deliberately sets no `polling` flag (see its own note), so a
// background poll's board read and an action's own are routinely outstanding
// together - one cycle runs 0.4-2.4s against a 1200ms tick. `mutations`
// separates reads issued BEFORE one of this seat's requests from reads
// issued after it, and that is all it can do: two reads issued after the
// same request both pass it, and then whichever is ANSWERED last wins.
// Answer order is not read order - nothing about HTTP promises it is - so
// the older of the two can land second and put the board back a turn, and
// hand `tickRun()` a world that has already moved.
//
// So every board read carries the ordinal it was issued with, and writes
// only if nothing newer has been written already. Issue order is the order
// used because it is the only one available: the board payload carries no
// version of its own, and two reads issued in order do describe the server
// at two points in that order. This sits under the `mutations` guard rather
// than replacing any part of it.
let boardReads = 0;
let boardWritten = 0;

/** Whether the board a read with ordinal `seq` came back with is still the
 * newest one this seat has seen - and, when it is, records it as written. */
function boardIsNewest(seq) {
  if (seq <= boardWritten) return false;
  boardWritten = seq;
  return true;
}

/** One poll cycle: read the board, read the log, show both, let the run act.
 *
 * `mutations` is what keeps those four steps honest with each other. The two
 * reads are answered at the moment the server handles them, and `tickRun()`
 * runs only once BOTH have landed - so anything this seat does in between (a
 * "Pass until" click, a blocker declaration, a cast) lands on a server whose
 * answer to the already-issued board read was written before it. Writing
 * that answer into the store as current puts the pre-action board back on
 * the screen, and then hands it to `tickRun()`, which reads
 * `priority_holder` as still this seat's and passes a second time. The
 * engine refuses that pass, and the run is over before it went anywhere -
 * the owner's "my pass_until doesn't work", reproduced against a live server
 * (`tests/test_ui_app.py`'s stale-board tests, and MEMORY.md's own note).
 *
 * It is not a rare race. One poll cycle grew past `POLL_MS` in the late game
 * - a bigger board and a longer log make the `/log` read the slow half - so
 * a poll was in flight at every instant and every click landed inside one.
 *
 * So a cycle that spans one of this seat's own requests throws its reads
 * away rather than publishing them: nothing is written, nothing acts, and
 * the next tick (1.2s) reads a board that includes the action. Losing a
 * cycle is cheap; acting on a board that has already moved is not.
 *
 * Exported for `tests/test_ui_app.py` only, the same reason `store`/
 * `tickRun`/`declareNoAttackers` are: the defect lives in the ordering
 * between a poll's reads and this seat's own action, so a test of it has to
 * drive the real poll rather than a restatement of it. */
export async function poll() {
  const app = store.get();
  if (!app.gameId || app.polling) return;
  store.set({ polling: true });
  try {
    const mutationsBefore = app.mutations;
    const since = app.log.lastSeq;
    const reads = cycleReads(app);
    const [state, log] = await Promise.all([reads.state, reads.log]);
    if (store.get().mutations !== mutationsBefore) {
      // One of this seat's own actions was issued while these two reads were
      // outstanding, so they describe a board that no longer exists. The
      // connection is fine, though - both reads landed - and saying so is
      // what keeps a discarded cycle from reading as a dropped one.
      store.set({ connection: "ok" });
      return;
    }
    // The board half is written only if no read issued behind this one has
    // already published its own (`boardIsNewest`); the log half has its own
    // guard, below, and the two are independent.
    const newest = boardIsNewest(reads.seq);
    store.set((s) => ({
      ...(newest ? { state } : {}),
      connection: "ok",
      // The log half is written only if nothing has advanced it since this
      // cycle asked for `since` - the same guard `refresh()` carries, and now
      // needed on both sides of the pair. `refresh()` sets no `polling` flag,
      // so an action's log read and a background poll's log read are
      // routinely outstanding together (`/log` runs 0.4-2.4s against a 1200ms
      // tick); both asked from the same `since`, so whichever lands second is
      // a second copy of entries already on the screen. `patchChildren`
      // builds its `existing` map before it creates anything, so a key
      // repeated inside one items list becomes a second node the next patch
      // cannot see - the duplicated lines never go away. Dropping this half
      // costs at most one tick of log, and the next cycle asks from the
      // `lastSeq` the other read published.
      log:
        s.log.lastSeq === since
          ? { entries: [...s.log.entries, ...log.log].slice(-500), lastSeq: log.last_seq }
          : s.log,
    }));
    refreshDecisionTargets();
    await tickOnce();
  } catch (err) {
    store.set({ connection: `error: ${err.message}` });
  } finally {
    store.set({ polling: false });
  }
}

/** Set while a `refresh()` or a `poll()` is driving `tickRun`.
 *
 * `declareNoAttackers`/`declareNoBlockers` are called from inside `tickRun`
 * and refresh like every other action, so without this the run's own
 * auto-answer recurses without bound whenever the server keeps handing back
 * the same unanswered decision. `poll()`'s `polling` flag used to bound that
 * at one nested tick; this keeps the same bound now that an action no longer
 * goes through `poll()` - and it covers the poll's own tick too, which the
 * flag did not when it was only set around `refresh()`'s: a poll landing
 * mid-tick would start a second one, and only `send()`'s `inFlight` dedupe
 * kept the duplicate pass off the wire. */
let ticking = false;

/** `tickRun()`, unless one is already running - see `ticking`. */
async function tickOnce() {
  if (ticking) return;
  ticking = true;
  try {
    await tickRun();
  } finally {
    ticking = false;
  }
}

/** The board this seat's own action just produced, on the screen now rather
 * than up to a full tick later.
 *
 * Every action path in this file used to end in `await poll()`, and that was
 * frequently a silent no-op: `poll()` returns immediately whenever
 * `app.polling` is set, and late-game a background poll is in flight at
 * every instant (its own docstring above). The background poll then
 * discarded its own reads through the `mutations` guard - correctly, they
 * predate the action - so nothing was published by either, and the board sat
 * showing the pre-action world for up to `POLL_MS` after a click that had
 * already succeeded. That is what a player reads as a lost click, and it is
 * what provokes the second one.
 *
 * So this is a read of its own rather than a request to a poll that is
 * already busy. Two shapes were possible; this is the simpler one:
 *
 * - awaiting the in-flight poll and then re-polling makes the player's
 *   feedback wait on the very cycle that is already too slow (and, against
 *   a held `/log`, waits on it forever - `tests/test_ui_app.py`'s stale-poll
 *   tests hold exactly that read open);
 * - issuing a fresh pair of reads costs one extra board read per action and
 *   answers as fast as the server can answer.
 *
 * Only the board half is awaited. `/log` is the slow half of a cycle and no
 * part of "did my move happen" - it is issued alongside (`cycleReads`) and
 * written whenever it lands, if it still applies.
 *
 * The `mutations` guard applies here exactly as it does to a poll, and for
 * the same reason: a read of this seat's own is no more current than anyone
 * else's once a later request of this seat's has gone out behind it. */
async function refresh() {
  const app = store.get();
  if (!app.gameId) return;
  const mutationsBefore = app.mutations;
  const since = app.log.lastSeq;
  const reads = cycleReads(app);
  reads.log
    .then((log) => {
      const now = store.get();
      // Discarded for the same reason a poll's reads are - and additionally
      // when something else has already advanced the log past `since`, since
      // appending the same entries twice would double them on the screen.
      if (now.mutations !== mutationsBefore || now.log.lastSeq !== since) return;
      store.set((s) => ({
        log: { entries: [...s.log.entries, ...log.log].slice(-500), lastSeq: log.last_seq },
      }));
    })
    // Nothing here is awaited, so this is the whole of this read's error
    // handling - and it has to cover the write as well as the read, or a
    // malformed payload becomes an unhandled rejection rather than the
    // "error: ..." on the connection pill that `poll()` shows for exactly
    // the same failure. The board itself is unaffected either way: the log
    // is the half of the pair nothing acts on.
    .catch((err) => store.set({ connection: `error: ${err.message}` }));
  try {
    const state = await reads.state;
    if (store.get().mutations !== mutationsBefore) {
      store.set({ connection: "ok" });
      return;
    }
    // A poll's read issued behind this one may already have published a
    // newer board - this read was answered late, not last.
    if (!boardIsNewest(reads.seq)) {
      store.set({ connection: "ok" });
      return;
    }
    store.set({ state, connection: "ok" });
    refreshDecisionTargets();
  } catch (err) {
    store.set({ connection: `error: ${err.message}` });
    return;
  }
  await tickOnce();
}

/** S7 (2b's T8): the play UI's own half of the fix. One `GET
 * ${lobbyBase()}/games/{id}/seats` answers every seat's agent settings and
 * liveness together - `app.aiStatus` reads out the one entry matching
 * `app.aiUid`, the same shape `GET .../ai-seat/status` used to hand back
 * directly. A missing or unreachable game (a stale poll after leaving it)
 * or no AI seat at all is a silent no-op - the next tick tries again.
 *
 * The `uid` query parameter is what makes the match possible now: that
 * route only puts a seat's `uid` on the entry matching the credential the
 * caller actually presents (`decks_api.SeatDetail.uid`), because a seat uid
 * is the authority to act as that seat and handing every one of them to
 * every reader made this route a way to obtain one. This browser holds the
 * AI seat's uid already - it is what `?uid=` presents - so the entry it is
 * looking for is exactly the entry it is answered about. */
async function pollSeats() {
  const app = store.get();
  if (!app.gameId || !app.aiUid) return;
  try {
    const { seats } = await api(
      "GET",
      withUid(`${lobbyBase()}/games/${app.gameId}/seats`, app.aiUid)
    );
    const mine = (seats || []).find((seat) => seat.uid === app.aiUid);
    if (mine) store.set({ aiStatus: mine.ai_seat });
  } catch {
    /* transient - the next poll tries again */
  }
}

/** Starts both poll loops - the board's fast one and the AI seat's slower
 * one - rather than each of `main()`'s two boot paths repeating the pair of
 * `setInterval` calls. */
function startPolling() {
  setInterval(poll, POLL_MS);
  setInterval(pollSeats, SEAT_POLL_MS);
}

// ------------------------------------------------------- decision targets --
//
// `board.js` reads `app.decisionTargets` (`{slots, affordance}`) to light up
// legal targets on the board while a `choose_targets` decision (not a cast -
// see `ui/decisions.js`'s own header) is pending. `decisions.js`'s own
// `viewModel` already builds exactly that shape as its `targets`-type
// control; this only asks for it and keeps it in the store, rather than
// rebuilding the affordance a second way.

function computeDecisionTargets(state, decisionDraft) {
  const pending = state && state.pending_decision;
  if (!state || !pending || pending.player !== state.you || pending.kind !== "choose_targets") {
    return null;
  }
  const vm = viewModel(pending, decisionDraft || {});
  const control = (vm.controls || []).find((c) => c.type === "targets");
  return control ? { slots: control.slots, affordance: control.affordance } : null;
}

function refreshDecisionTargets() {
  const app = store.get();
  store.set({ decisionTargets: computeDecisionTargets(app.state, app.decisionDraft) });
}

// ---- actions -------------------------------------------------------------

async function refreshAffordance() {
  const app = store.get();
  const action = app.action;
  if (!action || action.kind === "special") return;
  const query = new URLSearchParams({ player: you() });
  const q = A.affordanceQuery(action);
  if (q.ability_index !== undefined) query.set("ability_index", q.ability_index);
  if (q.modes) query.set("modes", q.modes.join(","));
  if (q.x_value !== undefined) query.set("x_value", q.x_value);
  if (action.kind === "cast" && !A.modesReady(action)) return;
  const affordance = await send("GET", gamePath(`affordances/${action.cardId}?${query}`));
  if (affordance === null) return;
  store.set((s) => (s.action && s.action.cardId === action.cardId ? { action: A.withAffordance(s.action, affordance) } : {}));
  await previewAndMaybeFire();
}

async function previewAndMaybeFire() {
  const app = store.get();
  const action = app.action;
  if (!action) return;
  if (!A.readyToFire(action)) {
    store.set({ preview: null });
    return;
  }
  const body = { ...A.bodyFor(action, you(), untappedSources(app.state)), dry_run: true };
  const probe = await send("POST", gamePath(A.endpointFor(action)), body);
  if (probe === null) return;
  if (probe.allowed === false) {
    store.set({ action: { ...action, error: probe.reason }, preview: { tapped: probe.tapped || [] } });
    return;
  }
  store.set({ preview: { tapped: probe.tapped || [] } });
  if (action.kind !== "special" && A.needsExplicitFire(action)) return;
  await fire();
}

// `needsExplicitFire` lives in actions.js (Task 15 moved it there so the
// ribbon's Cast button and this loop agree): pinned mana, a division, or an
// "up to"/unbounded slot waits for the player's Cast or Done.

// Exported for `tests/test_ui_app.py` only - same reason `store` is (its own
// comment above): a real, awaited drive of the keep-priority default against
// a stubbed `fetch`, rather than a Python restatement of it.
export async function fire() {
  const app = store.get();
  const action = app.action;
  const body = A.bodyFor(action, you(), untappedSources(app.state));
  const res = await send("POST", gamePath(A.endpointFor(action)), body);
  if (res === null) return;
  if (res.allowed === false) {
    store.set({ action: { ...action, error: res.reason } });
    toast(res.reason);
    return;
  }
  store.set({ action: null, preview: null });
  const onStack = !(res.mana_ability || action.kind === "special");
  // CR 117.3c already gives you priority back after casting or activating.
  // `action.holdPriority` is this one action's tick; untouched (`null`) it
  // follows the stops panel's standing `keepPriorityByDefault`, read fresh
  // here and on unless explicitly `false`.
  const hold = action.holdPriority ?? store.get().stops.keepPriorityByDefault !== false;
  if (onStack && !hold) {
    await passOnce();
  }
  await refresh();
}

/** The element a card-scoped menu anchors itself over: the card node that
 * was actually clicked, so the menu sits against that card and not against
 * whatever else shares its id. `board.js`'s `patchStacks` wraps a permanent
 * and everything tucked under it in a `.perm-stack` keyed by the *same* id,
 * so a `[data-key]` lookup finds the wrapper first - the click's own
 * `currentTarget` is the card. The lookup is the fallback for the callers
 * that have no event (nothing anchors to `document` - `showPopover` with no
 * anchor does nothing rather than opening a menu in the corner). */
function anchorFor(card, event) {
  return (event && event.currentTarget) || document.querySelector(`[data-key="${card.id}"]`);
}

/** Open a card-scoped menu over the clicked card. `items` are `menus.js`'s
 * `{key, label, sublabel}` descriptors; `run(key)` is what this file does
 * about the one that gets picked. `menus.js` decides what is offered, this
 * decides what happens - neither knows about the other's half. */
function openCardMenu(card, event, items, run) {
  return openCardMenuAt(anchorFor(card, event), card, items, run);
}

/** The same, against an element already resolved. A menu opened from
 * *inside* another menu's `onSelect` cannot go back to the original click's
 * `event`: that dispatch has finished, and a browser nulls `currentTarget`
 * once it has. The caller resolves the anchor while the click is still live
 * and hands the element on. */
function openCardMenuAt(anchor, card, items, run) {
  return showPopover(anchor, menuOptions(items, run), { title: card.name });
}

/** `menus.js`'s `{key, label, sublabel}` descriptors, each given the thing
 * this file does about it. Shared by the open and the enrichment's redraw so
 * that a re-aimed option runs the same `run(key)` the original did. */
function menuOptions(items, run) {
  return items.map((item) => ({ ...item, onSelect: () => run(item.key) }));
}

/** The menu EVERY action click opens - one option or six. A click that acts
 * (playing a land, casting, activating the one ability a permanent has) used
 * to fire on the click itself; it opens this instead, and the one-option
 * popover IS the confirm. Nothing else is drawn for it: no second component,
 * and no Cancel item, because `showPopover` already leaves on Escape, on a
 * click outside it, and on picking something.
 *
 * The menu comes from local state and is on screen before this function
 * returns - a click has to feel immediate, so nothing here waits on a
 * request. `enrichActionMenu` then asks the engine which of the options it
 * can actually pay for and folds that into the menu already open.
 *
 * An empty `items` opens nothing at all rather than an empty box - the same
 * silence a click with nothing to do gets.
 *
 * The anchor is resolved ONCE, here, while the click is still being
 * dispatched, for the reason `pinLand` already does it: the enrichment
 * redraws this menu long after that dispatch is over, and a browser has
 * nulled `event.currentTarget` by then. */
function openActionMenu(card, event, items, run) {
  if (!items.length) return;
  const anchor = actionAnchor(card, event);
  openCardMenuAt(anchor, card, items, run);
  return enrichActionMenu(anchor, card, items, run);
}

/** `anchorFor`, plus the one case where the clicked node is the wrong thing
 * to anchor to: a card inside `#modal` (the report form and AI-seat error
 * box are the two things that can be open there now that the zones popover
 * is gone - neither renders a clickable card, so this branch is dormant in
 * practice, but it costs nothing to keep and `tests/test_ui_app.py` still
 * pins the behaviour).
 *
 * That modal and an anchored menu is a `.popover`, and the modal is drawn on
 * top of it (`style.css`) - so a confirm opened over one of the modal's own
 * cards would sit under the scrim, dimmed and unclickable, and Escape would
 * close the modal rather than the menu. The menu's geometry and stacking are
 * deliberately not this file's to change, so the modal is closed instead: the
 * player clicked a card to act on it, which is the end of whatever the modal
 * was showing either way, and the confirm anchors over the board's own node
 * for that card. There always is one - the board draws every graveyard and
 * exile card in its zone ribbon and every play permission in
 * `#permitted-row`, both keyed by card id (`dom.js`'s `patchChildren`) - and
 * the clicked node itself is the fallback if there somehow is not, so the
 * menu opens either way. */
function actionAnchor(card, event) {
  const clicked = event && event.currentTarget;
  const modal = document.querySelector("#modal");
  if (!clicked || !modal || modal.hidden || !modal.contains(clicked)) {
    return anchorFor(card, event);
  }
  closeModal(document);
  return document.querySelector(`[data-key="${card.id}"]`) || clicked;
}

/** Fold `GET .../legal-actions` into the menu that is already on screen: an
 * option the engine reports it cannot pay for is disabled and captioned with
 * the engine's own `unaffordable_reason` (`menus.js`'s `enrichItems` decides
 * that; this only fetches and redraws).
 *
 * On the CLICK, never on the poll: per-card affordance work is banned from
 * the 1.2s tick (it is a whole-board read per card there, and on-demand is
 * the established pattern - `refreshAffordance` is the same shape). The read
 * is `player`'s own hidden information, so it carries this tab's uid; it is
 * deliberately NOT sent through `send()`, which counts mutations for
 * `poll()`'s staleness check and toasts what it catches - this is a read,
 * and a failed one has nothing to say to the player.
 *
 * Every failure is the same silent no-op: the dialog stays exactly as it
 * opened. So is an answer that arrives after the player has left the dialog
 * - reopening a menu under the cursor is worse than not enriching it. */
async function enrichActionMenu(anchor, card, items, run) {
  const app = store.get();
  if (!app.gameId || !app.uid || !app.state) return;
  let answer;
  try {
    answer = await api(
      "GET",
      withUid(gamePath(`legal-actions?player=${encodeURIComponent(you())}`), app.uid)
    );
  } catch {
    return;
  }
  const enriched = M.enrichItems(items, (answer && answer.actions) || []);
  if (enriched.every((item, i) => item === items[i])) return;
  if (!popoverIsOpenFor(anchor)) return;
  // Patched in place, never reopened: the answer can land while the player
  // is mid-press on one of these options, and a browser dispatches the click
  // to the nearest inclusive ancestor common to the mousedown and the mouseup
  // target - so an option detached between the two eats the press outright.
  // `updatePopoverItems` is the whole of that argument.
  updatePopoverItems(anchor, menuOptions(enriched, run));
}

/** Exported for `tests/test_ui_app.py` only, alongside `store`/`pinLand`/
 * `fire` - the confirm dialog is a property of the click ACTING, not of the
 * plan, so proving "every action click opens it and no selection click does"
 * means driving the real handler through the real `showPopover`. Not part of
 * any render module's handler contract; `board.js` gets these through
 * `main()`'s own handler object. */
export async function onHandCard(card, event) {
  const app = store.get();
  const plan = planHandCardClick(card, app);
  if (plan.kind === "cancel") return cancel();
  if (plan.kind === "target") return tryTarget(plan.id);
  // `land`, `cast` and `menu` all reach the same menu: `handMenuItems` gives
  // the first two a single Play or Cast item, which is the confirm.
  return openActionMenu(card, event, M.handMenuItems(plan, card), (key) => {
    if (key === "play") return playLand(card);
    if (key === "cast") return beginCast(card);
    if (key === "cycle") return cycle(card);
    if (key.startsWith("special:")) return fireSpecial(card, key.slice("special:".length));
  });
}

async function beginCast(card) {
  hidePopover();
  store.set({ action: A.begin("cast", card, {}), preview: null });
  if (!A.needsChoices(card, null)) return previewAndMaybeFire();
  return refreshAffordance();
}

function beginCrew(card) {
  hidePopover();
  store.set({ action: A.begin("crew", card, {}), preview: null });
  return refreshAffordance();
}

/** CR 601.3's dynamic play permissions (a flashback grant, an exiled card
 * made playable "this turn" - cascade's hit and suspend's own third ability
 * no longer grant one of these, battle 8): begin casting `card` from
 * `fromZone` (exile, a graveyard) rather than the hand - `actions.js`'s
 * `bodyFor` sends `from_zone` only when this is set, so an ordinary hand
 * cast is unaffected. */
function beginCastFromPermission(card, fromZone) {
  hidePopover();
  store.set({ action: A.begin("cast", card, { fromZone }), preview: null });
  if (!A.needsChoices(card, null)) return previewAndMaybeFire();
  return refreshAffordance();
}

/** Exported for `tests/test_ui_app.py` only, alongside `onHandCard`/
 * `onPermanent` - see `onHandCard`.
 *
 * A play permission is an action click like any other: it begins a cast, so
 * it opens the same one-option confirm rather than casting on the click that
 * landed on the card. Three surfaces reach here and two of them are the
 * board itself - `#permitted-row` and the zone ribbon (`render/board.js`) -
 * with the zones popover the third (`render/panels.js`). */
export function onPermission(permission, event) {
  const card = permission.card;
  return openActionMenu(card, event, M.permissionMenuItems(permission), (key) => {
    if (key === "cast") return beginCastFromPermission(card, permission.from_zone);
  });
}

async function playLand(card) {
  const res = await send("POST", gamePath("actions/play-land"), { player: you(), card_id: card.id });
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await refresh();
}

async function fireSpecial(card, keyword) {
  hidePopover();
  const res = await send("POST", gamePath(`actions/${keyword}`), { player: you(), card_id: card.id });
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await refresh();
}

async function cycle(card) {
  hidePopover();
  const res = await send("POST", gamePath("actions/cycle"), { player: you(), card_id: card.id });
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await refresh();
}

/** Exported for `tests/test_ui_app.py` only - see `onHandCard` above. */
export async function onPermanent(card, event) {
  const app = store.get();
  const state = app.state;
  const plan = planPermanentClick(card, app, state);
  if (plan.kind === "pin") return pinLand(card, event);
  if (plan.kind === "target") return tryTarget(plan.id);
  if (plan.kind === "attack") return toggleAttacker(card, event);
  if (plan.kind === "block") return toggleBlock(card);
  if (plan.kind === "none") {
    if (plan.reason) toast(plan.reason);
    return;
  }
  // An Equipment's menu cannot open from local state alone (see
  // `planPermanentClick`'s own comment) - it has to ask the engine whether
  // Equip is legal right now before it can say whether the item belongs in
  // the menu at all, not merely whether to grey it out.
  if (plan.kind === "menu" && plan.equipment) {
    return openPermanentMenu(card, event, plan);
  }
  // `activate` and `menu` alike: `abilityMenuItems` reads the one ability an
  // `activate` plan names off the card, so a one-ability permanent opens the
  // same menu with one item in it rather than activating on the click.
  return openActionMenu(card, event, M.abilityMenuItems(plan, card), (key) => {
    if (key === "crew") return beginCrew(card);
    if (key.startsWith("activate:")) return beginActivate(card, Number(key.slice("activate:".length)));
  });
}

/** The menu for an Equipment permanent (`planPermanentClick`'s `equipment`
 * flag): unlike every other action click, this one has to ask the engine
 * BEFORE opening anything, rather than opening from local state and
 * enriching afterwards (`enrichActionMenu`'s own pattern). Equip's CR 702.6a
 * "activate only as a sorcery" timing makes the engine drop the row
 * entirely outside a main phase (`action_rows.legal_action_rows`) rather
 * than mark it unaffordable, and `menus.js`'s `enrichItems` only ever
 * disables a row it can already see - so an item added unconditionally and
 * enriched afterwards would sit there, enabled, offering an action the
 * engine never listed at all. One `candidates=true` legal-actions read
 * settles both questions at once: whether Equip belongs in the menu, and,
 * if so, which creatures it may legally attach to - so `beginEquip` never
 * has to ask again.
 *
 * The anchor is resolved before the read, not after, for the reason
 * `openActionMenu`'s own comment gives: the browser nulls `event.
 * currentTarget` once the click's dispatch is over, and this function's own
 * `await` outlives it. */
async function openPermanentMenu(card, event, plan) {
  const anchor = actionAnchor(card, event);
  const app = store.get();
  let answer = { actions: [], last_seq: 0 };
  if (app.gameId && app.uid && app.state) {
    try {
      answer = await api(
        "GET",
        withUid(gamePath(`legal-actions?player=${encodeURIComponent(you())}&candidates=true`), app.uid)
      );
    } catch {
      // The same silent no-op every enrichment failure gets: the click
      // asked and got nothing back, so this card offers exactly what its
      // own abilities/vehicle status already say.
      answer = { actions: [], last_seq: 0 };
    }
  }
  const rows = (answer && answer.actions) || [];
  const equipRow = rows.find((row) => row.card_id === card.id && row.kind === "equip");
  const items = M.enrichItems(
    M.abilityMenuItems({ ...plan, equipment: Boolean(equipRow) }, card),
    rows
  );
  if (!items.length) return;
  openCardMenuAt(anchor, card, items, (key) => {
    if (key === "crew") return beginCrew(card);
    if (key === "equip") return beginEquip(anchor, card, equipRow, answer.last_seq);
    if (key.startsWith("activate:")) return beginActivate(card, Number(key.slice("activate:".length)));
  });
}

/** Picking "Equip" out of that menu: a second small popover, over the same
 * card, listing the row's own candidates (CR 702.6a's "target creature you
 * control") - `showPopover` already replaces the first popover with this
 * one, the same two-step shape `settlePin` uses for a mana source's
 * ability-then-colour choice. Equip carries no modes, no X and exactly one
 * target (CR 301.5c), so picking a candidate here IS the confirm; nothing
 * further is asked. */
function beginEquip(anchor, card, row, lastSeq) {
  const slot = row && row.targets && row.targets.slots && row.targets.slots[0];
  const lines = (slot && slot.candidates) || [];
  if (!lines.length) {
    hidePopover();
    toast(`${card.name} has no legal creature to equip`);
    return;
  }
  openCardMenuAt(anchor, card, M.equipTargetMenuItems(lines), (key) => {
    fireEquip(card, key.slice("target:".length), lastSeq);
  });
}

/** `POST .../actions/equip` (CR 702.6a) - the route the api, the CLI and the
 * MCP server already share (`schemas.EquipRequest`). `expectedSeq` is the
 * `last_seq` the menu's own legal-actions read carried, named back as
 * `expected_seq` so a board that moved between that read and this click is a
 * refusal (`records.require_current_board`) rather than a stale action fired
 * blind. No `mana_sources`: the engine's own `payment.pay()` auto-taps the
 * rest, exactly as `playLand`/`fireSpecial`/`cycle` already leave it to. */
async function fireEquip(card, targetId, expectedSeq) {
  hidePopover();
  const body = { player: you(), card_id: card.id, target: targetId };
  if (expectedSeq !== undefined && expectedSeq !== null) body.expected_seq = expectedSeq;
  const res = await send("POST", gamePath("actions/equip"), body);
  if (res === null) return;
  if (res.allowed === false) {
    toast(res.reason);
    return;
  }
  // CR 117.3c already gives priority back; equip has no ribbon panel of its
  // own to carry a per-action hold tick, so this follows the stops panel's
  // standing default exactly as `fire()` does when nothing overrides it.
  const hold = store.get().stops.keepPriorityByDefault !== false;
  if (!hold) await passOnce();
  await refresh();
}

async function beginActivate(card, index) {
  hidePopover();
  let action = A.begin("activate", card, { abilityIndex: index });
  const colors = colorOptions(card, index);
  if (colors.length === 1) action = A.chooseColor(action, colors[0]);
  store.set({ action, preview: null });
  if (!A.needsChoices(card, index) || (colors.length <= 1 && !(card.abilities[index].targeting || {}).targets_anything && !card.abilities[index].needs_x)) {
    return previewAndMaybeFire();
  }
  return refreshAffordance();
}

/** What a mid-cast click on a mana source should do - the step after
 * `planPermanentClick`'s own `{kind: "pin"}`, and pure for the same reason
 * it is: `pinLand` below only acts on what this decides.
 *
 * `{kind: "unpin"}` (it was already pinned - the click takes it back) |
 * `{kind: "ability", abilities}` (more than one mana ability, so *which one*
 * has to be asked before anything else: Eldrazi Temple taps for {C}, or for
 * {C}{C} that may only be spent on Eldrazi spells, and the two are not
 * interchangeable) | `{kind: "colour", abilityIndex, colors}` (one ability,
 * but it offers a choice of colour) | `{kind: "pin", abilityIndex, color}`
 * (nothing left to ask - pin it now).
 *
 * `abilityIndex` in the two settled plans is what goes on the wire, and it
 * is deliberately `null` whenever the source had only ONE mana ability:
 * there was no choice to record, so the request keeps exactly the shape it
 * has always had and the engine's `auto_tap` keeps solving it. A basic land
 * therefore still pins on one click, with no menu of any kind. */
export function planPin(card, action) {
  if ((action.pinned || []).some((p) => p.card_id === card.id)) return { kind: "unpin" };
  const abilities = manaAbilities(card);
  if (abilities.length > 1) return { kind: "ability", abilities };
  // `planPermanentClick`'s `{kind: "pin"}` guard already established there
  // is at least one `is_mana_ability` entry; index 0 is the fallback for a
  // caller that reached here another way.
  return pinStep(card, abilities.length ? abilities[0].index : 0, false);
}

/** The same step once the player has picked one of several mana abilities:
 * the index IS named on the wire from here, because it is the choice the
 * player just made. */
export function planPinnedAbility(card, abilityIndex) {
  return pinStep(card, abilityIndex, true);
}

function pinStep(card, index, named) {
  const colors = colorOptions(card, index);
  const abilityIndex = named ? index : null;
  if (colors.length > 1) return { kind: "colour", abilityIndex, colors };
  // No single colour to pin (`colorOptions` came back empty - a one-option
  // ability reports `mana_options: null`, `affordances.py`'s own
  // `ability_hints`) still pins *which* source, with no colour named, so
  // `mana.js`'s `manaSources` leaves the colour to the engine's own
  // `auto_tap` rather than forcing a colourless `"C"` choice onto a land
  // that never produces colourless mana at all.
  return { kind: "pin", abilityIndex, color: colors[0] ?? null };
}

/** Exported for `tests/test_ui_app.py` only, alongside `store`/`fire`/
 * `tryTarget` above - the two-menu flow (which ability, then which colour)
 * is only real once it has been driven through `showPopover` itself. */
export async function pinLand(card, event) {
  const plan = planPin(card, store.get().action);
  if (plan.kind === "unpin") {
    store.set((s) => ({ action: A.unpinSource(s.action, card.id) }));
    return previewAndMaybeFire();
  }
  // Resolved once, here, while the click that opened this is still being
  // dispatched - the ability menu's own `onSelect` runs after it is over.
  const anchor = anchorFor(card, event);
  if (plan.kind === "ability") {
    return openCardMenuAt(anchor, card, M.manaAbilityMenuItems(plan.abilities), (key) => {
      const index = Number(key.slice("ability:".length));
      return settlePin(anchor, card, planPinnedAbility(card, index));
    });
  }
  return settlePin(anchor, card, plan);
}

/** Act on a settled `planPin`/`planPinnedAbility` step: ask for the colour
 * if the chosen ability offers one (a second popover over the same card),
 * otherwise pin it. Both answers carry `plan.abilityIndex` through, so a
 * choice of ability and a choice of colour reach the request together. */
function settlePin(anchor, card, plan) {
  if (plan.kind === "colour") {
    return openCardMenuAt(anchor, card, M.manaMenuItems(plan.colors), (key) => {
      const colour = key.slice("colour:".length);
      store.set((s) => ({ action: A.pinSource(s.action, card.id, colour, plan.abilityIndex) }));
      previewAndMaybeFire();
    });
  }
  store.set((s) => ({ action: A.pinSource(s.action, card.id, plan.color, plan.abilityIndex) }));
  return previewAndMaybeFire();
}

// Exported for `tests/test_ui_app.py` only, alongside `store` above - see
// its own comment there.
export async function tryTarget(id) {
  const app = store.get();
  const pending = app.state.pending_decision;
  if (pending && pending.player === you() && pending.kind === "choose_targets" && !app.action) {
    const draft = app.decisionDraft.targets || [];
    const next = draft.includes(id) ? draft.filter((t) => t !== id) : [...draft, id];
    store.set({ decisionDraft: { ...app.decisionDraft, targets: next } });
    return refreshDecisionTargets();
  }
  if (!app.action) return;
  const action = app.action;
  if (action.kind === "crew") {
    store.set({ action: A.toggleCrewing(action, id) });
    return previewAndMaybeFire();
  }
  if (action.affordance && action.affordance.understood === false) {
    return store.set({ action: A.addFreeTarget(action, id) });
  }
  // The affordance is still an async GET in flight (right after
  // `beginCast`/`beginActivate`) - there are no real slots yet to fill, so
  // the click is queued the way `actions.js`'s `chooseTarget` always has for
  // exactly this case, and `withAffordance` replays it once real slots
  // exist.
  if (action.slots.length === 0 && !action.affordance) {
    store.set({ action: A.chooseTarget(action, id) });
    return;
  }
  // A second click on an already-chosen target is not always an un-choose:
  // if the active (highlighted) slot can also legally take this same id -
  // Kozilek's Command choosing modes 1 and 2 with you as both targets is
  // exactly this case - the click instead fills that slot too, so the same
  // object ends up in two different slots rather than being removed from
  // the one it already sits in. Only when the active slot cannot take it
  // does this fall back to un-choosing it from whichever slot it actually
  // sits in - not necessarily the highlighted one, and not gated on that
  // slot having room (an "up to two" slot already filled to its cap, or any
  // slot once the action is only waiting on an explicit Cast/Done, still has
  // to let the player take a target back out). `actions.js`'s `chooseTarget`
  // already has exactly this logic, so it is called for this case rather
  // than reimplemented here.
  if (action.slots.some((s) => s.targets.includes(id))) {
    store.set({ action: A.chooseTarget(action, id) });
    return previewAndMaybeFire();
  }
  // Not already chosen: `actions.js`'s `targetSlotIndex` is the single place
  // that decides which slot a fresh click fills - a manual focus from a slot
  // chip's own click (`onFocusSlot`), or `targeting.js`'s own
  // `activeSlotIndex` guess. It is the exact same helper `board.js`'s
  // highlight uses, so the outlined slot and the slot a click actually fills
  // can never disagree - and a click that lands on something illegal for it
  // says so, rather than a silent no-op.
  const slotIndex = A.targetSlotIndex(action);
  if (slotIndex < 0 || !action.slots[slotIndex]) return;
  const slot = action.slots[slotIndex];
  if (!canFill(action.affordance, slot, id)) {
    toast(`that is not a legal target for ${slot.label}`);
    return;
  }
  store.set({ action: { ...action, slots: fill(action.slots, slotIndex, id) } });
  return previewAndMaybeFire();
}

function onFocusSlot(i) {
  store.set((s) => ({
    action: s.action ? { ...s.action, focusSlot: s.action.focusSlot === i ? null : i } : null,
  }));
}

function cancel() {
  hidePopover();
  store.set({ action: null, preview: null });
}

// ---- combat --------------------------------------------------------------

function toggleAttacker(card, event) {
  const app = store.get();
  const defenders = C.defenders(app.state);
  if (!C.attackCandidates(app.state).some((c) => c.id === card.id) && !app.combatDraft[card.id]) {
    return toast(`${card.name} cannot attack now`);
  }
  if (defenders.length <= 1 || app.combatDraft[card.id]) {
    return store.set({
      combatDraft: C.toggleAttacker(app.combatDraft, card.id, app.combatDraft[card.id] || defenders[0].id),
    });
  }
  openCardMenu(card, event, M.defenderMenuItems(defenders), (key) => {
    const defenderId = key.slice("defender:".length);
    store.set((s) => ({ combatDraft: C.toggleAttacker(s.combatDraft, card.id, defenderId) }));
  });
}

async function confirmAttack() {
  const body = C.attackBody(store.get().combatDraft);
  const res = await send("POST", withUid(gamePath("actions/attack"), store.get().uid), body);
  if (res === null) return;
  if (res.allowed === false) return toast(res.reason);
  store.set({ combatDraft: {} });
  await refresh();
}

/** "No attack" - CR 508.1a's "if any" makes declaring nobody a real, legal
 * declaration, not merely "do nothing": the game genuinely pauses on this
 * question now (`decisions.ANSWERABLE_KINDS` covers `declare_attackers`)
 * rather than silently defaulting to nobody on its own, so somebody has to
 * say so. Sends the same empty `attackers` `confirmAttack` would for an
 * empty draft, whatever the draft actually holds right now - "no attack"
 * overrides an in-progress draft rather than requiring it to be cleared by
 * hand first.
 *
 * Exported for `tests/test_ui_app.py` only, alongside `store`/`tryTarget`
 * above - see `store`'s own comment. */
export async function declareNoAttackers() {
  const res = await send(
    "POST",
    withUid(gamePath("actions/attack"), store.get().uid),
    C.attackBody({})
  );
  if (res === null) return;
  if (res.allowed === false) return toast(res.reason);
  store.set({ combatDraft: {} });
  await refresh();
}

let pendingBlocker = null;
function toggleBlock(card) {
  const app = store.get();
  if (card.controller === you()) {
    pendingBlocker = card.id;
    return toast(`${card.name}: now click the attacker it blocks`);
  }
  if (!pendingBlocker) return;
  store.set({ combatDraft: C.toggleBlock(app.combatDraft, pendingBlocker, card.id) });
  pendingBlocker = null;
}

async function confirmBlocks() {
  const res = await send(
    "POST",
    withUid(gamePath("actions/declare-blockers"), store.get().uid),
    C.blockBody(store.get().combatDraft, you())
  );
  if (res === null) return;
  if (res.allowed === false) return toast(res.reason);
  store.set({ combatDraft: {} });
  await refresh();
}

/** "No blocks" - `declareNoAttackers`'s sibling for CR 509.1a's own "if
 * any". Sends the same empty `blocks` `confirmBlocks` would for an empty
 * draft, overriding whatever is currently drafted.
 *
 * Exported for `tests/test_ui_app.py` only - see `declareNoAttackers`'s own
 * comment above. */
export async function declareNoBlockers() {
  const res = await send(
    "POST",
    withUid(gamePath("actions/declare-blockers"), store.get().uid),
    C.blockBody({}, you())
  );
  if (res === null) return;
  if (res.allowed === false) return toast(res.reason);
  store.set({ combatDraft: {} });
  await refresh();
}

// ---- priority ------------------------------------------------------------

async function passOnce() {
  const res = await send("POST", gamePath("actions/pass-priority"), { player: you() });
  // `transport: true`: `send` already told the player the connection is
  // down, and nothing was decided - the pass was never refused, it was never
  // asked. `tickRun` must tell that apart from a refusal, which is a real
  // answer from the engine.
  if (res === null) {
    return { ok: false, reason: null, outcome: null, settledThrough: null, transport: true };
  }
  const out = P.interpretPassOutcome(res);
  if (!out.ok) toast(out.reason);
  return out;
}

/** One board read, and nothing else: no log, no `store.set` of anything but
 * the state, and - unlike `poll()` - no `tickRun()`, so this can be called
 * from inside `tickRun` without re-entering it. `null` when the read itself
 * failed, which is not the same answer as any board. */
async function readState() {
  try {
    const state = await api("GET", withUid(gamePath(""), store.get().uid));
    store.set({ state, connection: "ok" });
    return state;
  } catch (err) {
    store.set({ connection: `error: ${err.message}` });
    return null;
  }
}

/** CR 502.4/514.3: nobody receives priority during untap or (most) cleanup
 * steps, so `passOnce` has nothing to pass - `POST .../advance`
 * (`engine_api.advance`) is the active player's own way past either.
 * `priority.js`'s `advanceOffered` decides when `#priority-box` offers this. */
async function advanceStep() {
  const res = await send("POST", withUid(gamePath("advance"), store.get().uid), { player: you() });
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await refresh();
}

/** `pass` is one priority, given up, and the board comes back (CR 117.3d).
 * `pass_until` is the same verb on a run: `tickRun` keeps making it until
 * `priority.js`'s `stopReason` says to stop. */
async function onVerb(verb) {
  const app = store.get();
  if (verb === "pass") {
    await passOnce();
    return refresh();
  }
  store.set({ run: P.startRun(verb, app.state) });
  await tickRun();
}

// Exported for `tests/test_ui_app.py` only - same reason `store`/
// `declareNoAttackers` above are: a real, awaited drive of the
// unticked-declare_attackers auto-answer against a stubbed `fetch`.
export async function tickRun() {
  const app = store.get();
  if (!app.run || !app.state) return;
  // Not stopping here is not the same as the game being free to move: the
  // pause is a `pending_decision` (CR 508.1 is a turn-based action, not a
  // priority move), so passing priority cannot answer it and nothing else
  // clears it. Checked - and acted on - before the `priority_holder` guard
  // below, because that value is stale for the whole freeze (`commands.py`
  // rolls the committed game back to before the paused attempt), not just
  // for this one case. `declareNoAttackers` submits `{attackers: []}` and
  // itself calls `refresh()`, which is what picks the run back up (its own
  // nested `tickRun` call): this is not a stop, so the run stays armed.
  if (P.ownUnansweredAttackDeclaration(app.state, app.stops)) {
    await declareNoAttackers();
    return;
  }
  // A decision addressed to someone else freezes the game exactly the same
  // way (`priority_holder` is stale for the whole freeze, not just for the
  // declare_attackers case above) - `stopReason` no longer ends the run for
  // this (the owner's fix), but that must never be read as "so pass on the
  // strength of whatever priority_holder says": the pass would cross the
  // same unanswered question, pause too, and `app.py`'s `_run_command`
  // stores that second pause over the first, clobbering it and discarding
  // the question the other seat was about to answer. Checked ahead of the
  // `priority_holder` guard below for the same reason the declare_attackers
  // check is: this is not a stop, just a reason to stay idle this tick.
  if (app.state.pending_decision && app.state.pending_decision.player !== you()) return;
  if (app.state.priority_holder !== you()) return;
  const run = P.forgetStartingStep(app.run, app.state);
  if (run !== app.run) store.set({ run });
  const reason = P.stopReason(app.state, run, app.stops);
  if (reason) {
    store.set({ run: null });
    toast(`stopped: ${reason}`);
    return;
  }
  const out = await passOnce();
  if (out.paused) {
    // This very pass is what crossed a turn-based action mid-flight (CR
    // 508.1/509.1 - own or the opponent's, since this seat's priority
    // round runs on the opponent's turn too) rather than completing: the
    // committed game rolled back to before the attempt (`commands.py`), so
    // nothing here actually happened. Not a completed pass (do not
    // record it) and not a reason to end the run either - the next tick
    // sees the fresh pause through the normal checks
    // (`ownUnansweredAttackDeclaration` for this seat's own combat, or the
    // "someone else's decision" guard above for anyone else's). Ending the
    // run here was the owner's report: "pass_until is also interrupted on
    // opponent's attack step".
    return;
  }
  if (!out.ok) {
    // A refusal is two different things wearing one word, and ending the run
    // on both of them - which `if (!out.ok) store.set({run: null})` did - is
    // how a run dies silently.
    //
    // "This seat does not hold priority any more" is a LOST RACE: the board
    // moved between the read this tick acted on and the pass it sent. The
    // opponent's own move landing in that gap is ordinary, and the very next
    // tick sees the new board and does the right thing - there is nothing
    // here for a player to be told about, let alone to end a run over.
    //
    // "This move is not available" is a real disagreement, and a run that
    // cannot make its one move has nowhere to go.
    //
    // The board says which, so ask it rather than pattern-matching the
    // refusal text: engine wording is not a client contract, and this is the
    // one place a wrong guess costs the player their run. A read that itself
    // fails answers nothing, so it decides nothing either - the run stays,
    // and the next tick asks again.
    if (out.transport) return;
    const fresh = await readState();
    if (!fresh || fresh.priority_holder !== you()) return;
    store.set({ run: null });
    return;
  }
  store.set((s) => ({ run: s.run ? P.recordPass(run, s.state) : null }));
}

// ---- decisions -----------------------------------------------------------

async function decide(answer) {
  const res = await send("POST", withUid(gamePath("decide"), store.get().uid), { player: you(), ...answer });
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  store.set({ decisionDraft: {} });
  await refresh();
}

async function onPregame(path, body) {
  const app = store.get();
  const res = await send("POST", withUid(gamePath(`pregame/${path}`), app.uid), body);
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await refresh();
}

function toast(text) {
  const el = document.querySelector("#toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    el.hidden = true;
  }, 4000);
}

// ---- names, zones, the report form, the AI seat, and leaving the game -----

function buildCardLookup(state) {
  const lookup = {};
  const add = (card) => {
    if (card && card.id) lookup[card.id] = card;
  };
  (state.battlefield || []).forEach(add);
  (state.stack || []).forEach(add);
  (state.exile || []).forEach(add);
  ((state.hands && state.hands[state.you]) || []).forEach(add);
  Object.values(state.graveyards || {}).forEach((cards) => (cards || []).forEach(add));
  // CR 601.3's dynamic play permissions can name a card nowhere else on this
  // list (exiled or in a graveyard, same as above, but worth naming
  // explicitly since a permission's own card is how a player reaches it).
  ((state.play_permissions && state.play_permissions[state.you]) || []).forEach((p) => add(p.card));
  return lookup;
}

/** A player's own username reads as itself; anything else (a card, a stack
 * object) reads as its name - used by the ribbon's slot chips and combat
 * prompt so "target" reads as a name rather than an id. */
function nameOf(id) {
  const state = store.get().state;
  if (!state) return id;
  const card = buildCardLookup(state)[id];
  return card ? card.name : id;
}

async function reportProblem(text) {
  const res = await send("POST", gamePath("report"), { player: you(), text });
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await refresh();
}

/** The mtg-buddy advisor's proposal, approved: recorded as approved, then
 * performed as this seat's own play.
 *
 * THE ORDER IS THE CONTRACT, and it is record-then-act. The approval row is
 * what lets the post-match retrospective pair a play with the advice that
 * proposed it; a play performed before (or without) that row is, in the only
 * ledger anyone reads afterwards, indistinguishable from one the human
 * thought of themselves - and the advisor cannot be judged on games it
 * cannot be shown to have influenced. So a record the server would not take
 * - a transport failure or a non-2xx (`send()` returns `null` and has
 * already toasted), or a 2xx refusal - performs nothing at all.
 *
 * `advice` is what `render/advice.js` hands over: `{seq, method, path,
 * body}`, the request verbatim out of the ledger row. Nothing here rewrites
 * it except to take the advisor's own uid off the path (`withoutUid`) so
 * `send()`'s single `uidOf` can put THIS seat's on: the advisor is read-only
 * server-side, and an approved play is a play the human made.
 *
 * The residual race - the board moving between the 1.2s log read the panel
 * judged freshness on and this click - is the same window every human click
 * in this UI already has. The engine's own refusal and the existing toast
 * are the backstop, exactly as for a manual click; there is deliberately no
 * second mechanism for it here.
 *
 * Exported for `tests/test_ui_app.py`, which pins that ordering against a
 * stubbed fetch. */
export async function approveAdvice(advice) {
  if (!advice || !advice.method || !advice.path) return;
  const recorded = await send("POST", gamePath(`advice/${advice.seq}/approve`), { player: you() });
  if (recorded === null) return;
  if (recorded.allowed === false) {
    toast(recorded.reason);
    return;
  }
  const res = await send(advice.method, withoutUid(advice.path), advice.body);
  if (res !== null && res.allowed === false) toast(res.reason);
  await refresh();
}

async function passForAiSeat(aiPlayer) {
  const app = store.get();
  if (!aiPlayer || !app.aiUid) return;
  const res = await send("POST", gamePath("actions/pass-priority"), { player: aiPlayer });
  if (res !== null && res.allowed === false) toast(`pass for ${aiPlayer} refused: ${res.reason}`);
  await refresh();
}

/** "Lobby" in the topbar: back to the lobby, but the game itself is left
 * running - it stays in `mcm.games.v1` and in the lobby's own table, exactly
 * the point of that table over the old one-game `mcm.game` key it replaced.
 * Only **End game** (the lobby's own button, `render/lobby.js`'s `endGame`)
 * actually ends a game.
 *
 * T4: a real navigation back to `/` rather than an in-page `hidden` toggle -
 * a game now has its own address (`/<uuid>/`, T7), so leaving it is leaving
 * that address, the same way a browser's own Back would. Exported for
 * `tests/test_ui_app.py` only, alongside `store`/`tryTarget` above. */
export function leaveGame() {
  store.set({
    gameId: null,
    uid: null,
    aiUid: null,
    state: null,
    action: null,
    combatDraft: {},
    run: null,
    decisionDraft: {},
    decisionTargets: null,
    log: { entries: [], lastSeq: 0 },
    aiStatus: null,
    connection: "idle",
  });
  if (typeof window !== "undefined" && window.location) {
    window.location.href = "/";
    return;
  }
  document.querySelector("#game").hidden = true;
  document.querySelector("#lobby").hidden = false;
  if (lobbyApi) lobbyApi.refresh().catch((e) => console.error(e));
}

// ---- boot ----------------------------------------------------------------

/** Enter `created`'s game screen. `navigate` (default `true`) is what the
 * lobby's Start/Open buttons drive: a real navigation to the game's own
 * canonical `/<uuid>/` address (T7; T4 originally navigated to `/g/<uuid>/`,
 * now a redirect to this same place - see `app.py`) rather than an in-page
 * `hidden` toggle, so the game has its own address from the moment it is
 * entered - and so a second tab, started or opened onto a different game,
 * never disturbs the first: each tab's `document.baseURI` names its own
 * game, and `store` (this module's own in-memory state) exists once per tab
 * already, never shared across them. `navigate: false` is `main()`'s own
 * boot path - a page load that is *already* at that address (the id came
 * from `document.baseURI`, not from a click) renders straight into the game
 * screen on this same page instead of navigating again. Either way the game
 * is saved to `mcm.games.v1` first, so the reloaded/navigated page can find
 * its own `uid`/`aiUid` again. */
export async function startGame(created, { navigate = true } = {}) {
  store.set({ gameId: created.game_id, uid: created.uid, aiUid: created.aiUid, log: { entries: [], lastSeq: 0 } });
  // Appends rather than overwrites: `mcm.games.v1` is a list, one entry per
  // game this browser has started or opened, not the single `{gameId, uid,
  // aiUid}` the old `mcm.game` key held. `upsertSavedGame` also covers Open
  // on a row already in the list (the game itself is unchanged; only its
  // `uid`/`aiUid` are refreshed to whatever this call was actually handed).
  const storage = currentStorage();
  if (storage) {
    saveGames(
      storage,
      upsertSavedGame(loadSavedGames(storage), {
        gameId: created.game_id,
        uid: created.uid,
        aiUid: created.aiUid,
      })
    );
  }
  if (navigate) {
    if (typeof window !== "undefined" && window.location) {
      window.location.href = `/${encodeURIComponent(created.game_id)}/`;
    }
    return;
  }
  document.querySelector("#lobby").hidden = true;
  document.querySelector("#game").hidden = false;
  await poll();
  await pollSeats();
}

/** What `main()`'s boot should show, given the game id embedded in the
 * page's own URL (`gameIdFromBaseURI`'s result - `null` for the plain lobby
 * root) and the list of games this browser has saved (`mcm.games.v1`). A
 * URL naming a game always wins: `{kind: "game", gameId, uid, aiUid}`,
 * `uid`/`aiUid` from this browser's saved entry for it or `null` for a
 * spectator with no saved seat - a reload of a game tab must stay in that
 * game, and the URL is what says so, not stored state.
 *
 * The bare lobby root always answers `{kind: "lobby"}`, regardless of how
 * many games are saved. An earlier version special-cased exactly one saved
 * game and auto-navigated into it instead - the owner's reported bug: that
 * branch ran on *every* visit to `/`, not once, so as long as one game was
 * saved (the ordinary case - nothing drops an entry from `mcm.games.v1`
 * short of the lobby's own End button) the lobby could never be reached at
 * all. The only navigation into a game now happens the moment it is
 * created or reopened from the lobby (`ui/render/lobby.js`'s Start/Open,
 * both routed through `startGame`'s default `navigate: true`) - that is the
 * "once". Exported for `tests/test_ui_app.py` only, the same reason
 * `planHandCardClick`/`planPermanentClick` above are: a pure decision,
 * exercised without DOM or network rather than reimplemented in Python. */
export function bootTarget(urlGameId, saved) {
  if (urlGameId) {
    const entry = (saved || []).find((g) => g.gameId === urlGameId);
    return {
      kind: "game",
      gameId: urlGameId,
      uid: entry ? entry.uid : null,
      aiUid: entry ? entry.aiUid : null,
    };
  }
  return { kind: "lobby" };
}

/** `{uid, aiUid}` for `gameId`, recovered from the server rather than from
 * `mcm.games.v1` - `{uid: null, aiUid: null}` when the server will not
 * answer for this game.
 *
 * This is what makes a bookmarked `/<uuid>/` URL open on a *second computer*.
 * `bootTarget` answers `uid: null` for a game this browser has never saved,
 * which is honest - `localStorage` really is empty - but it used to be the
 * end of the story: the page rendered the board as a spectator with no seat
 * to act from. The seat is recoverable, just not locally: `GET
 * /api/lobby/games` hands the authenticated **owner** the full `seats` map
 * for her own games (`decks_api.list_lobby_games`), so one request resolves
 * it. `seatsFromRow` then picks the human's seat out of that map the same
 * way the lobby's own table does - one rule for both, rather than two
 * copies that can drift.
 *
 * Best-effort by construction, never a gate: a failure, a 401, an empty
 * listing, or a game that is simply not this caller's all resolve to "no
 * seat", leaving the page rendering exactly as it does today. A deployment
 * with no OIDC configured therefore behaves identically to before - nothing
 * is ever listed for an anonymous caller that it did not already hold a uid
 * for, so this lookup finds nothing and changes nothing. */
export async function seatFromServer(gameId) {
  try {
    const { games } = await api("GET", `${lobbyBase()}/games`);
    const row = (games || []).find((g) => g.game_id === gameId);
    if (!row) return { uid: null, aiUid: null };
    const { myUid, aiUid } = seatsFromRow(row, null);
    return { uid: myUid || null, aiUid: aiUid || null };
  } catch (e) {
    console.error(`could not resolve a seat for game ${gameId}: ${e.message}`);
    return { uid: null, aiUid: null };
  }
}

async function main() {
  // Before anything mounts: no re-render may replace a button between the
  // player's mousedown and the click it is about to become (`ui/store.js`).
  installPressGuard(store);
  await loadConfig();
  store.set({ symbols: await loadSymbols(api, apiBase()) });
  // The post-game review, mounted before the lobby because the lobby's
  // "Show thinking" button (only ever on a finished game - see
  // `render/lobby.js`'s `reviewable`) opens it. Coming back from it refreshes
  // the table, the same way leaving a game does.
  const reviewApi = mountReview(document, {
    onBack: () => {
      if (lobbyApi) lobbyApi.refresh().catch((e) => console.error(e));
    },
  });
  lobbyApi = mountLobby(document, store, {
    startGame,
    showReview: (target) => reviewApi.open(target).catch((e) => console.error(e)),
  });
  mountBoard(document, store, {
    onHandCard,
    onPermanent,
    onPlayer: (u) => tryTarget(u),
    onStackObject: (o) => tryTarget(o.id),
    onPermission,
  });
  // One draft for both decision surfaces: the prompt box's small controls and
  // the decision window's cards write the same `decisionDraft` keys.
  const onDecisionDraft = (patch) => {
    store.set((s) => ({ decisionDraft: { ...s.decisionDraft, ...patch } }));
    refreshDecisionTargets();
  };
  mountRibbon(document, store, {
    nameOf,
    onShowDecisionWindow: () => restoreDecisionWindow(document),
    onToggleMode: (n) => {
      store.set((s) => ({ action: A.toggleMode(s.action, n) }));
      refreshAffordance();
    },
    onSetX: (n) => {
      store.set((s) => ({ action: A.setX(s.action, n) }));
      refreshAffordance();
    },
    onChooseColor: (c) => {
      store.set((s) => ({ action: A.chooseColor(s.action, c) }));
      previewAndMaybeFire();
    },
    onChooseAltCost: (i) => {
      store.set((s) => ({ action: A.chooseAltCost(s.action, i) }));
      previewAndMaybeFire();
    },
    onTogglePitch: (id) => {
      store.set((s) => ({ action: A.togglePitch(s.action, id) }));
      previewAndMaybeFire();
    },
    onToggleConvoke: (id) => {
      store.set((s) => ({ action: A.toggleConvoke(s.action, id) }));
      previewAndMaybeFire();
    },
    onToggleDelve: (id) => {
      store.set((s) => ({ action: A.toggleDelve(s.action, id) }));
      previewAndMaybeFire();
    },
    onUnfill: (slotIndex, id) => {
      store.set((s) => ({
        action: {
          ...s.action,
          slots: s.action.slots.map((sl, i) => (i === slotIndex ? { ...sl, targets: sl.targets.filter((t) => t !== id) } : sl)),
        },
      }));
      previewAndMaybeFire();
    },
    onFocusSlot,
    onSetShare: (id, n) => store.set((s) => ({ action: A.setShare(s.action, id, n) })),
    onDone: () => {
      store.set((s) => ({ action: A.markDone(s.action) }));
      fire();
    },
    onCast: () => fire(),
    onCancel: cancel,
    onHoldPriority: (hold) => store.set((s) => ({ action: { ...s.action, holdPriority: hold } })),
    onDecisionDraft,
    onDecide: decide,
    onVerb: (verb) => {
      store.set({ concedeArmed: false });
      onVerb(verb);
    },
    onStopRun: () => store.set({ run: null }),
    onToggleStop: (side, step) =>
      store.set((s) => {
        const list = s.stops[side].includes(step) ? s.stops[side].filter((x) => x !== step) : [...s.stops[side], step];
        const stops = { ...s.stops, [side]: list };
        P.saveStops(localStorage, stops);
        return { stops };
      }),
    // The stack tick is not a step and lives beside the two step lists, so it
    // gets its own toggle rather than a fake `side`. Same persistence.
    onToggleStackStop: () =>
      store.set((s) => {
        const stops = { ...s.stops, onStack: s.stops.onStack === false };
        P.saveStops(localStorage, stops);
        return { stops };
      }),
    // Same shape, beside it: not a step either, and `fire()` reads it fresh
    // on every action rather than only at the moment the action began.
    onToggleKeepPriorityByDefault: () =>
      store.set((s) => {
        const stops = { ...s.stops, keepPriorityByDefault: s.stops.keepPriorityByDefault === false };
        P.saveStops(localStorage, stops);
        return { stops };
      }),
    onAttackConfirm: confirmAttack,
    onAttackNone: declareNoAttackers,
    onBlockConfirm: confirmBlocks,
    onBlockNone: declareNoBlockers,
    // Two clicks, never one. Conceding is a legal move at any time
    // (CR 104.3a) and it is also the only irreversible one in the ribbon:
    // the first click only arms the button, the second sends it. Any pass
    // verb disarms it again.
    onConcede: async () => {
      if (P.concedeIntent(store.get().concedeArmed) === "arm") {
        store.set({ concedeArmed: true });
        return;
      }
      store.set({ concedeArmed: false });
      const res = await send("POST", gamePath("actions/concede"), { player: you() });
      if (res === null) return;
      await refresh();
    },
    onPregame,
    onAdvance: advanceStep,
  });
  // The complex decisions (scry, surveil, every `choose_cards`,
  // `order_triggers`, this seat's own mulligan), answered with real cards in
  // a floating window that posts through the same `decide` and `onPregame`.
  mountDecisionWindow(document, store, {
    onDecisionDraft,
    onDecide: decide,
    onPregame,
  });
  // The small decisions whose source card is on screen, answered in a popover
  // beside that card through the same controls and the same `decide`.
  // Mounted after the board and the ribbon, so it anchors to the cards this
  // update has already drawn.
  mountDecisionPopover(document, store, {
    nameOf,
    onDecisionDraft,
    onDecide: decide,
  });
  mountLog(document, store);
  mountAdvice(document, store, { onApprove: approveAdvice });
  mountTopbar(document, store, {
    onLeaveGame: leaveGame,
    onPassForAiSeat: passForAiSeat,
    onReportProblem: reportProblem,
  });
  mountCardPreview(document, store);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const modal = document.querySelector("#modal");
    if (modal && !modal.hidden) {
      closeModal(document);
      return;
    }
    // A pending decision's window minimizes; it is never closed while the
    // question is still standing.
    if (decisionWindowIsOpen(document)) {
      minimizeDecisionWindow(document);
      return;
    }
    cancel();
    hidePopover();
  });
  let saved = [];
  try {
    saved = loadSavedGames(currentStorage());
  } catch {
    // a corrupted or unreadable localStorage entry is no worse than none
    saved = [];
  }
  // T7 (originally T4, `/g/<uuid>/`): bare `/<uuid>/` addressing. A page
  // loaded at that path - a bookmark, a shared link, a reload - names its
  // own game in `document.baseURI`, not in `store`, so this always wins over
  // anything saved. `saved` only supplies this browser's own seat when it
  // has one; with no matching entry the game still renders, just with no
  // seat to act as (a spectator). This is also what keeps two tabs on two
  // different games from interfering: each tab's own `document.baseURI`
  // resolves its own `urlGameId` independently, and `store`/`main()` are
  // per-tab already (a fresh JS module instance per tab, never shared) - the
  // only cross-tab state is `mcm.games.v1` below, and that is keyed by
  // `gameId`, so each tab only ever reads its own game's saved entry out of
  // it.
  //
  // The bare lobby root always shows the lobby now, no matter how many games
  // are saved - `bootTarget`'s own docstring has the fixed bug this replaced
  // (an auto-navigate into the sole saved game that fired on every visit to
  // `/`, not once, making the lobby unreachable while exactly one game
  // existed).
  const urlGameId = gameIdFromBaseURI(document.baseURI);
  const target = bootTarget(urlGameId, saved);
  if (target.kind === "game") {
    let { uid, aiUid } = target;
    // Nothing saved for this game - a bookmark, a shared link, or the same
    // person on a second computer. Ask the server whether it knows a seat
    // for the logged-in user before settling for spectating; `startGame`
    // then writes whatever came back into `mcm.games.v1`, so this costs one
    // request on the first visit from a given browser and none after.
    if (!uid) ({ uid, aiUid } = await seatFromServer(target.gameId));
    await startGame({ game_id: target.gameId, uid, aiUid }, { navigate: false });
  }
  startPolling();
}

// A no-op under Node (`tests/test_ui_app.py`'s fake-DOM smoke test), a real
// boot in the browser.
if (typeof window !== "undefined" && typeof document !== "undefined") main();
