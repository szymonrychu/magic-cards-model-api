// The post-game review: what the agent said it expected, and what actually
// happened next, turn by turn, out of one append-only ledger.
//
// This is the page the whole plan-declaration seam exists for, and it is
// written for one reader: somebody who does not believe an agent can reason
// through a game of Magic. So the layout carries the argument rather than
// decorating it.
//
// 1. **The pairing is one element, two columns, left to right in time.**
//    The prediction (written first, at `seq` N) sits on the left; what the
//    opponent actually did (rows after `seq` N) sits on the right, and both
//    carry their `seq` so the ordering is checkable rather than asserted.
//    They are drawn inside a single `.pairing` node on purpose - the two
//    halves are one claim, and nothing about the page may separate them.
// 2. **Nothing is scored.** No hit rate, no verdict, no "correct"/"wrong"
//    badge. `review.py` says why at length and it is the same reason here: a
//    number is what a sceptic argues with, and the raw pairing is not.
// 3. **The reflection goes last, and is labelled.** Everything above it was
//    written into the ledger while the game was being played and could not
//    have known the result. The reflection was written afterwards, knowing
//    it. That distinction is the whole argument, so the page states it in
//    words rather than leaving it to the reader.
//
// The boards are the game's own boards: `board_layout.js` sorts them and
// `render/board.js`'s `patchStacks` plus `cards.js`'s `cardNode` draw them,
// so a frame looks like the table did. What the snapshot cannot supply -
// a printing's type line and its art - is joined in by name from
// `GET /api/cards/{name}`, which is also what puts the lands in the land row
// (`board_layout.isLandRow` reads a type line, and the snapshot has none).
// The page renders immediately with text cards and re-renders once those
// printings arrive, so a cold cache delays the art, never the reading.
//
// Hidden information: this page is only ever reached for a finished game
// (`render/lobby.js` only offers the door for one), and `GET .../review`
// refuses with 409 for a live one. Both gates matter - a live game's plans
// and reasoning are exactly what one seat must not read while the other is
// still playing - and the 409's own message is what gets rendered here
// rather than a friendlier one invented locally.

import { api } from "../api.js";
import { clipRationale } from "../advice_model.js";
import { apiBase } from "../config.js";
import { h, clear } from "../dom.js";
import { cardNode, updateCardNode } from "../cards.js";
import { boardLayout, seatRows } from "../board_layout.js";
import { patchStacks } from "./board.js";
import {
  reviewTurns,
  frameState,
  cardNamesIn,
  perspectiveOf,
  aiReviewOf,
  reflectionOf,
  collapseRoutine,
  adviceOf,
} from "../review_model.js";

/** How many printings to look up at once. The lookups are one HTTP call per
 * distinct card name (`GET /api/cards/{name}`, Scryfall behind a cache), and
 * a game touches a few dozen names at most - this only keeps a cold cache
 * from opening fifty sockets at once. */
const LOOKUP_BATCH = 6;

function text(node) {
  return node === null || node === undefined ? "" : String(node);
}

/** `#123`, the ledger's own way of naming a row - the same form
 * `audit.py`'s console line uses, so a reader can match a line on this page
 * against the server's log. */
function seqLabel(seq) {
  return `#${seq}`;
}

function seqRange(frames) {
  if (!frames.length) return "";
  const first = frames[0].seq;
  const last = frames[frames.length - 1].seq;
  return first === last ? seqLabel(first) : `${seqLabel(first)} to ${seqLabel(last)}`;
}

function stamp(ts) {
  if (!ts) return "";
  const at = new Date(ts);
  return Number.isNaN(at.getTime()) ? "" : at.toISOString().slice(11, 19);
}

// ---------------------------------------------------------------- boards --

