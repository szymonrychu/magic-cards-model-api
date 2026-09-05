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
import { mountTopbar, openZonesModal, mountHoverPreview, closeModal } from "./render/panels.js";

const POLL_MS = 1200;
const LS_KEY = "mcm.game";

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
  const base = `/api/games/${store.get().gameId}`;
  return rest ? `${base}/${rest}` : base;
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
  if (
    state.step === "declare_attackers" &&
    state.active_player === seat &&
    state.priority_holder === seat &&
    card.controller === seat
  ) {
    return { kind: "attack" };
  }
  if (state.step === "declare_blockers" && state.active_player !== seat && state.priority_holder === seat) {
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

async function poll() {
  const app = store.get();
  if (!app.gameId || app.polling) return;
  store.set({ polling: true });
  try {
    const state = await api("GET", withUid(gamePath(""), app.uid));
    const log = await api("GET", withUid(gamePath(`log?since=${app.log.lastSeq}`), app.uid));
    store.set((s) => ({
      state,
      connection: "ok",
      log: { entries: [...s.log.entries, ...log.log].slice(-500), lastSeq: log.last_seq },
    }));
    refreshDecisionTargets();
    if (app.aiUid) {
      try {
        store.set({
          aiStatus: await api("GET", `/api/ui/games/${app.gameId}/ai-seat/status?uid=${app.aiUid}`),
        });
      } catch {
        /* transient - the next poll tries again */
      }
    }
    await tickRun();
  } catch (err) {
    store.set({ connection: `error: ${err.message}` });
  } finally {
    store.set({ polling: false });
  }
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

async function fire() {
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
  if (onStack && !action.holdPriority) await passOnce();
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

async function pinLand(card, event) {
  const app = store.get();
  const colors = colorOptions(card, 0);
  const pinnedAlready = app.action.pinned.some((p) => p.card_id === card.id);
  if (pinnedAlready) {
    store.set({ action: A.unpinSource(app.action, card.id) });
    return previewAndMaybeFire();
  }
  if (colors.length <= 1) {
    store.set({ action: A.pinSource(app.action, card.id, colors[0] || "C") });
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
  // A second click on an already-chosen target is always an un-choose, in
  // whichever slot it actually sits in - not necessarily the highlighted
  // one, and not gated on that slot having room (an "up to two" slot
  // already filled to its cap, or any slot once the action is only waiting
  // on an explicit Cast/Done, still has to let the player take a target back
  // out). `actions.js`'s `chooseTarget` already has exactly this logic
  // (unfill from the last slot the id sits in, unless the active slot can
  // still take it too), so it is called for this case rather than
  // reimplemented here.
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
  const res = await send("POST", gamePath("actions/attack"), body);
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
  const res = await send("POST", gamePath("actions/declare-blockers"), C.blockBody(store.get().combatDraft, you()));
  if (res === null) return;
  if (res.allowed === false) return toast(res.reason);
  store.set({ combatDraft: {} });
  await poll();
}

// ---- priority ------------------------------------------------------------

async function passOnce() {
  const res = await send("POST", gamePath("actions/pass-priority"), { player: you() });
  if (res === null) return { ok: false, reason: null, outcome: null, settledThrough: null };
  const out = P.interpretPassOutcome(res);
  if (!out.ok) toast(out.reason);
  return out;
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

async function onVerb(verb) {
  const app = store.get();
  if (verb === "pass") {
    await passOnce();
    return poll();
  }
  store.set({ run: P.startRun(verb, app.state) });
  await tickRun();
}

async function castable() {
  const app = store.get();
  const hand = app.state.hands[you()] || [];
  for (const card of hand) {
    if (/land/i.test(card.type_line || "")) continue;
    const res = await send("POST", gamePath("actions/cast"), {
      player: you(),
      card_id: card.id,
      mana_sources: untappedSources(app.state),
      dry_run: true,
    });
    if (res && res.allowed) return true;
  }
  return false;
}

async function tickRun() {
  const app = store.get();
  if (!app.run || !app.state) return;
  if (app.state.priority_holder !== you()) return;
  const run = P.forgetStartingStep(app.run, app.state);
  if (run !== app.run) store.set({ run });
  const reason = P.stopReason(app.state, run, app.stops, run.verb === "until_castable" ? await castable() : false);
  if (reason) {
    store.set({ run: null });
    toast(`stopped: ${reason}`);
    return;
  }
  const out = await passOnce();
  store.set((s) => ({ run: s.run ? P.recordPass(run, s.state) : null }));
  if (!out.ok) store.set({ run: null });
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

function leaveGame() {
  localStorage.removeItem(LS_KEY);
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
  document.querySelector("#game").hidden = true;
  document.querySelector("#lobby").hidden = false;
}

// ---- boot ----------------------------------------------------------------

export async function startGame(created) {
  store.set({ gameId: created.game_id, uid: created.uid, aiUid: created.aiUid, log: { entries: [], lastSeq: 0 } });
  localStorage.setItem(LS_KEY, JSON.stringify({ gameId: created.game_id, uid: created.uid, aiUid: created.aiUid }));
  document.querySelector("#lobby").hidden = true;
  document.querySelector("#game").hidden = false;
  await poll();
}

async function main() {
  store.set({ symbols: await loadSymbols(api) });
  mountLobby(document, store, { startGame });
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
    onAttackConfirm: confirmAttack,
    onAttackNone: () => passOnce().then(poll),
    onBlockConfirm: confirmBlocks,
    onBlockNone: () => passOnce().then(poll),
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
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(LS_KEY) || "null");
  } catch {
    // a corrupted or unreadable localStorage entry is no worse than none
    saved = null;
  }
  if (saved && saved.gameId) await startGame({ game_id: saved.gameId, uid: saved.uid, aiUid: saved.aiUid });
  setInterval(poll, POLL_MS);
}

// A no-op under Node (`tests/test_ui_app.py`'s fake-DOM smoke test), a real
// boot in the browser.
if (typeof window !== "undefined" && typeof document !== "undefined") main();
