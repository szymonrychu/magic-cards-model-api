// The post-game review, as pure functions over `GET /api/games/{id}/review`
// - no DOM and no `fetch`, so `tests/test_ui_review_model.py` can ask the
// awkward questions directly (`ui/board_layout.js` is the same seam).
// `render/review.js` draws what this returns and decides nothing about it.
//
// What this module exists to protect is one ordering claim: the agent wrote
// down what it expected *before* the rows that show what happened. That
// claim is only as good as the grouping, so the grouping lives here, in one
// tested place, rather than being re-derived inside a render loop.
//
// Nothing here grades a prediction. `review.py` says why at length and it is
// the same reason: a score is a thing a sceptic argues with, and the raw
// pairing is not. These functions put the two side by side and stop.
//
// The one genuinely awkward piece is the board. `audit.record` writes
// `serialize.snapshot(game)` on every row, and that snapshot renders each
// permanent as *one string* - "alice: Grizzly Bears (tapped) (sick) [4/5]
// dmg:2 (+1/+1=2)" - with hands, graveyards and exile as bare card names. So
// a review frame's board has to be parsed back out of those lines
// (`parsePermanent`) and joined against the printings by name
// (`frameState`'s `catalogue`, which `render/review.js` fills from
// `GET /api/cards/{name}`) before `board_layout.js` and `cards.js` can draw
// it with the same renderers the live board uses.
//
// Both of the live board's "this card lives under that card" relationships
// - `attached_to` on a battlefield card, `exile_pile.linked_permanent_id`
// on an exiled one (`ui/board_layout.js`'s own module docstring) - ride
// along on the snapshot's strings too, as a trailing `(attached:N)`: `N` is
// the *index* the host occupies in this same snapshot's own `battlefield`
// array, which is exactly the identity `frameState` below already invents
// for it (`bf-${index}`) since the snapshot carries no card ids of its own.
// `parsePermanent` and the exile line's own parse (`frameState`) turn that
// index straight into `{kind: "card", id: "bf-N"}` /
// `{linked_permanent_id: "bf-N"}` - the live board's exact shape - so
// `board_layout.js` needs no changes at all to tuck a review frame's Aura or
// imprinted card the same way it tucks a live one.

//: `audit.py`'s own literals, mirrored here because a browser module cannot
//: import Python. `log.js` mirrors `PROBLEM_ACTION` the same way.
export const PLAN_ACTION = "plan_declared";
export const GAME_OVER_ACTION = "game_over";

//: The mtg-buddy advisor's own proposal to a human seat, and the seat's
//: one-click approval of one - `audit.ADVICE_ACTION`/`ADVICE_APPROVED_
//: ACTION`. Neither mutates the game; see `adviceOf` for what is built from
//: them.
export const ADVICE_ACTION = "advice";
export const ADVICE_APPROVED_ACTION = "advice_approved";

//: The actions a post-game write-up would arrive as, should one ever be
//: written into the ledger. Today nothing writes any of them: the agent's
//: reflection turn (`mtg_agent/driver/prompt.py`'s `reflection_turn`) writes
//: markdown into its own memory directory and never posts it to the API, so
//: `reflectionOf` returns `null` for every game this server can serve. It is
//: matched anyway, and `review.reflection` with it, so the section appears
//: the day either one starts carrying it rather than needing this page
//: changed again.
export const REFLECTION_ACTIONS = ["game_reflection", "reflection", "post_game_reflection"];

//: The rows that are the turn walking forward rather than a seat doing
//: something. A real game is mostly these - 219 `advance` rows out of 297 in
//: the game this page was built against - so a frame carries `routine` and
//: the page draws them at a lower weight. It never drops them: they stay in
//: order, in the same list, and a refused row is never routine whatever its
//: action, because the engine's refusal is the evidence.
export const ROUTINE_ACTIONS = ["advance", "pass_priority"];