/** One frame's board, drawn the way the game drew it: the seat whose
 * reasoning this page is about at the bottom, its lands nearest it, the
 * other seat mirrored above, the stack and exile in between.
 *
 * Both hands are shown face up. That is legitimate here and only here - the
 * game is over, `GET .../review` has already refused to answer for a live
 * one, and a review that hid half the information would not show what the
 * agent was actually reasoning about. */
function boardNode(snapshot, you, catalogue) {
  const state = frameState(snapshot, catalogue);
  if (!state) return h("div", { class: "review-board empty" }, "no board was recorded for this row");
  const seats = state.seats.length ? state.seats : Object.keys(state.hands);
  const me = seats.includes(you) ? you : seats[0];
  const them = seats.find((seat) => seat !== me) || null;
  const layout = boardLayout(state);
  const mine = seatRows(layout, me);
  const theirs = them ? seatRows(layout, them) : { lands: [], main: [] };

  const board = h("div", { class: "review-board" });
  board.appendChild(seatLine(state, them, "opp"));
  board.appendChild(handRow(state, them, catalogue, "hand-row-opp"));
  board.appendChild(stacksRow(theirs.lands, "lands-row"));
  board.appendChild(stacksRow(theirs.main, "main-row"));
  const middle = h("div", { class: "review-middle" });
  if (state.stack.length) middle.appendChild(stackRow(state.stack));
  if (state.combat.length) middle.appendChild(combatRow(state.combat));
  if (layout.exile.length) middle.appendChild(exileRow(layout.exile));
  if (middle.children.length) board.appendChild(middle);
  board.appendChild(stacksRow(mine.main, "main-row"));
  board.appendChild(stacksRow(mine.lands, "lands-row"));
  board.appendChild(handRow(state, me, catalogue, ""));
  board.appendChild(seatLine(state, me, "you"));
  return board;
}

function stacksRow(entries, extra) {
  const row = h("div", { class: `battlefield-row ${extra}` });
  // The live board's own row renderer, decorating each card with nothing:
  // a review board is read, never clicked.
  patchStacks(row, entries, "card-sm", (el, card) => updateCardNode(el, card, {}));
  return row;
}

function handRow(state, seat, catalogue, extra) {
  const cards = (seat && state.hands[seat]) || [];
  const row = h("div", { class: `hand-row ${extra}` });
  for (const card of cards) row.appendChild(cardNode(card, "card-sm", {}));
  return row;
}

function seatLine(state, seat, side) {
  const life = seat === null ? "" : state.life[seat];
  const graveyard = ((seat && state.graveyards[seat]) || []).length;
  const hand = ((seat && state.hands[seat]) || []).length;
  return h(
    "div",
    { class: `info-row ${side}` },
    h("span", { class: "name" }, text(seat)),
    h("span", { class: "life", title: "life" }, text(life)),
    h("span", { class: "counts" }, `hand ${hand} / graveyard ${graveyard}`)
  );
}

function stackRow(stack) {
  const row = h("div", { class: "stack-row" });
  for (const obj of stack) {
    row.appendChild(
      h(
        "div",
        { class: "stack-object" },
        h("span", { class: "name" }, `${obj.name}${obj.kind === "ability" ? " (ability)" : ""}`),
        h("span", { class: "targets" }, (obj.targets || []).length ? `-> ${obj.targets.join(", ")}` : "")
      )
    );
  }
  return row;
}

function combatRow(lines) {
  return h("div", { class: "review-combat" }, lines.join("; "));
}

function exileRow(cards) {
  const row = h("div", { class: "exile-row" });
  for (const card of cards) row.appendChild(cardNode(card, "card-sm", {}));
  return row;
}

// ---------------------------------------------------------------- frames --

/** One ledger row: what was done, the seat's own stated reason for doing it,
 * the engine's verbatim words when it refused, and the board as it stood.
 *
 * The board is built on demand rather than up front. A real game is a few
 * hundred rows (297 in the game this page was built against) and building
 * every board eagerly would be a few thousand card nodes for a page nobody
 * reads end to end. A frame the agent gave a reason for opens by default -
 * those are the moments the page exists for. */
