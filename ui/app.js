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
import { createStore } from "./store.js";
import { loadSymbols } from "./symbols.js";
import * as A from "./actions.js";
import * as P from "./priority.js";
import * as C from "./combat.js";
import { untappedSources, colorOptions } from "./mana.js";
import { viewModel } from "./decisions.js";
import { fill, canFill } from "./targeting.js";
import { mountBoard } from "./render/board.js";
import { mountRibbon, showPopover, hidePopover } from "./render/ribbon.js";
import { mountLog } from "./render/log.js";
import { mountLobby } from "./render/lobby.js";
import { mountReview } from "./render/review.js";
import { mountTopbar, openZonesModal, mountHoverPreview, closeModal } from "./render/panels.js";
import { loadSavedGames, saveGames, upsertSavedGame } from "./lobby_model.js";
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
  concedeArmed: false,
});

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
  // Bumped BEFORE the request, not after it: a board read that was answered
  // while this request was still in flight is just as much a picture of the
  // world before it as one answered before it was sent, and `poll()` has to
  // discard both. Every mutating request in this file goes through here, so
  // this is the only place the counter moves.
  store.set((s) => ({ mutations: s.mutations + 1 }));
  try {
    return await api(method, path, body);
  } catch (err) {
    store.set({ connection: `error: ${err.message}` });
    toast(err.message);
    return null;
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
  if (!abilities.length && !vehicle) {
    return { kind: "none", reason: `${card.name} has no activated abilities` };
  }
  if (abilities.length === 1 && !vehicle) return { kind: "activate", abilityIndex: 0 };
  return { kind: "menu", abilities, vehicle };
}

// -------------------------------------------------------------- polling --

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
    const state = await api("GET", withUid(gamePath(""), app.uid));
    const log = await api("GET", withUid(gamePath(`log?since=${app.log.lastSeq}`), app.uid));
    if (store.get().mutations !== mutationsBefore) {
      // One of this seat's own actions was issued while these two reads were
      // outstanding, so they describe a board that no longer exists. The
      // connection is fine, though - both reads landed - and saying so is
      // what keeps a discarded cycle from reading as a dropped one.
      store.set({ connection: "ok" });
      return;
    }
    store.set((s) => ({
      state,
      connection: "ok",
      log: { entries: [...s.log.entries, ...log.log].slice(-500), lastSeq: log.last_seq },
    }));
    refreshDecisionTargets();
    await tickRun();
  } catch (err) {
    store.set({ connection: `error: ${err.message}` });
  } finally {
    store.set({ polling: false });
  }
}

/** S7 (2b's T8): the play UI's own half of the fix. One `GET
 * ${lobbyBase()}/games/{id}/seats` answers every seat's agent settings and
 * liveness together - `app.aiStatus` reads out the one entry matching
 * `app.aiUid`, the same shape `GET .../ai-seat/status` used to hand back
 * directly. A missing or unreachable game (a stale poll after leaving it)
 * or no AI seat at all is a silent no-op - the next tick tries again. */
