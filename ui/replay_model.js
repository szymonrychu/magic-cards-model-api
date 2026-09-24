// The pure replay model behind the "Replay" door that succeeded the old
// "Show thinking" scroll page (`ui/render/review.js`, retired by R5) - no
// DOM and no timers, so `tests/test_ui_replay_model.py` can ask it the
// awkward questions directly, the same seam `ui/review_model.js` and
// `ui/board_layout.js` already use.
//
// The replay's whole argument was different from the scroll page's: the
// scroll page grouped by turn and paired a declaration with its outcome;
// this module instead walks the ledger as a *sequence of decision points* a
// viewer steps through, one board at a time, with a thinking-log message
// beside each one - closer to the live game than to a report. It reuses
// `review_model.js` for everything that module already got right (parsing
// a text snapshot back into cards, the console-style row label, whose
// perspective a review is drawn from) and adds nothing that duplicates it.
//
// Five pieces, one per section below: which ledger rows are a *step*
// (`replaySteps`), which rows become a *message* in the thinking log and how
// they align to steps by `seq` (`logMessages`/`messagesAt`), folding a run of
// consecutive identical messages (a `pass_until` run's own repeated
// reasoning, most often) into one with a repeat count (`collapseMessages`), a
// ledger row's snapshot turned into the exact state-view shape the live
// board's own renderers read (`snapshotView`/`omniscient`), and the
// play/pause/step transport as pure reducers over an injected clock.

import {
  PLAN_ACTION,
  GAME_OVER_ACTION,
  ADVICE_ACTION,
  ADVICE_APPROVED_ACTION,
  REFLECTION_ACTIONS,
  AI_REVIEW_ACTIONS,
  ROUTINE_ACTIONS,
  frameState,
  frameLabel,
  adviceOf,
  perspectiveOf,
} from "./review_model.js";

// ------------------------------------------------------------- replaySteps

/** `entry.action`s that are never a decision point and never routine either
 * - a row that exists to say something, not to have happened. Every one of
 * these becomes a message instead (`logMessages`), never a step. Mirrors
 * `review_model.js`'s own constants for the four it already names
 * (`PLAN_ACTION`, `ADVICE_ACTION`, `ADVICE_APPROVED_ACTION`,
 * `REFLECTION_ACTIONS`, `AI_REVIEW_ACTIONS`) plus the ones only this module
 * needs to know by name. `script_run*` is matched by prefix - `audit.py`
 * has three spellings (`script_run_started`, `script_run`,
 * `script_run_offered`) and a differently-named fourth would still be a
 * script event, not a seat's move. */
function isMetaAction(action) {
  if (action === PLAN_ACTION) return true;
  if (action === ADVICE_ACTION || action === ADVICE_APPROVED_ACTION) return true;
  if (REFLECTION_ACTIONS.includes(action)) return true;
  if (AI_REVIEW_ACTIONS.includes(action)) return true;
  if (action === "ai_review_failed") return true;
  if (action === "report_problem") return true;
  if (action === "pregame_overruled") return true;
  if (typeof action === "string" && action.startsWith("script_run")) return true;
  return false;
}

/** A ledger row that can never be a step, whatever its `allowed`: a meta
 * row (see `isMetaAction`) or one `audit.record` wrote with no `snapshot`
 * at all (a game the store no longer held). The latter covers `create_game`
 * and anything else the ledger carries that this module has no board to
 * draw for it - a step with nothing to show is not a decision point on
 * screen, it is a gap. */
function isMeta(entry) {
  return isMetaAction(entry.action) || !entry.snapshot;
}

/** The seat this row is about, or `null` - the same `detail.player` every
 * other reader of this ledger (`review_model.js`'s `frameOf`,
 * `advice_model.js`'s `viewOf`) already reads it as. */
function actorOf(entry) {
  return (entry.detail || {}).player || null;
}

/** Whether the engine refused this row - `detail.allowed === false`, the
 * one flag that overrides everything else about whether a row is routine:
 * a refused `pass_priority` (there is no such action, but the principle
 * holds for any action) is evidence, never a routine tick. */
function isRefused(entry) {
  return (entry.detail || {}).allowed === false;
}

/** Whether `entry` is the board simply advancing rather than a seat
 * choosing something - `ROUTINE_ACTIONS`, and never true for a row the
 * engine refused (see `isRefused`). */
