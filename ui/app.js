"use strict";
// Node has no `window` - this file is still `require()`-able there for
// `tests/test_ui_autopilot.py`, which loads the real `autopilotStopReason`/
// `interpretPassOutcome` functions (see the `module.exports` guard near the
// bottom) rather than a Python reimplementation of their logic. A no-op in
// the browser, where `window` already exists.
if (typeof window === "undefined") {
  var window = globalThis; // eslint-disable-line no-var
}

/* Server-rendered HTML + plain JS UI over the existing seat API
 * (app.py/schemas.py/decisions.py) - no build step, no framework.
 *
 * This file never invents game rules: every button below calls one of the
 * endpoints the API already documents in its README, and every refusal shown
 * to the player is the engine's own `reason` text, verbatim. It also never
 * invents what it cannot see: the opponent's hand is rendered as card backs,
 * counted but not named, because `visibility.py` already strips their
 * contents server-side before this file ever gets a response.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const LS_KEY = "mcm_last_game";

let game = {
  gameId: null,
  uid: null,
  you: null,
  pollHandle: null,
  polling: false, // re-entrancy guard: skip a tick if the previous one is still in flight
  state: null,
  cardLookup: {}, // card/ability id -> its state-view object, for hover preview
  selectedHandCard: null, // card id being cast/played
  selectedTargets: [], // token strings (card id or username)
  holdOnCast: false, // "keep priority" checkbox - the opt-out of feature 2b
  // Priority automation (feature 2). `autopilot` is null when nothing is
  // running; a run is driven from inside the ordinary poll() tick, never an
  // unbounded loop of its own, so a `Stop` click (or reaching a stop
  // condition) takes effect on the very next tick, at most ~1.2s away.
  autopilot: null,
  autopilotStoppedReason: null,
  settings: {
    // The safe default (task requirement): stop and ask whenever anything
    // the player could respond to goes on the stack, rather than blow
    // through it. A player who wants to auto-pass through the opponent's
    // whole turn can turn this off explicitly.
    stopOnStack: true,
  },
};

// ---------------------------------------------------------------- fetch ----

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let json;
  try {
    json = await res.json();
  } catch {
    json = { detail: `HTTP ${res.status}: could not parse response` };
  }
  if (!res.ok && json && json.detail !== undefined) {
    const message = Array.isArray(json.detail)
      ? json.detail.map((d) => `${(d.loc || []).join(".")}: ${d.msg}`).join("; ")
      : json.detail;
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return json;
}

function withUid(path) {
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}uid=${encodeURIComponent(game.uid)}`;
}

// ---------------------------------------------------------------- lobby ----

async function loadBundledDecks() {
  const { decks } = await api("GET", "/api/ui/decks");
  for (const selId of ["#you-bundled", "#ai-bundled"]) {
    const sel = $(selId);
    for (const name of decks) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    }
  }
}

function wireDeckPicker(prefix) {
  $(`#${prefix}-bundled`).addEventListener("change", async (e) => {
    if (!e.target.value) return;
    const { text } = await api("GET", `/api/ui/decks/${encodeURIComponent(e.target.value)}`);
    $(`#${prefix}-text`).value = text;
  });
  $(`#${prefix}-upload`).addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      $(`#${prefix}-text`).value = reader.result;
    };
    reader.readAsText(file);
  });
  $(`#${prefix}-validate`).addEventListener("click", async () => {
    await validateAndReport(prefix);
  });
}

async function validateAndReport(prefix) {
  const text = $(`#${prefix}-text`).value;
  const out = $(`#${prefix}-validation`);
  out.innerHTML = "checking...";
  if (!text.trim()) {
    out.innerHTML = '<span class="card-error">paste or pick a decklist first</span>';
    return null;
  }
  try {
    const result = await api("POST", "/api/ui/decks/validate", { text });
    renderValidation(out, result);
    return result;
  } catch (err) {
    out.innerHTML = `<span class="card-error">${escapeHtml(err.message)}</span>`;
    return null;
  }
}

function renderValidation(out, result) {
  const lines = [];
  if (result.valid) {
    lines.push(
      `<div class="ok">legal: ${result.mainboard_size} mainboard, ${result.sideboard_size} sideboard</div>`
    );
  } else {
    for (const e of result.errors) {
      lines.push(`<div class="card-error">${escapeHtml(e.card ? `${e.card}: ` : "")}${escapeHtml(e.message)}</div>`);
    }
  }
  for (const w of result.warnings || []) {
    lines.push(`<div class="card-warning">${escapeHtml(w.message)}</div>`);
  }
  out.innerHTML = lines.join("");
}

async function startGame() {
  const err = $("#start-error");
  err.textContent = "";
  const youText = $("#you-text").value;
  const aiText = $("#ai-text").value;
  const youName = $("#you-name").value.trim() || "you";
  const aiName = $("#ai-name").value.trim() || "claude";
  if (youName === aiName) {
    err.textContent = "the two players need different names";
    return;
  }
  const youResult = await validateAndReport("you");
  const aiResult = await validateAndReport("ai");
  if (!youResult || !youResult.valid || !aiResult || !aiResult.valid) {
    err.textContent = "fix the decklist errors above before starting";
    return;
  }
  const seedRaw = $("#seed").value;
  const body = {
    players: [
      { username: youName, decklist_text: youText, ai: false },
      { username: aiName, decklist_text: aiText, ai: true },
    ],
  };
  if (seedRaw) body.seed = parseInt(seedRaw, 10);
  let created;
  try {
    created = await api("POST", "/api/ui/games", body);
  } catch (e) {
    err.textContent = e.message;
    return;
  }
  const youUid = created.seat_uids[youName];
  const aiUid = created.seat_uids[aiName];
  enterGame(created.game_id, youUid);
  // Start the AI seat right away - fire and forget; its own status poll
  // (pollAiStatus) reports whatever came back, including a fallback.
  api("POST", `/api/ui/games/${created.game_id}/ai-seat/start`, { uid: aiUid }).catch(() => {});
  game.aiUid = aiUid;
  localStorage.setItem(LS_KEY, JSON.stringify({ gameId: created.game_id, uid: youUid, aiUid }));
}

// ----------------------------------------------------------------- game ----

function enterGame(gameId, uid) {
  game.gameId = gameId;
  game.uid = uid;
  $("#lobby").hidden = true;
  $("#game").hidden = false;
  wireCardPreview();
  poll();
  game.pollHandle = setInterval(poll, 1200);
}

function leaveGame() {
  if (game.pollHandle) clearInterval(game.pollHandle);
  game.autopilot = null;
  localStorage.removeItem(LS_KEY);
  game.gameId = null;
  game.uid = null;
  $("#game").hidden = true;
  $("#lobby").hidden = false;
}

async function poll() {
  if (!game.gameId || game.polling) return;
  game.polling = true;
  try {
    let state;
    try {
      state = await api("GET", withUid(`/api/games/${game.gameId}`));
    } catch (e) {
      $("#conn-status").textContent = `disconnected: ${e.message}`;
      $("#conn-status").className = "pill bad";
      return;
    }
    $("#conn-status").textContent = "connected";
    $("#conn-status").className = "pill ok";
    game.state = state;
    render(state);
    if (game.aiUid) pollAiStatus();
    await tickAutopilot(state);
  } finally {
    game.polling = false;
  }
}

async function pollAiStatus() {
  try {
    const status = await api("GET", `/api/ui/games/${game.gameId}/ai-seat/status?uid=${game.aiUid}`);
    renderAiPanel(status);
  } catch {
    // transient - next poll tries again
  }
}

// -------------------------------------------------------------- render ----

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function manaLine(pool) {
  const floating = pool.floating || {};
  const parts = Object.entries(floating).filter(([, n]) => n > 0).map(([c, n]) => `${n}${c}`);
  return parts.length ? parts.join(" ") : "empty";
}

function render(state) {
  const you = state.you;
  const opponent = state.players.find((p) => p.username !== you) || state.players[0];
  const youPlayer = state.players.find((p) => p.username === you) || state.players[0];

  game.cardLookup = buildCardLookup(state);

  const winnerLine = state.winner
    ? ` — WINNER: ${escapeHtml(state.winner)}`
    : state.drawn
    ? " — game drawn"
    : "";
  $("#turn-info").innerHTML =
    `turn ${state.turn_number} · ${escapeHtml(state.step)} (${escapeHtml(state.phase)}) · ` +
    `active: ${escapeHtml(state.active_player)} · ` +
    `<span class="prio">priority: ${escapeHtml(state.priority_holder || "-")}</span>${winnerLine}`;

  $("#opp-info").innerHTML = infoRowHtml(opponent, false);
  $("#you-info").innerHTML = infoRowHtml(youPlayer, true);
  $("#opp-info").onclick = () => toggleTarget(opponent.username);
  $("#you-info").onclick = () => toggleTarget(youPlayer.username);

  renderOpponentHand(state, opponent);
  renderStack(state);
  renderBattlefield(state, you);
  renderHand(state, you);
  renderDecision(state, you);
  renderAutopilotBanner();
  renderActionBar(state, you);

  $("#raw-state").textContent = JSON.stringify(state, null, 1);
}

function buildCardLookup(state) {
  const lookup = {};
  const add = (card) => {
    if (card && card.id) lookup[card.id] = card;
  };
  (state.battlefield || []).forEach(add);
  (state.stack || []).forEach(add);
  ((state.hands && state.hands[state.you]) || []).forEach(add);
  return lookup;
}

function infoRowHtml(player, isYou) {
  const lifeClass = player.life <= 5 ? "life low" : "life";
  return `
    <div class="name">${escapeHtml(player.username)}${isYou ? " (you)" : ""}</div>
    <div class="${lifeClass}">${player.life} life</div>
    <div class="pool">mana: ${manaLine(player.mana_pool)}</div>
    <div class="counts">library ${player.library_count} · graveyard ${player.graveyard_count} · hand ${player.hand_count}</div>
  `;
}

// -------------------------------------------------------- card rendering ----

function fallbackInnerHtml(card) {
  const pt = card.power !== null && card.power !== undefined ? `${card.power}/${card.toughness}` : "";
  const typeLine = card.type_line || (card.kind === "ability" ? "Ability" : "");
  const text = card.oracle_text || card.text || "";
  return `<span class="name">${escapeHtml(card.name || card.card_name || "?")}</span>
    <span class="type">${escapeHtml(typeLine)}</span>
    <span class="oracle">${escapeHtml(text.slice(0, 200))}</span>
    ${pt ? `<span class="pt">${escapeHtml(pt)}</span>` : ""}`;
}

function cardVisualInner(card, sizeClass) {
  const img = sizeClass === "card-md" ? card.image || card.small_image : card.small_image || card.image;
  if (!img) {
    return `<div class="card-fallback ${sizeClass}">${fallbackInnerHtml(card)}</div>`;
  }
  // Cards with no image fall back to a legible text card (above); a card
  // that *has* an image URL but fails to load falls back the same way, via
  // `onerror` swapping visibility to the pre-rendered sibling below, rather
  // than a broken-image box.
  return (
    `<img class="card-visual ${sizeClass}" src="${escapeHtml(img)}" alt="${escapeHtml(card.name || "")}" ` +
    `onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';" />` +
    `<div class="card-fallback ${sizeClass}" style="display:none">${fallbackInnerHtml(card)}</div>`
  );
}

function cardEl(card, sizeClass, opts) {
  opts = opts || {};
  const classes = ["card", sizeClass];
  if (card.tapped) classes.push("tapped");
  if (opts.opponent) classes.push("opponent-card");
  if (opts.selected) classes.push("selected");
  if (opts.attacking) classes.push("attacking");
  if (opts.blocking) classes.push("blocking");
  if (card.summoning_sick) classes.push("summoning-sick");
  const hasImage = !!(card.image || card.small_image);
  const pt = card.power !== null && card.power !== undefined ? `${card.power}/${card.toughness}` : "";
  const counters = Object.entries(card.counters || {})
    .filter(([, n]) => n)
    .map(([k, n]) => `${k}:${n}`)
    .join(" ");
  return `<div class="${classes.join(" ")}" data-card-id="${card.id}">
    ${cardVisualInner(card, sizeClass)}
    ${hasImage && pt ? `<span class="badge-pt">${escapeHtml(pt)}</span>` : ""}
    ${hasImage && counters ? `<span class="badge-counters">${escapeHtml(counters)}</span>` : ""}
  </div>`;
}

function cardBackHtml() {
  return `<div class="card-back"></div>`;
}

// -------------------------------------------------------------- preview ----

function wireCardPreview() {
  const board = $("#mtg-board");
  if (!board || board.dataset.previewWired) return;
  board.dataset.previewWired = "1";
  board.addEventListener("mouseover", (e) => {
    const el = e.target.closest(".card[data-card-id]");
    if (!el) return;
    const card = game.cardLookup[el.dataset.cardId];
    if (card) showPreview(card);
  });
  board.addEventListener("mouseout", (e) => {
    const el = e.target.closest(".card[data-card-id]");
    const to = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest(".card") : null;
    if (el && !to) hidePreview();
  });
}

function showPreview(card) {
  const box = $("#card-preview");
  const img = card.image || card.small_image;
  if (img) {
    box.innerHTML = `<img src="${escapeHtml(img)}" alt="${escapeHtml(card.name || "")}" onerror="this.remove()" />`;
  } else {
    box.innerHTML = `<div class="fallback-text">
      <span class="name">${escapeHtml(card.name || card.card_name || "?")}</span><br/>
      <span class="type">${escapeHtml(card.type_line || "")}</span>
      <div class="oracle">${escapeHtml(card.oracle_text || card.text || "")}</div>
    </div>`;
  }
  box.hidden = false;
}

function hidePreview() {
  $("#card-preview").hidden = true;
}

// -------------------------------------------------------------- zones ----

function renderOpponentHand(state, opponent) {
  const box = $("#opp-hand");
  const count = opponent.hand_count || 0;
  // Card backs only, counted but not named - the visibility filter already
  // hides their contents server-side (`state.hands[opponent] === []`), so
  // this must not invent what it cannot see.
  box.innerHTML = Array.from({ length: count }, cardBackHtml).join("") || '<div class="hint">(empty hand)</div>';
}

function renderStack(state) {
  const box = $("#stack-row");
  box.classList.toggle("empty", state.stack.length === 0);
  box.innerHTML = state.stack.map((o) => cardEl(o, "card-sm", {})).join("");
  $$("#stack-row .card").forEach((el) => {
    el.addEventListener("click", () => toggleTarget(el.dataset.cardId));
  });
}

function attackingBlockingSets(state) {
  const attacking = new Set();
  const blocking = new Set();
  for (const attack of (state.combat && state.combat.attacks) || []) {
    attacking.add(attack.attacker);
    for (const blocker of attack.blockers || []) blocking.add(blocker);
  }
  return { attacking, blocking };
}

function renderBattlefield(state, you) {
  const { attacking, blocking } = attackingBlockingSets(state);
  const mine = state.battlefield.filter((c) => c.controller === you);
  const theirs = state.battlefield.filter((c) => c.controller !== you);

  $("#opp-battlefield").innerHTML = theirs
    .map((c) => cardEl(c, "card-sm", { opponent: true, attacking: attacking.has(c.id), blocking: blocking.has(c.id) }))
    .join("");
  $("#my-battlefield").innerHTML = mine
    .map((c) => cardEl(c, "card-sm", { attacking: attacking.has(c.id), blocking: blocking.has(c.id) }))
    .join("");

  // Your own permanent: a click either adds/removes it as a target (when a
  // hand card is selected and being cast) or opens its own ability list.
  $$("#my-battlefield .card").forEach((el) => {
    el.addEventListener("click", () => onBattlefieldCardClickWithAbilities(el.dataset.cardId));
  });
  // An opponent's permanent can only ever be a target, never something you
  // activate - clicking it only ever adds/removes it as a target.
  $$("#opp-battlefield .card").forEach((el) => {
    el.addEventListener("click", () => onOpponentCardClick(el.dataset.cardId));
  });
}

function renderHand(state, you) {
  const box = $("#my-hand");
  const hand = (state.hands && state.hands[you]) || [];
  box.innerHTML =
    hand.map((c) => cardEl(c, "card-md", { selected: c.id === game.selectedHandCard })).join("") ||
    '<div class="hint">(empty hand)</div>';
  $$("#my-hand .card").forEach((el) => {
    el.addEventListener("click", () => onHandCardClick(el.dataset.cardId, hand));
  });
}

// ----------------------------------------------------------- decisions ----

const DECISION_RENDERERS = {
  may: renderYesNoDecision("accept"),
  pay_life: renderYesNoDecision("accept"),
  pay_energy: renderYesNoDecision("accept"),
  pay_mana: renderYesNoDecision("accept"),
  pay_life_amount: renderAmountDecision("life_amount"),
  pay_energy_amount: renderAmountDecision("energy_amount"),
  scry: renderCardSplitDecision(false),
  surveil: renderCardSplitDecision(true),
  choose_cards: renderChooseCardsDecision,
  choose_targets: renderChooseTargetsDecision,
  choose_a_quality: renderChoiceDecision,
  choose_a_mode: renderChoiceDecision,
  put_on_top_or_bottom: renderTopOrBottomDecision,
};

function renderYesNoDecision(field) {
  return (pending) => {
    const legal = pending.affirmative_is_legal !== false;
    return `
      <button ${legal ? "" : "disabled"} onclick="decide({${field}: true})">Yes</button>
      <button onclick="decide({${field}: false})">No</button>
      ${legal ? "" : '<div class="hint">yes is not legal right now</div>'}
    `;
  };
}

function renderAmountDecision(field) {
  return (pending) => `
    <input type="number" id="decision-amount" min="0" max="${pending.amount}" value="0" />
    <button onclick="decide({${field}: parseInt(document.getElementById('decision-amount').value || '0', 10)})">Pay</button>
  `;
}

function renderChoiceDecision(pending) {
  if (pending.options && pending.options.length) {
    return pending.options
      .map((opt) => `<button class="option" onclick='decide({choice: ${JSON.stringify(opt)}})'>${escapeHtml(opt)}</button>`)
      .join("");
  }
  return `
    <input type="text" id="decision-choice" placeholder="type your answer" />
    <button onclick="decide({choice: document.getElementById('decision-choice').value})">Choose</button>
  `;
}

function renderTopOrBottomDecision() {
  return `
    <button onclick="decide({top: true})">Top</button>
    <button onclick="decide({top: false})">Bottom</button>
  `;
}

function renderChooseCardsDecision(pending) {
  const details = pending.card_details || [];
  const boxes = details
    .map(
      (c) =>
        `<label class="option"><input type="checkbox" value="${c.id}" /> ${escapeHtml(c.name)}</label>`
    )
    .join("");
  return `
    <div>${boxes || "(no candidates shown)"}</div>
    <div class="hint">choose up to ${pending.amount}</div>
    <button onclick="submitChooseCards(${pending.amount})">Confirm</button>
  `;
}

function submitChooseCards(amount) {
  const ids = $$("#decision-box input[type=checkbox]:checked").map((el) => el.value);
  decide({ card_ids: ids });
}

function renderCardSplitDecision(toGraveyard) {
  return (pending) => {
    const details = pending.card_details || [];
    const label = toGraveyard ? "send to graveyard" : "send to bottom";
    const boxes = details
      .map(
        (c) =>
          `<label class="option"><input type="checkbox" value="${c.id}" /> ${escapeHtml(c.name)} <em>(${label})</em></label>`
      )
      .join("");
    return `
      <div>${boxes || "(nothing to look at)"}</div>
      <div class="hint">unchecked cards stay on top, in the order shown</div>
      <button onclick="submitCardSplit()">Confirm</button>
    `;
  };
}

function submitCardSplit() {
  const moved = $$("#decision-box input[type=checkbox]:checked").map((el) => el.value);
  decide({ moved, top_order: null });
}

function renderChooseTargetsDecision(pending) {
  const targets = pending.targets || [];
  const max = pending.target_maximum;
  const boxes = targets
    .map((t) => {
      const label = t.kind === "player" ? t.id : t.id;
      return `<label class="option"><input type="checkbox" value="${t.id}" /> ${escapeHtml(t.kind)}: ${escapeHtml(label)}</label>`;
    })
    .join("");
  return `
    <div>${boxes || "(no legal targets)"}</div>
    <div class="hint">${max !== null && max !== undefined ? `up to ${max} target(s)` : "choose target(s)"}</div>
    <button onclick="submitChooseTargets()">Confirm</button>
  `;
}

function submitChooseTargets() {
  const targets = $$("#decision-box input[type=checkbox]:checked").map((el) => el.value);
  decide({ targets });
}

function renderDecision(state, you) {
  const box = $("#decision-box");
  const pending = state.pending_decision;
  if (!pending || pending.player !== you) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  const renderer = DECISION_RENDERERS[pending.kind];
  const body = renderer ? renderer(pending) : `<div class="error">no UI handler for decision kind ${escapeHtml(pending.kind)} - this is a drift bug, see tests/test_ui_decision_coverage.py</div>`;
  box.innerHTML = `<h3>Decision: ${escapeHtml(pending.prompt)}</h3>${body}`;
}

async function decide(answer) {
  const body = Object.assign({ player: game.uid ? game.state.you : null }, answer);
  body.player = game.state.you;
  try {
    await api("POST", withUid(`/api/games/${game.gameId}/decide`), body);
  } catch (e) {
    alert(`decision refused: ${e.message}`);
  }
  poll();
}
window.decide = decide;
window.submitChooseCards = submitChooseCards;
window.submitCardSplit = submitCardSplit;
window.submitChooseTargets = submitChooseTargets;

// --------------------------------------------------- priority automation ----
//
// Feature 2 in full: (a) "pass until my upkeep", (b) casting auto-passes by
// default with a "keep priority" opt-out. Both ultimately drive the same
// `pass-priority` endpoint a manual click would; the loop for (a) runs one
// step per ordinary poll() tick (never its own unbounded loop), so a `Stop`
// click - or any of the conditions in `tickAutopilot` below - takes effect
// within one tick (~1.2s), never mid-flight and never silently.

function startPassUntilUpkeep() {
  if (!game.state) return;
  game.autopilotStoppedReason = null;
  game.autopilot = {
    target: "upkeep",
    knownStackIds: new Set((game.state.stack || []).map((o) => o.id)),
  };
  renderAutopilotBanner();
}
window.startPassUntilUpkeep = startPassUntilUpkeep;

function stopAutopilot(reason) {
  const wasRunning = game.autopilot !== null;
  game.autopilot = null;
  game.autopilotStoppedReason = reason || null;
  if (wasRunning || reason) renderAutopilotBanner();
}
window.stopAutopilot = stopAutopilot;

function toggleStopOnStackSetting(checked) {
  game.settings.stopOnStack = checked;
}
window.toggleStopOnStackSetting = toggleStopOnStackSetting;

function renderAutopilotBanner() {
  const banner = $("#autopilot-banner");
  if (game.autopilot) {
    banner.hidden = false;
    banner.innerHTML = `
      <span>Auto-passing until your upkeep…</span>
      <button class="danger" onclick="stopAutopilot('stopped by you')">Stop</button>
    `;
    return;
  }
  if (game.autopilotStoppedReason) {
    banner.hidden = false;
    banner.innerHTML = `<span class="why">Auto-pass stopped: ${escapeHtml(game.autopilotStoppedReason)}</span>
      <button onclick="dismissAutopilotBanner()">Dismiss</button>`;
    return;
  }
  banner.hidden = true;
  banner.innerHTML = "";
}

function dismissAutopilotBanner() {
  game.autopilotStoppedReason = null;
  renderAutopilotBanner();
}
window.dismissAutopilotBanner = dismissAutopilotBanner;

/**
 * The stop-condition check for a running autopilot run, as a pure function
 * of (state, autopilot, you, settings) - no DOM, no fetch, no mutation of
 * `game` itself. Pulled out of `tickAutopilot` on purpose: these are exactly
 * the paths that "cost games" if they regress (a decision-timeout already
 * has, per this task), so they need to be exercised by a real test without
 * standing up a browser - see `tests/test_ui_autopilot.py`, which runs this
 * exact function under Node.
 *
 * Returns a human-readable stop reason, or `null` to keep running. Checked,
 * in order: the game ending, a decision addressed to this seat, the target
 * step being reached, then (gated by `settings.stopOnStack`, on by default -
 * the safe default) a new object having arrived on the stack since the run
 * started or since the last tick, covering both "the opponent cast
 * something" and "a trigger went on the stack" the same way, because from
 * this seat's own point of view both are just "something new is here".
 */