async function pollSeats() {
  const app = store.get();
  if (!app.gameId || !app.aiUid) return;
  try {
    const { seats } = await api("GET", `${lobbyBase()}/games/${app.gameId}/seats`);
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
// comment above): a real, awaited drive of the auto-pass-after-casting
// default against a stubbed `fetch`, rather than a Python restatement of it.
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
  // CR 117.3c already gives you priority back after casting or activating;
  // the harness spends it for you with one auto-pass by default (the
  // MCP/agent side's own `hold_priority` default, `mcp.py`'s module
  // docstring). `action.holdPriority` is this one cast's own override, reset
  // every time a new cast begins; `stops.keepPriorityAfterCasting` is the
  // standing one from the stops panel, checked every time in its place
  // rather than only at the moment casting began - either is enough to keep
  // priority.
  if (onStack && !action.holdPriority && !store.get().stops.keepPriorityAfterCasting) {
    await passOnce();
  }
  await poll();
}

async function onHandCard(card) {
  const app = store.get();
  const plan = planHandCardClick(card, app);
  if (plan.kind === "cancel") return cancel();
  if (plan.kind === "target") return tryTarget(plan.id);
  if (plan.kind === "land") return playLand(card);
  if (plan.kind === "cast") return beginCast(card);
  return showPopover(document.querySelector(`[data-key="${card.id}"]`), [
    {
      label: plan.isLand ? "Play" : "Cast",
      onSelect: () => (plan.isLand ? playLand(card) : beginCast(card)),
    },
    ...plan.specials.map((k) => ({
      label: k[0].toUpperCase() + k.slice(1),
      sublabel: card.special_actions[k].cost,
      onSelect: () => fireSpecial(card, k),
    })),
    ...plan.cycling.map((c) => ({ label: "Cycle", sublabel: c.cost, onSelect: () => cycle(card) })),
  ]);
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

/** CR 601.3's dynamic play permissions (cascade, suspend's own third
 * ability): begin casting `card` from `fromZone` (exile, a graveyard) rather
 * than the hand - `actions.js`'s `bodyFor` sends `from_zone` only when this
 * is set, so an ordinary hand cast is unaffected. */
function beginCastFromPermission(card, fromZone) {
  hidePopover();
  store.set({ action: A.begin("cast", card, { fromZone }), preview: null });
  if (!A.needsChoices(card, null)) return previewAndMaybeFire();
  return refreshAffordance();
}

function onPermission(permission) {
  beginCastFromPermission(permission.card, permission.from_zone);
}

async function playLand(card) {
  const res = await send("POST", gamePath("actions/play-land"), { player: you(), card_id: card.id });
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await poll();
}

async function fireSpecial(card, keyword) {
  hidePopover();
  const res = await send("POST", gamePath(`actions/${keyword}`), { player: you(), card_id: card.id });
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await poll();
}

async function cycle(card) {
  hidePopover();
  const res = await send("POST", gamePath("actions/cycle"), { player: you(), card_id: card.id });
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await poll();
}

async function onPermanent(card, event) {
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
  if (plan.kind === "activate") return beginActivate(card, plan.abilityIndex);
  const items = plan.abilities.map((a) => ({
    label: a.text,
    sublabel: a.cost || "",
    onSelect: () => beginActivate(card, a.index),
  }));
  if (plan.vehicle) items.push({ label: "Crew", onSelect: () => beginCrew(card) });
  showPopover(event.currentTarget, items);
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

/** The index of `card`'s first mana ability - what `pinLand` asks
 * `colorOptions` about, rather than hardcoding index 0. A basic land parses
 * to exactly one ability at index 0, but a permanent with a printed ability
 * ahead of its intrinsic mana ability (or several mana abilities) does not,
 * and `planPermanentClick`'s own `{kind: "pin"}` guard already established
 * `card` has at least one `is_mana_ability` entry before this is ever
 * called. */
function manaAbilityIndex(card) {
  const index = (card.abilities || []).findIndex((a) => a.is_mana_ability);
  return index < 0 ? 0 : index;
}

async function pinLand(card, event) {
  const app = store.get();
  const abilityIndex = manaAbilityIndex(card);
  const colors = colorOptions(card, abilityIndex);
  const pinnedAlready = app.action.pinned.some((p) => p.card_id === card.id);
  if (pinnedAlready) {
    store.set({ action: A.unpinSource(app.action, card.id) });
    return previewAndMaybeFire();
  }
  if (colors.length <= 1) {
    // No single colour to pin (`colorOptions` came back empty - a one-option
    // ability reports `mana_options: null`, `affordances.py`'s own
    // `ability_hints`) still pins *which* source, with no colour named, so
    // `mana.js`'s `manaSources` sends it as a bare id and the engine's own
    // `auto_tap` picks the colour, rather than forcing a colourless `"C"`
    // choice onto a land that never produces colourless mana at all.
    store.set({ action: A.pinSource(app.action, card.id, colors[0] || null) });
    return previewAndMaybeFire();
  }
  showPopover(
    event.currentTarget,
    colors.map((c) => ({
      label: `Tap for {${c}}`,
      onSelect: () => {
        hidePopover();
        store.set((s) => ({ action: A.pinSource(s.action, card.id, c) }));
        previewAndMaybeFire();
      },
    }))
  );
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
  showPopover(
    event.currentTarget,
    defenders.map((d) => ({
      label: `Attack ${d.label}`,
      onSelect: () => {
        hidePopover();
        store.set((s) => ({ combatDraft: C.toggleAttacker(s.combatDraft, card.id, d.id) }));
      },
    }))
  );
}

async function confirmAttack() {
  const body = C.attackBody(store.get().combatDraft);
  const res = await send("POST", withUid(gamePath("actions/attack"), store.get().uid), body);
  if (res === null) return;
  if (res.allowed === false) return toast(res.reason);
  store.set({ combatDraft: {} });
  await poll();
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
  await poll();
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
  await poll();
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
  await poll();
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
  await poll();
}

/** `pass` is one priority, given up, and the board comes back (CR 117.3d).
 * `pass_until` is the same verb on a run: `tickRun` keeps making it until
 * `priority.js`'s `stopReason` says to stop. */
async function onVerb(verb) {
  const app = store.get();
  if (verb === "pass") {
    await passOnce();
    return poll();
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
  // itself calls `poll()`, which is what picks the run back up (its own
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
  await poll();
}

async function onPregame(path, body) {
  const app = store.get();
  const res = await send("POST", withUid(gamePath(`pregame/${path}`), app.uid), body);
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await poll();
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

function onOpenZones() {
  const app = store.get();
  if (!app.state) return;
  openZonesModal(document, app, { onPermanent, onPermission });
}

async function reportProblem(text) {
  const res = await send("POST", gamePath("report"), { player: you(), text });
  if (res === null) return;
  if (res.allowed === false) toast(res.reason);
  await poll();
}

async function passForAiSeat(aiPlayer) {
  const app = store.get();
  if (!aiPlayer || !app.aiUid) return;
  const res = await send("POST", gamePath("actions/pass-priority"), { player: aiPlayer });
  if (res !== null && res.allowed === false) toast(`pass for ${aiPlayer} refused: ${res.reason}`);
  await poll();
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

async function main() {
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
  mountRibbon(document, store, {
    nameOf,
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
    onDecisionDraft: (patch) => {
      store.set((s) => ({ decisionDraft: { ...s.decisionDraft, ...patch } }));
      refreshDecisionTargets();
    },
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
    // on every cast rather than only at the moment casting began.
    onToggleKeepPriorityAfterCasting: () =>
      store.set((s) => {
        const stops = { ...s.stops, keepPriorityAfterCasting: !s.stops.keepPriorityAfterCasting };
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
      await poll();
    },
    onPregame,
    onAdvance: advanceStep,
  });
  mountLog(document, store);
  mountTopbar(document, store, {
    onOpenZones,
    onLeaveGame: leaveGame,
    onPassForAiSeat: passForAiSeat,
    onReportProblem: reportProblem,
  });
  mountHoverPreview(document, store);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const modal = document.querySelector("#modal");
    if (modal && !modal.hidden) {
      closeModal(document);
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
    await startGame(
      { game_id: target.gameId, uid: target.uid, aiUid: target.aiUid },
      { navigate: false }
    );
  }
  startPolling();
}

// A no-op under Node (`tests/test_ui_app.py`'s fake-DOM smoke test), a real
// boot in the browser.
if (typeof window !== "undefined" && typeof document !== "undefined") main();