function frameNode(frame, you, catalogue) {
  const el = h("div", {
    class: `frame${frame.routine ? " routine" : ""}${frame.refused ? " refused" : ""}${frame.isApproval ? " approval" : ""}`,
  });
  const head = h(
    "div",
    { class: "frame-head" },
    h("span", { class: "seq" }, seqLabel(frame.seq)),
    h("span", { class: "when" }, `${frame.step || "?"}${frame.ts ? ` ${stamp(frame.ts)}` : ""}`),
    h("span", { class: "who" }, text(frame.player)),
    h("span", { class: "what" }, frame.label)
  );
  el.appendChild(head);
  if (frame.refused) {
    el.appendChild(
      h(
        "div",
        { class: "frame-refusal" },
        "the engine refused it: ",
        h("span", { class: "refusal" }, frame.reason || "(no reason given)")
      )
    );
  }
  if (frame.reasoning) {
    el.appendChild(h("div", { class: "frame-why" }, h("span", { class: "quiet" }, "because "), frame.reasoning));
  }
  const boardBox = h("div", { class: "frame-board" });
  const toggle = h("button", { class: "ghost frame-toggle" }, "board");
  let drawn = false;
  const draw = () => {
    if (drawn) return;
    boardBox.appendChild(boardNode(frame.snapshot, you, catalogue));
    drawn = true;
  };
  toggle.addEventListener("click", () => {
    draw();
    boardBox.hidden = !boardBox.hidden;
  });
  head.appendChild(toggle);
  el.appendChild(boardBox);
  // Open where the board is the point: a refusal, or an action the seat gave
  // a reason for. A routine row is left closed even when it carries one -
  // an agent seat owes a reason for *every* call it makes, including walking
  // the turn marker forward, and drawing a board for each of those would be
  // a few thousand card nodes nobody asked for.
  if ((frame.reasoning || frame.refused) && !frame.routine) draw();
  else boardBox.hidden = true;
  return el;
}

/** A folded run of routine rows: one line saying what it stands for, which
 * opens into the rows themselves. Nothing is hidden from a reader who wants
 * it - only from one who does not. */
function runNode(item, you, catalogue) {
  const el = h("div", { class: "frame-run" });
  const label = h(
    "div",
    { class: "frame-run-head" },
    h("span", { class: "seq" }, `${seqLabel(item.from)} to ${seqLabel(item.to)}`),
    h("span", { class: "what" }, `${item.count} rows of the turn marker moving on`)
  );
  const rest = h("div", { class: "frame-run-body", hidden: true });
  let drawn = false;
  const toggle = h("button", { class: "ghost frame-toggle" }, "show");
  toggle.addEventListener("click", () => {
    if (!drawn) {
      for (const frame of item.frames) rest.appendChild(frameNode(frame, you, catalogue));
      drawn = true;
    }
    rest.hidden = !rest.hidden;
  });
  label.appendChild(toggle);
  el.appendChild(label);
  el.appendChild(rest);
  return el;
}

// -------------------------------------------------------------- pairings --

/** The page's central element: the declaration on the left, what the
 * opponent actually did on the right, in that order because that is the
 * order they were written.
 *
 * Deliberately not scored, and deliberately not sorted or filtered by
 * agreement with the prediction. The routine rows (the turn marker walking
 * forward) are drawn smaller so the opponent's actual plays lead, but they
 * are all there, in `seq` order, and a reader can count them. */