function autopilotStopReason(state, autopilot, you, settings) {
  if (!autopilot) return null;
  if (state.winner || state.drawn) {
    return state.winner ? `game over - ${state.winner} won` : "game over - drawn";
  }
  if (state.pending_decision && state.pending_decision.player === you) {
    return `a decision is waiting for you: ${state.pending_decision.prompt}`;
  }
  if (autopilot.target === "upkeep" && state.step === "upkeep" && state.active_player === you) {
    return "reached your upkeep";
  }
  const currentIds = new Set((state.stack || []).map((o) => o.id));
  const arrived = [...currentIds].filter((id) => !autopilot.knownStackIds.has(id));
  if (arrived.length && settings.stopOnStack) {
    const obj = state.stack.find((o) => o.id === arrived[0]);
    return `something new went on the stack: ${obj ? obj.name : arrived[0]}`;
  }
  return null;
}

/** Whether a `pass-priority` outcome should stop a running autopilot, and
 * why - the fourth stop condition ("any command is refused"), split out as
 * its own pure function for the same testability reason as
 * `autopilotStopReason` above. `null` means the pass went through cleanly. */
function interpretPassOutcome(res) {
  if (res && res.allowed === false) {
    return `pass was refused: ${res.reason}`;
  }
  return null;
}

/** One step of a running "pass until my upkeep" - called from every poll()
 * tick. Stops immediately (and says why) on: a pending decision addressed to
 * this seat, the game ending, reaching the target step, a new object on the
 * stack (gated by the `stopOnStack` setting, default on - the safe default),
 * or the pass itself being refused. Never blocks longer than one HTTP call:
 * this function does exactly one thing (check, then maybe one pass call) and
 * returns - the loop is `poll()`'s own 1.2s interval, so a `Stop` click is
 * never more than one tick away from taking effect. */
