"use strict";
// Node has no `window` - this file is still `require()`-able there for
// `tests/test_ui_autopilot.py`, which loads the real `autopilotStopReason`/
// `interpretPassOutcome` functions (see the `module.exports` guard near the
// bottom) rather than a Python reimplementation of their logic. A no-op in
// the browser, where `window` already exists.
if (typeof window === "undefined") {
  var window = globalThis; // eslint-disable-line no-var
}

/* Server-rendered HTML + plain JS over the seat API (app.py / schemas.py /
 * decisions.py / affordances.py) - no build step, no framework.
 *
 * Two rules this file keeps:
 *
 * 1. It never invents game rules. Every button calls an endpoint the API
 *    documents, every refusal shown is the engine's own `reason` text
 *    verbatim, and every question about what an action needs - does it
 *    target, how many, which objects are legal, is there an X, is it modal -
 *    is answered by `affordances.py`, not guessed at here. That is what
 *    makes the action tray context-sensitive rather than a wall of boxes:
 *    an X field appears only for a spell that has an X in its cost, a mode
 *    picker only for a modal spell, a target picker only for something that
 *    targets, listing only what the engine says is legal right now.
 *
 * 2. It never invents what it cannot see. The opponent's hand is card backs,
 *    counted but not named, because `visibility.py` strips it server-side
 *    before this file gets a response.
 *
 * The one distinction that is load-bearing throughout: `targets: undefined`
 * ("the player has not chosen") is NOT `targets: []` ("the player chose
 * none"). The engine reads the second as a real CR 115.6 answer of zero and
 * puts the ability on the stack targeting nothing; only the first makes it
 * ask. Karn, the Great Creator's +1 was broken exactly this way. See
 * `sendTargets` below - nothing else in this file may build that field.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const LS_KEY = "mcm_last_game";
const POLL_MS = 1200;

/** CR 500.1's turn structure, in order, with the phase each step belongs to.
 * Mirrors the engine's own `Step`/`PHASE_OF` (machines/turn.py); the labels
 * are only for display. */
const STEPS = [
  { step: "untap", phase: "beginning", label: "untap" },
  { step: "upkeep", phase: "beginning", label: "upkeep" },
  { step: "draw", phase: "beginning", label: "draw" },
  { step: "precombat_main", phase: "precombat_main", label: "main 1" },
  { step: "beginning_of_combat", phase: "combat", label: "combat" },
  { step: "declare_attackers", phase: "combat", label: "attack" },
  { step: "declare_blockers", phase: "combat", label: "block" },
  { step: "combat_damage", phase: "combat", label: "damage" },
  { step: "end_of_combat", phase: "combat", label: "end cbt" },
  { step: "postcombat_main", phase: "postcombat_main", label: "main 2" },
  { step: "end_step", phase: "ending", label: "end" },
  { step: "cleanup", phase: "ending", label: "cleanup" },
];

