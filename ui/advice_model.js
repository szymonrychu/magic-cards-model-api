// The mtg-buddy advisor's proposal, as a view model - pure, no DOM, no
// network. `ui/render/advice.js` renders what this returns; this decides
// what there is to render and, above all, whether it is still SAFE to act
// on.
//
// A proposal is not a message on a side channel. The advisor pod watches the
// human's own view and records its suggestion as a row in this game's audit
// ledger (`audit.py`), so it reaches the page on the very
// `GET /api/games/{id}/log?since=` read `app.js` already makes every 1.2s,
// under that read's own staleness guard. No third concurrent read exists and
// none should: a read written without a staleness guard is how the board
// goes stale, and the log read already has one.
//
// The two ledger rows this module knows about, and their `detail` shapes:
//
// - `advice`: `{player, method, path, body, summary, reasoning,
//   based_on_seq, validated}`. `player` is the HUMAN the advice is for, not
//   the advisor. `summary` is server-derived; `reasoning` is the agent's own
//   words and is the rationale the human actually reads. `{method, path,
//   body}` is the request the human is being offered, verbatim - nothing
//   here or downstream assembles a request of its own. `based_on_seq` is the
//   ledger watermark the agent read the board at. `validated` is false for
//   request shapes the server could not dry-run probe.
// - `advice_approved`: `{advice_seq}` - recorded when the human approves, so
//   the post-match retrospective can pair an approval with its proposal.
//
// Both action names are mirrored here as literals, the way
// `render/log.js` mirrors `audit.py`'s `PROBLEM_ACTION`: these modules are
// served to the browser as-is and cannot import Python.

export const ADVICE_ACTION = "advice";
export const ADVICE_APPROVED_ACTION = "advice_approved";

// The rows that do NOT stale a proposal. The advice row itself always lands
// after the board the agent read (it is recorded once the agent has spoken),
// and an `advice_approved` row lands after that - so a freshness rule that
// counted either as movement would make every proposal stale on arrival and
// nothing could ever be approved.
const ADVISORY_ACTIONS = new Set([ADVICE_ACTION, ADVICE_APPROVED_ACTION]);

/** The log's entries, whether it was handed over as `app.log` (`{entries,
 * lastSeq}`, which is what the store holds) or as a bare array. */
function entriesOf(log) {
  if (Array.isArray(log)) return log;
  return (log && log.entries) || [];
}

/** One `advice` ledger row flattened into the view model the panel renders:
 * the row's own `seq`/`turn`/`step`/`phase` alongside its whole `detail`.
 * Flat on purpose - the panel and `isFresh` both read `based_on_seq`, and
 * making them reach through `.detail` for some fields and not others is how
 * one of them ends up reading the wrong thing. */
function viewOf(entry) {
  const detail = entry.detail || {};
  return {
    seq: entry.seq,
    turn: entry.turn,
    step: entry.step,
    phase: entry.phase,
    player: detail.player,
    method: detail.method,
    path: detail.path,
    body: detail.body,
    summary: detail.summary,
    reasoning: detail.reasoning,
    based_on_seq: detail.based_on_seq,
    validated: detail.validated,
  };
}

/** The newest `advice` row addressed to `you`, or `null`.
 *
 * Newest, and only the newest: a second proposal supersedes the first
 * outright (the panel is a static popup showing one play, never a list), and
 * the opponent's own advisor rows are in the same ledger but are none of
 * this seat's business. */
export function currentAdvice(log, you) {
  if (!you) return null;
  let newest = null;
  for (const entry of entriesOf(log)) {
    if (entry.action !== ADVICE_ACTION) continue;
    if (!entry.detail || entry.detail.player !== you) continue;
    if (newest === null || entry.seq > newest.seq) newest = entry;
  }
  return newest === null ? null : viewOf(newest);
}

/** The `seq` of the first ledger row that invalidated `advice`, or `null`
 * while it still stands.
 *
 * The FIRST such row, not the newest: the panel says which entry moved the
 * board out from under the proposal, and that is the one to go and read. */
export function stalingSeq(advice, log) {
  if (!advice) return null;
  const base = advice.based_on_seq;
  // No recorded baseline: `isFresh` refuses it outright (see below), and
  // there is no particular row to blame for that, so the panel says
  // "unknown" its own way rather than naming an innocent entry.
  if (base === null || base === undefined) return null;
  let first = null;
  for (const entry of entriesOf(log)) {
    if (entry.seq <= base) continue;
    if (ADVISORY_ACTIONS.has(entry.action)) continue;
    if (first === null || entry.seq < first) first = entry.seq;
  }
  return first;
}

/** Whether `advice` may still be acted on: true only when every ledger row
 * after the board the agent read (`based_on_seq`) is itself an `advice` or
 * `advice_approved` row.
 *
 * DELIBERATELY OVER-CONSERVATIVE. Any other row stales the proposal -
 * including rows that move nothing on the board at all, like the opponent's
 * `plan_declared` or anybody's `report_problem`. The rule is "the ledger
 * grew in a way this agent has not seen", never "the board provably
 * changed", because deciding the latter in the browser means
 * re-implementing the engine here.
 *
 * The asymmetry is the whole reason: a false "stale" costs the human one
 * re-think and the advisor one more turn of thought, while a false "fresh"
 * means approving a play against a board that has already moved - the
 * cardinal sin `poll()`'s `mutations` guard and `boardIsNewest` exist to
 * prevent everywhere else in this client.
 *
 * A proposal with no `based_on_seq` is never fresh: there is nothing to
 * compare the ledger against, and an unknown baseline is not a fresh
 * board. */
export function isFresh(advice, log) {
  if (!advice) return false;
  const base = advice.based_on_seq;
  if (base === null || base === undefined) return false;
  return stalingSeq(advice, log) === null;
}

/** `path` with any `uid` query parameter removed.
 *
 * The proposal's path arrives carrying whatever credential the advisor used,
 * and the advisor is read-only server-side (`game_access.py`): sending its
 * uid would be refused, and it would also be a lie - an approved play is a
 * play the HUMAN made. Stripping it here lets `app.js`'s `send()` attach
 * this seat's own uid through the one idempotent place that does that
 * (`uidOf`), rather than a second uid-handling path growing up beside it. */
export function withoutUid(path) {
  const [base, query] = String(path).split("?");
  if (!query) return base;
  const kept = query
    .split("&")
    .filter((pair) => pair !== "uid" && !pair.startsWith("uid="));
  return kept.length ? `${base}?${kept.join("&")}` : base;
}