async function tickAutopilot(state) {
  if (!game.autopilot) return;
  const you = state.you;

  const stopReason = autopilotStopReason(state, game.autopilot, you, game.settings);
  if (stopReason) {
    stopAutopilot(stopReason);
    return;
  }
  game.autopilot.knownStackIds = new Set((state.stack || []).map((o) => o.id));

  if (state.priority_holder !== you) return; // nothing for us to do this tick - keep waiting
  try {
    const res = await api("POST", `/api/games/${game.gameId}/actions/pass-priority`, { player: you });
    const refusalReason = interpretPassOutcome(res);
    if (refusalReason) stopAutopilot(refusalReason);
  } catch (e) {
    stopAutopilot(`pass failed: ${e.message}`);
  }
}

// Node-loadable for tests (`tests/test_ui_autopilot.py`) - a no-op in the
// browser, where `module` does not exist.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { autopilotStopReason, interpretPassOutcome };
}

// ------------------------------------------------------------- actions ----

function renderActionBar(state, you) {
  const bar = $("#action-bar");
  const isYourPriority = state.priority_holder === you;
  const isActive = state.active_player === you;
  const buttons = [];

  const passControls = [`
    <div class="pass-controls">
      ${isYourPriority ? '<button class="primary" onclick="passPriority()">Pass priority</button>' : ""}
      <button ${game.autopilot ? "disabled" : ""} onclick="startPassUntilUpkeep()">Pass until my upkeep</button>
      <label title="Stop and ask whenever a spell, ability trigger, or anything else lands on the stack while auto-passing - the safe default. Turn off to auto-pass through the opponent's whole turn.">
        <input type="checkbox" ${game.settings.stopOnStack ? "checked" : ""} onchange="toggleStopOnStackSetting(this.checked)" />
        stop on new stack object
      </label>
    </div>
  `];
  buttons.push(...passControls);

  if (isActive && (state.step === "untap" || state.step === "cleanup")) {
    buttons.push(`<button onclick="advanceTurn()">Advance (${escapeHtml(state.step)})</button>`);
  }
  if (state.stack.length) {
    buttons.push(`<button onclick="resolveTop()">Resolve top of stack</button>`);
  }
  if (state.step === "declare_attackers" && isActive) {
    buttons.push(`<button onclick="openAttackModal()">Declare attackers</button>`);
  }
  if (state.step === "declare_blockers" && !isActive) {
    buttons.push(`<button onclick="openBlockModal()">Declare blockers</button>`);
  }
  if (game.selectedHandCard) {
    buttons.push(`<span class="hint">selected: ${escapeHtml(cardNameById(state, game.selectedHandCard))} - click targets on the board, then:</span>`);
    buttons.push(`<button class="primary" onclick="openCastModal()">Cast / Play</button>`);
    buttons.push(`<button onclick="clearSelection()">Clear selection</button>`);
  }
  bar.innerHTML = buttons.join(" ");
}