let game = {
  gameId: null,
  uid: null,
  aiUid: null,
  pollHandle: null,
  polling: false, // re-entrancy guard: skip a tick if the previous is in flight
  state: null,
  symbols: {}, // "{R}" -> {svg, text}, from Scryfall via /api/ui/symbology
  cardLookup: {}, // card/ability id -> its view object, for hover preview
  // The one action being built right now, or null. Everything the tray shows
  // is derived from this plus its `affordance` (the server's answer about
  // what this action needs), never from a guess about the card.
  action: null,
  autopilot: null,
  autopilotStoppedReason: null,
  settings: {
    // The safe default: stop and ask whenever anything the player could
    // respond to goes on the stack, rather than blow through it.
    stopOnStack: true,
    // Casting releases priority so the spell resolves, unless the player
    // asks to keep it (CR 117.3c). Remembered across casts this session.
    holdPriority: false,
    // The auto-pass destination the player picked, kept here rather than
    // read off the <select> only at click time: the panel re-renders on a
    // poll tick, and a value that lived only in the DOM would reset itself.
    autopilotTarget: "upkeep|you",
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

/**
 * Replace a container's HTML - but never while the player is using a control
 * inside it, and never when nothing actually changed.
 *
 * `render()` runs on every poll tick (~1.2s) and rebuilds whole panels from
 * `innerHTML`. That destroys and recreates every element underneath, which
 * for a form control means: an open `<select>` snaps shut, a half-typed
 * `<input>` loses its text and its focus, and a checkbox mid-click is
 * replaced. Both reported symptoms - the auto-pass dropdown closing before it
 * could be used, and the card-name box for Disruptor Flute's "as this enters,
 * choose a card name" being impossible to type into - are this one bug.
 *
 * Two guards, cheapest first: skip when the markup is byte-identical (the
 * common case between ticks, so most polls touch no DOM at all), and skip
 * when the focused element lives inside this container. The panel refreshes
 * on the next tick after the player moves on, so nothing goes stale for long.
 */
// Repaint `el`, unless a control inside it is mid-edit and the repaint would
// throw away what the player is typing.
//
// A checkbox or radio is deliberately NOT mid-edit: it holds no typed text,
// and ticking one is exactly what asks for the repaint. Skipping there is how
// the CR 103.5 bottoming picker wedged - tick the card you are bottoming, the
// panel refuses to repaint because that very checkbox now has focus, and the
// "Keep" button stays `disabled` forever no matter how many cards you tick.
// From the player's chair the mulligan simply does not work. So repaint, and
// put the focus back on the same control afterwards.
function setHtml(el, html) {
  if (!el) return;
  if (el.innerHTML === html) return;
  const active = document.activeElement;
  const inside =
    active && active !== document.body && el.contains(active) ? active : null;
  if (inside && /^(INPUT|SELECT|TEXTAREA)$/.test(inside.tagName)) {
    const toggle = /^(checkbox|radio)$/.test(inside.type || "");
    if (!toggle) return;
    const value = inside.value;
    el.innerHTML = html;
    const again = value
      ? el.querySelector(`input[value="${(window.CSS && CSS.escape ? CSS.escape(value) : value)}"]`)
      : null;
    if (again) again.focus();
    return;
  }
  el.innerHTML = html;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// -------------------------------------------------------- mana symbols ----
//
// Scryfall's symbology (`/api/ui/symbology`) is keyed by the exact `{R}` /
// `{2/U}` / `{T}` token that already appears verbatim in a card's mana cost
// and in an activated ability's printed cost, so nothing here needs a symbol
// table of its own - and hybrid, Phyrexian and snow symbols come out right
// without a single special case. A token with no entry (or a cold symbol
// cache, or no network at first run) falls back to the token drawn as a
// text pip, which is why `symbolHtml` never returns nothing.

async function loadSymbols() {
  try {
    const { symbols } = await api("GET", "/api/ui/symbology");
    game.symbols = symbols || {};
  } catch {
    game.symbols = {};
  }
}

function symbolHtml(token, big) {
  const entry = game.symbols[token];
  const cls = big ? "ms ms-lg" : "ms";
  if (entry && entry.svg) {
    return `<img class="${cls}" src="${escapeHtml(entry.svg)}" alt="${escapeHtml(token)}" title="${escapeHtml(entry.text || token)}" />`;
  }
  return `<span class="ms-text" title="${escapeHtml(token)}">${escapeHtml(token.replace(/[{}]/g, ""))}</span>`;
}

/** Any string with `{...}` tokens in it - a mana cost, an ability's cost, a
 * line of oracle text - with the tokens drawn as symbols and everything
 * else escaped. */
function withSymbols(text, big) {
  if (!text) return "";
  return String(text).replace(/(\{[^}]{1,8}\})|([^{]+)/g, (_, token, plain) =>
    token ? symbolHtml(token, big) : escapeHtml(plain)
  );
}

/** A mana pool as symbols: three red mana is `3` next to one red pip, not
 * `RRR` and not the string `3R`. Feature 4's whole point. */
function manaPoolHtml(pool) {
  const floating = (pool && pool.floating) || {};
  const snow = (pool && pool.snow) || {};
  const groups = [];
  for (const [color, count] of Object.entries(floating)) {
    if (!count) continue;
    groups.push(
      `<span class="pool-group" title="${count} ${escapeHtml(color)}">` +
        `<span class="n">${count}</span>${symbolHtml(`{${color}}`, true)}</span>`
    );
  }
  for (const [color, count] of Object.entries(snow)) {
    if (!count) continue;
    groups.push(
      `<span class="pool-group" title="${count} snow ${escapeHtml(color)}">` +
        `<span class="n">${count}</span>${symbolHtml(`{S}`, true)}</span>`
    );
  }
  if (!groups.length) return '<span class="pool empty">empty pool</span>';
  return `<span class="pool">${groups.join("")}</span>`;
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
  $(`#${prefix}-validate`).addEventListener("click", () => validateAndReport(prefix));
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
      lines.push(
        `<div class="card-error">${escapeHtml(e.card ? `${e.card}: ` : "")}${escapeHtml(e.message)}</div>`
      );
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
  // Both at once: the two validations are independent, write to different
  // panels, and each can still cost a round trip per uncached card.
  const [youResult, aiResult] = await Promise.all([
    validateAndReport("you"),
    validateAndReport("ai"),
  ]);
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
  game.aiUid = aiUid;
  enterGame(created.game_id, youUid);
  // Never swallowed: if the opponent's agent does not start, the human is in
  // a game whose opponent will not act, and the only thing worse than that is
  // not being told. The pregame is playable regardless - `ai_pregame.py` makes
  // the bot seat's CR 103.5/103.6 default moves once its grace lapses - so say
  // what happened and let them keep going.
  api("POST", `/api/ui/games/${created.game_id}/ai-seat/start`, { uid: aiUid })
    .then((status) => {
      game.aiStatus = status;
      renderAiPanel(status);
      if (status && status.mechanism === "fallback") {
        notice(
          `AI seat not running: ${status.reason || "unknown reason"}. The game still starts - ` +
            `its pregame moves are made automatically.`,
          "bad"
        );
      }
    })
    .catch((e) => {
      notice(`Could not start the AI seat: ${e.message}. The game still starts.`, "bad");
    });
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
  game.pollHandle = setInterval(poll, POLL_MS);
}

function leaveGame() {
  if (game.pollHandle) clearInterval(game.pollHandle);
  game.pollHandle = null;
  game.autopilot = null;
  game.action = null;
  localStorage.removeItem(LS_KEY);
  game.gameId = null;
  game.uid = null;
  $("#game").hidden = true;
  $("#lobby").hidden = false;
}

function setConnection(text, ok) {
  for (const sel of ["#conn-status", "#conn-status-game"]) {
    const el = $(sel);
    if (!el) continue;
    el.textContent = text;
    el.className = `pill ${ok ? "ok" : "bad"}`;
  }
}

async function poll() {
  if (!game.gameId || game.polling) return;
  game.polling = true;
  try {
    let state;
    try {
      state = await api("GET", withUid(`/api/games/${game.gameId}`));
    } catch (e) {
      setConnection(`disconnected: ${e.message}`, false);
      return;
    }
    setConnection("connected", true);
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
    const status = await api(
      "GET",
      `/api/ui/games/${game.gameId}/ai-seat/status?uid=${game.aiUid}`
    );
    game.aiStatus = status;
    renderAiPanel(status);
  } catch {
    /* transient - the next poll tries again */
  }
}

// -------------------------------------------------------------- render ----

function render(state) {
  const you = state.you;
  const opponent = state.players.find((p) => p.username !== you) || state.players[0];
  const youPlayer = state.players.find((p) => p.username === you) || state.players[0];

  game.cardLookup = buildCardLookup(state);

  $("#turn-info").innerHTML =
    `<strong>turn ${state.turn_number}</strong> · ` +
    `active ${escapeHtml(state.active_player)} · ` +
    `<span class="prio">priority ${escapeHtml(state.priority_holder || "-")}</span>`;
  renderPhaseTrack(state);
  renderGameOver(state);

  renderInfoRow($("#opp-info"), opponent, false);
  renderInfoRow($("#you-info"), youPlayer, true);

  renderOpponentHand(opponent);
  renderStack(state);
  renderBattlefield(state, you);
  renderHand(state, you);

  renderPregame(state, you);
  renderDecision(state, you);
  renderActionTray(state);
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
  (state.exile || []).forEach(add);
  ((state.hands && state.hands[state.you]) || []).forEach(add);
  Object.values(state.graveyards || {}).forEach((cards) => (cards || []).forEach(add));
  return lookup;
}

function renderPhaseTrack(state) {
  const track = $("#phase-track");
  const currentIndex = STEPS.findIndex((s) => s.step === state.step);
  const target = game.autopilot ? game.autopilot.step : null;
  track.innerHTML = STEPS.map((entry, index) => {
    const classes = [];
    if (index === currentIndex) classes.push("current");
    else if (index < currentIndex) classes.push("done");
    if (entry.step === target) classes.push("target");
    return `<li class="${classes.join(" ")}" title="${escapeHtml(entry.step)} (${escapeHtml(entry.phase)})">${escapeHtml(entry.label)}</li>`;
  }).join("");
}

function renderGameOver(state) {
  const box = $("#game-over");
  if (state.winner) {
    box.hidden = false;
    box.innerHTML = `<span>Game over - <strong>${escapeHtml(state.winner)}</strong> won.</span>`;
  } else if (state.drawn) {
    box.hidden = false;
    box.innerHTML = "<span>Game over - drawn.</span>";
  } else {
    box.hidden = true;
    box.innerHTML = "";
  }
}

function renderInfoRow(el, player, isYou) {
  const lifeClass = player.life <= 5 ? "life low" : "life";
  const counters = Object.entries(player.counters || {})
    .filter(([, n]) => n)
    .map(([kind, n]) => `<span class="pcounter">${escapeHtml(kind)} ${n}</span>`)
    .join("");
  el.innerHTML = `
    <div class="name">${escapeHtml(player.username)}${isYou ? " (you)" : ""}</div>
    <div class="${lifeClass}">${player.life}</div>
    ${manaPoolHtml(player.mana_pool)}
    ${counters}
    <div class="counts">hand ${player.hand_count} · library ${player.library_count} · graveyard ${player.graveyard_count}</div>
  `;
  const targetable = isTargetCandidate(player.username);
  el.classList.toggle("targetable", targetable);
  el.classList.toggle("chosen", isChosenTarget(player.username));
  el.onclick = () => (targetable ? toggleTarget(player.username) : null);
}

// -------------------------------------------------------- card rendering ----

function fallbackInnerHtml(card) {
  const pt =
    card.power !== null && card.power !== undefined ? `${card.power}/${card.toughness}` : "";
  const typeLine = card.type_line || (card.kind === "ability" ? "Ability" : "");
  const text = card.oracle_text || card.text || "";
  return `<span class="name">${escapeHtml(card.name || card.card_name || "?")}</span>
    <span class="type">${escapeHtml(typeLine)}</span>
    <span class="oracle">${escapeHtml(text.slice(0, 200))}</span>
    ${pt ? `<span class="pt">${escapeHtml(pt)}</span>` : ""}`;
}

function cardVisualInner(card, sizeClass) {
  const img = sizeClass === "card-sm" ? card.small_image || card.image : card.image || card.small_image;
  if (!img) {
    return `<div class="card-fallback ${sizeClass}">${fallbackInnerHtml(card)}</div>`;
  }
  // A card with no image gets a legible text card; a card that *has* an
  // image URL but fails to load falls back the same way via `onerror`,
  // rather than showing a broken-image box.
  return (
    `<img class="card-visual ${sizeClass}" src="${escapeHtml(img)}" alt="${escapeHtml(card.name || "")}" ` +
    `onerror="this.style.display='none'; this.nextElementSibling.style.display='flex';" />` +
    `<div class="card-fallback ${sizeClass}" style="display:none">${fallbackInnerHtml(card)}</div>`
  );
}

/** The badges the art itself cannot carry (feature 5). Loyalty gets the
 * shield a real planeswalker card has, rather than being one row of a
 * counters list; `+1/+1` and `-1/-1` counters are coloured by which they
 * are; time counters (suspend) get their own colour so a suspended card's
 * clock is readable at a glance; summoning sickness greys the card and adds
 * the swirl; and CR 614.12a's chosen name (Pithing Needle, Disruptor Flute,
 * Cavern of Souls) is written across the art, because on those permanents
 * the chosen name *is* the card's function. */
function badgesHtml(card) {
  const bits = [];
  const counters = card.counters || {};
  const loyalty = counters.loyalty;

  const pt =
    card.power !== null && card.power !== undefined ? `${card.power}/${card.toughness}` : "";
  if (pt) {
    const buffed =
      card.printed_power !== undefined &&
      (card.printed_power !== card.power || card.printed_toughness !== card.toughness);
    const title = buffed ? `printed ${card.printed_power}/${card.printed_toughness}` : "";
    bits.push(
      `<span class="badge badge-pt ${buffed ? "buffed" : ""}" title="${escapeHtml(title)}">${escapeHtml(pt)}</span>`
    );
  }
  if (loyalty !== undefined && loyalty !== null) {
    bits.push(`<span class="badge badge-loyalty" title="loyalty">${loyalty}</span>`);
  } else if (card.damage) {
    bits.push(`<span class="badge badge-damage" title="damage marked">${card.damage}</span>`);
  }

  const chips = Object.entries(counters)
    .filter(([kind, n]) => n && kind !== "loyalty")
    .map(([kind, n]) => {
      let cls = "";
      if (kind === "+1/+1") cls = "plus";
      else if (kind === "-1/-1") cls = "minus";
      else if (kind === "time") cls = "time";
      return `<span class="counter-chip ${cls}" title="${escapeHtml(kind)} counters">${escapeHtml(kind)} ${n}</span>`;
    });
  if (chips.length) bits.push(`<div class="counter-strip">${chips.join("")}</div>`);

  if (card.summoning_sick) {
    bits.push(
      '<span class="badge badge-sick" title="summoning sick: it cannot attack or use {T} abilities (CR 302.6)">↻</span>'
    );
  } else if ((card.abilities || []).length && card.zone === "battlefield") {
    bits.push(
      `<span class="badge badge-ability" title="${(card.abilities || []).length} activated abilit(ies)">${card.abilities.length}</span>`
    );
  }
  if (card.choice) {
    bits.push(
      `<span class="badge badge-named" title="named as it entered (CR 614.12a): ${escapeHtml(card.choice.kind)}">${escapeHtml(card.choice.value)}</span>`
    );
  }
  return bits.join("");
}

function cardEl(card, sizeClass, opts) {
  opts = opts || {};
  const classes = ["card", sizeClass];
  if (card.tapped) classes.push("tapped");
  if (opts.selected) classes.push("selected");
  if (opts.attacking) classes.push("attacking");
  if (opts.blocking) classes.push("blocking");
  if (card.summoning_sick) classes.push("summoning-sick");
  if (opts.targetable) classes.push("targetable");
  if (opts.chosen) classes.push("chosen");
  if (opts.dimmed) classes.push("dimmed");
  return `<div class="${classes.join(" ")}" data-card-id="${escapeHtml(card.id)}">
    ${cardVisualInner(card, sizeClass)}
    ${badgesHtml(card)}
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
      <div class="oracle">${withSymbols(card.oracle_text || card.text || "")}</div>
    </div>`;
  }
  box.hidden = false;
}

function hidePreview() {
  $("#card-preview").hidden = true;
}

// --------------------------------------------------------------- zones ----

function renderOpponentHand(opponent) {
  const box = $("#opp-hand");
  const count = opponent.hand_count || 0;
  // Card backs only, counted but not named: `visibility.py` already hides
  // their contents server-side, so this must not invent what it cannot see.
  box.innerHTML =
    Array.from({ length: count }, cardBackHtml).join("") || '<div class="hint">(empty hand)</div>';
}

function renderStack(state) {
  const box = $("#stack-row");
  box.classList.toggle("empty", state.stack.length === 0);
  box.innerHTML = state.stack
    .map((o) =>
      cardEl(o, "card-sm", {
        targetable: isTargetCandidate(o.id),
        chosen: isChosenTarget(o.id),
      })
    )
    .join("");
  wireCardClicks("#stack-row", (id) => onBoardCardClick(id));
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

function wireCardClicks(rootSel, handler) {
  $$(`${rootSel} .card`).forEach((el) => {
    el.addEventListener("click", () => handler(el.dataset.cardId));
  });
}

function renderBattlefield(state, you) {
  const { attacking, blocking } = attackingBlockingSets(state);
  const picking = isPickingTargets();
  const paint = (c) =>
    cardEl(c, "card-sm", {
      attacking: attacking.has(c.id),
      blocking: blocking.has(c.id),
      targetable: isTargetCandidate(c.id),
      chosen: isChosenTarget(c.id),
      // While targets are being chosen, anything the engine says is not a
      // legal target is dimmed rather than left looking clickable.
      dimmed: picking && !isTargetCandidate(c.id) && !isChosenTarget(c.id),
      selected: game.action && game.action.cardId === c.id,
    });

  $("#opp-battlefield").innerHTML = state.battlefield
    .filter((c) => c.controller !== you)
    .map(paint)
    .join("");
  $("#my-battlefield").innerHTML = state.battlefield
    .filter((c) => c.controller === you)
    .map(paint)
    .join("");

  wireCardClicks("#opp-battlefield", (id) => onBoardCardClick(id));
  wireCardClicks("#my-battlefield", (id) => onBoardCardClick(id));
}

function renderHand(state, you) {
  const box = $("#my-hand");
  const hand = (state.hands && state.hands[you]) || [];
  const picking = isPickingTargets();
  box.innerHTML =
    hand
      .map((c) =>
        cardEl(c, "card-md", {
          selected: game.action && game.action.cardId === c.id,
          dimmed: picking,
        })
      )
      .join("") || '<div class="hint">(empty hand)</div>';
  wireCardClicks("#my-hand", (id) => onHandCardClick(id));
}

// ----------------------------------------------------------- decisions ----
//
// One renderer per `DecisionKind` the server will answer (`decisions.py`'s
// `ANSWERABLE_KINDS`). `tests/test_ui_decision_coverage.py` reads the keys of
// this exact object and fails by name the moment the engine grows a kind that
// has no form here - so a new engine decision cannot silently hang a live
// browser game. Do not rename this constant without updating that test.

const DECISION_RENDERERS = {
  may: renderYesNoDecision("accept"),
  opening_hand_action: renderYesNoDecision("accept"),
  // CR 103.5's declaration, when it arrives as a decision rather than through
  // the pregame panel above - the admin-driven `pregame/settle` path.
  mulligan: renderYesNoDecision("accept"),
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
      <button class="primary" ${legal ? "" : "disabled"} onclick="decide({${field}: true})">Yes</button>
      <button onclick="decide({${field}: false})">No</button>
      ${legal ? "" : '<div class="hint">yes is not legal right now</div>'}
    `;
  };
}

function renderAmountDecision(field) {
  return (pending) => `
    <input type="number" id="decision-amount" min="0" max="${pending.amount}" value="0" />
    <div class="hint">up to ${pending.amount}</div>
    <button class="primary" onclick="decide({${field}: parseInt(document.getElementById('decision-amount').value || '0', 10)})">Pay</button>
  `;
}

function renderChoiceDecision(pending) {
  if (pending.options && pending.options.length) {
    return pending.options
      .map(
        (opt) =>
          `<button class="option" onclick='decide({choice: ${JSON.stringify(opt)}})'>${withSymbols(opt)}</button>`
      )
      .join("");
  }
  return `
    <input type="text" id="decision-choice" placeholder="type your answer" />
    <button class="primary" onclick="decide({choice: document.getElementById('decision-choice').value})">Choose</button>
  `;
}

function renderTopOrBottomDecision() {
  return `
    <button onclick="decide({top: true})">Top of library</button>
    <button onclick="decide({top: false})">Bottom of library</button>
  `;
}

function renderChooseCardsDecision(pending) {
  const boxes = (pending.card_details || [])
    .map(
      (c) => `<label class="option"><input type="checkbox" value="${escapeHtml(c.id)}" /> ${escapeHtml(c.name)}</label>`
    )
    .join("");
  return `
    <div>${boxes || "(no candidates shown)"}</div>
    <div class="hint">choose up to ${pending.amount}</div>
    <button class="primary" onclick="submitChooseCards()">Confirm</button>
  `;
}

function submitChooseCards() {
  decide({ card_ids: $$("#decision-box input[type=checkbox]:checked").map((el) => el.value) });
}

function renderCardSplitDecision(toGraveyard) {
  return (pending) => {
    const label = toGraveyard ? "to graveyard" : "to bottom";
    const boxes = (pending.card_details || [])
      .map(
        (c) =>
          `<label class="option"><input type="checkbox" value="${escapeHtml(c.id)}" /> ${escapeHtml(c.name)} <em>(${label})</em></label>`
      )
      .join("");
    return `
      <div>${boxes || "(nothing to look at)"}</div>
      <div class="hint">unchecked cards stay on top, in the order shown</div>
      <button class="primary" onclick="submitCardSplit()">Confirm</button>
    `;
  };
}

function submitCardSplit() {
  decide({
    moved: $$("#decision-box input[type=checkbox]:checked").map((el) => el.value),
    top_order: null,
  });
}

function renderChooseTargetsDecision(pending) {
  const max = pending.target_maximum;
  const min = pending.target_minimum;
  const boxes = (pending.targets || [])
    .map(
      (t) =>
        `<label class="option"><input type="checkbox" value="${escapeHtml(t.id)}" /> ` +
        `${escapeHtml(t.name || t.id)} <em>(${escapeHtml(t.kind)})</em></label>`
    )
    .join("");
  const range =
    max === null || max === undefined
      ? `at least ${min || 0} target(s)`
      : min === max
      ? `exactly ${max} target(s)`
      : `${min || 0} to ${max} target(s)`;
  return `
    <div>${boxes || "(no legal targets)"}</div>
    <div class="hint">${escapeHtml(range)}</div>
    <button class="primary" onclick="submitChooseTargets()">Confirm</button>
  `;
}

function submitChooseTargets() {
  decide({ targets: $$("#decision-box input[type=checkbox]:checked").map((el) => el.value) });
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
  const body = renderer
    ? renderer(pending)
    : `<div class="error">no UI handler for decision kind ${escapeHtml(pending.kind)} - this is a drift bug, see tests/test_ui_decision_coverage.py</div>`;
  setHtml(box, `<h3>${withSymbols(pending.prompt)}</h3>${body}`);
}

// ------------------------------------------------------------- pregame ----
//
// CR 103.5's mulligan process and CR 103.6's opening-hand actions. A game
// booted from decklists *rests* in these two steps before turn 1, so this is
// the first thing a human sees after "Start game": nothing on the board
// happens until both seats have declared.
//
// Every button here posts to one of the `pregame` endpoints (`app.py`), which
// never block - unlike every other question in this UI, there is no pending
// decision to answer and no timeout to race.

/** Which cards the player has ticked to put on the bottom when keeping. */
let pregameBottom = [];

function renderPregame(state, you) {
  const box = $("#pregame");
  const pregame = state.pregame;
  if (!pregame) {
    box.hidden = true;
    box.innerHTML = "";
    pregameBottom = [];
    return;
  }
  box.hidden = false;
  if (pregame.step === "mulligan") {
    setHtml(box, renderMulliganStep(state, pregame, you));
  } else {
    setHtml(box, renderOpeningHandStep(pregame, you));
  }
}

function renderMulliganStep(state, pregame, you) {
  const waiting = pregame.awaiting_mulligan || [];
  const taken = (pregame.mulligans_taken || {})[you] || 0;
  const taking = taken ? ` You have taken ${taken} mulligan${taken > 1 ? "s" : ""}.` : "";
  if (!waiting.includes(you)) {
    const others = waiting.filter((name) => name !== you);
    const who = others.length ? others.map(escapeHtml).join(", ") : "the other seat";
    return `<span>Mulligans (CR 103.5): waiting on ${who}.${taking}</span>${opponentSeatNote()}`;
  }
  const needed = (pregame.cards_to_bottom || {})[you] || 0;
  const hand = (state.hands && state.hands[you]) || [];
  const picker = needed
    ? `<div class="pregame-bottom">
         <span>Keeping costs ${needed} card${needed > 1 ? "s" : ""} on the bottom of your
         library - tick exactly ${needed}:</span>
         ${hand
           .map(
             (card) =>
               `<label><input type="checkbox" value="${escapeHtml(card.id)}"
                 ${pregameBottom.includes(card.id) ? "checked" : ""}
                 onchange="togglePregameBottom('${escapeHtml(card.id)}')" />
                 ${escapeHtml(card.name)}</label>`
           )
           .join("")}
       </div>`
    : "";
  const canKeep = pregameBottom.length === needed;
  const keepWhy = canKeep ? "" : ` title="tick exactly ${needed} card(s) first"`;
  const mullButton = pregame.can_mulligan && pregame.can_mulligan[you]
    ? `<button onclick="declareMulligan(true)">Mulligan</button>`
    : `<span class="why">no mulligan left - a hand of nothing is the floor (CR 103.5)</span>`;
  return `<span>Your opening hand (CR 103.5): keep it, or shuffle back for a new seven.${taking}</span>
    ${picker}
    <button class="primary"${canKeep ? "" : " disabled"}${keepWhy}
      onclick="declareMulligan(false)">Keep</button>
    ${mullButton}`;
}

function renderOpeningHandStep(pregame, you) {
  if (pregame.opening_hand_actor !== you) {
    const who = pregame.opening_hand_actor;
    return `<span>Opening-hand actions (CR 103.6): waiting on
      ${escapeHtml(who || "the other seat")}.</span>${opponentSeatNote()}`;
  }
  const options = (pregame.opening_hand_options || {})[you] || [];
  const buttons = options
    .map(
      (option) =>
        `<button onclick="takeOpeningHandAction('${escapeHtml(option.card_id)}')">
           ${escapeHtml(option.source)}: ${escapeHtml(option.prompt)}
         </button>`
    )
    .join("");
  const none = options.length
    ? ""
    : "<span class=\"why\">nothing in your opening hand offers one.</span>";
  return `<span>Opening-hand actions (CR 103.6) - yours to take, in any order.</span>
    ${none}${buttons}
    <button class="primary" onclick="finishOpeningHand()">Done</button>`;
}

// What the opponent's agent is doing, said in the pregame panel itself rather
// than only in the AI strip at the top - "waiting on claude" with no way to
// tell whether claude is alive is the panel that reads as broken. The server
// backstop (`ai_pregame.py`) moves a quiet bot seat along on its own, so this
// is a status line, never a dead end.
function opponentSeatNote() {
  const status = game.aiStatus;
  if (!status) return "";
  if (status.mechanism === "fallback" || status.state === "not_started") {
    return `<div class="why">Its agent is not running (${escapeHtml(
      status.reason || "not started"
    )}) - the server will make its pregame moves shortly.</div>`;
  }
  if (status.state === "error") {
    return `<div class="why">Its agent stopped with an error - the server will make its
      pregame moves shortly.</div>`;
  }
  if (status.state === "stopped") {
    return `<div class="why">Its agent has exited - the server will make its pregame moves
      shortly.</div>`;
  }
  return `<div class="why">Its agent is ${escapeHtml(status.state)}.</div>`;
}

function togglePregameBottom(cardId) {
  pregameBottom = pregameBottom.includes(cardId)
    ? pregameBottom.filter((id) => id !== cardId)
    : pregameBottom.concat([cardId]);
  render(game.state);
}
window.togglePregameBottom = togglePregameBottom;

async function pregamePost(path, body) {
  try {
    await api("POST", withUid(`/api/games/${game.gameId}/pregame/${path}`), body);
  } catch (e) {
    notice(`Refused: ${e.message}`, "bad");
  }
  poll();
}

async function declareMulligan(take) {
  const body = { player: game.state.you, take };
  if (!take && pregameBottom.length) body.bottom = pregameBottom;
  pregameBottom = [];
  await pregamePost("mulligan", body);
}
window.declareMulligan = declareMulligan;

async function takeOpeningHandAction(cardId) {
  await pregamePost("opening-hand/action", { player: game.state.you, card_id: cardId });
}
window.takeOpeningHandAction = takeOpeningHandAction;

async function finishOpeningHand() {
  await pregamePost("opening-hand/finish", { player: game.state.you });
}
window.finishOpeningHand = finishOpeningHand;

async function decide(answer) {
  const body = Object.assign({ player: game.state.you }, answer);
  try {
    await api("POST", withUid(`/api/games/${game.gameId}/decide`), body);
  } catch (e) {
    notice(`Decision refused: ${e.message}`, "bad");
  }
  poll();
}
window.decide = decide;
window.submitChooseCards = submitChooseCards;
window.submitCardSplit = submitCardSplit;
window.submitChooseTargets = submitChooseTargets;

// --------------------------------------------------- priority automation ----
//
// Feature 3: auto-pass until a *named* point in the turn - your upkeep, your
// second main, the opponent's end step, whatever - with an explicit stop
// whenever something lands on the stack. The run is driven one step per
// ordinary `poll()` tick, never its own unbounded loop, so `Stop` (or any
// stop condition) takes effect within one tick, never mid-flight.

/** Every stopping point the player can name, as `{step, who}` pairs.
 * `who` is "you" (the step in your own turn), "opponent" (the same step in
 * theirs), or "any" (whichever comes first). */
function autopilotTargets() {
  const out = [];
  for (const who of ["you", "opponent"]) {
    for (const entry of STEPS) {
      if (entry.step === "untap" || entry.step === "cleanup") continue;
      out.push({ step: entry.step, who, label: `${who === "you" ? "my" : "opponent's"} ${entry.label}` });
    }
  }
  return out;
}

function startAutopilot(step, who) {
  if (!game.state) return;
  game.autopilotStoppedReason = null;
  game.autopilot = {
    step,
    who: who || "you",
    // Kept for the shape the older single-target runs used, and read by
    // `autopilotStopReason` as the step name when `step` is absent.
    target: step,
    knownStackIds: new Set((game.state.stack || []).map((o) => o.id)),
  };
  renderAutopilotBanner();
}
window.startAutopilot = startAutopilot;

function rememberAutopilotTarget(value) {
  game.settings.autopilotTarget = value;
}
window.rememberAutopilotTarget = rememberAutopilotTarget;

function startAutopilotFromPicker() {
  const select = $("#autopilot-target");
  const value = (select && select.value) || game.settings.autopilotTarget;
  const [step, who] = value.split("|");
  startAutopilot(step, who);
}
window.startAutopilotFromPicker = startAutopilotFromPicker;

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

/**
 * Show a refusal or an error in the page, never in a native dialog.
 *
 * `alert()` and `confirm()` block the whole renderer thread. This page polls
 * the server every ~1.2s and drives an AI seat alongside it, so a native
 * dialog freezes the board behind it and stacks up ticks until it is
 * dismissed - and while one is open the page is genuinely unresponsive, not
 * merely busy. Every refusal path goes through here instead.
 */
function notice(message, kind) {
  const box = $("#notice");
  if (!box) return;
  box.hidden = false;
  box.className = kind === "bad" ? "notice bad" : "notice";
  box.innerHTML = `<span>${escapeHtml(message)}</span>
    <button class="ghost" onclick="dismissNotice()">Dismiss</button>`;
}
window.notice = notice;

function dismissNotice() {
  const box = $("#notice");
  box.hidden = true;
  box.innerHTML = "";
}
window.dismissNotice = dismissNotice;

function renderAutopilotBanner() {
  const banner = $("#autopilot-banner");
  if (game.autopilot) {
    banner.hidden = false;
    banner.innerHTML = `
      <span>Auto-passing until ${escapeHtml(autopilotLabel(game.autopilot))}…</span>
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

/** How a running autopilot describes its own destination. */
function autopilotLabel(autopilot) {
  const step = (autopilot.step || autopilot.target || "").replace(/_/g, " ");
  const who = autopilot.who || "you";
  if (who === "you") return `your ${step}`;
  if (who === "opponent") return `your opponent's ${step}`;
  return `the ${step} step`;
}

/**
 * The stop-condition check for a running autopilot, as a pure function of
 * (state, autopilot, you, settings) - no DOM, no fetch, no mutation of
 * `game`. Pulled out of `tickAutopilot` on purpose: these are exactly the
 * paths that cost games if they regress, so they are exercised by a real
 * test running this exact function under Node - see
 * `tests/test_ui_autopilot.py`.
 *
 * Returns a human-readable stop reason, or `null` to keep running. Checked
 * in order: the game ending, a decision addressed to this seat, the named
 * destination being reached, then (gated by `settings.stopOnStack`, on by
 * default) a new object having arrived on the stack, which covers "the
 * opponent cast something" and "a trigger went on the stack" identically -
 * from this seat's point of view both are just "something new is here".
 *
 * `autopilot.step` names the step to stop at and `autopilot.who` says whose
 * turn it must be ("you", "opponent", or "any"). `autopilot.target` is
 * accepted as the step name when `step` is absent, and `who` defaults to
 * "you", so a run built the older way still stops where it always did.
 */
function autopilotStopReason(state, autopilot, you, settings) {
  if (!autopilot) return null;
  if (state.winner || state.drawn) {
    return state.winner ? `game over - ${state.winner} won` : "game over - drawn";
  }
  if (state.pending_decision && state.pending_decision.player === you) {
    return `a decision is waiting for you: ${state.pending_decision.prompt}`;
  }
  const wantedStep = autopilot.step || autopilot.target;
  const who = autopilot.who || "you";
  if (wantedStep && state.step === wantedStep) {
    const isYours = state.active_player === you;
    const whoMatches = who === "any" || (who === "you") === isYours;
    if (whoMatches) {
      return `reached ${autopilotLabel(autopilot)}`;
    }
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
 * why - the stop condition for "any command is refused". `null` means the
 * pass went through cleanly. */
function interpretPassOutcome(res) {
  if (res && res.allowed === false) {
    return `pass was refused: ${res.reason}`;
  }
  return null;
}

/** One step of a running autopilot - called from every `poll()` tick. Does
 * exactly one thing (check, then at most one pass call) and returns; the
 * loop is `poll()`'s own interval, so `Stop` is never more than one tick
 * from taking effect. */
async function tickAutopilot(state) {
  if (!game.autopilot) return;
  const you = state.you;

  const stopReason = autopilotStopReason(state, game.autopilot, you, game.settings);
  if (stopReason) {
    stopAutopilot(stopReason);
    return;
  }
  game.autopilot.knownStackIds = new Set((state.stack || []).map((o) => o.id));

  if (state.priority_holder !== you) return; // not ours to act on this tick
  try {
    const res = await api("POST", `/api/games/${game.gameId}/actions/pass-priority`, {
      player: you,
    });
    const refusalReason = interpretPassOutcome(res);
    if (refusalReason) stopAutopilot(refusalReason);
  } catch (e) {
    stopAutopilot(`pass failed: ${e.message}`);
  }
}

// Node-loadable for tests (`tests/test_ui_autopilot.py`,
// `tests/test_ui_targets.py`, `tests/test_ui_pregame_panel.py`) - a no-op in
// the browser, where `module` does not exist. `sendTargets` is defined
// further down the file; a function declaration is hoisted, so naming it here
// is fine.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { autopilotStopReason, interpretPassOutcome, sendTargets, setHtml };
}

// -------------------------------------------------------- the action tray ----
//
// Feature 1: ask for exactly what this action needs and nothing else. The
// tray is a small state machine over `game.action`:
//
//   {kind, cardId, abilityIndex, keyword, modes, xValue, targets, affordance}
//
// `affordance` is the server's answer (`GET .../affordances/{card_id}`) about
// what this announcement requires: whether it targets, how many, and exactly
// which objects on the board are legal right now. Everything the tray shows
// is derived from it. A spell with no X gets no X field; a spell that is not
// modal gets no mode picker; a spell that does not target gets no target
// step and no dimmed board.

function beginAction(kind, cardId, extra) {
  game.action = Object.assign(
    {
      kind, // "cast" | "land" | "activate" | "special"
      cardId,
      abilityIndex: null,
      keyword: null,
      modes: [],
      xValue: null,
      color: null, // chosen colour for a mana ability that offers one
      convoke: [], // creatures tapped for convoke (CR 702.51a)
      delve: [], // graveyard cards exiled for delve (CR 702.66a)
      crewing: [], // creatures tapped to crew a Vehicle (CR 702.122a)
      altCost: null, // chosen CR 118.9 alternative cost index, or null
      choosing: [], // objects supplied to pay it (pitched cards, sacrifices)
      targets: [],
      touchedTargets: false, // has the player engaged the target picker at all?
      affordance: null,
      error: null,
    },
    extra || {}
  );
  refreshAffordance();
  render(game.state);
}

function cancelAction() {
  game.action = null;
  render(game.state);
}
window.cancelAction = cancelAction;

/** Ask the server what this announcement can legally target right now. Called
 * whenever anything that changes the answer changes: the ability picked, the
 * modes chosen (CR 700.2f), or X announced (CR 107.3a). */
async function refreshAffordance() {
  const action = game.action;
  if (!action || action.kind === "land" || action.kind === "special") return;
  const params = new URLSearchParams({ player: game.state.you });
  if (action.abilityIndex !== null) params.set("ability_index", String(action.abilityIndex));
  if (action.modes.length) params.set("modes", action.modes.join(","));
  if (action.xValue !== null && action.xValue !== "") params.set("x_value", String(action.xValue));
  try {
    const result = await api(
      "GET",
      `/api/games/${game.gameId}/affordances/${action.cardId}?${params}`
    );
    if (game.action === action) {
      action.affordance = result;
      // A target already chosen that this new answer no longer offers (the
      // mode changed, X changed) is dropped rather than silently sent.
      const legal = new Set((result.candidates || []).map((c) => c.id));
      action.targets = action.targets.filter((id) => legal.has(id));
      render(game.state);
    }
  } catch (e) {
    if (game.action === action) {
      action.error = e.message;
      render(game.state);
    }
  }
}

function actionCard() {
  if (!game.action) return null;
  return game.cardLookup[game.action.cardId] || null;
}

/** The modes this card offers, from its own `cast` hints. */
function actionModes() {
  const card = actionCard();
  return card && card.cast && card.cast.modes ? card.cast.modes : null;
}

/** Whether this action needs an X announced (CR 107.3a) - a spell whose mana
 * cost has one, or an ability whose activation cost does. */
function actionNeedsX() {
  const action = game.action;
  const card = actionCard();
  if (!action || !card) return false;
  if (action.abilityIndex !== null) {
    const ability = (card.abilities || [])[action.abilityIndex];
    return !!(ability && ability.needs_x);
  }
  return !!(card.cast && card.cast.needs_x);
}

/** The targeting answer governing the action being built. Prefers the live
 * affordance (which accounts for the chosen modes and X); falls back to the
 * card's own cheap hints before the first fetch lands. */
function actionTargeting() {
  const action = game.action;
  if (!action) return null;
  if (action.affordance) return action.affordance;
  const card = actionCard();
  if (!card) return null;
  if (action.abilityIndex !== null) {
    const ability = (card.abilities || [])[action.abilityIndex];
    return ability ? ability.targeting : null;
  }
  return card.cast ? card.cast.targeting : null;
}

function isPickingTargets() {
  const targeting = actionTargeting();
  return !!(targeting && targeting.targets_anything);
}

function isTargetCandidate(id) {
  const action = game.action;
  if (!action || !action.affordance) return false;
  return (action.affordance.candidates || []).some((c) => c.id === id);
}

function isChosenTarget(id) {
  return !!(game.action && game.action.targets.includes(id));
}

function toggleTarget(id) {
  const action = game.action;
  if (!action) return;
  action.touchedTargets = true;
  const index = action.targets.indexOf(id);
  if (index >= 0) {
    action.targets.splice(index, 1);
  } else {
    const max = action.affordance ? action.affordance.maximum : null;
    // CR 601.2c: no more than the announcement takes. Choosing one past the
    // maximum replaces the oldest rather than silently doing nothing, which
    // is what a player retargeting a one-target spell actually means.
    if (max !== null && max !== undefined && action.targets.length >= max) {
      action.targets.shift();
    }
    action.targets.push(id);
  }
  render(game.state);
}
window.toggleTarget = toggleTarget;

function toggleMode(number) {
  const action = game.action;
  const modes = actionModes();
  if (!action || !modes) return;
  const index = action.modes.indexOf(number);
  if (index >= 0) {
    action.modes.splice(index, 1);
  } else {
    if (action.modes.length >= modes.maximum) action.modes.shift();
    action.modes.push(number);
  }
  refreshAffordance();
  render(game.state);
}
window.toggleMode = toggleMode;

function setXValue(value) {
  if (!game.action) return;
  game.action.xValue = value === "" ? null : parseInt(value, 10);
  refreshAffordance();
}
window.setXValue = setXValue;

/**
 * The `targets` field for a cast or activate request - and the single place
 * in this file allowed to build it.
 *
 * `undefined` means "the player has not chosen", which makes the engine ask
 * (CR 601.2c through CR 602.2b, as a `choose_targets` decision). `[]` means
 * "the player chose none", a real and legal answer for an "up to N"
 * requirement (CR 115.6) and a wrong one for anything else. Collapsing the
 * first into the second is the bug that put Karn, the Great Creator's +1 on
 * the stack targeting nothing every time it was activated from this UI.
 *
 * So: send what the player picked if they picked anything; send `[]` only
 * when they engaged the picker and deliberately left it empty on a
 * requirement whose minimum is zero; otherwise send nothing and let the
 * engine ask.
 */
function sendTargets(action) {
  if (action.targets.length) return action.targets;
  const targeting = action.affordance;
  if (!targeting || !targeting.targets_anything) return undefined;
  if (action.touchedTargets && targeting.minimum === 0) return [];
  return undefined;
}

function trayStepHtml(label, body, satisfied) {
  return `<div class="tray-step ${satisfied ? "satisfied" : ""}">
    <div class="label">${escapeHtml(label)}</div>${body}</div>`;
}

function renderActionTray() {
  const tray = $("#action-tray");
  const action = game.action;
  const card = actionCard();
  if (!action || !card) {
    tray.hidden = true;
    tray.innerHTML = "";
    return;
  }
  tray.hidden = false;

  const parts = [];
  const ability =
    action.abilityIndex !== null ? (card.abilities || [])[action.abilityIndex] : null;

  const title =
    action.kind === "land"
      ? "Play land"
      : action.kind === "crew"
      ? "Crew"
      : action.kind === "special"
      ? `${action.keyword}`
      : ability
      ? "Activate"
      : "Cast";
  parts.push(
    `<h3>${escapeHtml(title)}: ${escapeHtml(card.name)}` +
      (card.mana_cost && !ability ? ` ${withSymbols(card.mana_cost)}` : "") +
      `</h3>`
  );
  if (ability) {
    parts.push(
      `<div class="hint">${ability.loyalty ? `<strong>${escapeHtml(ability.loyalty.label)}</strong> ` : ""}${withSymbols(ability.text)}</div>`
    );
  }

  // --- modes (CR 700.2), only for a modal card ---
  const modes = actionModes();
  if (modes && action.kind === "cast") {
    const chips = modes.options
      .map(
        (m) =>
          `<button class="chip ${action.modes.includes(m.number) ? "on" : ""}" onclick="toggleMode(${m.number})">${m.number}. ${withSymbols(m.text)}</button>`
      )
      .join("");
    const enough = action.modes.length >= modes.minimum;
    parts.push(
      trayStepHtml(
        `${modes.instruction} (${modes.minimum === modes.maximum ? modes.minimum : `${modes.minimum}-${modes.maximum}`})`,
        `<div class="chip-row">${chips}</div>`,
        enough
      )
    );
  }

  // --- which colour (CR 106.1b), only for a source that offers a choice ---
  if (ability && ability.mana_options) {
    const chips = ability.mana_options
      .map(
        (color) =>
          `<button class="chip ${action.color === color ? "on" : ""}" onclick="chooseColor('${color}')">${symbolHtml(`{${color}}`, true)}</button>`
      )
      .join("");
    parts.push(
      trayStepHtml("Which colour", `<div class="chip-row">${chips}</div>`, action.color !== null)
    );
  }

  // --- convoke / delve / crew: paying a cost with objects, not mana ---
  const objectCosts = (action.affordance && action.affordance.object_costs) || {};
  if (action.kind === "crew" && objectCosts.crew) {
    parts.push(renderCrewStep(action, objectCosts.crew));
  } else if (action.kind === "cast") {
    if (objectCosts.convoke) {
      parts.push(renderObjectCostStep(action, objectCosts.convoke, "convoke", "Convoke - tap creatures to pay"));
    }
    if (objectCosts.delve) {
      parts.push(renderObjectCostStep(action, objectCosts.delve, "delve", "Delve - exile from your graveyard"));
    }
  }

  // --- alternative costs (CR 118.9), only when this card offers one ---
  const alts = (action.affordance && action.affordance.alternative_costs) || [];
  if (alts.length && action.kind === "cast") {
    parts.push(renderAlternativeCostStep(action, alts));
  }

  // --- X (CR 107.3a), only when the cost has one ---
  if (actionNeedsX()) {
    parts.push(
      trayStepHtml(
        "Announce X",
        `<input type="number" min="0" value="${action.xValue === null ? "" : action.xValue}" onchange="setXValue(this.value)" placeholder="X" />`,
        action.xValue !== null
      )
    );
  }

  // --- targets (CR 601.2c), only when it targets ---
  const targeting = actionTargeting();
  if (targeting && targeting.targets_anything) {
    parts.push(renderTargetStep(action, targeting));
  }

  // --- keep priority (CR 117.3c), only for something going on the stack ---
  if (action.kind === "cast" || (ability && !ability.is_mana_ability)) {
    parts.push(`<label class="hint">
      <input type="checkbox" ${game.settings.holdPriority ? "checked" : ""} onchange="game.settings.holdPriority = this.checked" />
      keep priority afterwards (CR 117.3c) - otherwise it auto-passes so this resolves
    </label>`);
  }

  parts.push(
    `<div class="chip-row" style="margin-top:10px">
      <button class="primary" onclick="confirmAction()">${escapeHtml(title)}</button>
      <button onclick="cancelAction()">Cancel</button>
    </div>`
  );
  if (action.error) parts.push(`<div class="error">${escapeHtml(action.error)}</div>`);
  setHtml(tray, parts.join(""));
}

function renderTargetStep(action, targeting) {
  if (!targeting.understood) {
    // The engine's parser could not read this card's targeting. Say so
    // rather than showing an empty list a player would read as "nothing is
    // legal" - and let them click the board freely, since the engine will
    // still refuse an illegal announcement with its own reason.
    return trayStepHtml(
      "Targets",
      `<div class="hint">the engine could not read this card's targeting - click objects on the board to name them, and it will refuse anything illegal</div>
       <div class="chip-row">${action.targets.map((id) => `<button class="chip on" onclick="toggleTarget('${escapeHtml(id)}')">${escapeHtml(nameOf(id))} ✕</button>`).join("")}</div>`,
      false
    );
  }
  const min = targeting.minimum;
  const max = targeting.maximum;
  const range =
    max === null || max === undefined
      ? `${min} or more`
      : min === max
      ? `exactly ${min}`
      : `${min} to ${max}`;
  const phrases = (targeting.requirements || []).map((r) => r.phrase).join(", ");
  const candidates = action.affordance ? action.affordance.candidates || [] : [];
  const chips = candidates
    .map(
      (c) =>
        `<button class="chip ${c.kind === "player" ? "player" : ""} ${action.targets.includes(c.id) ? "on" : ""}" onclick="toggleTarget('${escapeHtml(c.id)}')">${escapeHtml(c.name)}</button>`
    )
    .join("");
  const body = candidates.length
    ? `<div class="chip-row">${chips}</div>`
    : action.affordance
    ? '<div class="hint">nothing on the board is a legal target right now</div>'
    : '<div class="hint">checking…</div>';
  const note =
    min === 0
      ? `<div class="hint">${escapeHtml(phrases)} - ${escapeHtml(range)}; choosing none is legal (CR 115.6)</div>`
      : `<div class="hint">${escapeHtml(phrases)} - ${escapeHtml(range)}</div>`;
  const satisfied = action.targets.length >= min;
  return trayStepHtml(`Targets (${action.targets.length}/${range})`, body + note, satisfied);
}

/** CR 118.9's alternative costs. Every option the engine found is shown,
 * including the ones that cannot be announced right now - CR 118.9b makes
 * "this card has no pitch cost" and "it has one but it is not your turn"
 * different answers, and a player shown only the legal ones cannot tell them
 * apart. An option that needs objects supplied (Force of Negation exiling a
 * blue card, Fireblast sacrificing two Mountains) opens a picker for them. */
function renderAlternativeCostStep(action, alts) {
  const chips = alts
    .map((alt) => {
      const label = `${alt.keyword || alt.source_name}: ${alt.description}`;
      const on = action.altCost === alt.index;
      return alt.available
        ? `<button class="chip ${on ? "on" : ""}" onclick="chooseAlternativeCost(${alt.index})" title="${escapeHtml(alt.rule)}">${escapeHtml(label)}</button>`
        : `<button class="chip" disabled title="${escapeHtml(alt.unavailable || "")}">${escapeHtml(label)} ✕</button>`;
    })
    .join("");
  const chosen = alts.find((a) => a.index === action.altCost);
  let picker = "";
  if (chosen && chosen.requires) {
    // The engine does not enumerate which of your own cards may pay a
    // pitch cost, so this offers your hand and lets the engine refuse with
    // its own reason - the same posture the unreadable-targeting branch takes.
    const hand = ((game.state.hands || {})[game.state.you] || []).filter(
      (c) => c.id !== action.cardId
    );
    picker =
      `<div class="hint">pay it with ${chosen.requires} card(s) from your hand:</div>` +
      `<div class="chip-row">${hand
        .map(
          (c) =>
            `<button class="chip ${action.choosing.includes(c.id) ? "on" : ""}" onclick="toggleChoosing('${escapeHtml(c.id)}')">${escapeHtml(c.name)}</button>`
        )
        .join("")}</div>`;
  }
  const satisfied =
    action.altCost !== null && (!chosen || action.choosing.length >= (chosen.requires || 0));
  return trayStepHtml(
    "Alternative cost (CR 118.9) - optional",
    `<div class="chip-row">${chips}</div>${picker}` +
      (action.altCost !== null
        ? `<div class="hint"><button class="chip" onclick="chooseAlternativeCost(null)">pay the mana cost instead</button></div>`
        : ""),
    satisfied
  );
}

/** Convoke and delve: each chosen object covers one symbol of an
 * already-determined cost (CR 702.51b, CR 702.66a), so this is a plain
 * multi-select over whatever the engine says is eligible. Neither is a cost
 * reduction nor an alternative cost, which is why this sits alongside the
 * mana payment rather than replacing it. */
function renderObjectCostStep(action, offer, field, label) {
  const chips = offer.candidates
    .map(
      (candidate) =>
        `<button class="chip ${action[field].includes(candidate.id) ? "on" : ""}" onclick="toggleObjectCost('${field}','${escapeHtml(candidate.id)}')">${escapeHtml(candidate.name)}</button>`
    )
    .join("");
  const body = offer.candidates.length
    ? `<div class="chip-row">${chips}</div>`
    : '<div class="hint">nothing eligible right now</div>';
  return trayStepHtml(
    `${label} (${action[field].length} chosen) - optional`,
    body + `<div class="hint">${escapeHtml(offer.rule)}</div>`,
    action[field].length > 0
  );
}

/** Crew (CR 702.122a): unlike convoke and delve this is not optional padding
 * on a mana cost - the creatures ARE the cost, and the total power has to
 * reach the Vehicle's crew number before the ability can be activated at
 * all, so the step shows the running total against it. */
function renderCrewStep(action, offer) {
  const chosen = offer.candidates.filter((c) => action.crewing.includes(c.id));
  const total = chosen.reduce((sum, c) => sum + (c.power || 0), 0);
  const chips = offer.candidates
    .map(
      (candidate) =>
        `<button class="chip ${action.crewing.includes(candidate.id) ? "on" : ""}" onclick="toggleObjectCost('crewing','${escapeHtml(candidate.id)}')">${escapeHtml(candidate.name)} (${candidate.power})</button>`
    )
    .join("");
  const body = offer.candidates.length
    ? `<div class="chip-row">${chips}</div>`
    : '<div class="hint">no other untapped creature you control can crew this</div>';
  return trayStepHtml(
    `Crew ${offer.number} - total power ${total}/${offer.number}`,
    body + `<div class="hint">${escapeHtml(offer.rule)}</div>`,
    total >= (offer.number || 0)
  );
}

function toggleObjectCost(field, id) {
  const action = game.action;
  if (!action) return;
  const at = action[field].indexOf(id);
  if (at >= 0) action[field].splice(at, 1);
  else action[field].push(id);
  render(game.state);
}
window.toggleObjectCost = toggleObjectCost;

function chooseColor(color) {
  if (!game.action) return;
  game.action.color = game.action.color === color ? null : color;
  render(game.state);
}
window.chooseColor = chooseColor;

function chooseAlternativeCost(index) {
  if (!game.action) return;
  game.action.altCost = game.action.altCost === index ? null : index;
  game.action.choosing = [];
  render(game.state);
}
window.chooseAlternativeCost = chooseAlternativeCost;

function toggleChoosing(id) {
  const action = game.action;
  if (!action) return;
  const at = action.choosing.indexOf(id);
  if (at >= 0) action.choosing.splice(at, 1);
  else action.choosing.push(id);
  render(game.state);
}
window.toggleChoosing = toggleChoosing;

function nameOf(id) {
  const card = game.cardLookup[id];
  return card ? card.name : id;
}

async function confirmAction() {
  const action = game.action;
  if (!action) return;
  action.error = null;
  const you = game.state.you;
  const hold = game.settings.holdPriority;
  let goesOnStack = false;
  try {
    if (action.kind === "land") {
      const res = await api("POST", `/api/games/${game.gameId}/actions/play-land`, {
        player: you,
        card_id: action.cardId,
      });
      if (res.allowed === false) return failAction(res.reason);
    } else if (action.kind === "special") {
      const path = action.keyword.toLowerCase();
      const res = await api("POST", `/api/games/${game.gameId}/actions/${path}`, {
        player: you,
        card_id: action.cardId,
      });
      if (res.allowed === false) return failAction(res.reason);
    } else if (action.kind === "crew") {
      const res = await api("POST", `/api/games/${game.gameId}/actions/crew`, {
        player: you,
        card_id: action.cardId,
        crewing: action.crewing,
      });
      if (res.allowed === false) return failAction(res.reason);
      goesOnStack = true;
    } else if (action.abilityIndex !== null) {
      const body = { player: you, card_id: action.cardId, ability_index: action.abilityIndex };
      if (action.color) body.color = action.color;
      const targets = sendTargets(action);
      if (targets !== undefined) body.targets = targets;
      if (action.xValue !== null) body.x_value = action.xValue;
      const res = await api("POST", `/api/games/${game.gameId}/actions/activate`, body);
      if (res.allowed === false) return failAction(res.reason);
      goesOnStack = !res.mana_ability;
    } else {
      const body = { player: you, card_id: action.cardId };
      const targets = sendTargets(action);
      if (targets !== undefined) body.targets = targets;
      if (action.modes.length) body.modes = action.modes;
      if (action.xValue !== null) body.x_value = action.xValue;
      if (action.keyword) body.keyword = action.keyword;
      if (action.convoke.length) {
        body.convoke = action.convoke.map((id) => ({ card_id: id }));
      }
      if (action.delve.length) body.delve = action.delve;
      if (action.altCost !== null) body.alternative_cost = action.altCost;
      if (action.choosing.length) body.choosing = action.choosing;
      if (action.fromZone) body.from_zone = action.fromZone;
      const res = await api("POST", `/api/games/${game.gameId}/actions/cast`, body);
      if (res.allowed === false) return failAction(res.reason);
      goesOnStack = true;
    }
  } catch (e) {
    return failAction(e.message);
  }
  game.action = null;
  // CR 117.3c: a player who acted keeps priority and may act again. Releasing
  // it is what makes the spell actually resolve, so that is the default and
  // "keep priority" is the opt-out.
  if (goesOnStack && !hold) passPriority();
  poll();
}
window.confirmAction = confirmAction;

function failAction(reason) {
  if (game.action) game.action.error = reason;
  render(game.state);
}

// -------------------------------------------------------------- clicks ----

function onHandCardClick(cardId) {
  // While a target picker is open, a hand card is not a target of anything
  // this engine models - clicking one swaps to that card instead.
  const card = game.cardLookup[cardId];
  if (!card) return;
  if (game.action && game.action.cardId === cardId) {
    cancelAction();
    return;
  }
  const isLand = (card.type_line || "").toLowerCase().includes("land");
  beginAction(isLand ? "land" : "cast", cardId);
}

function onBoardCardClick(cardId) {
  if (isPickingTargets() && (isTargetCandidate(cardId) || isChosenTarget(cardId))) {
    toggleTarget(cardId);
    return;
  }
  const card = game.cardLookup[cardId];
  if (!card) return;
  if (card.controller !== game.state.you) return; // not yours to activate
  const abilities = card.abilities || [];
  // Crew is an activated ability of the Vehicle (CR 702.122a) but carries no
  // ability index - its cost is the creatures - so it is offered alongside
  // the printed abilities rather than being one of them. The type line is a
  // display hint only; `crew_activation` is what actually decides, and it
  // refuses with its own reason.
  const isVehicle = (card.type_line || "").toLowerCase().includes("vehicle");
  if (!abilities.length && !isVehicle) return;
  if (!abilities.length && isVehicle) {
    beginAction("crew", cardId);
    return;
  }
  if (abilities.length === 1 && !isVehicle) {
    beginAction("activate", cardId, { abilityIndex: 0 });
    return;
  }
  openAbilityPicker(card, isVehicle);
}

function openAbilityPicker(card, isVehicle) {
  const crewRow = isVehicle
    ? `<button class="option" onclick="chooseCrew('${escapeHtml(card.id)}')"><strong>Crew</strong> - tap creatures with enough total power (CR 702.122a)</button>`
    : "";
  const rows = crewRow + (card.abilities || [])
    .map(
      (a) =>
        `<button class="option" onclick="chooseAbility('${escapeHtml(card.id)}', ${a.index})">` +
        (a.loyalty ? `<strong>${escapeHtml(a.loyalty.label)}</strong> ` : "") +
        `${withSymbols(a.text)}${a.is_mana_ability ? " <em>(mana)</em>" : ""}` +
        (a.targeting && a.targeting.targets_anything ? " <em>(targets)</em>" : "") +
        `</button>`
    )
    .join("");
  showModal(`<h3>Activate: ${escapeHtml(card.name)}</h3>${rows}
    <button onclick="closeModal()">Cancel</button>`);
}

function chooseCrew(cardId) {
  closeModal();
  beginAction("crew", cardId);
}
window.chooseCrew = chooseCrew;

function chooseAbility(cardId, index) {
  closeModal();
  beginAction("activate", cardId, { abilityIndex: index });
}
window.chooseAbility = chooseAbility;

// ------------------------------------------------------------ action bar ----

function renderActionBar(state, you) {
  const bar = $("#action-bar");
  const isYourPriority = state.priority_holder === you;
  const isActive = state.active_player === you;
  const groups = [];

  // -- priority --
  const targets = autopilotTargets();
  const current = game.autopilot
    ? `${game.autopilot.step}|${game.autopilot.who}`
    : game.settings.autopilotTarget;
  groups.push(`<div class="group"><div class="label">Priority</div>
    <div class="pass-controls">
      <div class="row">
        <button class="primary" ${isYourPriority ? "" : "disabled"} onclick="passPriority()">Pass priority</button>
        ${state.stack.length ? '<button onclick="resolveTop()">Resolve top</button>' : ""}
      </div>
      <div class="row">
        <select id="autopilot-target" onchange="rememberAutopilotTarget(this.value)">
          ${targets
            .map(
              (t) =>
                `<option value="${t.step}|${t.who}" ${`${t.step}|${t.who}` === current ? "selected" : ""}>${escapeHtml(t.label)}</option>`
            )
            .join("")}
        </select>
        <button ${game.autopilot ? "disabled" : ""} onclick="startAutopilotFromPicker()">Auto-pass until…</button>
      </div>
      <label title="Stop and ask whenever a spell or a triggered ability lands on the stack while auto-passing - the safe default. Turn it off to run through the opponent's whole turn.">
        <input type="checkbox" ${game.settings.stopOnStack ? "checked" : ""} onchange="toggleStopOnStackSetting(this.checked)" />
        stop when anything lands on the stack
      </label>
    </div></div>`);

  // -- turn / combat, each button shown only in the step it belongs to --
  const turnButtons = [];
  if (isActive && (state.step === "untap" || state.step === "cleanup")) {
    turnButtons.push(`<button onclick="advanceTurn()">Advance (${escapeHtml(state.step)})</button>`);
  }
  if (state.step === "declare_attackers" && isActive) {
    turnButtons.push(`<button class="primary" onclick="openAttackModal()">Declare attackers</button>`);
  }
  if (state.step === "declare_blockers" && !isActive) {
    turnButtons.push(`<button class="primary" onclick="openBlockModal()">Declare blockers</button>`);
  }
  if (turnButtons.length) {
    groups.push(
      `<div class="group"><div class="label">This step</div><div class="buttons">${turnButtons.join("")}</div></div>`
    );
  }

  // -- special actions on cards in hand (CR 116.2): suspend, plot, foretell,
  //    and cycling. Each appears only for a card that can actually take it
  //    right now - `serialize._special_actions` builds the engine's own
  //    action object to find out, so a card that cannot is simply absent.
  const hand = (state.hands && state.hands[you]) || [];
  const specials = [];
  for (const card of hand) {
    const actions = card.special_actions || {};
    for (const keyword of ["Suspend", "Plot", "Foretell"]) {
      const entry = actions[keyword.toLowerCase()];
      if (!entry) continue;
      specials.push(
        `<button onclick="beginSpecial('${escapeHtml(card.id)}','${keyword}')" title="${escapeHtml(card.name)}">${keyword}: ${escapeHtml(card.name)} ${withSymbols(entry.cost)}</button>`
      );
    }
    for (const cycler of actions.cycling || []) {
      specials.push(
        `<button onclick="cycleCard('${escapeHtml(card.id)}', ${cycler.index})">${escapeHtml(cycler.keyword)}: ${escapeHtml(card.name)} ${withSymbols(cycler.cost)}</button>`
      );
    }
  }
  if (specials.length) {
    groups.push(
      `<div class="group"><div class="label">Special actions (CR 116.2)</div><div class="buttons">${specials.join("")}</div></div>`
    );
  }

  // -- CR 601.3 play permissions: a card you may cast from somewhere other
  //    than your hand right now (cascade's exiled hit, suspend's free cast).
  //    A real game lost one of these because nothing named it anywhere.
  const permissions = (state.play_permissions && state.play_permissions[you]) || [];
  const usable = permissions.filter((p) => p.usable_now);
  if (permissions.length) {
    const rows = permissions.map((p) => {
      const label = `${p.card.name} (${p.granted_by})`;
      return p.usable_now
        ? `<button class="primary" onclick="castFromPermission('${escapeHtml(p.card.id)}','${escapeHtml(p.from_zone)}')" title="${escapeHtml(p.expires)}">${escapeHtml(label)}</button>`
        : `<button disabled title="${escapeHtml(p.why_not || "")}">${escapeHtml(label)}</button>`;
    });
    groups.push(
      `<div class="group"><div class="label">You may cast (CR 601.3) — ${usable.length} now</div><div class="buttons">${rows.join("")}</div></div>`
    );
  }

  groups.push(`<div class="group"><div class="label">Game</div><div class="buttons">
    <button onclick="runStateBasedActions()" title="Run state-based actions by hand (CR 704) - the engine does this on its own; this is a debugging handle">Check SBAs</button>
    <button onclick="openReportModal()">Report a problem</button>
    <button class="danger" onclick="concede()">Concede</button>
  </div></div>`);

  setHtml(bar, groups.join(""));
}

async function simpleAction(path, body, label) {
  try {
    const res = await api("POST", withUid(`/api/games/${game.gameId}/${path}`), body);
    if (res && res.allowed === false) notice(`${label} refused: ${res.reason}`, "bad");
    return res;
  } catch (e) {
    notice(`${label} failed: ${e.message}`, "bad");
  } finally {
    poll();
  }
}

function passPriority() {
  return simpleAction("actions/pass-priority", { player: game.state.you }, "pass");
}
window.passPriority = passPriority;

function advanceTurn() {
  return simpleAction("advance", { player: game.state.you }, "advance");
}
window.advanceTurn = advanceTurn;

function resolveTop() {
  return simpleAction("resolve-top", { player: game.state.you }, "resolve");
}
window.resolveTop = resolveTop;

function runStateBasedActions() {
  return simpleAction("state-based-actions", { player: game.state.you }, "state-based actions");
}
window.runStateBasedActions = runStateBasedActions;

function cycleCard(cardId, index) {
  return simpleAction(
    "actions/cycle",
    { player: game.state.you, card_id: cardId, ability_index: index },
    "cycling"
  );
}
window.cycleCard = cycleCard;

function beginSpecial(cardId, keyword) {
  beginAction("special", cardId, { keyword });
}
window.beginSpecial = beginSpecial;

function castFromPermission(cardId, fromZone) {
  beginAction("cast", cardId, { fromZone });
}
window.castFromPermission = castFromPermission;

function concede() {
  // An irreversible action, so it is confirmed - but in the page, not behind
  // a blocking `confirm()`.
  showModal(`<h3>Concede this game?</h3>
    <p class="hint">You leave the game immediately (CR 104.3a). This cannot be undone.</p>
    <div class="chip-row">
      <button class="danger" onclick="confirmConcede()">Concede</button>
      <button onclick="closeModal()">Cancel</button>
    </div>`);
}
window.concede = concede;

async function confirmConcede() {
  closeModal();
  await simpleAction("actions/concede", { player: game.state.you }, "concede");
}
window.confirmConcede = confirmConcede;

function openReportModal() {
  showModal(`<h3>Report a problem</h3>
    <p class="hint">Recorded in this game's audit log, where the engine's own developers read it. Say what you expected and what happened.</p>
    <textarea id="report-text" rows="6"></textarea>
    <div class="chip-row"><button class="primary" onclick="submitReport()">Send</button>
    <button onclick="closeModal()">Cancel</button></div>`);
}
window.openReportModal = openReportModal;

async function submitReport() {
  const text = $("#report-text").value.trim();
  if (!text) return closeModal();
  await simpleAction("report", { player: game.state.you, text }, "report");
  closeModal();
}
window.submitReport = submitReport;

// ------------------------------------------------------------- modals ----

function showModal(html) {
  const modal = $("#modal");
  modal.hidden = false;
  modal.innerHTML = `<div class="modal-inner">${html}</div>`;
}

function closeModal() {
  const modal = $("#modal");
  modal.hidden = true;
  modal.innerHTML = "";
}
window.closeModal = closeModal;

function openZonesModal() {
  const state = game.state;
  const you = state.you;
  const zone = (cards, empty) =>
    cards && cards.length
      ? `<div class="zone-grid">${cards.map((c) => cardEl(c, "card-sm", {})).join("")}</div>`
      : `<div class="zone-empty">${empty}</div>`;
  const opponent = state.players.find((p) => p.username !== you);
  showModal(`<h3>Zones</h3>
    <div class="label">Your graveyard</div>
    ${zone((state.graveyards || {})[you], "empty")}
    <div class="label">${escapeHtml(opponent ? opponent.username : "opponent")}'s graveyard</div>
    ${zone((state.graveyards || {})[opponent && opponent.username], "empty")}
    <div class="label">Exile (shared)</div>
    ${zone(state.exile, "empty")}
    <div class="label">Marked exile piles (CR 406.4) - suspended, plotted, foretold</div>
    ${zone(state.exile_pile, "nothing waiting in exile")}
    <div class="label">Command zone</div>
    ${zone(state.command, "empty")}
    <button onclick="closeModal()">Close</button>`);
}
window.openZonesModal = openZonesModal;

// ----- attack / block -----

function openAttackModal() {
  const state = game.state;
  const you = state.you;
  // The filter here is a *hint*, not a rule: the engine's own
  // `declare_attack` is what actually decides, and it refuses with its own
  // reason. Anything it would obviously reject (tapped, summoning sick,
  // not a creature) is simply not offered.
  const mine = state.battlefield.filter(
    (c) =>
      c.controller === you &&
      !c.tapped &&
      !c.summoning_sick &&
      (c.type_line || "").toLowerCase().includes("creature")
  );
  const defenders = [
    ...state.players.filter((p) => p.username !== you).map((p) => ({ id: p.username, name: p.username })),
    // A planeswalker an opponent controls is a legal thing to attack.
    ...state.battlefield
      .filter((c) => c.controller !== you && (c.type_line || "").toLowerCase().includes("planeswalker"))
      .map((c) => ({ id: c.id, name: c.name })),
  ];
  showModal(`<h3>Declare attackers</h3>
    ${
      mine
        .map(
          (c) => `<div><label><input type="checkbox" class="atk-check" value="${escapeHtml(c.id)}" />
        ${escapeHtml(c.name)} ${c.power !== null ? `(${c.power}/${c.toughness})` : ""}</label>
      <select class="atk-defender" data-card="${escapeHtml(c.id)}">${defenders
            .map((d) => `<option value="${escapeHtml(d.id)}">${escapeHtml(d.name)}</option>`)
            .join("")}</select></div>`
        )
        .join("") || "<div class='hint'>no eligible attackers</div>"
    }
    <div class="chip-row"><button class="primary" onclick="confirmAttack()">Attack</button>
    <button onclick="closeModal()">Cancel (attack with nobody)</button></div>
    <div class="error" id="modal-error"></div>`);
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
  const attacks = (state.combat.attacks || []).filter((a) => {
    const attacker = state.battlefield.find((c) => c.id === a.attacker);
    return attacker && attacker.controller !== you;
  });
  const myCreatures = state.battlefield.filter(
    (c) => c.controller === you && !c.tapped && (c.type_line || "").toLowerCase().includes("creature")
  );
  showModal(`<h3>Declare blockers</h3>
    ${
      attacks
        .map((a) => {
          const attacker = state.battlefield.find((c) => c.id === a.attacker);
          const name = attacker ? attacker.name : a.attacker;
          const pt = attacker && attacker.power !== null ? ` (${attacker.power}/${attacker.toughness})` : "";
          return `<div><strong>${escapeHtml(name)}${escapeHtml(pt)}</strong> attacking ${escapeHtml(a.defender)}:
          ${myCreatures
            .map(
              (c) =>
                `<label class="option"><input type="checkbox" class="blk-check" data-attacker="${escapeHtml(a.attacker)}" value="${escapeHtml(c.id)}" />
                ${escapeHtml(c.name)} ${c.power !== null ? `(${c.power}/${c.toughness})` : ""}</label>`
            )
            .join("")}</div>`;
        })
        .join("") || "<div class='hint'>no attackers to block</div>"
    }
    <div class="chip-row"><button class="primary" onclick="confirmBlocks()">Block</button>
    <button onclick="confirmNoBlocks()">Block with nobody</button></div>
    <div class="error" id="modal-error"></div>`);
}
window.openBlockModal = openBlockModal;

function confirmNoBlocks() {
  // CR 509.1c: declaring no blockers is legal. `declare-blockers` requires at
  // least one pair, so "no blocks" is expressed by passing priority instead
  // of calling it at all.
  closeModal();
  poll();
}
window.confirmNoBlocks = confirmNoBlocks;

async function confirmBlocks() {
  const blocks = [];
  $$(".blk-check:checked").forEach((el) => {
    blocks.push({ blocker: el.value, blocked: el.dataset.attacker });
  });
  if (!blocks.length) return confirmNoBlocks();
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
    panel.innerHTML = `<span class="pill">AI seat: not started</span>`;
    return;
  }
  if (status.mechanism === "fallback") {
    panel.innerHTML = `
      <span class="pill bad">AI seat: no agent running</span>
      <button class="ghost" onclick='showFallbackBox(${JSON.stringify(JSON.stringify(status.prompt || ""))})'>Run it by hand</button>
    `;
    return;
  }
  panel.innerHTML =
    `<span class="pill state-${escapeHtml(status.state)}">AI (${escapeHtml(status.player)}): ${escapeHtml(status.state)}</span>` +
    (status.state === "error"
      ? `<button class="ghost" onclick='showAiError(${JSON.stringify(status)})'>error</button>`
      : "");
}

function showAiError(status) {
  showModal(`<h3>AI seat error (returncode ${escapeHtml(status.returncode)})</h3>
    <textarea rows="12" readonly>${escapeHtml((status.stderr_tail || []).join("\n"))}</textarea>
    <button onclick="closeModal()">Close</button>`);
}
window.showAiError = showAiError;

function showFallbackBox(promptJson) {
  showModal(`<h3>Run the AI seat yourself</h3>
    <p class="hint">The <code>claude</code> CLI could not be spawned from this server. Run this in another terminal, from the API repo. Note it disables that subprocess's permission prompts.</p>
    <textarea rows="3" readonly>claude --print --dangerously-skip-permissions --allowedTools Bash -- "&lt;the prompt below&gt;"</textarea>
    <p class="hint">Full prompt (includes the opponent's uid and this game id):</p>
    <textarea rows="12" readonly id="fallback-prompt"></textarea>
    <button onclick="closeModal()">Close</button>`);
  document.getElementById("fallback-prompt").value = JSON.parse(promptJson);
}
window.showFallbackBox = showFallbackBox;

// -------------------------------------------------------------- startup ----

function init() {
  loadSymbols();
  loadBundledDecks().catch((e) => console.error(e));
  wireDeckPicker("you");
  wireDeckPicker("ai");
  $("#start-game").addEventListener("click", () =>
    startGame().catch((e) => ($("#start-error").textContent = e.message))
  );
  $("#leave-game").addEventListener("click", leaveGame);
  $("#open-zones").addEventListener("click", openZonesModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$("#modal").hidden) closeModal();
      else if (game.action) cancelAction();
    }
  });

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