function isRoutine(entry) {
  return !isRefused(entry) && ROUTINE_ACTIONS.includes(entry.action);
}

function stepOf(entry) {
  return {
    seq: entry.seq,
    entry,
    label: frameLabel(entry),
    actor: actorOf(entry),
    refused: isRefused(entry),
    folded: [],
  };
}

/**
 * Every ledger row that is a real seat action - a decision point a viewer
 * can step to - in `seq` order: `[{seq, entry, label, actor, refused,
 * folded: [seq...]}]`.
 *
 * A row qualifies when it carries a snapshot, is not one of the meta
 * actions (`isMeta`), and is either not routine or was refused (a refusal
 * is always a step, whatever action it names - CR trainability is the
 * whole point of this engine, and a refused attempt is the evidence).
 * `game_over` needs no special case: it is never meta and never routine, so
 * it becomes a step - the final board - on the same rule as everything
 * else.
 *
 * A run of routine rows (`advance`, `pass_priority`, and a row the engine
 * refused is never counted as routine) never becomes steps of its own: each
 * one's `seq` is collected and handed to the *next* step as `folded`, so a
 * long stretch of passes shows up as one line under the step that follows
 * it rather than as a dozen near-identical stops. A trailing run with no
 * step after it - the game ended, or the ledger's tail is nothing but
 * `advance` rows - becomes one final step at the *last* of those rows, with
 * the earlier ones in that same run folded into it; this is the only case
 * where a routine row's own action becomes a step's action.
 */
export function replaySteps(review) {
  const entries = [...((review && review.entries) || [])].sort((a, b) => a.seq - b.seq);
  const steps = [];
  let foldedSeqs = [];
  let lastRoutineEntry = null;
  for (const entry of entries) {
    if (isMeta(entry)) continue;
    if (isRoutine(entry)) {
      foldedSeqs.push(entry.seq);
      lastRoutineEntry = entry;
      continue;
    }
    const step = stepOf(entry);
    step.folded = foldedSeqs;
    steps.push(step);
    foldedSeqs = [];
    lastRoutineEntry = null;
  }
  if (foldedSeqs.length) {
    const step = stepOf(lastRoutineEntry);
    step.folded = foldedSeqs.slice(0, -1);
    steps.push(step);
  }
  return steps;
}

// ------------------------------------------------------------ logMessages

/** The step (from `replaySteps`) this exact `seq` names, or `null`. */
function stepAtSeq(seq, stepBySeq) {
  return stepBySeq.get(seq) || null;
}

/** The index into `steps` a message at `seq` aligns to: the first step at
 * or after `seq`, or the last step when `seq` falls after every step (a
 * reflection or AI review, always written after `game_over`) - so a message
 * with no step of its own still has somewhere to attach, and it is always
 * the *later* side of the gap it falls in rather than the earlier one. `0`
 * for a game with no steps at all. */
function stepIndexFor(seq, steps) {
  for (let i = 0; i < steps.length; i += 1) {
    if (steps[i].seq >= seq) return i;
  }
  return steps.length ? steps.length - 1 : 0;
}

/** `text`, with the engine's own refusal reason appended verbatim - never
 * reworded, since a sceptic reading this log should see the exact words the
 * engine used to say no. */
function withRefusalReason(text, entry) {
  const reason = (entry.detail || {}).reason || "";
  const said = reason ? `Refused: ${reason}` : "";
  if (text && said) return `${text}\n\n${said}`;
  return text || said;
}

/** `plan_declared`'s four fields, compact - board read, role, plan, and the
 * prediction's own `expecting`/`because`/`confidence` (`PREDICTION` in
 * `review_model.js`'s own tests is this same shape). */
function planText(detail) {
  const bits = [];
  if (detail.board_read) bits.push(`Board: ${detail.board_read}`);
  if (detail.my_role) bits.push(`Role: ${detail.my_role}`);
  if (detail.plan) bits.push(`Plan: ${detail.plan}`);
  const prediction = detail.prediction;
  if (prediction && prediction.expecting) {
    const because = prediction.because ? ` (${prediction.because})` : "";
    const confidence = prediction.confidence ? ` [${prediction.confidence}]` : "";
    bits.push(`Predicts: ${prediction.expecting}${because}${confidence}`);
  }
  return bits.join("\n");
}

/** One `script_run*` row, compact - whatever of `script_id`/`role`/`note`
 * the request actually carried (`app.py`'s `finish` "writes one `script_run`
 * row with every field of the request", which this module cannot enumerate
 * without importing the server's own schema, so it reads defensively). */