function cardNameById(state, id) {
  const all = [...state.battlefield, ...((state.hands && state.hands[state.you]) || []), ...state.stack];
  const found = all.find((c) => c.id === id);
  return found ? found.name : id;
}

function clearSelection() {
  game.selectedHandCard = null;
  game.selectedTargets = [];
  render(game.state);
}
window.clearSelection = clearSelection;

function onHandCardClick(cardId) {
  if (game.selectedHandCard === cardId) {
    clearSelection();
    return;
  }
  game.selectedHandCard = cardId;
  game.selectedTargets = [];
  render(game.state);
}

function onOpponentCardClick(cardId) {
  toggleTarget(cardId);
}
function toggleTarget(cardId) {
  if (!game.selectedHandCard) return;
  const idx = game.selectedTargets.indexOf(cardId);
  if (idx >= 0) game.selectedTargets.splice(idx, 1);
  else game.selectedTargets.push(cardId);
  render(game.state);
}

async function passPriority() {
  try {
    await api("POST", `/api/games/${game.gameId}/actions/pass-priority`, { player: game.state.you });
  } catch (e) {
    alert(e.message);
  }
  poll();
}
window.passPriority = passPriority;

async function advanceTurn() {
  try {
    const res = await api("POST", withUid(`/api/games/${game.gameId}/advance`), { player: game.state.you });
    if (res.allowed === false) alert(res.reason);
  } catch (e) {
    alert(e.message);
  }
  poll();
}
window.advanceTurn = advanceTurn;