function pairingNode(declared, you) {
  const before = h(
    "div",
    { class: "pairing-before" },
    h(
      "div",
      { class: "pairing-label" },
      h("span", { class: "tag" }, "before"),
      `${text(declared.player)} wrote this at `,
      h("span", { class: "seq" }, seqLabel(declared.seq)),
      declared.ts ? `, ${stamp(declared.ts)}` : ""
    ),
    h("div", { class: "expecting-label" }, `what ${text(declared.player)} expected ${text(declared.opponent)} to do`),
    h("blockquote", { class: "expecting" }, text(declared.prediction && declared.prediction.expecting)),
    h(
      "div",
      { class: "because" },
      h("span", { class: "quiet" }, "because "),
      text(declared.prediction && declared.prediction.because)
    ),
    h(
      "div",
      { class: "confidence" },
      `confidence: ${text((declared.prediction && declared.prediction.confidence) || "not stated")}`
    )
  );

  const rows = declared.opponentDid;
  const after = h(
    "div",
    { class: "pairing-after" },
    h(
      "div",
      { class: "pairing-label" },
      h("span", { class: "tag" }, "after"),
      `what ${text(declared.opponent) || "the opponent"} actually did next, `,
      h("span", { class: "seq" }, rows.length ? seqRange(rows) : "nothing before the next declaration")
    ),
    outcomeList(rows, declared.player),
    // Said plainly, because the window is the one thing about this pairing a
    // reader could otherwise get wrong: it is not "the rest of this turn",
    // it is everything the opponent did between the line on the left and
    // the declaring seat's next one - which normally runs into the
    // opponent's own turn, and is exactly what the prediction was about.
    h(
      "div",
      { class: "pairing-note" },
      `every row the opponent wrote between that line and ${text(declared.player)}'s next declaration`
    )
  );

  return h(
    "div",
    { class: "pairing" },
    before,
    h("div", { class: "pairing-rule" }),
    after
  );
}

function outcomeList(rows, declaringSeat) {
  if (!rows.length) {
    return h(
      "div",
      { class: "outcome-empty" },
      `${text(declaringSeat)} declared again before the opponent took a single action.`
    );
  }
  const list = h("ul", { class: "outcome-list" });
  for (const entry of collapseRoutine(rows)) {
    if (entry.kind === "run") {
      list.appendChild(
        h(
          "li",
          { class: "outcome routine" },
          h("span", { class: "seq" }, `${seqLabel(entry.from)} to ${seqLabel(entry.to)}`),
          h("span", { class: "what" }, `${entry.count} rows of the turn marker moving on`)
        )
      );
      continue;
    }
    const frame = entry.frame;
    const item = h(
      "li",
      { class: `outcome${frame.refused ? " refused" : ""}` },
      h("span", { class: "seq" }, seqLabel(frame.seq)),
      h("span", { class: "what" }, frame.label)
    );
    if (frame.refused) {
      item.appendChild(h("span", { class: "refusal" }, frame.reason || "(no reason given)"));
    }
    if (frame.reasoning) item.appendChild(h("div", { class: "frame-why" }, frame.reasoning));
    list.appendChild(item);
  }
  return list;
}

/** The declaration's own reading of the board, its role in the matchup, and
 * its plan - context under the pairing rather than beside it. The pairing is
 * the evidence; this is the agent showing its working, and putting it first
 * would bury the thing a sceptic came to check. */
function declarationContext(declared) {
  const box = h("div", { class: "declaration-context" });
  const lines = [
    ["what it saw", declared.board_read],
    ["its role in the matchup", declared.my_role],
    ["what it meant to do", declared.plan],
  ];
  for (const [label, value] of lines) {
    if (!value) continue;
    box.appendChild(h("div", { class: "context-line" }, h("span", { class: "context-label" }, label), value));
  }
  return box;
}

// ------------------------------------------------------------------ advice --

/** One row of `advice.refused`, drawn like `frameNode`'s own refusal box -
 * what was attempted, and the engine's verbatim reason. This is the single
 * most instructive line the review can show: the engine's whole reason for
 * existing is saying out loud why a refused action is illegal, and dropping
 * that line here (in favour of a blank "nothing happened") would be keeping
 * the least useful half. */