function scriptRunText(detail) {
  const bits = [];
  if (detail.script_id) bits.push(String(detail.script_id));
  if (detail.role) bits.push(String(detail.role));
  if (detail.note) bits.push(String(detail.note));
  return bits.join(" - ");
}

/**
 * Every message the thinking log shows, in `seq` order: `[{seq, kind,
 * actor, title, text, stepIndex}]`.
 *
 * One row of `review.entries` produces at most one message - never more,
 * and for most rows (`create_game`, a routine row with no `reasoning`)
 * none at all, since a message for every one of a real game's few hundred
 * `advance` rows would bury the ones worth reading. `steps` (from
 * `replaySteps`) decides two things here: which rows are step rows (`kind:
 * "action"`/`"refused"`, reading the step's own precomputed `label`/`actor`
 * rather than recomputing them) and, for every message, which step it
 * aligns to (`stepIndexFor`).
 */
export function logMessages(review, steps) {
  const entries = [...((review && review.entries) || [])].sort((a, b) => a.seq - b.seq);
  const stepBySeq = new Map((steps || []).map((s) => [s.seq, s]));
  const adviceBySeq = new Map(adviceOf(review).map((a) => [a.seq, a]));
  const messages = [];
  const push = (entry, kind, actor, title, text) => {
    messages.push({
      seq: entry.seq,
      kind,
      actor: actor || null,
      title: title || "",
      text: text || "",
      stepIndex: stepIndexFor(entry.seq, steps || []),
    });
  };

  for (const entry of entries) {
    const detail = entry.detail || {};
    const step = stepAtSeq(entry.seq, stepBySeq);

    if (step && entry.action !== GAME_OVER_ACTION) {
      const text = step.refused ? withRefusalReason(entry.reasoning || "", entry) : entry.reasoning || "";
      push(entry, step.refused ? "refused" : "action", step.actor, step.label, text);
      continue;
    }
    if (entry.action === GAME_OVER_ACTION) {
      const note = detail.note || (detail.winner ? `${detail.winner} wins` : "the game is a draw");
      push(entry, "game_over", null, "Game over", note);
      continue;
    }
    if (entry.action === PLAN_ACTION) {
      push(entry, "plan", detail.player, "Plan", planText(detail));
      continue;
    }
    if (entry.action === ADVICE_ACTION) {
      const advice = adviceBySeq.get(entry.seq);
      const summary = advice ? advice.summary : detail.summary;
      const reasoning = advice ? advice.reasoning : entry.reasoning || detail.reasoning || null;
      const text = [summary, reasoning].filter(Boolean).join(" - ");
      push(entry, "advice", detail.player, "Advice", text);
      continue;
    }
    if (entry.action === ADVICE_APPROVED_ACTION) {
      const adviceSeq = detail.advice_seq === undefined ? null : detail.advice_seq;
      const approved = adviceSeq === null ? null : adviceBySeq.get(adviceSeq);
      const title = `approving advice #${adviceSeq === null ? "?" : adviceSeq}`;
      push(entry, "advice_approved", detail.player, title, (approved && approved.summary) || "");
      continue;
    }
    if (REFLECTION_ACTIONS.includes(entry.action)) {
      const text = detail.text || detail.reflection || detail.narrative || entry.reasoning || "";
      push(entry, "reflection", detail.player, "Reflection", text);
      continue;
    }
    if (AI_REVIEW_ACTIONS.includes(entry.action)) {
      const text = detail.text || entry.reasoning || "";
      push(entry, "ai_review", detail.player, "AI review", text);
      continue;
    }
    if (entry.action === "ai_review_failed") {
      const text = detail.reason || "";
      const title = detail.stage ? `AI review failed (${detail.stage})` : "AI review failed";
      push(entry, "ai_review_failed", detail.player, title, text);
      continue;
    }
    if (entry.action === "report_problem") {
      // DR2: `category` (`engine`/`dsl`/`ux`, `audit.ReportProblemCategory`)
      // - a row written before the field existed carries no `category` key
      // at all and reads as `engine`, `audit.problem_category`'s own
      // default, so a legacy row never shows as uncategorized here.
      const category = detail.category || "engine";
      push(entry, "report_problem", detail.player, `Problem reported (${category})`, detail.text || "");
      continue;
    }
    if (entry.action === "pregame_overruled") {
      push(entry, "pregame_overruled", detail.player, "Pregame decision overruled", detail.text || "");
      continue;
    }
    if (typeof entry.action === "string" && entry.action.startsWith("script_run")) {
      push(entry, "script_run", detail.seat || detail.player, entry.action, scriptRunText(detail));
      continue;
    }
    // A routine row (`advance`, `pass_priority`) that was not promoted into
    // a step - the only remaining case worth a message, and only when the
    // seat actually said something.
    if (!step && isRoutine(entry) && entry.reasoning) {
      push(entry, "pass", actorOf(entry), frameLabel(entry), entry.reasoning);
    }
    // Every other row (`create_game`, a routine row with no reasoning, a
    // meta row with nothing to say) produces no message at all.
  }
  return messages;
}