async function resolveTop() {
  try {
    await api("POST", withUid(`/api/games/${game.gameId}/resolve-top`), { player: game.state.you });
  } catch (e) {
    alert(e.message);
  }
  poll();
}
window.resolveTop = resolveTop;

// ----- cast / play-land / activate modal -----

function openCastModal() {
  const state = game.state;
  const hand = (state.hands && state.hands[state.you]) || [];
  const card = hand.find((c) => c.id === game.selectedHandCard);
  if (!card) return;
  const isLand = (card.type_line || "").toLowerCase().includes("land");
  const modal = $("#action-modal");
  modal.hidden = false;
  modal.innerHTML = `<div class="modal-inner">
    <h3>${isLand ? "Play land" : "Cast"}: ${escapeHtml(card.name)}</h3>
    <div class="hint">selected targets: ${game.selectedTargets.length ? escapeHtml(game.selectedTargets.join(", ")) : "(none - click cards/opponent to add)"}</div>
    ${isLand ? "" : `
      <label>Modes (comma-separated numbers, leave blank if none) <input type="text" id="modal-modes" /></label>
      <label>X value (leave blank if none) <input type="number" id="modal-x" /></label>
      <label>Keyword permission (e.g. Plot, Foretell - leave blank normally) <input type="text" id="modal-keyword" /></label>
      <label><input type="checkbox" id="modal-hold" ${game.holdOnCast ? "checked" : ""} />
        keep priority after casting (CR 117.3c) - normally priority auto-passes so the spell resolves</label>
    `}
    <div>
      <button class="primary" onclick="confirmCast(${isLand})">${isLand ? "Play" : "Cast"}</button>
      <button onclick="closeModal()">Cancel</button>
    </div>
    <div class="error" id="modal-error"></div>
  </div>`;
}

