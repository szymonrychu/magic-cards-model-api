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
//
// Task 9 adds the lobby's own game list: `#games-tbody` gets one row per
// game this browser has ever started or opened (`mcm.games.v1`, via
// `lobby_model.js`'s storage helpers), joined against
// `GET /api/ui/games?uids=...` through `lobbyViewModel`. Open re-enters a
// game the same way a fresh Start does (`handlers.startGame`); End calls
// `DELETE /api/ui/games/{id}?uid=` and drops the row from storage. The AI
// deck picker also gains `#ai-model`/`#ai-effort`/`#ai-thinking`, read
// through `agentFrom` into the `agent` field `POST /api/ui/games` sends for
// the AI player.

import { api } from "../api.js";
import {
  agentFrom,
  lobbyViewModel,
  loadSavedGames,
  saveGames,
  dropSavedGame,
} from "../lobby_model.js";

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
  const agent = agentFrom({
    model: fieldValue(root, "#ai-model"),
    effort: fieldValue(root, "#ai-effort"),
    thinking: fieldValue(root, "#ai-thinking"),
  });
  const body = {
    players: [
      { username: youName, decklist_text: youText, ai: false },
      { username: aiName, decklist_text: aiText, ai: true, agent },
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
  // Refresh the table too - not awaited, since `handlers.startGame` has
  // already moved on to the game screen: this only makes sure the row is
  // there and current the next time the lobby is shown (`app.js`'s own
  // `startGame` is what actually saves this game to `mcm.games.v1`).
  refreshGames(root, handlers).catch((e) => console.error(e));
}

function fieldValue(root, sel) {
  const el = $(root, sel);
  return el ? el.value : "";
}

function currentStorage() {
  return typeof localStorage !== "undefined" ? localStorage : null;
}

/** "pending" (still in CR 103.5/103.6's pregame - `booted_game`'s own boot
 * leaves a game resting at turn 0 until both seats clear it), "playing", or
 * "over" (a winner or a draw). */
function gameState(row) {
  if (row.over) return "over";
  if (row.turn === 0) return "pending";
  return "playing";
}

/** `tbody.replaceChildren()` on a real DOM; falls back to reassigning
 * `.children` for the plain fake DOM `tests/test_ui_lobby.py` uses, which
 * has neither `replaceChildren` nor `removeChild`. A real `<tbody>`'s
 * `.children` has no setter (an ES module always runs in strict mode, so
 * that assignment would throw `TypeError`) - `replaceChildren` is tried
 * first for exactly that reason, and it is the one real browsers actually
 * have. */
function clearChildren(el) {
  if (typeof el.replaceChildren === "function") el.replaceChildren();
  else el.children = [];
}

function renderGamesTable(root, handlers, rows) {
  const tbody = $(root, "#games-tbody");
  if (!tbody) return;
  clearChildren(tbody);
  for (const row of rows) {
    const tr = document.createElement("tr");
    const opponentTd = document.createElement("td");
    opponentTd.textContent = row.opponent || "(unknown)";
    const turnTd = document.createElement("td");
    turnTd.textContent = `${row.turn ?? "-"} / ${row.step || "-"}`;
    const agentTd = document.createElement("td");
    agentTd.textContent = row.model ? `${row.model} / ${row.effort || "high"}` : "-";
    const stateTd = document.createElement("td");
    stateTd.textContent = gameState(row);
    const actionsTd = document.createElement("td");
    const openBtn = document.createElement("button");
    openBtn.textContent = "Open";
    openBtn.addEventListener("click", () => {
      handlers.startGame({ game_id: row.gameId, uid: row.myUid, aiUid: row.aiUid });
    });
    const endBtn = document.createElement("button");
    endBtn.textContent = "End";
    endBtn.className = "danger";
    endBtn.addEventListener("click", () => {
      endGame(root, handlers, row).catch((e) => console.error(e));
    });
    actionsTd.appendChild(openBtn);
    actionsTd.appendChild(endBtn);
    for (const cell of [opponentTd, turnTd, agentTd, stateTd, actionsTd]) tr.appendChild(cell);
    tbody.appendChild(tr);
  }
}

async function endGame(root, handlers, row) {
  try {
    await api("DELETE", `/api/ui/games/${row.gameId}?uid=${encodeURIComponent(row.myUid)}`);
  } catch (e) {
    console.error(`could not end game ${row.gameId}: ${e.message}`);
  }
  const storage = currentStorage();
  saveGames(storage, dropSavedGame(loadSavedGames(storage), row.gameId));
  await refreshGames(root, handlers);
}

/** Read `mcm.games.v1`, ask the server for every one of those games, and
 * render the joined rows - the "on mount and after Start/End" refresh the
 * lobby's game list gets. Every saved uid (both seats, not only this
 * browser's own) goes into the `uids` query so `list_lobby_games`'s own
 * filter includes this browser's seat in the `seats` it hands back for each
 * game (`lobbyViewModel` needs at least one seat present to know who "you"
 * are in that game). With nothing saved, this skips the network call
 * entirely and just renders an empty table. */
async function refreshGames(root, handlers) {
  const storage = currentStorage();
  const saved = loadSavedGames(storage);
  const uids = [...new Set(saved.flatMap((g) => [g.uid, g.aiUid]).filter(Boolean))];
  if (!uids.length) {
    renderGamesTable(root, handlers, []);
    return;
  }
  let games;
  try {
    ({ games } = await api("GET", `/api/ui/games?uids=${encodeURIComponent(uids.join(","))}`));
  } catch (e) {
    console.error(`could not load the game list: ${e.message}`);
    return;
  }
  renderGamesTable(root, handlers, lobbyViewModel(games, saved));
}

/** Mount the lobby into `root`: load the bundled deck list into both
 * selects, wire each seat's bundled-deck/upload/paste/validate controls,
 * wire `#start-game` to validate both decks and, on success, call
 * `handlers.startGame({game_id, uid, aiUid})`, and render the games table
 * (`#games-tbody`) from `mcm.games.v1` joined against the server. `store` is
 * accepted for signature parity with the other `mount*` modules; the lobby
 * itself has no store-driven rendering of its own - everything it reads and
 * writes lives on the lobby's own form fields and on `localStorage`.
 *
 * Returns `{refresh}` - `refresh()` re-reads `mcm.games.v1` and re-renders
 * the table, for `app.js`'s `leaveGame` to call when a player returns to the
 * lobby from a game that may have moved on since the table was last drawn. */
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
  refreshGames(root, handlers).catch((e) => console.error(e));
  return { refresh: () => refreshGames(root, handlers) };
}