// `serialize.battlefield_line`'s optional suffixes, in the order that
// function appends them - stripped off the end in reverse, so whatever is
// left is the card's own name however many brackets or colons it contains.
// `ATTACHED_RE` is last here because it is appended last (and so comes off
// first) - both the battlefield and the exile line share it, since
// `serialize.snapshot`'s `battlefield_line`/`exile_line` both append it the
// same way, over an otherwise different set of suffixes.
const COUNTERS_RE = /\s\(([^()]*=[^()]*)\)$/;
const DAMAGE_RE = /\sdmg:(\d+)$/;
const PT_RE = /\s\[([^\]/]*)\/([^\]]*)\]$/;
const SICK_RE = /\s\(sick\)$/;
const TAPPED_RE = /\s\(tapped\)$/;
const ATTACHED_RE = /\s\(attached:(\d+)\)$/;

/** The `(attached:N)` suffix stripped off the end of `rest`, if present -
 * `{ rest, hostIndex }`, `hostIndex` the battlefield index `N` names, or
 * `null` for a line with no such suffix. Shared by `parsePermanent` (the
 * battlefield) and `frameState`'s exile parse - `serialize.snapshot`
 * appends the identical suffix to both. */
function stripAttached(rest) {
  const match = rest.match(ATTACHED_RE);
  if (!match) return { rest, hostIndex: null };
  return { rest: rest.slice(0, match.index), hostIndex: Number(match[1]) };
}

/** One `serialize.battlefield_line` string, back as a card object of the
 * shape `cards.js` draws and `board_layout.js` sorts.
 *
 * `id` is supplied by the caller rather than found in the line: the snapshot
 * carries no card ids at all, and `dom.js`'s keyed patch needs one per card
 * per frame. Power and toughness stay strings - the engine's computed
 * characteristics can be a `*` as easily as a number, and `cards.js` only
 * ever prints them.
 */
export function parsePermanent(line, id) {
  let rest = String(line == null ? "" : line);
  let counters = {};
  let damage = 0;
  let power = null;
  let toughness = null;

  // Stripped first: it is the last suffix `battlefield_line` appends, so it
  // is the outermost one to come off.
  const attached = stripAttached(rest);
  rest = attached.rest;
  const attachedTo = attached.hostIndex === null ? null : { kind: "card", id: `bf-${attached.hostIndex}` };

  const countersMatch = rest.match(COUNTERS_RE);
  if (countersMatch) {
    rest = rest.slice(0, countersMatch.index);
    for (const pair of countersMatch[1].split(",")) {
      const at = pair.indexOf("=");
      if (at < 0) continue;
      const kind = pair.slice(0, at).trim();
      const amount = Number(pair.slice(at + 1).trim());
      if (kind) counters[kind] = Number.isFinite(amount) ? amount : pair.slice(at + 1).trim();
    }
  }
  const damageMatch = rest.match(DAMAGE_RE);
  if (damageMatch) {
    rest = rest.slice(0, damageMatch.index);
    damage = Number(damageMatch[1]);
  }
  const ptMatch = rest.match(PT_RE);
  if (ptMatch) {
    rest = rest.slice(0, ptMatch.index);
    power = ptMatch[1];
    toughness = ptMatch[2];
  }
  const sick = SICK_RE.test(rest);
  if (sick) rest = rest.replace(SICK_RE, "");
  const tapped = TAPPED_RE.test(rest);
  if (tapped) rest = rest.replace(TAPPED_RE, "");

  const split = rest.indexOf(": ");
  const controller = split < 0 ? null : rest.slice(0, split);
  const name = split < 0 ? rest.trim() : rest.slice(split + 2).trim();
  return {
    id,
    name,
    controller,
    zone: "battlefield",
    tapped,
    summoning_sick: sick,
    power,
    toughness,
    damage,
    counters,
    type_line: "",
    // The live board's own field name and shape (`serialize._card_view`'s
    // `attached_to`) - `null` rather than absent, matching every other
    // optional field `parsePermanent` already returns this way, and exactly
    // what `board_layout.rootParentId` reads (`current.attached_to`).
    attached_to: attachedTo,
  };
}

/** A card the snapshot knows only by name - a hand, a graveyard, exile, a
 * stack object's own card. */
function namedCard(name, id, zone) {
  return { id, name, controller: null, zone, tapped: false, counters: {}, type_line: "" };
}