async function confirmCast(isLand) {
  const cardId = game.selectedHandCard;
  try {
    if (isLand) {
      await api("POST", `/api/games/${game.gameId}/actions/play-land`, {
        player: game.state.you,
        card_id: cardId,
      });
    } else {
      const modes = $("#modal-modes").value.trim();
      const x = $("#modal-x").value.trim();
      const keyword = $("#modal-keyword").value.trim();
      const hold = $("#modal-hold").checked;
      game.holdOnCast = hold; // remember the choice for next cast, this session only
      const body = {
        player: game.state.you,
        card_id: cardId,
        targets: game.selectedTargets,
      };
      if (modes) body.modes = modes.split(",").map((n) => parseInt(n.trim(), 10));
      if (x) body.x_value = parseInt(x, 10);
      if (keyword) body.keyword = keyword;
      const res = await api("POST", `/api/games/${game.gameId}/actions/cast`, body);
      if (res.allowed === false) {
        $("#modal-error").textContent = res.reason;
        return;
      }
      // Feature 2b: casting auto-passes priority by default (CR 117.3c: a
      // player who acted keeps priority and is entitled to act again - "hold"
      // is that entitlement exercised, matching `seatkit.run_action`'s own
      // `hold_priority` semantics; the default here mirrors that module's
      // default of releasing unless told to hold). Fire-and-forget: any
      // refusal is reported by `passPriority` itself, and `poll()` below
      // still refreshes the board either way.
      if (!hold) {
        passPriority();
      }
    }
    closeModal();
    clearSelection();
  } catch (e) {
    $("#modal-error").textContent = e.message;
    return;
  }
  poll();
}
window.confirmCast = confirmCast;

