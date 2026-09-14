// Everything above the board that is not the ribbon: the topbar (turn info,
// phase track, AI seat panel, connection pill), the AI-stall banner, and
// the "report a problem" form, `#modal`'s other occupant. Split out of
// `ui/app.js` per Task 17's own size guidance, once porting the old classic
// script's AI panel and modal pieces onto the new buildless modules would
// have pushed `app.js` well past ~700 lines.
//
// Ported from the old `ui/app.js` (deleted by this task): `renderAiPanel`,
// `showAiError`, `showFallbackBox` (app.js:2835-2883), `renderPhaseTrack`/the
// `$("#turn-info")` line (app.js:456-512), `renderAiStallBanner`/
// `aiUnchangedSeconds` (app.js:1712-1748), `setConnection` (app.js:411-418),
// and `openReportModal`/`showModal`/`closeModal` (app.js:2645-2698) - rebuilt
// as `h()`/`patchChildren` nodes instead of `innerHTML` strings. The card
// preview is not here: it is `ui/preview.js`'s `mountCardPreview`.
//
// This file used to hold a "Zones" popover too (graveyards, shared exile,
// CR 406.4's marked exile piles, and the command zone) and a `toggleLogModal`/
// `closeLogModal`/`logModalIsOpen` trio for a persistent log overlay, both
// behind topbar buttons. The owner asked for both buttons gone: the command
// zone (the one thing the popover showed that nothing else did) now draws in
// the zone ribbon itself (`ui/board_layout.js`'s `zoneRibbon`), and the log
// (`render/log.js`'s `mountLog`) moved back into `#side-panel`, always
// visible, instead of behind an overlay.
//
// CR numbers here (verified with `knowledge_search(type="rules")` in the
// session that wrote this file): 117.3d (passing priority for a silent AI
// seat is an ordinary pass, nothing special-cased), 601.3 (a player can cast
// a spell only if some rule or effect allows it - the dynamic half of that is
// `PlayPermission`, `machines/casting.py`).

import { h, clear } from "../dom.js";
import { STEPS, PHASE_OF } from "../priority.js";
import { aiStallNotice, aiSeatLoadingNotice, aiSeatActivityLabel } from "../store.js";

const STEP_LABELS = {
  untap: "untap",
  upkeep: "upkeep",
  draw: "draw",
  precombat_main: "main 1",
  beginning_of_combat: "combat",
  declare_attackers: "attack",
  declare_blockers: "block",
  combat_damage: "damage",
  end_of_combat: "end cbt",
  postcombat_main: "main 2",
  end_step: "end",
  cleanup: "cleanup",
};

// ------------------------------------------------------------------ modal --

/** Close `#modal` and empty it - exported so `app.js`'s own Escape handler
 * can close whatever is open without knowing anything about how it was
 * built (the report form and an AI-seat error box both share this one
 * overlay). */
export function closeModal(root) {
  const modal = root.querySelector("#modal");
  if (!modal) return;
  modal.hidden = true;
  clear(modal);
}

// ------------------------------------------------------- modal (shared) --

/** Build `#modal`'s content via `builder(inner, close)` and show it. One
 * shared overlay for every dialog-shaped thing this module needs (the report
 * form, the AI seat's error/fallback boxes) rather than one element per
 * dialog. */
function showModal(root, builder) {
  const modal = root.querySelector("#modal");
  if (!modal) return;
  clear(modal);
  const inner = h("div", { class: "modal-inner" });
  const close = () => closeModal(root);
  builder(inner, close);
  modal.appendChild(inner);
  modal.hidden = false;
}

// -------------------------------------------------------------- AI panel --

function showAiError(root, status) {
  // Two shapes reach here: the subprocess seat (a `returncode` and captured
  // `stderr_tail`) and the wrapper seat (a driver-reported `last_error`).
  // Show whichever the status actually carries.
  const detail = status.last_error || (status.stderr_tail || []).join("\n") || "no detail was reported";
  const heading =
    status.returncode === undefined || status.returncode === null
      ? "AI seat error"
      : `AI seat error (returncode ${status.returncode})`;
  showModal(root, (inner, close) => {
    inner.appendChild(h("h3", {}, heading));
    inner.appendChild(h("textarea", { rows: 12, readonly: true }, detail));
    inner.appendChild(h("button", { onClick: close }, "Close"));
  });
}