/** One `serialize.snapshot` exile line back as a card object - a bare name,
 * or a name plus the `(attached:N)` suffix `serialize.exile_line` appends
 * for a card CR 406.4's `ExileAnnotation.linked_permanent_id` (imprint,
 * hideaway) links to a permanent. The link becomes `exile_pile.
 * linked_permanent_id`, the live board's own field (`serialize._exile_pile`)
 * and shape (a `bf-${index}` id, here the same battlefield index
 * `attached_to` uses) - so `board_layout.js` reads it with no changes of
 * its own. */
function parseExileCard(line, id) {
  const { rest, hostIndex } = stripAttached(String(line == null ? "" : line));
  const card = namedCard(rest.trim(), id, "exile");
  if (hostIndex !== null) card.exile_pile = { linked_permanent_id: `bf-${hostIndex}` };
  return card;
}

/** The printing joined in by name: the type line `board_layout.isLandRow`
 * reads to decide the land row, and the art `cards.js` draws. Absent from
 * the catalogue, the card is still drawn - with `cards.js`'s legible text
 * fallback and no type line, never a type guessed from its name. */
function withPrinting(card, catalogue) {
  const printing = (catalogue && catalogue[card.name]) || null;
  if (!printing) return card;
  return {
    ...card,
    type_line: card.type_line || printing.type_line || "",
    oracle_text: printing.oracle_text || "",
    mana_cost: printing.mana_cost || "",
    image: printing.image || null,
    small_image: printing.small_image || printing.image || null,
  };
}

/**
 * One ledger row's `snapshot`, as the state view `board_layout.js` and
 * `cards.js` already read - `{battlefield, hands, graveyards, exile, stack,
 * life, pools, combat, seats}`. `null` for a row that carries no snapshot
 * (`audit.record` writes one for a game the store no longer holds), so the
 * caller draws no board rather than throwing.
 *
 * Every card gets an id unique within the frame, because `dom.js`'s keyed
 * patch is keyed on it and the snapshot supplies none.
 */
export function frameState(snapshot, catalogue) {
  if (!snapshot) return null;
  const seats = Object.keys(snapshot.life || snapshot.hands || {});
  const battlefield = (snapshot.battlefield || []).map((line, index) =>
    withPrinting(parsePermanent(line, `bf-${index}`), catalogue)
  );
  const hands = {};
  for (const [seat, cards] of Object.entries(snapshot.hands || {})) {
    hands[seat] = cards.map((name, index) =>
      withPrinting(namedCard(name, `hand-${seat}-${index}`, "hand"), catalogue)
    );
  }
  const graveyards = {};
  for (const [seat, cards] of Object.entries(snapshot.graveyards || {})) {
    graveyards[seat] = cards.map((name, index) =>
      withPrinting(namedCard(name, `gy-${seat}-${index}`, "graveyard"), catalogue)
    );
  }
  const exile = (snapshot.exile || []).map((line, index) =>
    withPrinting(parseExileCard(line, `ex-${index}`), catalogue)
  );
  const stack = (snapshot.stack || []).map((obj, index) => ({
    id: `st-${index}`,
    name: obj.name,
    kind: obj.kind,
    controller: obj.controller,
    targets: obj.targets || [],
  }));
  return {
    seats,
    battlefield,
    hands,
    graveyards,
    exile,
    stack,
    life: snapshot.life || {},
    pools: snapshot.pools || {},
    combat: snapshot.combat || [],
  };
}

/** Every card name anywhere in this review, sorted and deduplicated - what
 * `render/review.js` fetches printings for so the frames are drawn with the
 * game's own art rather than text boxes. */
export function cardNamesIn(review) {
  const names = new Set();
  for (const entry of (review && review.entries) || []) {
    const snapshot = entry.snapshot;
    if (!snapshot) continue;
    for (const line of snapshot.battlefield || []) names.add(parsePermanent(line, "x").name);
    for (const cards of Object.values(snapshot.hands || {})) for (const n of cards) names.add(n);
    for (const cards of Object.values(snapshot.graveyards || {})) for (const n of cards) names.add(n);
    for (const line of snapshot.exile || []) names.add(parseExileCard(line, "x").name);
    for (const obj of snapshot.stack || []) if (obj && obj.name) names.add(obj.name);
  }
  names.delete("");
  return [...names].sort();
}