/**
 * The messages a viewer at step index `i` should see: everything with
 * `seq <= steps[i].seq`, except at the *last* step, which shows every
 * message regardless of `seq` - the reflection and the AI reviews are
 * written after `game_over`, and the point of reaching the last step is to
 * see the whole retrospective, not just the board's own final frame.
 *
 * Monotonic in `i` by construction: `steps` is `seq`-ascending
 * (`replaySteps` builds it that way), so the boundary only grows as `i`
 * grows, and growing a `<=` filter's boundary can only add messages, never
 * remove one already shown. `[]` for an empty game (`steps` has nothing to
 * index).
 */
export function messagesAt(messages, steps, i) {
  if (!steps || !steps.length) return [];
  const index = Math.max(0, Math.min(i, steps.length - 1));
  if (index === steps.length - 1) return [...(messages || [])];
  const boundary = steps[index].seq;
  return (messages || []).filter((m) => m.seq <= boundary);
}

// ---------------------------------------------------------- collapseMessages

/** Whether `a` and `b` are the same repeated occurrence, worth folding
 * together rather than shown twice: same `actor` (including two `null`
 * actors, which do merge - a `game_over` row has none, and two of those
 * still repeating each other would be exactly as redundant), same `kind`,
 * same `text`. `title` is deliberately left out of the comparison, and
 * `seq`/`stepIndex` necessarily differ between any two real rows, so
 * neither belongs in it either.
 *
 * The concrete case this exists for: `seatkit._pass_until_once` threads one
 * `reasoning` string onto *every* individual pass a `pass_until` run makes
 * ("I am auto-passing through this stretch" is given once for the whole
 * run, never a fresh one per pass - its own docstring says so), so a long
 * auto-pass run writes several `pass_priority` ledger rows that carry
 * identical `actor`/`text` by design. That is a faithful ledger, one row
 * per real priority pass, exactly as it should be - `audit.record` needs no
 * change - but `logMessages` turning each of those rows into its own
 * message is what put the same line on screen four times in a row (the
 * owner's own screenshot). Fixing it here, at the one place that decides
 * what a viewer sees, is the whole point of this function. */
function sameMessage(a, b) {
  return a.actor === b.actor && a.kind === b.kind && a.text === b.text;
}

/**
 * `messages` (`logMessages`'s own shape, or any subset of it) with every
 * run of *consecutive*, identical (`sameMessage`) entries folded into one,
 * carrying `count` (how many collapsed into it), `firstSeq`/`lastSeq` and
 * `firstStepIndex`/`lastStepIndex` - the step range the run spans. A
 * message that never repeats comes back unchanged in every field it
 * already had, plus `count: 1` and every `first*`/`last*` equal to its own
 * `seq`/`stepIndex` - so a caller never has to branch on whether collapsing
 * actually happened before reading `count`.
 *
 * Only ever merges what is *adjacent* in `messages` as handed in: a
 * different message between two identical ones (whatever produced it)
 * keeps them apart, and two messages naming different actors never merge,
 * whatever their `kind`/`text` (`sameMessage`, above). Nothing here re-
 * sorts or re-filters - it trusts `messages` is already in the order and
 * subset the caller wants shown, which is exactly what makes composing
 * this with `messagesAt` do the right thing: `render/replay.js` calls this
 * on `messagesAt`'s own filtered output, never on the full log, so a
 * four-row run only reads `count: 4` once every one of those four rows has
 * actually scrolled into view - stepping backward into the middle of that
 * same run correctly shows `count: 2` or `count: 3`, because
 * `messagesAt` already trimmed the run down to however many of those rows
 * are visible at that step before this function ever sees it. A run of
 * identical *routine* rows (the `pass_until` case above) almost always
 * collapses to a single-step range, since `logMessages` gives every folded
 * routine row the same *next* real step's index; a run of identical
 * *unfolded* rows - several refusals of the same illegal play in a row,
 * say, each one its own step because `replaySteps` never folds a refusal -
 * genuinely spans one index per row, and the range here reflects that.
 */
