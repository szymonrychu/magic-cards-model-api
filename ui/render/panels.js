// Everything above the board that is not the ribbon: the topbar (turn info,
// phase track, AI seat panel, connection pill), the AI-stall banner, and the
// two modal-shaped overlays - the zones popover (graveyards, shared exile,
// CR 406.4's marked exile piles, and the command zone) and the "report a
// problem" form. Split out of `ui/app.js` per Task 17's own size guidance,
// once porting the old classic script's AI panel and modal pieces onto the
// new buildless modules would have pushed `app.js` well past ~700 lines.
//
// Ported from the old `ui/app.js` (deleted by this task): `renderAiPanel`,
// `showAiError`, `showFallbackBox` (app.js:2835-2883), `renderPhaseTrack`/the
// `$("#turn-info")` line (app.js:456-512), `renderAiStallBanner`/
// `aiUnchangedSeconds` (app.js:1712-1748), `setConnection` (app.js:411-418),
// and `openZonesModal`/`openReportModal`/`showModal`/`closeModal`
// (app.js:2645-2698) - rebuilt as `h()`/`patchChildren` nodes instead of
// `innerHTML` strings, and the zones popover's cards wired to the same
// `onPermanent` targeting click the board itself uses (the old modal only
// ever displayed them) via `render/board.js`'s exported `targetingOpts`.
//
// CR numbers here (verified with `knowledge_search(type="rules")` in the
// session that wrote this file): 406.4 (exiled cards kept in separate,
// marked piles - suspend/plot/foretell), 117.3d (passing priority for a
// silent AI seat is an ordinary pass, nothing special-cased).

import { h, clear } from "../dom.js";
import { cardNode } from "../cards.js";
import { targetingOpts } from "./board.js";
import { STEPS, PHASE_OF } from "../priority.js";
import { aiStallNotice } from "../store.js";

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

/** Build `#modal`'s content via `builder(inner, close)` and show it. One
 * shared overlay for every dialog-shaped thing this module needs (the zones
 * popover, the report form, the AI seat's error/fallback boxes) rather than
 * one element per dialog. */
function showModal(root, builder) {
  const modal = root.querySelector("#modal");
  if (!modal) return;
  clear(modal);
  const inner = h("div", { class: "modal-inner" });
  const close = () => {
    modal.hidden = true;
    clear(modal);
  };
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
  panel.appendChild(
    h("span", { class: `pill ${bad ? "bad " : ""}state-${status.state}` }, `AI (${status.player}): ${status.state}`)
  );
  if (bad) panel.appendChild(h("button", { class: "ghost", onClick: () => showAiError(root, status) }, "why"));
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
 * `#ai-stall-banner`, plus wiring `#open-zones`/`#leave-game`/
 * `#report-problem`. `handlers = {onOpenZones(), onLeaveGame(),
 * onPassForAiSeat(aiPlayer), onReportProblem(text)}`. Returns the
 * `store.subscribe` unsubscribe function. */
export function mountTopbar(root, store, handlers) {
  const openZones = root.querySelector("#open-zones");
  if (openZones) openZones.onclick = () => handlers.onOpenZones();
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

// --------------------------------------------------------------- zones ----

function zoneSection(app, handlers, label, cards) {
  const section = h("div", {}, h("div", { class: "label" }, label));
  if (!cards || !cards.length) {
    section.appendChild(h("div", { class: "zone-empty" }, "empty"));
    return section;
  }
  const grid = h("div", { class: "zone-grid" });
  for (const card of cards) {
    const opts = targetingOpts(app, card.id);
    const node = cardNode(card, "card-sm", {});
    node.classList.toggle("targetable", opts.targetable);
    node.classList.toggle("chosen", opts.chosen);
    node.classList.toggle("dimmed", opts.dimmed);
    // The same targeting click the board itself wires (`onPermanent`) - a
    // graveyard/exile card is exactly as legal a target as a battlefield one
    // for whatever cast or decision is asking, and this is the only way to
    // reach it.
    node.onclick = (event) => handlers.onPermanent(card, event);
    grid.appendChild(node);
  }
  section.appendChild(grid);
  return section;
}

/** Graveyards, the shared exile zone, CR 406.4's separately-tracked marked
 * exile piles (suspended/plotted/foretold cards), and the command zone -
 * `handlers.onPermanent` wired onto every card the same way the board wires
 * it, so a spell that targets a graveyard or exiled card can actually be
 * pointed at one. */
export function openZonesModal(root, app, handlers) {
  const state = app.state;
  const you = state.you;
  const opponent = (state.players || []).find((p) => p.username !== you);
  showModal(root, (inner, close) => {
    inner.appendChild(h("h3", {}, "Zones"));
    inner.appendChild(zoneSection(app, handlers, "Your graveyard", (state.graveyards || {})[you]));
    inner.appendChild(
      zoneSection(
        app,
        handlers,
        `${opponent ? opponent.username : "opponent"}'s graveyard`,
        (state.graveyards || {})[opponent && opponent.username]
      )
    );
    inner.appendChild(zoneSection(app, handlers, "Exile (shared)", state.exile));
    inner.appendChild(
      zoneSection(app, handlers, "Marked exile piles (CR 406.4) - suspended, plotted, foretold", state.exile_pile)
    );
    inner.appendChild(zoneSection(app, handlers, "Command zone", state.command));
    inner.appendChild(h("button", { onClick: close }, "Close"));
  });
}
