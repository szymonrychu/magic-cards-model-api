// Everything above the board that is not the ribbon: the topbar (turn info,
// phase track, AI seat panel, connection pill), the AI-stall banner, the
// hover card preview, and the two modal-shaped overlays - the zones popover
// (graveyards, shared exile, CR 406.4's marked exile piles, and the command
// zone) and the "report a problem" form. Split out of `ui/app.js` per Task
// 17's own size guidance, once porting the old classic script's AI panel and
// modal pieces onto the new buildless modules would have pushed `app.js`
// well past ~700 lines.
//
// Ported from the old `ui/app.js` (deleted by this task): `renderAiPanel`,
// `showAiError`, `showFallbackBox` (app.js:2835-2883), `renderPhaseTrack`/the
// `$("#turn-info")` line (app.js:456-512), `renderAiStallBanner`/
// `aiUnchangedSeconds` (app.js:1712-1748), `setConnection` (app.js:411-418),
// `openZonesModal`/`openReportModal`/`showModal`/`closeModal`/`toggleLogModal`
// (app.js:2645-2698), and `wireCardPreview`/`showPreview`/`hidePreview`
// (app.js:656-700) - rebuilt as `h()`/`patchChildren` nodes instead of
// `innerHTML` strings. The zones popover's cards are wired to the same
// `onPermanent` targeting click the board itself uses (the old modal only
// ever displayed them) via `render/board.js`'s exported `targetingOpts`, or
// to `onPermission` when a CR 601.3 play permission names that exact card
// (a cast begun from exile/a graveyard rather than the hand).
//
// CR numbers here (verified with `knowledge_search(type="rules")` in the
// session that wrote this file): 406.4 (exiled cards kept in separate,
// marked piles - suspend/plot/foretell), 117.3d (passing priority for a
// silent AI seat is an ordinary pass, nothing special-cased), 601.3 (a
// player can cast a spell only if some rule or effect allows it - the
// dynamic half of that is `PlayPermission`, `machines/casting.py`).

import { h, clear } from "../dom.js";
import { cardNode } from "../cards.js";
import { withSymbols } from "../symbols.js";
import { decisionCards } from "../decisions.js";
import { targetingOpts } from "./board.js";
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
 * built (a report form, a zones popover, an AI-seat error box all share this
 * one overlay). */
export function closeModal(root) {
  const modal = root.querySelector("#modal");
  if (!modal) return;
  modal.hidden = true;
  clear(modal);
}

// ---------------------------------------------------------- log overlay --
//
// `#log-modal` is the game log's own persistent overlay, and it is
// deliberately NOT built through `showModal` above. That empties `#modal` on
// every open and `closeModal` empties it again on every close, which is
// right for a dialog whose content is rebuilt each time - and fatal for the
// log: `render/log.js`'s `mountLog` subscribes once, at boot, and keyed-
// patches one `#log-box` element for the life of the page. Destroying that
// element out from under it leaves it patching a detached node, so the log
// silently stops growing. `#log-modal` is built once in `index.html` and
// these three functions only ever toggle it.

/** Whether the log overlay is open. */
export function logModalIsOpen(root) {
  const modal = root.querySelector("#log-modal");
  return Boolean(modal && !modal.hidden);
}

/** Close the log overlay, content untouched. */
export function closeLogModal(root) {
  const modal = root.querySelector("#log-modal");
  if (modal) modal.hidden = true;
}

/** Show the log overlay if it is hidden, hide it if it is not - what the
 * topbar's Log button does. `hidden`, never `style.display`: `style.css`'s
 * global `[hidden] { display: none !important }` is the only thing that
 * beats `.modal`'s own `display: flex`, which is why a "hidden" modal once
 * covered the whole page and swallowed every click. */
export function toggleLogModal(root) {
  const modal = root.querySelector("#log-modal");
  if (!modal) return;
  modal.hidden = !modal.hidden;
  if (modal.hidden) return;
  // Newest entries first sight, which needs doing HERE and not in `mountLog`
  // (untouched on purpose). `mountLog`'s auto-scroll reads real scroll
  // geometry, and a hidden element has none: while this overlay is closed,
  // `scrollHeight` is 0, so every patch pins `scrollTop` to 0 and the log
  // would open showing entry #1 of 500. Reading `scrollHeight` immediately
  // after clearing `hidden` is what forces the browser's own synchronous
  // reflow, so the height is the laid-out one rather than the stale zero.
  const box = modal.querySelector("#log-box");
  if (box) box.scrollTop = box.scrollHeight;
}