function closeModal() {
  const modal = $("#action-modal");
  modal.hidden = true;
  modal.innerHTML = "";
}
window.closeModal = closeModal;
window.openCastModal = openCastModal;

function abilityListHtml(card) {
  return (card.abilities || [])
    .map(
      (a) =>
        `<button onclick="activateAbility('${card.id}', ${a.index})">${a.index}: ${escapeHtml(a.text)}${a.mana ? " (mana)" : ""}</button>`
    )
    .join(" ");
}

async function activateAbility(cardId, abilityIndex) {
  const targets = game.selectedTargets.slice();
  try {
    const res = await api("POST", `/api/games/${game.gameId}/actions/activate`, {
      player: game.state.you,
      card_id: cardId,
      ability_index: abilityIndex,
      targets,
    });
    if (res.allowed === false) alert(res.reason);
  } catch (e) {
    alert(e.message);
  }
  clearSelection();
  poll();
}
window.activateAbility = activateAbility;

// Long-press-free simplicity: clicking a controlled permanent selects it as
// a target if a hand card is selected; otherwise it opens its ability list.
function onBattlefieldCardClickWithAbilities(cardId) {
  if (game.selectedHandCard) {
    toggleTarget(cardId);
    return;
  }
  const card = game.state.battlefield.find((c) => c.id === cardId);
  if (!card || !card.abilities || !card.abilities.length) return;
  const modal = $("#action-modal");
  modal.hidden = false;
  modal.innerHTML = `<div class="modal-inner">
    <h3>Activate: ${escapeHtml(card.name)}</h3>
    <div class="hint">click board cards/opponent first to add targets, then an ability</div>
    <div>${abilityListHtml(card)}</div>
    <button onclick="closeModal()">Cancel</button>
  </div>`;
}
window.onBattlefieldCardClickWithAbilities = onBattlefieldCardClickWithAbilities;

// ----- attack / block -----

function openAttackModal() {
  const state = game.state;
  const you = state.you;
  const mine = state.battlefield.filter(
    (c) => c.controller === you && !c.tapped && !c.summoning_sick && (c.type_line || "").toLowerCase().includes("creature")
  );
  const opponents = state.players.map((p) => p.username).filter((u) => u !== you);
  const modal = $("#action-modal");
  modal.hidden = false;
  modal.innerHTML = `<div class="modal-inner">
    <h3>Declare attackers</h3>
    ${mine
      .map(
        (c) => `<div><label><input type="checkbox" class="atk-check" value="${c.id}" /> ${escapeHtml(c.name)}</label>
      <select class="atk-defender" data-card="${c.id}">${opponents.map((o) => `<option value="${o}">${escapeHtml(o)}</option>`).join("")}</select></div>`
      )
      .join("") || "<div>no eligible attackers</div>"}
    <button class="primary" onclick="confirmAttack()">Attack</button>
    <button onclick="closeModal()">Cancel</button>
    <div class="error" id="modal-error"></div>
  </div>`;
}
window.openAttackModal = openAttackModal;