function adviceRefusalNode(frame) {
  return h(
    "div",
    { class: "advice-refusal" },
    h("span", { class: "seq" }, seqLabel(frame.seq)),
    h("span", { class: "what" }, frame.label),
    " - the engine refused it: ",
    h("span", { class: "refusal" }, frame.reason || "(no reason given)")
  );
}

/** Whether what the seat actually did is what was proposed, drawn as its own
 * line above the `did` frame.
 *
 * `review.py` computes `followed` (same action kind for the proposal's
 * `path`, same card where its `body` names one) and `review_model.js`
 * mirrors it, and for a while no renderer read it - so "they approved it and
 * did exactly that" and "they approved it and then did something else"
 * rendered byte-identically, both an `approved` tag over a `did` frame. The
 * second is the one a trainer most needs to see.
 *
 * Deliberately independent of `approved`, because `followed` is: a human can
 * do exactly what the buddy proposed without ever pressing approve, and the
 * tag and this line compose to say so. Only rendered where `did` exists - a
 * "they did something else" line over no move at all would be a claim about
 * a move nobody made, and those two outcomes are already told apart by the
 * refusal box and the empty-outcome line.
 */
function adviceFollowThroughNode(advice) {
  return h(
    "div",
    { class: `advice-followthrough ${advice.followed ? "followed" : "diverged"}` },
    advice.followed
      ? "and that is what they did:"
      : "but they did something else:"
  );
}

/** One piece of mtg-buddy advice: what it proposed, why, whether the human
 * approved it, and what actually happened - four distinguishable outcomes,
 * never a verdict on whether the advice was *good* (`review.py`'s stand on
 * that, restated for the same reason as the declaration pairing above).
 *
 * - **approved and followed** - the follow-through line says "and that is
 *   what they did", and `did` is drawn as a normal `frameNode`, so the
 *   reader sees the same evidence (reasoning, refusal if any, board) any
 *   other row gets.
 * - **approved, then something else** - the same `approved` tag and a real
 *   `did` frame, with the follow-through line saying "but they did something
 *   else". That line is the whole of what separates this from the outcome
 *   above (`adviceFollowThroughNode`).
 * - **approved, attempted, and refused by the engine** - `did` is `None` (a
 *   refusal is a failed attempt, never handed back as "what happened"), but
 *   `advice.refused` is not empty, so an `adviceRefusalNode` renders the
 *   engine's own words in place of the empty-outcome line. It is never left
 *   as an ambiguous blank.
 * - **ignored, something else instead** - the tag reads "never approved",
 *   and `did` is a real frame.
 * - **ignored, nothing at all** - `did` is `None` and `advice.refused` is
 *   also empty, so the honest empty-outcome line is what renders; this is
 *   the only case that reaches it now.
 */
function adviceNode(advice, you, catalogue) {
  const el = h("div", { class: "advice" });
  el.appendChild(
    h(
      "div",
      { class: "advice-label" },
      h("span", { class: "tag" }, "advice"),
      `${text(advice.player)} was advised at `,
      h("span", { class: "seq" }, seqLabel(advice.seq)),
      advice.ts ? `, ${stamp(advice.ts)}` : ""
    )
  );
  el.appendChild(h("div", { class: "advice-summary" }, text(advice.summary)));
  // Clipped to the same cap `POST .../advice` enforces: a row recorded
  // before the cap existed must not render as an unbounded blob.
  const why = clipRationale(advice.reasoning);
  if (why) {
    el.appendChild(h("blockquote", { class: "advice-reasoning" }, why));
  }
  el.appendChild(
    h(
      "div",
      { class: `advice-tag ${advice.approved ? "approved" : "ignored"}` },
      advice.approved
        ? `approved at ${seqLabel(advice.approvedSeq)}`
        : "never approved"
    )
  );
  for (const refused of advice.refused || []) {
    el.appendChild(adviceRefusalNode(refused));
  }
  if (advice.did) {
    el.appendChild(adviceFollowThroughNode(advice));
    el.appendChild(frameNode(advice.did, you, catalogue));
  } else if (!(advice.refused && advice.refused.length)) {
    el.appendChild(
      h(
        "div",
        { class: "advice-outcome-empty" },
        `nothing before ${text(advice.player)}'s next piece of advice`
      )
    );
  }
  return el;
}