export function collapseMessages(messages) {
  const list = messages || [];
  const out = [];
  for (const m of list) {
    const last = out[out.length - 1];
    if (last && sameMessage(last, m)) {
      last.count += 1;
      last.lastSeq = m.seq;
      last.lastStepIndex = m.stepIndex;
      continue;
    }
    out.push({
      ...m,
      count: 1,
      firstSeq: m.seq,
      lastSeq: m.seq,
      firstStepIndex: m.stepIndex,
      lastStepIndex: m.stepIndex,
    });
  }
  return out;
}

// ------------------------------------------------------------- perspective

/** Whose side of the table the replay opens on: the review's own recorded
 * `viewer` when it has one (R2's own addition - absent from every review
 * this module was written against, and treated as optional here for
 * exactly that reason), else `review_model.js`'s own `perspectiveOf` - the
 * seat that declared the most, or the first seat when neither did. */
export function perspective(review) {
  return (review && review.viewer) || perspectiveOf(review);
}

// ------------------------------------------------------------ snapshotView

/** One seat's `player` entry the live board's badge and topbar read:
 * `username`, `life`, `hand_count` - `library_count` is left off rather than
 * fabricated, since a text snapshot never carried a library count at all
 * (only the board, both hands, the stack, graveyards, exile, life and
 * mana pools - `review_model.js`'s own module docstring), and `render/
 * board.js`'s badge only ever prints whatever this object actually has. */
function playerViewOf(seat, frame) {
  const life = frame.life && frame.life[seat];
  return {
    username: seat,
    life: life === undefined ? 0 : life,
    hand_count: (frame.hands[seat] || []).length,
    mana_pool: (frame.pools && frame.pools[seat]) || {},
  };
}

/**
 * One ledger row's `snapshot`, turned into the `GameStateView` shape
 * `board_layout.js`'s `boardLayout`/`seatRows`/`zoneRibbon`,
 * `render/board.js`'s `mountBoard`, and `render/panels.js`'s
 * `renderTurnInfo`/`renderPhaseTrack` all already read - so the replay
 * draws with the exact renderers the live board uses, none of them touched
 * by this task. `null` when `entry` carries no snapshot at all (a meta
 * row, or a game the store no longer held when this row was written).
 *
 * `catalogue` is `frameState`'s own second argument - printings keyed by
 * card name, joined in for the type line and art (`render/replay.js`
 * builds one this way, via `ui/catalogue.js`'s shared batched lookup).
 *
 * This is deliberately the *fallback* view, not the omniscient one:
 * `known_cards[you]` is left empty, so `opponentHandItems` (`render/
 * board.js`, unchanged) draws the opponent's hand as card backs, exactly
 * as a live, redacted view would. `omniscient` below is what turns this
 * into the replay's real drawing - every card face up, both seats' hands
 * shown - because a finished game's review is full information by design
 * (`review.py`'s own docstring) and the replay has no reason to hide what
 * the ledger already gives it.
 */
export function snapshotView(entry, catalogue, you) {
  const frame = frameState(entry && entry.snapshot, catalogue);
  if (!frame) return null;
  const seats = frame.seats && frame.seats.length ? frame.seats : Object.keys(frame.hands || {});
  const knownCards = {};
  if (you !== undefined && you !== null) {
    knownCards[you] = { hand: [], library: [], sideboard: [] };
  }
  return {
    you: you === undefined ? null : you,
    players: seats.map((seat) => playerViewOf(seat, frame)),
    battlefield: frame.battlefield,
    hands: frame.hands,
    graveyards: frame.graveyards,
    exile: frame.exile,
    stack: frame.stack,
    turn_number: entry.turn === undefined ? null : entry.turn,
    step: entry.step || null,
    active_player: entry.active_player || null,
    priority_holder: entry.priority_holder || null,
    play_permissions: {},
    known_cards: knownCards,
  };
}