async function confirmAttack() {
  const attackers = [];
  $$(".atk-check:checked").forEach((el) => {
    const defender = document.querySelector(`.atk-defender[data-card="${el.value}"]`).value;
    attackers.push({ card_id: el.value, defender });
  });
  if (!attackers.length) {
    closeModal();
    return;
  }
  try {
    const res = await api("POST", `/api/games/${game.gameId}/actions/attack`, { attackers });
    if (res.allowed === false) {
      $("#modal-error").textContent = res.reason;
      return;
    }
  } catch (e) {
    $("#modal-error").textContent = e.message;
    return;
  }
  closeModal();
  poll();
}
window.confirmAttack = confirmAttack;

function openBlockModal() {
  const state = game.state;
  const you = state.you;
  const attackers = state.combat.attacks.filter((a) => {
    const attackerCard = state.battlefield.find((c) => c.id === a.attacker);
    return attackerCard && attackerCard.controller !== you;
  });
  const myCreatures = state.battlefield.filter(
    (c) => c.controller === you && !c.tapped && (c.type_line || "").toLowerCase().includes("creature")
  );
  const modal = $("#action-modal");
  modal.hidden = false;
  modal.innerHTML = `<div class="modal-inner">
    <h3>Declare blockers</h3>
    ${attackers
      .map((a) => {
        const attackerCard = state.battlefield.find((c) => c.id === a.attacker);
        const name = attackerCard ? attackerCard.name : a.attacker;
        return `<div><strong>${escapeHtml(name)}</strong> attacking ${escapeHtml(a.defender)}:
        ${myCreatures
          .map(
            (c) =>
              `<label><input type="checkbox" class="blk-check" data-attacker="${a.attacker}" value="${c.id}" /> ${escapeHtml(c.name)}</label>`
          )
          .join(" ")}</div>`;
      })
      .join("") || "<div>no attackers to block</div>"}
    <button class="primary" onclick="confirmBlocks()">Block</button>
    <button onclick="closeModal()">Cancel (no blocks)</button>
    <div class="error" id="modal-error"></div>
  </div>`;
}
window.openBlockModal = openBlockModal;

async function confirmBlocks() {
  const blocks = [];
  $$(".blk-check:checked").forEach((el) => {
    blocks.push({ blocker: el.value, blocked: el.dataset.attacker });
  });
  if (!blocks.length) {
    // CR 509.1c: declaring no blockers is legal. `declare-blockers` requires
    // at least one pair, so "no blocks" is expressed by simply passing
    // priority instead of calling it at all.
    closeModal();
    poll();
    return;
  }
  try {
    const res = await api("POST", `/api/games/${game.gameId}/actions/declare-blockers`, {
      player: game.state.you,
      blocks,
    });
    if (res.allowed === false) {
      $("#modal-error").textContent = res.reason;
      return;
    }
  } catch (e) {
    $("#modal-error").textContent = e.message;
    return;
  }
  closeModal();
  poll();
}
window.confirmBlocks = confirmBlocks;

// ------------------------------------------------------------- AI panel ----

function renderAiPanel(status) {
  const panel = $("#ai-panel");
  if (!status || status.state === "not_started") {
    panel.innerHTML = `<span>AI seat: not started</span>`;
    return;
  }
  if (status.mechanism === "fallback") {
    panel.innerHTML = `
      <span class="state-error">AI seat: no headless agent running (${escapeHtml(status.reason || "fallback")})</span>
      <button onclick="showFallbackBox('${escapeHtml(JSON.stringify(status.prompt || "")).replace(/'/g, "&#39;")}')">Show command to run by hand</button>
    `;
    return;
  }
  const stateClass = `state-${status.state}`;
  const lastLine = (status.stdout_tail || []).slice(-1)[0] || "";
  panel.innerHTML = `<span class="${stateClass}">AI seat (${escapeHtml(status.player)}): ${escapeHtml(status.state)}</span>
    ${lastLine ? `<span class="hint">${escapeHtml(lastLine).slice(0, 120)}</span>` : ""}
    ${status.state === "error" ? `<button onclick='showAiError(${JSON.stringify(status)})'>show error</button>` : ""}`;
}

function showAiError(status) {
  alert(`AI seat error (returncode ${status.returncode}):\n${(status.stderr_tail || []).join("\n")}`);
}
window.showAiError = showAiError;

function showFallbackBox(escapedPrompt) {
  const modal = $("#action-modal");
  modal.hidden = false;
  modal.innerHTML = `<div class="modal-inner">
    <h3>Run the AI seat yourself</h3>
    <p>The <code>claude</code> CLI could not be spawned automatically from this server. Run this in another terminal, from the API repo:</p>
    <textarea rows="4" readonly>claude --print --dangerously-skip-permissions --allowedTools Bash -- "…"</textarea>
    <p>Full prompt (includes your opponent's uid and this game id):</p>
    <textarea rows="12" readonly id="fallback-prompt"></textarea>
    <button onclick="closeModal()">Close</button>
  </div>`;
  document.getElementById("fallback-prompt").value = escapedPrompt;
}
window.showFallbackBox = showFallbackBox;

// -------------------------------------------------------------- startup ----

function init() {
  loadBundledDecks().catch((e) => console.error(e));
  wireDeckPicker("you");
  wireDeckPicker("ai");
  $("#start-game").addEventListener("click", () => startGame().catch((e) => ($("#start-error").textContent = e.message)));
  $("#leave-game").addEventListener("click", leaveGame);

  const saved = localStorage.getItem(LS_KEY);
  if (saved) {
    try {
      const { gameId, uid, aiUid } = JSON.parse(saved);
      game.aiUid = aiUid;
      enterGame(gameId, uid);
    } catch {
      /* ignore a corrupt saved session */
    }
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", init);
}