// ----------------------------------------------------------------- turns --

/** The turn's own name. `turn 0` is the engine's own stamp for CR 103.5 and
 * CR 103.6 - the mulligan and opening-hand windows, which happen before CR
 * 103.8's first turn - so it is named for what it is rather than shown as a
 * turn number nobody uses. */
function turnTitle(turn) {
  if (turn === null || turn === undefined) return "before the first turn";
  if (turn === 0) return "the pregame";
  return `turn ${turn}`;
}

function turnNode(group, you, catalogue, expectDeclarations) {
  const section = h("section", { class: "rev-turn" });
  const title = turnTitle(group.turn);
  section.appendChild(
    h(
      "h2",
      { class: "rev-turn-head" },
      title,
      h("span", { class: "rev-turn-seq" }, seqRange(group.frames))
    )
  );
  for (const declared of group.declarations) {
    section.appendChild(pairingNode(declared, you));
    section.appendChild(declarationContext(declared));
  }
  // Only where a declaration was actually owed: a game whose seats never
  // declared says so once, at the top of the page, and CR 103.5/103.6's
  // pregame is before any turn a plan could be about (`records.require_plan`
  // exempts it for exactly that reason).
  if (!group.declarations.length && expectDeclarations && group.turn) {
    section.appendChild(h("div", { class: "no-declaration" }, "No plan was declared for this turn."));
  }
  for (const advice of group.advice || []) {
    section.appendChild(adviceNode(advice, you, catalogue));
  }
  const frames = h("div", { class: "frames" });
  for (const item of collapseRoutine(group.frames)) {
    frames.appendChild(
      item.kind === "run" ? runNode(item, you, catalogue) : frameNode(item.frame, you, catalogue)
    );
  }
  section.appendChild(frames);
  return section;
}

/** How this game finished, or an admission that nothing recorded it.
 *
 * The last line is not a fallback for tidiness: a review the server marked
 * `reconstructed` can carry `is_over: null`, meaning the board could not be
 * decoded and no `game_over` row was ever written, so nothing establishes
 * that anybody won. The payload deliberately refuses to invent that
 * (`review.py`'s `unestablished`), and the page must not invent it either -
 * "the game is over" is only ever said when something actually says so. */
function resultLine(review) {
  if (review.drawn) return "the game was a draw";
  if (review.winner) return `${review.winner} won`;
  if (review.ended_at) return "the game was ended before it finished";
  if (review.is_over === false || review.is_over === null || review.is_over === undefined) {
    return "how this game ended is not recorded";
  }
  return "the game is over";
}

/** The banner a review rebuilt from the ledger carries, or `null`.
 *
 * Every frame on this page was recorded when it happened either way - that
 * is what makes the pairing evidence - so nothing here is weaker than usual.
 * What is different is that the head's result did not come off a final
 * board, because this build refuses to decode the one this game was saved
 * with, and a reader is owed that in the engine's own words rather than in
 * a paraphrase of them. */
function reconstructedNode(review) {
  if (!review || !review.reconstructed) return null;
  const note = h("div", { class: "review-reconstructed" });
  note.appendChild(
    h(
      "strong",
      {},
      "Rebuilt from the recorded ledger. "
    )
  );
  note.appendChild(
    h(
      "span",
      {},
      "This build cannot decode the board this game was saved with, so everything below is ",
      "the server's own append-only record of what happened, written as it happened, rather than ",
      "a reading of a final state. The engine's reason: "
    )
  );
  note.appendChild(h("span", { class: "review-refusal" }, text(review.unloadable || "")));
  const missing = (review.unestablished || []).length;
  if (missing) {
    note.appendChild(
      h(
        "span",
        {},
        ` Not established from that record, and shown as unknown rather than guessed: ${(review.unestablished || []).join(", ")}.`
      )
    );
  }
  return note;
}

