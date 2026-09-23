// The "Replay" door: a finished game, driven through the same board/topbar
// the live game uses, but with the ribbon's phase/pass controls swapped for
// a transport (back/play-pause/forward/scrubber) and a thinking log beside
// it instead of a decision panel.
//
// This module draws none of the board itself. `mountBoard`/`mountTopbar`
// (`app.js`'s own callers, unchanged by this task) already subscribe to the
// shared store and read `state`/`you`/`players`/... in exactly the shape
// `ui/replay_model.js`'s `snapshotView`/`omniscient` produce - so this
// module's whole job is to hand the store the right view for the step the
// viewer is on, `store.set({replay: {...}, state: <view>})`, and let those
// renderers do what they already do for a live game. It builds the
// transport bar and the thinking log with `dom.js`'s own `h()`, and nothing
// else.
//
// Two frame sources, and both go through `omniscient` before they reach the
// store, per fork (e)'s recommendation (see the discovery brief): the
// snapshot every ledger row already carries (`snapshotView`, drawn while an
// exact frame is in flight or once one has failed) and, for the steps R2's
// `review.exact_frames` names, the engine's own exact state
// (`GET .../review/frames/{seq}`) - fetched once per `seq` (cached), with
// the next step's own exact frame prefetched so stepping forward rarely
// waits. A response that lands for a step the viewer has already left is
// dropped: `renderToken` is bumped on every navigation, and a fetch only
// applies its answer if the token it captured is still current.
//
// Autoplay is one `setInterval`-shaped timer for the life of an open
// replay, firing every `AUTOPLAY_MS` and handing `now` to
// `ui/replay_model.js`'s own `tick` - which is itself a no-op while paused,
// with nothing left to play, or before its own 5000ms have actually
// elapsed, so this timer costs nothing between real advances and needs no
// separate start/stop bookkeeping of its own beyond `open`/`close`.

import { api } from "../api.js";
import { apiBase } from "../config.js";
import { h, clear } from "../dom.js";
import { loadCatalogue } from "../catalogue.js";
import { cardNamesIn } from "../review_model.js";
import {
  replaySteps,
  logMessages,
  messagesAt,
  perspective,
  snapshotView,
  omniscient,
  createTransport,
  tick,
  forward,
  back,
  seek,
  toggle,
} from "../replay_model.js";

/** The owner's own "5s for each" - fixed, no speed selector (see the
 * discovery brief's owner question 6, left at the default). */
const AUTOPLAY_MS = 5000;

/**
 * Mount the replay's transport bar (`#replay-bar`) and thinking log
 * (`#replay-log`) into `root`, driving `store` and the shared `#lobby`/
 * `#game` visibility exactly the way `mountReview` does today. Returns
 * `{open({gameId, uid}), close()}`.
 *
 * `handlers.setInterval`/`clearInterval`/`now` default to the real globals;
 * a caller (this module's own tests, `tests/test_ui_replay.py`) can inject
 * a fully-controlled fake clock instead, the same seam
 * `ui/replay_model.js`'s own transport reducers already take a `now` for.
 * `handlers.onBack` is called by `close()`, same contract as
 * `mountReview`'s.
 */