function showFallbackBox(root, promptText) {
  showModal(root, (inner, close) => {
    inner.appendChild(h("h3", {}, "Run the AI seat yourself"));
    inner.appendChild(
      h(
        "p",
        { class: "hint" },
        "The claude CLI could not be spawned from this server. Run this in another " +
          "terminal, from the API repo. Note it disables that subprocess's permission prompts."
      )
    );
    inner.appendChild(
      h("textarea", { rows: 3, readonly: true }, 'claude --print --dangerously-skip-permissions --allowedTools Bash -- "<the prompt below>"')
    );
    inner.appendChild(h("p", { class: "hint" }, "Full prompt (includes the opponent's uid and this game id):"));
    inner.appendChild(h("textarea", { rows: 12, readonly: true }, promptText || ""));
    inner.appendChild(h("button", { onClick: close }, "Close"));
  });
}

function renderAiPanel(root, app) {
  const panel = root.querySelector("#ai-panel");
  if (!panel) return;
  clear(panel);
  const status = app.aiStatus;
  if (!status || status.state === "not_started") {
    panel.appendChild(h("span", { class: "pill" }, "AI seat: not started"));
    return;
  }
  if (status.mechanism === "fallback") {
    panel.appendChild(h("span", { class: "pill bad" }, "AI seat: no agent running"));
    panel.appendChild(
      h("button", { class: "ghost", onClick: () => showFallbackBox(root, status.prompt || "") }, "Run it by hand")
    );
    return;
  }
  const bad = status.state === "error" || status.state === "unreachable";
  // What the agent is actually doing, when the wrapper observed it and this
  // page can still stand behind the observation; otherwise the plain seat
  // state, exactly as this pill has always read. The class stays keyed to
  // `status.state` either way - the styling is about the seat's health, not
  // about which of eight things it is doing.
  const doing = aiSeatActivityLabel(app.state, status) || status.state;
  panel.appendChild(
    h("span", { class: `pill ${bad ? "bad " : ""}state-${status.state}` }, `AI (${status.player}): ${doing}`)
  );
  if (bad) panel.appendChild(h("button", { class: "ghost", onClick: () => showAiError(root, status) }, "why"));
}

// --------------------------------------------------------- AI boot banner --

/** `aiSeatLoadingNotice` (`ui/store.js`) turned into `#ai-loading-banner` -
 * the non-blocking half of the loading-gap fix: it never covers the board
 * and never gates a human's own pregame actions (CR 103.5/103.6 gate those
 * on nothing but the human's own seat), it just says what the opponent's
 * agent is doing while it is still booting, and clears itself the moment
 * `aiSeatLoadingNotice` says there is nothing left to report. */
function renderAiLoadingBanner(root, app) {
  const banner = root.querySelector("#ai-loading-banner");
  if (!banner) return;
  const notice = aiSeatLoadingNotice(app.aiUid, app.aiStatus);
  clear(banner);
  if (!notice) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  banner.classList.toggle("bad", notice.failed);
  const player = app.aiStatus && app.aiStatus.player;
  const who = player ? `${player}'s agent` : "The AI seat";
  banner.appendChild(h("span", {}, `${who}: ${notice.message}.`));
}

// ---------------------------------------------------------- AI stall banner

// How long the board has looked exactly like this, keyed by (priority
// holder, turn, step, stack size) - any real progress restarts the clock.
// Module-level, like the old `game.aiPriorityKey`/`game.aiPrioritySince`:
// there is only ever one game on screen at a time.
let aiPriorityKey = null;
let aiPrioritySince = 0;

function aiUnchangedSeconds(state, now) {
  const key = [state.priority_holder, state.turn_number, state.step, (state.stack || []).length].join("|");
  if (aiPriorityKey !== key) {
    aiPriorityKey = key;
    aiPrioritySince = now;
  }
  return (now - aiPrioritySince) / 1000;
}

function renderAiStallBanner(root, app, handlers) {
  const banner = root.querySelector("#ai-stall-banner");
  if (!banner) return;
  const aiPlayer = app.aiUid && app.aiStatus ? app.aiStatus.player : null;
  const seconds = aiUnchangedSeconds(app.state, Date.now());
  const message = aiStallNotice(app.state, aiPlayer, app.aiStatus, seconds);
  clear(banner);
  if (!message) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  banner.appendChild(h("span", {}, `${message}.`));
  banner.appendChild(
    h("span", { class: "why" }, "Nothing is wrong with your game - its opponent is an agent, and it has gone quiet.")
  );
  // CR 117.3d: passing priority for the AI seat, using the seat uid this page
  // was given when it created the game, is an ordinary pass - not a special
  // case - so the engine sees exactly what it would see from that seat's own
  // agent.
  banner.appendChild(h("button", { onClick: () => handlers.onPassForAiSeat(aiPlayer) }, `Pass priority for ${aiPlayer}`));
}