function headNode(review, handlers, onBack) {
  const head = h("div", { class: "review-head-inner" });
  const back = h("button", { class: "ghost", id: "review-back" }, "Lobby");
  back.addEventListener("click", onBack);
  head.appendChild(back);
  head.appendChild(h("h1", {}, (review.players || []).join(" vs ")));
  head.appendChild(h("div", { class: "review-result" }, resultLine(review)));
  const rebuilt = reconstructedNode(review);
  if (rebuilt) head.appendChild(rebuilt);
  // How much evidence there is, in the ledger's own units. A sceptic's first
  // question about a page like this is how much of the game it actually
  // covers, and the answer is "all of it" - so it is said in numbers.
  const rows = (review.entries || []).length;
  const declarations = (review.declarations || []).length;
  const advice = adviceOf(review).length;
  head.appendChild(
    h(
      "div",
      { class: "review-counts" },
      `${rows} recorded action${rows === 1 ? "" : "s"}, `,
      `${declarations} declaration${declarations === 1 ? "" : "s"}, `,
      `${advice} piece${advice === 1 ? "" : "s"} of advice`,
      rows ? `, ledger ${seqLabel(review.entries[0].seq)} to ${seqLabel(review.entries[rows - 1].seq)}` : ""
    )
  );
  head.appendChild(
    h(
      "p",
      { class: "review-lede" },
      "Read this left to right. On the left of each pairing is what the agent wrote down before it acted; ",
      "on the right is what its opponent then actually did. The ",
      h("span", { class: "seq" }, "#numbers"),
      " are the server's own append-only ledger, one per recorded action and never reissued, so the prediction ",
      "provably precedes every row it is paired with. Nothing here is scored: the two are side by side and the reading is yours."
    )
  );
  const count = ((review && review.declarations) || []).length;
  if (!count) {
    head.appendChild(
      h(
        "div",
        { class: "no-declaration" },
        "No seat in this game declared a plan - which is what every game played before this facility existed looks like, ",
        "and what a game of two human seats looks like. What follows is the ledger itself, board by board."
      )
    );
  }
  return head;
}

function reflectionNode(reflection) {
  return h(
    "section",
    { class: "reflection" },
    h("h2", {}, "Afterwards"),
    h(
      "div",
      { class: "reflection-label" },
      "Written after the result was known, and so evidence of nothing above. ",
      "Everything else on this page was recorded while the game was still being played."
    ),
    h("blockquote", { class: "reflection-text" }, reflection.text),
    reflection.player ? h("div", { class: "reflection-who" }, `- ${reflection.player}`) : null
  );
}

/** The post-game AI review, drawn right after `reflectionNode` (which it
 * follows in `draw` below): a separate section since it is a separate
 * thing - Opus's own opinion of the game, not the seat's own words, and
 * `aiReviewOf`'s own docstring says so. Written after the result exactly
 * like the reflection, for the same reason. */
function aiReviewNode(aiReview) {
  return h(
    "section",
    { class: "ai-review" },
    h("h2", {}, "AI review"),
    h(
      "div",
      { class: "reflection-label" },
      "A separate, Opus-generated critique - written after the result was known, ",
      "reading the seat's own reflection above plus this page's own record. An opinion, not a ruling."
    ),
    h("blockquote", { class: "reflection-text" }, aiReview.text),
    aiReview.player ? h("div", { class: "reflection-who" }, `- reviewing ${aiReview.player}`) : null
  );
}

// ---------------------------------------------------------------- printings

/** The printings the snapshots' bare card names need, by name. One call per
 * distinct name, a few at a time; a name the server cannot answer for is
 * simply absent, and `cards.js` draws that card as its text fallback. */