export function mountReplay(root, store, handlers) {
  const lobby = root.querySelector("#lobby");
  const game = root.querySelector("#game");
  const bar = root.querySelector("#replay-bar");
  const log = root.querySelector("#replay-log");
  if (!game || !bar || !log) return { open: async () => {}, close: () => {} };

  const opts = handlers || {};
  const setIntervalImpl = opts.setInterval || (typeof setInterval === "undefined" ? null : setInterval);
  const clearIntervalImpl = opts.clearInterval || (typeof clearInterval === "undefined" ? null : clearInterval);
  const nowImpl = opts.now || (() => Date.now());

  // The one open replay's own state - `null` whenever nothing is open.
  // `frameCache` is keyed by ledger `seq` and holds the RAW frame the
  // server returned (never wrapped in `omniscient` - that happens on
  // every read, so a later re-render always picks up the current
  // `you`/perspective rather than one baked in at fetch time).
  let session = null;
  let timer = null;
  // Bumped on every navigation (`open`, forward/back/seek/tick); an
  // in-flight exact-frame fetch only applies its answer if this still
  // matches the token it captured when it was issued - the stale-response
  // guard acceptance (2) asks for.
  let renderToken = 0;

  function stopTimer() {
    if (timer !== null && clearIntervalImpl) clearIntervalImpl(timer);
    timer = null;
  }

  function startTimer() {
    stopTimer();
    if (!setIntervalImpl) return;
    timer = setIntervalImpl(() => {
      if (!session) return;
      const next = tick(session.transport, nowImpl());
      if (next === session.transport) return;
      applyTransport(next);
    }, AUTOPLAY_MS);
  }

  function replayMeta() {
    return {
      gameId: session.gameId,
      uid: session.uid,
      index: session.transport.index,
      playing: session.transport.playing,
      count: session.transport.count,
      exactFrames: [...session.exactSeqs],
    };
  }

  /** `seq`'s exact frame - from the cache once it has landed once, else a
   * fresh request every time this is asked for an uncached `seq`. Concurrent
   * callers (a manual navigation and `prefetchNext` both wanting the same
   * still-uncached `seq`, say, or the viewer leaving a step and coming back
   * to it before its first fetch resolved) are deliberately NOT deduped
   * into one shared request: every caller gets its own promise, tags its own
   * application of the answer with the `renderToken` current when it asked,
   * and `renderFrame`'s own guard (see its own comment) is what keeps a
   * request answered after the viewer moved on from clobbering the frame
   * currently on screen - the same guard whether there were one request in
   * flight for a `seq` or several. */
  function fetchFrame(seq) {
    if (session.frameCache.has(seq)) return Promise.resolve(session.frameCache.get(seq));
    const { gameId, uid } = session;
    return api(
      "GET",
      `${apiBase()}/games/${encodeURIComponent(gameId)}/review/frames/${encodeURIComponent(seq)}?uid=${encodeURIComponent(uid || "")}`
    ).then((view) => {
      if (session) session.frameCache.set(seq, view);
      return view;
    });
  }

  /** Warm the cache for the step after `index`, but only when it is one of
   * `review.exact_frames` and not already cached - a step whose only frame
   * is the fallback snapshot needs no request at all, so none is made. */
  function prefetchNext(index) {
    const next = session.steps[index + 1];
    if (!next) return;
    if (!session.exactSeqs.has(next.seq)) return;
    if (session.frameCache.has(next.seq)) return;
    fetchFrame(next.seq).catch(() => {
      // A failed prefetch is not this step's problem - the step it belongs
      // to will simply fall back to its own snapshot when it is reached,
      // and try the fetch again then.
    });
  }

  /** The view for `steps[index]` right now: the cached exact frame if one
   * has already landed, else the row's own snapshot - both run through
   * `omniscient` so the replay is always full information, never the
   * redacted shape a live board would show. */
  function currentView(index) {
    const step = session.steps[index];
    if (!step) return null;
    const cached = session.frameCache.get(step.seq);
    const raw = cached !== undefined ? cached : snapshotView(step.entry, session.catalogue, session.you);
    return omniscient(raw, session.you);
  }

  /** Draw the frame at `index`: the fallback (or already-cached exact) view
   * lands on the store synchronously, then - only for a `seq` in
   * `exact_frames` not yet cached - the real fetch is kicked off, applying
   * its answer only if nothing has moved the viewer on since (`renderToken`).
   * Always also arms the next step's prefetch. */
  function renderFrame(index) {
    renderToken += 1;
    const myToken = renderToken;
    const step = session.steps[index];
    if (!step) {
      store.set({ replay: replayMeta() });
      renderBar();
      return;
    }
    store.set({ replay: replayMeta(), state: currentView(index) });
    renderBar();
    // Both the fetch and the prefetch are scoped to "this step is one of
    // `exact_frames`" - a routine step (the common case) triggers neither,
    // which is what acceptance (6)'s "an exact frame is requested only for
    // seqs in exact_frames" is checking.
    if (session.exactSeqs.has(step.seq)) {
      if (!session.frameCache.has(step.seq)) {
        fetchFrame(step.seq)
          .then(() => {
            if (!session || renderToken !== myToken) return; // the viewer has moved on - drop it
            store.set({ state: currentView(index) });
          })
          .catch(() => {
            // The fallback already on screen stands; nothing to undo.
          });
      }
      prefetchNext(index);
    }
  }

  function applyTransport(next) {
    if (!session) return;
    const movedIndex = next.index !== session.transport.index;
    session.transport = next;
    if (movedIndex) {
      renderFrame(next.index);
      renderLog();
    } else {
      store.set({ replay: replayMeta() });
      renderBar();
    }
  }

  // ------------------------------------------------------------- bar --

  function messageActorClass(actor) {
    if (!actor) return "replay-msg--neutral";
    return actor === session.you ? "replay-msg--you" : "replay-msg--opp";
  }

  function renderLog() {
    clear(log);
    if (!session) return;
    const index = session.transport.index;
    const shown = messagesAt(session.messages, session.steps, index);
    let currentEl = null;
    for (const msg of shown) {
      const isCurrent = msg.stepIndex === index;
      const el = h(
        "div",
        {
          class: `replay-msg ${messageActorClass(msg.actor)}${isCurrent ? " replay-msg--current" : ""}`,
          dataset: { seq: String(msg.seq) },
        },
        h("div", { class: "replay-msg-title" }, msg.title || ""),
        h("div", { class: "replay-msg-text" }, msg.text || "")
      );
      log.appendChild(el);
      if (isCurrent) currentEl = el;
    }
    if (currentEl && typeof currentEl.scrollIntoView === "function") {
      currentEl.scrollIntoView({ block: "nearest" });
    }
  }

  function renderBar() {
    clear(bar);
    if (!session) return;
    const { index, playing, count } = session.transport;
    const step = session.steps[index];
    bar.appendChild(h("button", { class: "ghost replay-back", type: "button", onClick: () => close() }, "Back to lobby"));
    bar.appendChild(
      h(
        "button",
        { class: "replay-step-back", type: "button", disabled: index <= 0, onClick: () => stepBack() },
        "◀"
      )
    );
    bar.appendChild(
      h("button", { class: "replay-play-pause", type: "button", onClick: () => togglePlay() }, playing ? "Pause" : "Play")
    );
    bar.appendChild(
      h(
        "button",
        {
          class: "replay-step-forward",
          type: "button",
          disabled: index >= count - 1,
          onClick: () => stepForward(),
        },
        "▶"
      )
    );
    bar.appendChild(h("span", { class: "replay-position" }, `${count ? index + 1 : 0} / ${count}`));
    bar.appendChild(
      h("input", {
        type: "range",
        class: "replay-scrubber",
        min: "0",
        max: String(Math.max(0, count - 1)),
        value: String(index),
        onChange: (e) => seekTo(Number(e.target.value)),
      })
    );
    bar.appendChild(h("span", { class: "replay-label" }, step ? step.label : ""));
  }

  function stepForward() {
    if (!session) return;
    applyTransport(forward(session.transport, nowImpl()));
  }

  function stepBack() {
    if (!session) return;
    applyTransport(back(session.transport, nowImpl()));
  }

  function seekTo(i) {
    if (!session) return;
    applyTransport(seek(session.transport, i, nowImpl()));
  }

  function togglePlay() {
    if (!session) return;
    applyTransport(toggle(session.transport, nowImpl()));
  }

  // ------------------------------------------------------------ open/close --

  function showGame() {
    if (lobby) lobby.hidden = true;
    game.hidden = false;
    game.classList.add("replaying");
  }

  function hideGame() {
    game.classList.remove("replaying");
    game.hidden = true;
    if (lobby) lobby.hidden = false;
  }

  async function open({ gameId, uid }) {
    stopTimer();
    session = null;
    renderToken += 1;
    clear(bar);
    clear(log);
    showGame();

    let review;
    try {
      review = await api(
        "GET",
        `${apiBase()}/games/${encodeURIComponent(gameId)}/review?uid=${encodeURIComponent(uid || "")}`
      );
    } catch (err) {
      // The server's own words, never a friendlier local paraphrase - the
      // same rule `mountReview`'s own error path follows, and for the same
      // reason: a 409 here means the game is not over yet, and that
      // refusal is the whole point.
      const backBtn = h(
        "button",
        { class: "ghost replay-error-back", type: "button", onClick: () => close() },
        "Back to lobby"
      );
      log.appendChild(h("div", { class: "replay-error" }, err.message));
      log.appendChild(backBtn);
      return;
    }

    const steps = replaySteps(review);
    const messages = logMessages(review, steps);
    const you = perspective(review);

    session = {
      gameId,
      uid,
      review,
      steps,
      messages,
      you,
      catalogue: {},
      frameCache: new Map(),
      exactSeqs: new Set(review.exact_frames || []),
      transport: createTransport({ count: steps.length, intervalMs: AUTOPLAY_MS, now: nowImpl() }),
    };

    startTimer();
    renderFrame(0);
    renderLog();

    // The printings arrive after the page does, on purpose - the same
    // reasoning as `mountReview`'s own catalogue load: a cold Scryfall
    // cache must never hold up the reading. Only refreshes the *fallback*
    // view - a step already showing its exact frame is left exactly as it
    // is, since the exact frame carries its own art-independent truth and
    // re-deriving its fallback would only race the exact fetch that is
    // already in flight or already landed.
    loadCatalogue(cardNamesIn(review))
      .then((catalogue) => {
        if (!session || session.gameId !== gameId) return;
        session.catalogue = catalogue;
        refreshFallbackArt();
      })
      .catch((err) => console.error(`could not load the card printings: ${err.message}`));
  }

  /** Redraw the current step's view once the card catalogue lands, WITHOUT
   * going through `renderFrame` - this must never bump `renderToken` or
   * start a new exact-frame fetch, or a slow-arriving catalogue would race
   * the viewer's own navigation and could re-fetch (or invalidate) a
   * frame already in flight for a reason that has nothing to do with the
   * catalogue. A step already showing its exact frame is left untouched:
   * the exact frame carries its own art-independent truth, and the
   * catalogue only ever improves the *fallback* snapshot's cards. */
  function refreshFallbackArt() {
    if (!session) return;
    const index = session.transport.index;
    const step = session.steps[index];
    if (!step || session.frameCache.has(step.seq)) return;
    store.set({ state: currentView(index) });
  }

  function close() {
    stopTimer();
    session = null;
    renderToken += 1;
    clear(bar);
    clear(log);
    store.set({ replay: null, state: null });
    hideGame();
    if (opts.onBack) opts.onBack();
  }

  return { open, close };
}