// --------------------------------------------------------- connection pill

function renderConnectionPill(root, app) {
  for (const sel of ["#conn-status", "#conn-status-game"]) {
    const el = root.querySelector(sel);
    if (!el) continue;
    const connection = app.connection || "idle";
    const ok = connection === "ok";
    const bad = typeof connection === "string" && connection.startsWith("error");
    el.textContent = connection;
    el.className = `pill ${ok ? "ok" : bad ? "bad" : ""}`.trim();
  }
}

// -------------------------------------------------------------- top bar --

function renderTurnInfo(el, state) {
  if (!el) return;
  clear(el);
  el.appendChild(h("strong", {}, `turn ${state.turn_number}`));
  el.appendChild(document.createTextNode(" · "));
  el.appendChild(document.createTextNode(`active ${state.active_player} · `));
  el.appendChild(h("span", { class: "prio" }, `priority ${state.priority_holder || "-"}`));
}

function renderPhaseTrack(el, state) {
  if (!el) return;
  clear(el);
  const currentIndex = STEPS.indexOf(state.step);
  STEPS.forEach((step, index) => {
    const classes = [];
    if (index === currentIndex) classes.push("current");
    else if (index < currentIndex) classes.push("done");
    el.appendChild(
      h("li", { class: classes.join(" "), title: `${step} (${PHASE_OF[step]})` }, STEP_LABELS[step] || step)
    );
  });
}

/** Mount the topbar into `root` (normally `document`): `#turn-info`,
 * `#phase-track`, `#ai-panel`, `#conn-status`/`#conn-status-game`, and
 * `#ai-loading-banner`/`#ai-stall-banner`, plus wiring `#leave-game`/
 * `#report-problem` - moved out of `#topbar` into the pinned top-right
 * `.game-toolbar` (`index.html`), but wired the same way regardless of where
 * in the page they physically sit. `handlers = {onLeaveGame(),
 * onPassForAiSeat(aiPlayer), onReportProblem(text)}`. Returns the
 * `store.subscribe` unsubscribe function.
 *
 * Zones and Log are gone: the "Zones" button and its popover (this file's
 * `openZonesModal`, until this task) are removed, and `ui/board_layout.js`'s
 * `zoneRibbon` now also draws the command zone - the one thing that popover
 * showed that the always-visible ribbon did not already. The "Log" button
 * and `#log-modal` are removed too; `#log-box` moved back into `#side-panel`,
 * always visible, wired by `render/log.js`'s `mountLog` exactly as before. */
export function mountTopbar(root, store, handlers) {
  const leave = root.querySelector("#leave-game");
  if (leave) leave.onclick = () => handlers.onLeaveGame();
  const report = root.querySelector("#report-problem");
  if (report) report.onclick = () => openReportModal(root, handlers);
  return store.subscribe((app) => {
    renderConnectionPill(root, app);
    if (!app.state) return;
    renderTurnInfo(root.querySelector("#turn-info"), app.state);
    renderPhaseTrack(root.querySelector("#phase-track"), app.state);
    renderAiPanel(root, app);
    renderAiLoadingBanner(root, app);
    renderAiStallBanner(root, app, handlers);
  });
}

// ------------------------------------------------------- report a problem --

/** The one form on `#report-problem`. `handlers.onReportProblem(text)` posts
 * it; this only collects the text and closes the dialog either way (an empty
 * report is simply not sent, same as the old code's `if (!text) return
 * closeModal()`). */
export function openReportModal(root, handlers) {
  showModal(root, (inner, close) => {
    inner.appendChild(h("h3", {}, "Report a problem"));
    inner.appendChild(
      h(
        "p",
        { class: "hint" },
        "Recorded in this game's audit log, where the engine's own developers read it. " +
          "Say what you expected and what happened."
      )
    );
    const textarea = h("textarea", { rows: 6 });
    inner.appendChild(textarea);
    inner.appendChild(
      h(
        "div",
        { class: "chip-row" },
        h(
          "button",
          {
            class: "primary",
            onClick: () => {
              const text = textarea.value.trim();
              close();
              if (text) handlers.onReportProblem(text);
            },
          },
          "Send"
        ),
        h("button", { onClick: close }, "Cancel")
      )
    );
  });
}