async function loadCatalogue(names) {
  const catalogue = {};
  for (let index = 0; index < names.length; index += LOOKUP_BATCH) {
    const batch = names.slice(index, index + LOOKUP_BATCH);
    const answers = await Promise.all(
      batch.map((name) =>
        api("GET", `${apiBase()}/cards/${encodeURIComponent(name)}`).catch(() => null)
      )
    );
    for (const answer of answers) {
      if (!answer || !answer.requested) continue;
      const card = answer.card || {};
      const images = answer.images || {};
      catalogue[answer.requested] = {
        type_line: card.type_line || "",
        oracle_text: card.oracle_text || "",
        mana_cost: card.mana_cost || "",
        image: images.normal || images.small || null,
        small_image: images.small || images.normal || null,
      };
    }
  }
  return catalogue;
}

// ------------------------------------------------------------------ mount --

/**
 * Mount the review into `root`'s `#review` section (with `#review-head` and
 * `#review-body` inside it), and return `{open, close}`.
 *
 * `open({gameId, uid})` fetches `GET /api/games/{id}/review?uid=` and draws
 * it, showing the review section and hiding `#lobby`. `close()` reverses
 * that and calls `handlers.onBack`, so the lobby can refresh its table.
 *
 * The fetch is the second gate on hidden information: the lobby only offers
 * the door for a finished game, and this call 409s for a live one. When it
 * does, the server's own message is what the page shows - nothing of the
 * plan is fetched, so nothing of it can be rendered.
 */
export function mountReview(root, handlers) {
  const section = root.querySelector("#review");
  const lobby = root.querySelector("#lobby");
  const head = root.querySelector("#review-head");
  const body = root.querySelector("#review-body");
  if (!section || !head || !body) return { open: async () => {}, close: () => {} };

  function close() {
    section.hidden = true;
    if (lobby) lobby.hidden = false;
    clear(body);
    clear(head);
    if (handlers && handlers.onBack) handlers.onBack();
  }

  function draw(review, catalogue) {
    const you = perspectiveOf(review);
    const expectDeclarations = ((review && review.declarations) || []).length > 0;
    clear(body);
    for (const group of reviewTurns(review)) {
      body.appendChild(turnNode(group, you, catalogue, expectDeclarations));
    }
    const reflection = reflectionOf(review);
    if (reflection) body.appendChild(reflectionNode(reflection));
    const aiReview = aiReviewOf(review);
    if (aiReview) body.appendChild(aiReviewNode(aiReview));
  }

  async function open({ gameId, uid }) {
    section.hidden = false;
    if (lobby) lobby.hidden = true;
    clear(head);
    clear(body);
    body.appendChild(h("div", { class: "review-loading" }, "reading the ledger..."));
    let review;
    try {
      review = await api(
        "GET",
        `${apiBase()}/games/${encodeURIComponent(gameId)}/review?uid=${encodeURIComponent(uid || "")}`
      );
    } catch (err) {
      clear(body);
      head.appendChild(headNode({ players: [], is_over: false }, handlers, close));
      // The server's own words, never a friendlier local paraphrase: a 409
      // here means the game is still being played, and that refusal is the
      // rule this page rests on.
      body.appendChild(h("div", { class: "review-error" }, err.message));
      return;
    }
    clear(head);
    head.appendChild(headNode(review, handlers, close));
    draw(review, {});
    // The printings arrive after the page does, on purpose: the reading is
    // the point and a cold Scryfall cache must not hold it up. When they
    // land, the same page is drawn again with the real art and with lands in
    // the land row.
    loadCatalogue(cardNamesIn(review))
      .then((catalogue) => {
        if (!section.hidden) draw(review, catalogue);
      })
      .catch((err) => console.error(`could not load the card printings: ${err.message}`));
  }

  return { open, close };
}
