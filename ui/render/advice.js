// The mtg-buddy advisor panel (`#advice-box`): the bottom of the right-hand
// ribbon, where the game log used to live.
//
// The owner's own spec, verbatim: "It mustn't be a scrolling log - more like
// a static popup with: decision summary, rationale, approve button (once
// clicked agent's proposed action is performed, so I don't need to click it
// on the webUI)". So this is a STATIC panel showing exactly one proposal:
// never a list, never a history, and never a second proposal beside the
// first - a newer `advice` row replaces the one on screen (`currentAdvice`
// returns only the newest). The log itself is not gone, it moved into
// `#log-modal` behind the topbar's Log button.
//
// What is on screen, in reading order: the decision summary, the agent's own
// reasoning (clipped to `RATIONALE_MAX_CHARS`), where in the game it was
// proposed (`#seq`, turn, step) so the human can place it against the log,
// one short status line (`advice_model.js`'s `adviceStatus`: "Buddy is
// thinking", "Buddy is waiting for your call", ...), and Approve. The
// internal verification and staleness sentences are not shown to the human;
// `validated` and `based_on_seq` stay on the row for agents and logs.
//
// Approve is enabled only when the proposal is FRESH (`advice_model.js`'s
// deliberately over-conservative `isFresh`) and this seat has nothing else
// outstanding (`busy === 0`). A stale proposal keeps its words - dimmed, and
// still worth reading, since re-reading the reasoning is how the human
// decides whether it still applies - and loses only the button's enabled
// state. There is no Dismiss: nothing here needs dismissing, because the
// next proposal takes the panel over.
//
// The residual race (the board moving between the last 1.2s log read and the
// click) is the same window every human click in this UI already has, and
// the engine's own refusal plus the existing toast is the backstop, exactly
// as for a manual click. There is deliberately no second mechanism for it.

import { h, clear } from "../dom.js";
import { adviceStatus, clipRationale, currentAdvice, isFresh } from "../advice_model.js";

// The "nothing has been rendered yet" marker for the guard below - a unique
// value rather than a string, since every string this guard builds is a
// legitimate render key.
const NEVER_RENDERED = Symbol("never rendered");

/** The render key: every input this panel reads, and nothing else.
 *
 * `render/log.js` guards on `log.lastSeq` alone, and its own comment says
 * that guard must NOT become a general "did the state change" test. This
 * panel is exactly the case that comment warns about: it reads three
 * independent things, so all three are in its key. `busy` decides whether
 * Approve is clickable, and an Approve left enabled through a submission
 * this seat is still waiting on is the frozen control that comment is
 * about; `state.you` decides whose advice this even is. */
function renderKey(app) {
  const log = app.log || {};
  const state = app.state || {};
  return `${log.lastSeq}|${app.busy}|${state.you}`;
}

function whereLine(advice) {
  const turn = advice.turn === null || advice.turn === undefined ? "?" : advice.turn;
  const step = advice.step || "?";
  return `proposed at #${advice.seq}, turn ${turn} / ${step}`;
}

function renderAdvice(el, app, handlers) {
  clear(el);
  const advice = currentAdvice(app.log, (app.state || {}).you);
  // Nothing proposed: the panel takes itself out of the ribbon entirely
  // rather than leaving an empty frame around nothing. `#side-panel > div`
  // gives every child of that column a border and padding, so an empty but
  // visible `#advice-box` is a bordered box with no content in it. `hidden`,
  // never `style.display` - `style.css`'s global
  // `[hidden] { display: none !important }` is what makes that work, and
  // `tests/test_ui_hidden_elements.py` pins it.
  if (!advice) {
    el.hidden = true;
    return;
  }
  el.hidden = false;

  const fresh = isFresh(advice, app.log);
  const status = adviceStatus(advice, app.log, app.busy);
  // Fresh AND idle. `busy` is `send()`'s live counter of this seat's own
  // outstanding submissions, so approving while it is non-zero would put two
  // of the human's own actions against one board.
  const enabled = fresh && app.busy === 0;

  el.appendChild(h("div", { class: "advice-head" }, "Buddy suggests"));
  el.appendChild(
    h(
      "div",
      { class: `advice-body${fresh ? "" : " advice-stale"}` },
      h("div", { class: "advice-summary" }, advice.summary || "(no summary was recorded)"),
      h("div", { class: "advice-why" }, clipRationale(advice.reasoning) || "(no rationale was recorded)"),
      h("div", { class: "advice-where" }, whereLine(advice))
    )
  );
  el.appendChild(
    h("div", { class: `advice-status advice-status-${status.state}` }, status.text)
  );
  el.appendChild(
    h(
      "button",
      {
        class: "primary",
        disabled: !enabled,
        // No listener at all when disabled, rather than a listener that
        // checks. A real browser never dispatches a click to a disabled
        // button, but the fake DOM these panels are tested against dispatches
        // for real (`tests/test_ui_ribbon.py`'s harness) - and the guarantee
        // that a dimmed Approve cannot fire an action should not depend on
        // which DOM it is running in.
        onClick: enabled
          ? () =>
              handlers.onApprove({
                seq: advice.seq,
                method: advice.method,
                path: advice.path,
                body: advice.body,
              })
          : null,
      },
      "Approve"
    )
  );
}

/** Mount the advisor panel into `root`'s `#advice-box`. `handlers =
 * {onApprove({seq, method, path, body})}` - the panel hands the proposal
 * straight back and never assembles a request of its own. Returns the
 * `store.subscribe` unsubscribe function. */
export function mountAdvice(root, store, handlers) {
  const el = root.querySelector("#advice-box");
  if (!el) return () => {};
  let rendered = NEVER_RENDERED;
  return store.subscribe((app) => {
    const key = renderKey(app);
    if (key === rendered) return;
    rendered = key;
    renderAdvice(el, app, handlers);
  });
}