/** The console-style subject of one row, from the same `detail` keys
 * `audit._console_line` reads - the card, the target, or a zone move. Never
 * a sentence invented here: a reader comparing the page against the server
 * log should see the same words. */
export function frameLabel(entry) {
  const detail = (entry && entry.detail) || {};
  if (detail.prompt) return `${entry.action}: ${detail.prompt}`;
  const bits = [entry.action];
  if (detail.from !== undefined && detail.to !== undefined) bits.push(`${detail.from} -> ${detail.to}`);
  else {
    const card = readable(detail.card);
    if (card) bits.push(card);
    const target = readable(detail.target);
    if (target) bits.push(`-> ${target}`);
  }
  const resolved = readable(detail.resolved);
  if (resolved) bits.push(resolved);
  if (detail.note) bits.push(String(detail.note));
  return bits.filter(Boolean).join(" ").trim();
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** A `detail` value worth showing a reader, or `""`.
 *
 * `POST .../actions/cast` accepts `card_id` *or* `card`, and records
 * whichever it was given - so a client that cast by id leaves a uuid where a
 * card name would be (`audit._console_line` prints it too). A uuid on this
 * page is noise, and a sceptic reads noise as sloppiness, so it is dropped:
 * the frame's own board still shows the spell sitting on the stack. */
function readable(value) {
  if (!value) return "";
  const asText = String(value);
  return UUID_RE.test(asText) ? "" : asText;
}

/** One ledger row as the page draws it: what happened, why the seat said it
 * did it, the engine's verbatim refusal if it refused, and the board as it
 * stood at that moment. */
function frameOf(entry) {
  const detail = entry.detail || {};
  return {
    seq: entry.seq,
    ts: entry.ts,
    turn: entry.turn,
    step: entry.step,
    phase: entry.phase,
    player: detail.player || null,
    action: entry.action,
    label: frameLabel(entry),
    refused: detail.allowed === false,
    reason: detail.allowed === false ? detail.reason || null : null,
    reasoning: entry.reasoning || null,
    routine: detail.allowed !== false && ROUTINE_ACTIONS.includes(entry.action),
    snapshot: entry.snapshot || null,
  };
}

/** The declaration `review.py` already paired, reshaped for the page: the
 * prediction on one side, what the opponent actually did on the other, both
 * with the `seq` that proves which came first. `opponentDid` is left empty
 * rather than hidden when the opponent did nothing before the next
 * declaration - that is an outcome too, and the page says so. */
function pairingOf(declared, players) {
  const did = (declared.did || []).map(frameOf);
  const opponentDid = (declared.opponent_did || []).map(frameOf);
  const opponent =
    (opponentDid.find((f) => f.player) || {}).player ||
    (players || []).find((p) => p !== declared.player) ||
    null;
  return {
    seq: declared.seq,
    ts: declared.ts,
    turn: declared.turn,
    turnsTaken: declared.turns_taken === undefined ? null : declared.turns_taken,
    player: declared.player || null,
    opponent,
    board_read: declared.board_read || null,
    my_role: declared.my_role || null,
    plan: declared.plan || null,
    prediction: declared.prediction || null,
    did,
    opponentDid,
  };
}

/** A `plan_declared` row the endpoint's own `declarations` list does not
 * cover - a payload from an older server, or a row whose window the endpoint
 * assembled differently. The prediction still shows; only the pairing is
 * empty. */
function pairingFromRow(entry, players) {
  const detail = entry.detail || {};
  return pairingOf(
    {
      seq: entry.seq,
      ts: entry.ts,
      turn: entry.turn,
      turns_taken: detail.turns_taken,
      player: detail.player,
      board_read: detail.board_read,
      my_role: detail.my_role,
      plan: detail.plan,
      prediction: detail.prediction,
      did: [],
      opponent_did: [],
    },
    players
  );
}

// -------------------------------------------------- mtg-buddy's advice -----
//
// A stream of its own, beside the declaration pairing above rather than
// folded into it: a declaration's window runs from one `plan_declared` row
// to the next *by any seat*, and an advice row - one per decision point,
// potentially several a turn - would slice that window into fragments it was
// never meant to have. `reviewTurns` below marks `PLAN_ACTION` and
// `ADVICE_ACTION` independently for exactly that reason.
//
// This mirrors `review.py`'s `_advice` line for line rather than trusting the
// server's own computed `review.advice` alone, because `adviceOf` (like
// `pairingOf`/`pairingFromRow` above) has to answer for a payload that
// carries no such list at all - every game reviewed before this facility
// existed, or a hand-assembled payload in a test.

/** The ledger's own name for a proposal's `path` - the route's last segment,
 * hyphens turned to underscores (`/actions/play-land` -> `play_land`),
 * exactly how every action route's own path relates to the name it audits
 * under. `null` for a path that names nothing (or is itself absent). */
function kindOfPath(path) {
  if (!path) return null;
  const tail = String(path)
    .replace(/\/+$/, "")
    .split("/")
    .pop();
  const kind = (tail || "").replace(/-/g, "_");
  return kind || null;
}

/** Whether `didRow` (a raw ledger row, not yet a frame) actually matches an
 * advice proposal: the same action kind for `path`, and the same card where
 * `body` names one by id or name. A proposal that names no card - a pass,
 * most often - has nothing further to check, so the kind match is enough. */
function adviceFollowed(path, body, didRow) {
  if (!didRow) return false;
  const kind = kindOfPath(path);
  if (!kind || didRow.action !== kind) return false;
  const proposed = (body || {}).card_id !== undefined ? body.card_id : (body || {}).card;
  if (proposed === undefined || proposed === null) return true;
  const didDetail = didRow.detail || {};
  const didCard = didDetail.card_id !== undefined ? didDetail.card_id : didDetail.card;
  return didCard !== undefined && didCard !== null && String(didCard) === String(proposed);
}

/** The first row in `entries[start, end)` that is a real move by `player`:
 * never an `advice`/`advice_approved` marker (neither mutates the game), and
 * never a row the engine refused - a refusal is a failed attempt, not a
 * thing that happened, and is deliberately never handed back as `did`. */
function firstRealAction(entries, start, end, player) {
  for (let index = start; index < end; index += 1) {
    const candidate = entries[index];
    if (candidate.action === ADVICE_ACTION || candidate.action === ADVICE_APPROVED_ACTION) continue;
    if ((candidate.detail || {}).player !== player) continue;
    if ((candidate.detail || {}).allowed === false) continue;
    return candidate;
  }
  return null;
}

/** Every `ADVICE_ACTION` row in `entries`, paired with whether it was
 * approved and what the seat did about it - the full derivation `adviceOf`
 * falls back to when `review.advice` is absent. See the section banner above
 * for why this is a separate pass from `reviewTurns`'s declaration pairing. */
function deriveAdvice(entries) {
  const approvals = entries.filter((row) => row.action === ADVICE_APPROVED_ACTION);
  const result = [];
  entries.forEach((row, index) => {
    if (row.action !== ADVICE_ACTION) return;
    const detail = row.detail || {};
    const player = detail.player || null;
    const seq = row.seq;
    const approvedRow =
      approvals.find((a) => (a.detail || {}).advice_seq === seq) || null;
    let boundary = entries.length;
    for (let later = index + 1; later < entries.length; later += 1) {
      const candidate = entries[later];
      if (candidate.action === ADVICE_ACTION && (candidate.detail || {}).player === player) {
        boundary = later;
        break;
      }
    }
    const didRow = firstRealAction(entries, index + 1, boundary, player);
    result.push({
      seq,
      ts: row.ts,
      turn: row.turn,
      step: row.step,
      phase: row.phase,
      player,
      summary: detail.summary === undefined ? null : detail.summary,
      reasoning: row.reasoning || detail.reasoning || null,
      path: detail.path === undefined ? null : detail.path,
      body: detail.body === undefined ? null : detail.body,
      basedOnSeq: detail.based_on_seq === undefined ? null : detail.based_on_seq,
      validated: detail.validated === undefined ? null : detail.validated,
      approved: approvedRow !== null,
      approvedSeq: approvedRow ? approvedRow.seq : null,
      did: didRow ? frameOf(didRow) : null,
      followed: adviceFollowed(detail.path, detail.body, didRow),
    });
  });
  return result;
}

/** One server-computed `review.advice` item (snake_case, `did` a paired
 * `ReviewEntry`-shaped object with no `snapshot`), reshaped into the same
 * camelCase form `deriveAdvice` returns - so a caller never has to know
 * which of the two produced the item it is holding. */
function reshapeServerAdvice(item) {
  return {
    seq: item.seq,
    ts: item.ts,
    turn: item.turn,
    step: item.step,
    phase: item.phase,
    player: item.player || null,
    summary: item.summary === undefined ? null : item.summary,
    reasoning: item.reasoning || null,
    path: item.path === undefined ? null : item.path,
    body: item.body === undefined ? null : item.body,
    basedOnSeq: item.based_on_seq === undefined ? null : item.based_on_seq,
    validated: item.validated === undefined ? null : item.validated,
    approved: !!item.approved,
    approvedSeq: item.approved_seq === undefined ? null : item.approved_seq,
    did: item.did ? frameOf(item.did) : null,
    followed: !!item.followed,
  };
}

/** Every piece of advice in this review, as the page reads it - `pairingOf`'s
 * mirror for the advice stream. Prefers the server's own computed `review.
 * advice` (reshaped to camelCase); falls back to deriving it directly from
 * `review.entries` for a payload that carries no such list at all. */
export function adviceOf(review) {
  const provided = review && Array.isArray(review.advice) ? review.advice : null;
  if (provided) return provided.map(reshapeServerAdvice);
  return deriveAdvice((review && review.entries) || []);
}

/** An `ADVICE_ACTION` row `adviceOf`'s own list does not cover - the
 * row-level fallback `pairingFromRow` is for declarations. Everything the
 * row itself carries still shows; only the cross-referenced pairing
 * (`approved`/`did`/`followed`) is left at its unknown default, since
 * establishing those needs the rest of the ledger this single row does not
 * carry. */
function adviceFromRow(row) {
  const detail = row.detail || {};
  return {
    seq: row.seq,
    ts: row.ts,
    turn: row.turn,
    step: row.step,
    phase: row.phase,
    player: detail.player || null,
    summary: detail.summary === undefined ? null : detail.summary,
    reasoning: row.reasoning || detail.reasoning || null,
    path: detail.path === undefined ? null : detail.path,
    body: detail.body === undefined ? null : detail.body,
    basedOnSeq: detail.based_on_seq === undefined ? null : detail.based_on_seq,
    validated: detail.validated === undefined ? null : detail.validated,
    approved: false,
    approvedSeq: null,
    did: null,
    followed: false,
  };
}

/** One `ADVICE_APPROVED_ACTION` row as a low-weight frame: never routine (so
 * `collapseRoutine` never folds it into an unrelated run of `advance`/
 * `pass_priority` rows under that run's own fixed label), but labelled
 * plainly by the advice it approves rather than by `frameLabel`'s generic
 * "advice_approved". */
function approvalFrameOf(entry) {
  const detail = entry.detail || {};
  const adviceSeq = detail.advice_seq === undefined ? null : detail.advice_seq;
  return {
    ...frameOf(entry),
    isApproval: true,
    adviceSeq,
    label: `approving advice #${adviceSeq === null ? "?" : adviceSeq}`,
  };
}

/**
 * The ledger as the page reads it: one group per turn, in order, each
 * carrying the declarations made in it, the advice given in it, and every
 * other row as a frame.
 *
 * A new group starts whenever the row's `turn` changes rather than by
 * bucketing on the number, so the order the ledger was written in is the
 * order the page shows - a group is a stretch of the game, never a
 * re-sorting of it.
 *
 * A `plan_declared` row is never a frame: it is the turn's header, and
 * showing it twice would make a declaration look like an action taken. An
 * `ADVICE_ACTION` row is the same - the turn's header for that piece of
 * advice - and for the identical reason. An `ADVICE_APPROVED_ACTION` row
 * *is* a frame, since it belongs to the ledger's own order of events rather
 * than to any one turn's header, just a low-weight one (`approvalFrameOf`).
 */
export function reviewTurns(review) {
  const entries = [...((review && review.entries) || [])].sort((a, b) => a.seq - b.seq);
  const players = (review && review.players) || [];
  const declaredBySeq = new Map(
    ((review && review.declarations) || []).map((d) => [d.seq, pairingOf(d, players)])
  );
  const adviceBySeq = new Map(adviceOf(review).map((a) => [a.seq, a]));
  const groups = [];
  let current = null;
  for (const entry of entries) {
    if (!current || current.turn !== entry.turn) {
      current = {
        key: `turn-${entry.turn === null || entry.turn === undefined ? "pregame" : entry.turn}-${entry.seq}`,
        turn: entry.turn === undefined ? null : entry.turn,
        declarations: [],
        advice: [],
        frames: [],
      };
      groups.push(current);
    }
    if (entry.action === PLAN_ACTION) {
      current.declarations.push(declaredBySeq.get(entry.seq) || pairingFromRow(entry, players));
    } else if (entry.action === ADVICE_ACTION) {
      current.advice.push(adviceBySeq.get(entry.seq) || adviceFromRow(entry));
    } else if (entry.action === ADVICE_APPROVED_ACTION) {
      current.frames.push(approvalFrameOf(entry));
    } else {
      current.frames.push(frameOf(entry));
    }
  }
  return groups;
}

/** The same frames, with consecutive routine rows folded into one run.
 *
 * `[{kind: "frame", frame} | {kind: "run", from, to, count, frames}]`, in the
 * order the rows were written. This is a rendering convenience and nothing
 * more: no row is dropped, none is reordered, and a run names the `seq` span
 * it stands for so a reader can count what it covers and open it. Without it
 * the one thing the opponent actually did sits eleventh in a list of twelve
 * identical step markers, which is a real ledger's ordinary shape and a
 * terrible way to show it.
 *
 * A lone routine row is left as itself: folding one row into a run that
 * says "1 row" is longer to read than the row.
 */
export function collapseRoutine(frames) {
  const items = [];
  let run = [];
  const flush = () => {
    if (!run.length) return;
    if (run.length === 1) items.push({ kind: "frame", frame: run[0] });
    else {
      items.push({
        kind: "run",
        from: run[0].seq,
        to: run[run.length - 1].seq,
        count: run.length,
        frames: run,
      });
    }
    run = [];
  };
  for (const frame of frames || []) {
    if (frame.routine) run.push(frame);
    else {
      flush();
      items.push({ kind: "frame", frame });
    }
  }
  flush();
  return items;
}

/** Which seat's side of the table the boards are drawn from: the seat that
 * declared, since the page exists to show that seat's reasoning. The one
 * with the most declarations when both declared, the first seat when
 * neither did. */
export function perspectiveOf(review) {
  const counts = new Map();
  for (const declared of (review && review.declarations) || []) {
    if (!declared.player) continue;
    counts.set(declared.player, (counts.get(declared.player) || 0) + 1);
  }
  let best = null;
  for (const [player, count] of counts) {
    if (best === null || count > counts.get(best)) best = player;
  }
  return best || ((review && review.players) || [])[0] || null;
}

/** The post-game write-up, if this review carries one at all - `null` for
 * every game today (see `REFLECTION_ACTIONS`). It is labelled
 * `afterTheResult` because that is the whole point of separating it: it was
 * written knowing how the game ended, so nothing on the page above it may
 * rest on it. */
export function reflectionOf(review) {
  const carried = review && review.reflection;
  if (carried) {
    const text = typeof carried === "string" ? carried : carried.text || carried.narrative || "";
    if (text) {
      return {
        text,
        player: typeof carried === "string" ? null : carried.player || null,
        seq: typeof carried === "string" ? null : carried.seq === undefined ? null : carried.seq,
        ts: typeof carried === "string" ? null : carried.ts || null,
        afterTheResult: true,
      };
    }
  }
  const entries = (review && review.entries) || [];
  for (const entry of [...entries].reverse()) {
    if (!REFLECTION_ACTIONS.includes(entry.action)) continue;
    const detail = entry.detail || {};
    const text = detail.text || detail.reflection || detail.narrative || entry.reasoning || "";
    if (!text) continue;
    return {
      text,
      player: detail.player || null,
      seq: entry.seq,
      ts: entry.ts,
      afterTheResult: true,
    };
  }
  return null;
}