// ------------------------------------------------------- modal (shared) --

/** Build `#modal`'s content via `builder(inner, close)` and show it. One
 * shared overlay for every dialog-shaped thing this module needs (the zones
 * popover, the report form, the AI seat's error/fallback boxes) rather than
 * one element per dialog. */
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
 * `#ai-loading-banner`/`#ai-stall-banner`, plus wiring `#open-zones`/
 * `#open-log`/`#leave-game`/`#report-problem`. `handlers = {onOpenZones(),
 * onToggleLog(), onLeaveGame(), onPassForAiSeat(aiPlayer),
 * onReportProblem(text)}`. Returns the `store.subscribe` unsubscribe
 * function. */
export function mountTopbar(root, store, handlers) {
  const openZones = root.querySelector("#open-zones");
  if (openZones) openZones.onclick = () => handlers.onOpenZones();
  const openLog = root.querySelector("#open-log");
  if (openLog) openLog.onclick = () => handlers.onToggleLog();
  // The log overlay covers the whole viewport, this topbar included, so the
  // button that opened it cannot close it again - without a backdrop click,
  // Escape would be the only way out of a panel a mouse opened. `e.target
  // === logModal` is the test that tells the dimmed backdrop apart from the
  // log itself, so reading (or selecting a line in) the log does not dismiss
  // it. Closing never empties it: `mountLog` keeps its element for the life
  // of the page.
  const logModal = root.querySelector("#log-modal");
  if (logModal) {
    logModal.addEventListener("click", (e) => {
      if (e.target === logModal) closeLogModal(root);
    });
  }
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

// --------------------------------------------------------------- zones ----

/** Append one zone's label and card grid straight into `inner` (siblings,
 * not wrapped in an extra container) - `.modal-inner > .label` (`style.css`)
 * is a direct-child selector, so a wrapper here would silently opt every
 * zone heading in this popover out of it. */
function zoneSection(inner, app, handlers, label, cards) {
  inner.appendChild(h("div", { class: "label" }, label));
  if (!cards || !cards.length) {
    inner.appendChild(h("div", { class: "zone-empty" }, "empty"));
    return;
  }
  const grid = h("div", { class: "zone-grid" });
  // CR 601.3's dynamic play permissions can name a card sitting in exile or
  // a graveyard (cascade, suspend's own third ability) - clicking one of
  // those begins casting it from there instead of an ordinary targeting
  // click, the same choice `render/board.js`'s `#permitted-row` offers.
  const permissions = (app.state.play_permissions && app.state.play_permissions[app.state.you]) || [];
  for (const card of cards) {
    const opts = targetingOpts(app, card.id);
    const node = cardNode(card, "card-sm", {});
    node.classList.toggle("targetable", opts.targetable);
    node.classList.toggle("chosen", opts.chosen);
    node.classList.toggle("dimmed", opts.dimmed);
    const permission = permissions.find((p) => p.card.id === card.id);
    node.onclick = (event) =>
      permission ? handlers.onPermission(permission, event) : handlers.onPermanent(card, event);
    grid.appendChild(node);
  }
  inner.appendChild(grid);
}

/** Graveyards, the shared exile zone, CR 406.4's separately-tracked marked
 * exile piles (suspended/plotted/foretold cards), and the command zone -
 * `handlers.onPermanent` wired onto every card the same way the board wires
 * it, so a spell that targets a graveyard or exiled card can actually be
 * pointed at one; a card a CR 601.3 permission names goes through
 * `handlers.onPermission` instead. */
export function openZonesModal(root, app, handlers) {
  const state = app.state;
  const you = state.you;
  const opponent = (state.players || []).find((p) => p.username !== you);
  showModal(root, (inner, close) => {
    inner.appendChild(h("h3", {}, "Zones"));
    zoneSection(inner, app, handlers, "Your graveyard", (state.graveyards || {})[you]);
    zoneSection(
      inner,
      app,
      handlers,
      `${opponent ? opponent.username : "opponent"}'s graveyard`,
      (state.graveyards || {})[opponent && opponent.username]
    );
    zoneSection(inner, app, handlers, "Exile (shared)", state.exile);
    zoneSection(inner, app, handlers, "Marked exile piles (CR 406.4) - suspended, plotted, foretold", state.exile_pile);
    zoneSection(inner, app, handlers, "Command zone", state.command);
    inner.appendChild(h("button", { onClick: close }, "Close"));
  });
}

// ---------------------------------------------------------- hover preview --

/** Every card a live seat's view can ever put a `.card[data-card-id]` node
 * on screen for - battlefield, stack, exile, your own hand, every
 * graveyard, (CR 601.3) any card a play permission names, and the cards of
 * this seat's own pending decision (a library card a scry or a search shows
 * exists nowhere else on the view) - keyed by id, fresh on each hover rather
 * than kept live, so a card that only exists because of a permission still
 * previews correctly. The decision's cards go in first, so a card that is
 * also on the board previews as its board view. The opponent's hand cards
 * this seat knows (`known_cards[you].hand`) are drawn face up on the board,
 * so they preview like any other visible card. */
export function hoverableCards(state) {
  const lookup = {};
  const add = (card) => {
    if (card && card.id) lookup[card.id] = card;
  };
  const pending = state.pending_decision;
  if (pending && pending.player === state.you) decisionCards(pending).forEach(add);
  (state.battlefield || []).forEach(add);
  (state.stack || []).forEach(add);
  (state.exile || []).forEach(add);
  (state.exile_pile || []).forEach(add);
  (state.command || []).forEach(add);
  ((state.hands && state.hands[state.you]) || []).forEach(add);
  ((state.known_cards && state.known_cards[state.you] && state.known_cards[state.you].hand) || []).forEach(add);
  Object.values(state.graveyards || {}).forEach((cards) => (cards || []).forEach(add));
  ((state.play_permissions && state.play_permissions[state.you]) || []).forEach((p) => add(p.card));
  return lookup;
}

function showPreview(root, symbols, card) {
  const box = root.querySelector("#card-preview");
  if (!box) return;
  clear(box);
  const img = card.image || card.small_image;
  if (img) {
    const image = h("img", {
      src: img,
      alt: card.name || "",
      onerror: (e) => e.target.remove(),
    });
    box.appendChild(image);
  } else {
    box.appendChild(
      h(
        "div",
        { class: "fallback-text" },
        h("span", { class: "name" }, card.name || card.card_name || "?"),
        h("br"),
        h("span", { class: "type" }, card.type_line || ""),
        h("div", { class: "oracle" }, withSymbols(symbols, card.oracle_text || card.text || ""))
      )
    );
  }
  box.hidden = false;
}

function hidePreview(root) {
  const box = root.querySelector("#card-preview");
  if (box) box.hidden = true;
}

/** Wire a `mouseover`/`mouseout` delegate on `boardEl` that shows whichever
 * `.card[data-card-id]` node is hovered in `#card-preview`, ported from the
 * classic script's `wireCardPreview`/`showPreview`/`hidePreview`. A no-op
 * (rather than double-wiring listeners) on an element already wired -
 * `boardEl.dataset.previewWired` marks it, the same guard the old code used
 * - so this can safely be called again after the zones popover rebuilds its
 * own content on every open. */
function wireOn(root, boardEl, store) {
  if (!boardEl || boardEl.dataset.previewWired) return;
  boardEl.dataset.previewWired = "1";
  boardEl.addEventListener("mouseover", (e) => {
    const el = e.target.closest(".card[data-card-id]");
    if (!el) return;
    const app = store.get();
    if (!app.state) return;
    const card = hoverableCards(app.state)[el.dataset.cardId];
    if (card) showPreview(root, app.symbols, card);
  });
  boardEl.addEventListener("mouseout", (e) => {
    const el = e.target.closest(".card[data-card-id]");
    const to = e.relatedTarget && e.relatedTarget.closest ? e.relatedTarget.closest(".card") : null;
    if (el && !to) hidePreview(root);
  });
}

/** Mount the hover preview onto `#mtg-board`, the zones popover's `#modal`
 * (its content is rebuilt fresh every time it opens, but the modal element
 * itself persists, so wiring it once here is enough), and the persistent
 * `#decision-window`. `store` supplies both the card lookup and the
 * mana-symbol map for oracle text. */
export function mountHoverPreview(root, store) {
  wireOn(root, root.querySelector("#mtg-board"), store);
  wireOn(root, root.querySelector("#modal"), store);
  wireOn(root, root.querySelector("#decision-window"), store);
}
