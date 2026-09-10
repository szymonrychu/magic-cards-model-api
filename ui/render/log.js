// The game log panel (`#log-box`): one line per audit entry from
// `GET /api/games/{id}/log?since=` (`audit.py`'s `record`/`entries`), in
// `app.log.entries` order. Patched with `dom.js`'s keyed `patchChildren`,
// keyed by `seq` - the log's own already-monotonic id - so a poll tick that
// appends one new entry does not tear down and rebuild the lines already on
// screen (which would also fight the auto-scroll below).
//
// Line format mirrors `audit.py`'s own console line
// (`#12 [turn 2 / precombat_main] alice cast Bears -> allowed | ...`),
// trimmed to what a player needs at a glance: `#seq [turn N / step] player
// action`. Two entries render differently:
// - a refused action (`detail.allowed === false`) appends its engine-verbatim
//   `reason` in a red `.refusal` span, never a message invented here;
// - a `report_problem` entry (`audit.py`'s `PROBLEM_ACTION`, mirrored below
//   as a literal since this module cannot import Python) renders bold,
//   matching the loud banner `audit.py` gives it in the server's own log.
//
// Auto-scroll: the panel stays pinned to the bottom across a patch, but only
// when it was already at the bottom beforehand - a player scrolled up to
// read history is not yanked back down by the next poll tick.

import { patchChildren, clear } from "../dom.js";

// Mirrors `audit.py`'s `PROBLEM_ACTION` literal ("report_problem") - the
// action a seat's `report_problem` MCP call turns into, and the one this
// module bolds to match the server's own loud banner for it.
const PROBLEM_ACTION = "report_problem";

// The "nothing has been rendered yet" marker for the `lastSeq` guard below.
// A unique value rather than 0 or null, both of which are real `lastSeq`
// values (`app.js` starts a fresh game at `{entries: [], lastSeq: 0}`), so
// the very first notification always renders.
const NEVER_RENDERED = Symbol("never rendered");

function lineLabel(entry) {
  const turn = entry.turn === null || entry.turn === undefined ? "?" : entry.turn;
  const step = entry.step || "?";
  const player = (entry.detail && entry.detail.player) || "";
  const head = `#${entry.seq} [turn ${turn} / ${step}]`;
  return player ? `${head} ${player} ${entry.action}` : `${head} ${entry.action}`;
}

function createLine() {
  return document.createElement("div");
}

function updateLine(el, entry) {
  el.className = `log-line${entry.action === PROBLEM_ACTION ? " log-problem" : ""}`;
  clear(el);
  el.appendChild(document.createTextNode(lineLabel(entry)));
  const detail = entry.detail || {};
  if (detail.allowed === false) {
    el.appendChild(document.createTextNode(" - refused: "));
    const reason = document.createElement("span");
    reason.className = "refusal";
    reason.appendChild(document.createTextNode(detail.reason || "(no reason given)"));
    el.appendChild(reason);
  }
}

/** Whether `el` is scrolled (at least nearly) to its own bottom - the state
 * that earns it an auto-scroll after the next patch. A box with no scroll
 * geometry yet (a fresh, empty panel; a fake DOM in a test that never sets
 * these) reads as already at the bottom, which is the right default: the
 * first few entries should still pin it. */
function isAtBottom(el) {
  const height = el.scrollHeight || 0;
  const viewport = el.clientHeight || 0;
  const top = el.scrollTop || 0;
  return height - viewport - top <= 4;
}

/** Mount the log into `root`'s `#log-box`, keyed-patched from
 * `store.get().log.entries` on every store update that actually appended
 * something (see the `lastSeq` guard below). Returns the `store.subscribe`
 * unsubscribe function. */
export function mountLog(root, store) {
  const el = root.querySelector("#log-box");
  if (!el) return () => {};
  // Nothing appended since the last render means nothing to do: the poll
  // loop notifies the store four times a tick (`polling: true`, the state
  // and log write, `connection`, `polling: false`) and at most one of those
  // ever carries a new entry, so without this the panel re-sorted up to 500
  // entries and re-patched 500 nodes three times a tick for no change at all.
  //
  // Keyed on `log.lastSeq` and deliberately nothing else. It is the log's
  // own monotonic server-side cursor - `app.js`'s poll writes `entries` and
  // `lastSeq` from the same `GET .../log?since=` response, so entries cannot
  // change without it changing, and a new game resetting to 0 differs from
  // whatever the finished one reached. This is NOT a general "did the state
  // change" guard and must not become one: every other renderer reads
  // several independent inputs, and a guard that forgets one silently
  // freezes a control.
  let renderedSeq = NEVER_RENDERED;
  return store.subscribe((app) => {
    const log = app.log || {};
    if (log.lastSeq === renderedSeq) return;
    renderedSeq = log.lastSeq;
    const entries = log.entries || [];
    // Defensive, not load-bearing: `app.js`'s poll loop already appends in
    // `seq` order, but a keyed patch that receives its items out of order
    // would still reorder the DOM to match them, so sorting here keeps the
    // two guarantees (`seq` order, newest last) true regardless of how the
    // caller assembled the list.
    const sorted = [...entries].sort((a, b) => a.seq - b.seq);
    const wasAtBottom = isAtBottom(el);
    patchChildren(el, sorted, (entry) => entry.seq, createLine, updateLine);
    if (wasAtBottom) el.scrollTop = el.scrollHeight;
  });
}
