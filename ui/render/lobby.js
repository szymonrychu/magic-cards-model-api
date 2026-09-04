// The lobby: pick two decks (bundled, uploaded, or pasted), validate them,
// and start a game. Ported from the old `ui/app.js`'s lobby functions -
// `loadBundledDecks`, `wireDeckPicker`, `validateAndReport`, `renderValidation`,
// `startGame` (roughly app.js:256-385) - onto the new `api.js` helper and a
// mountable-module shape, rather than the old file's `$`/globals.
//
// `mountLobby(root, store, handlers)` wires the two deck pickers (`#you-*` /
// `#ai-*`), the seed field, and `#start-game`. On a successful
// `POST /api/ui/games` it calls `handlers.startGame({game_id, uid, aiUid})` -
// `ui/app.js` (Task 17) owns everything after that: hiding `#lobby`, showing
// `#game`, and the first poll.
//
// This module also fires the AI seat's own
// `POST /api/ui/games/{id}/ai-seat/start`, the way the old code did right
// after creating the game - but, unlike the old code, does not render its
// result. `showFallbackBox`/`showAiError` read `renderAiPanel`'s state, and
// that panel only exists once the game screen is up; Task 17's own brief
// lists both as pieces it ports into `app.js`. By the time this POST
// resolves, `handlers.startGame` has already handed off to the game screen,
// whose own poll loop fetches AI-seat status independently - so nothing here
// needs to await it or show anything for it beyond a console line if it
// fails outright.

import { api } from "../api.js";

function $(root, sel) {
  return root.querySelector(sel);
}

function escapeHtml(s) {
  return String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
}

async function loadBundledDecks(root) {
  const { decks } = await api("GET", "/api/ui/decks");
  for (const selId of ["#you-bundled", "#ai-bundled"]) {
    const sel = $(root, selId);
    if (!sel) continue;
    for (const name of decks) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sel.appendChild(opt);
    }
  }
}

function wireDeckPicker(root, prefix) {
  const bundled = $(root, `#${prefix}-bundled`);
  if (bundled) {
    bundled.addEventListener("change", async (e) => {
      if (!e.target.value) return;
      const { text } = await api("GET", `/api/ui/decks/${encodeURIComponent(e.target.value)}`);
      $(root, `#${prefix}-text`).value = text;
    });
  }
  const upload = $(root, `#${prefix}-upload`);
  if (upload) {
    upload.addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        $(root, `#${prefix}-text`).value = reader.result;
      };
      reader.readAsText(file);
    });
  }
  const validateBtn = $(root, `#${prefix}-validate`);
  if (validateBtn) validateBtn.addEventListener("click", () => validateAndReport(root, prefix));
}

async function validateAndReport(root, prefix) {
  const text = $(root, `#${prefix}-text`).value;
  const out = $(root, `#${prefix}-validation`);
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

async function startGame(root, handlers) {
  const err = $(root, "#start-error");
  if (err) err.textContent = "";
  const youText = $(root, "#you-text").value;
  const aiText = $(root, "#ai-text").value;
  const youName = $(root, "#you-name").value.trim() || "you";
  const aiName = $(root, "#ai-name").value.trim() || "claude";
  if (youName === aiName) {
    if (err) err.textContent = "the two players need different names";
    return;
  }
  // Both at once: the two validations are independent, write to different
  // panels, and each can still cost a round trip per uncached card.
  const [youResult, aiResult] = await Promise.all([
    validateAndReport(root, "you"),
    validateAndReport(root, "ai"),
  ]);
  if (!youResult || !youResult.valid || !aiResult || !aiResult.valid) {
    if (err) err.textContent = "fix the decklist errors above before starting";
    return;
  }
  const seedEl = $(root, "#seed");
  const seedRaw = seedEl ? seedEl.value : "";
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
    if (err) err.textContent = e.message;
    return;
  }
  const uid = created.seat_uids[youName];
  const aiUid = created.seat_uids[aiName];
  // Fire-and-forget, exactly like the old code: the pregame is playable the
  // instant the game exists (`ai_pregame.py` makes the bot seat's CR
  // 103.5/103.6 default moves until its agent picks up), and the game
  // screen's own poll loop reads this seat's status independently once it is
  // up - so nothing here needs to await this call or render its result.
  api("POST", `/api/ui/games/${created.game_id}/ai-seat/start`, { uid: aiUid }).catch((e) => {
    console.error(`could not start the AI seat: ${e.message}`);
  });
  handlers.startGame({ game_id: created.game_id, uid, aiUid });
}

/** Mount the lobby into `root`: load the bundled deck list into both
 * selects, wire each seat's bundled-deck/upload/paste/validate controls, and
 * wire `#start-game` to validate both decks and, on success, call
 * `handlers.startGame({game_id, uid, aiUid})`. `store` is accepted for
 * signature parity with the other `mount*` modules; the lobby itself has no
 * store-driven rendering of its own - everything it reads and writes lives
 * on the lobby's own form fields. */
export function mountLobby(root, store, handlers) {
  loadBundledDecks(root).catch((e) => console.error(e));
  wireDeckPicker(root, "you");
  wireDeckPicker(root, "ai");
  const startBtn = $(root, "#start-game");
  if (startBtn) {
    startBtn.addEventListener("click", () => {
      startGame(root, handlers).catch((e) => {
        const err = $(root, "#start-error");
        if (err) err.textContent = e.message;
      });
    });
  }
}