/**
 * A copy of `view` (from `snapshotView`, or any view of the same shape)
 * with the opponent's hand made visible: `known_cards[you].hand` is filled
 * with every card in `hands[opponent]`, each carrying `owner: opponent` -
 * the exact field `opponentHandItems` (`render/board.js`) filters on, so
 * that function draws them face up with no change of its own. `you` is
 * forced to the given perspective (the argument, not whatever `view.you`
 * already was), so flipping sides for a "whose eyes" toggle is one call.
 *
 * Every other field of `view` is carried over unchanged - this never
 * invents a card, only relabels ones already on the view (a review is
 * full-information at the top level; only `known_cards`, the field a
 * live/redacted view would leave sparse, needed filling in).
 */
export function omniscient(view, you) {
  if (!view) return view;
  const players = view.players || [];
  const opponent = players.map((p) => p.username).find((name) => name !== you) || null;
  const opponentHand = (opponent && view.hands && view.hands[opponent]) || [];
  const hand = opponentHand.map((card) => ({ ...card, owner: opponent }));
  const knownCards = { ...(view.known_cards || {}) };
  knownCards[you] = { ...(knownCards[you] || { library: [], sideboard: [] }), hand };
  return { ...view, you, known_cards: knownCards };
}

// --------------------------------------------------------------- transport

/** The board's floor is empty rather than negative - `clampIndex` treats a
 * non-positive `count` as "nothing to index", always `0`. */
function clampIndex(index, count) {
  if (!count || count <= 0) return 0;
  return Math.max(0, Math.min(index, count - 1));
}

/**
 * The transport's starting state: `{index: 0, playing, lastAt: now, count,
 * intervalMs}` - `playing` true only when there is more than one step to
 * autoplay through (`count === 0` starts paused, since there is nowhere to
 * advance to and nothing to draw). `intervalMs` defaults to 5000 (the
 * owner's "5s for each"), and is carried on the state itself (rather than
 * threaded through every reducer call) so `tick` can read it without a
 * second argument.
 */
export function createTransport({ count, intervalMs = 5000, now }) {
  return {
    index: 0,
    playing: (count || 0) > 0,
    lastAt: now,
    count: count || 0,
    intervalMs,
  };
}

/**
 * One autoplay tick: advances one step once `now - lastAt >= intervalMs`,
 * and stops there - a no-op while paused, while there is nothing to index
 * (`count <= 0`), or before the interval has actually elapsed, so a caller
 * can tick this every animation frame and it costs nothing between real
 * advances. At the last index, elapsing the interval again stops autoplay
 * (`playing: false`) rather than trying to advance past the end - the owner's
 * "it can be paused/played" implies autoplay itself ends rather than
 * silently looping.
 */
export function tick(t, now) {
  if (!t.playing || t.count <= 0) return t;
  if (now - t.lastAt < t.intervalMs) return t;
  if (t.index >= t.count - 1) return { ...t, playing: false, lastAt: now };
  return { ...t, index: t.index + 1, lastAt: now };
}

/** One step forward, clamped to `count - 1`, resetting the autoplay clock
 * and leaving `playing` exactly as it was - a manual step during autoplay
 * does not pause it, it just moves the next tick's window. */
export function forward(t, now) {
  return { ...t, index: clampIndex(t.index + 1, t.count), lastAt: now };
}

/** One step back - `forward`'s mirror. */
export function back(t, now) {
  return { ...t, index: clampIndex(t.index - 1, t.count), lastAt: now };
}

/** Jump straight to index `i`, clamped to `[0, count - 1]` - a scrubber
 * drag, or the transport's own end/home. */
export function seek(t, i, now) {
  return { ...t, index: clampIndex(i, t.count), lastAt: now };
}

/**
 * Play/pause. Pausing holds the index exactly where it is (autoplay's own
 * `tick` will not move it again while `playing` is false). Resuming from
 * the last index restarts from the beginning - autoplay having reached the
 * end and stopped, pressing play again means "watch it again", not "do
 * nothing" - and resuming anywhere else keeps the current index. A
 * transport with nothing to play (`count <= 0`) never starts playing at
 * all, whatever `toggle` is asked to do.
 */
export function toggle(t, now) {
  if (t.count <= 0) return { ...t, playing: false, lastAt: now };
  if (t.playing) return { ...t, playing: false, lastAt: now };
  const atEnd = t.index >= t.count - 1;
  return { ...t, playing: true, index: atEnd ? 0 : t.index, lastAt: now };
}
