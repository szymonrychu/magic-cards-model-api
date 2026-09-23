"""`ui/render/replay.js` - the "Replay" door that drives the live game
screen from a finished game's own ledger, instead of drawing a page of its
own.

Run against a fake DOM and a stubbed global `fetch`, the way
`tests/test_ui_review.py` runs its own render module: the real shipped
module, under Node, no bundler and no library. The fake DOM here mirrors the
sliver of R5's markup this module actually touches: `#lobby`, `#game` (with
`#rail-pass` and `#replay-bar` as siblings inside it, per the DOM contract),
and `#side-panel` holding `#replay-log`.

What is pinned here is `mountReplay`'s own contract, never board drawing -
this module hands `store.set({state: ...})` the same `GameStateView` shape
`render/board.js`/`render/panels.js` already read and leaves drawing a card
to them, so nothing here ever builds a `.card` node:

- `open` fetches the review, and on success hides `#lobby`, shows `#game`
  with `replaying`, and seeds the store with the transport state and frame 0;
- on a server error, the error text lands in `#replay-log` with a working
  Back button and no board is ever set;
- a step whose `seq` is one of `review.exact_frames` is fetched exactly
  (cached per `seq`), replacing the fallback view once it lands, but a late
  answer for a step the viewer has already left is ignored;
- autoplay ticks a fixed 5000ms per step via the injected clock/timer,
  stopping at the end; play/pause/step/scrub all go through the same
  transport reducers `ui/replay_model.js` already has tests of its own for;
- the thinking log grows forward and shrinks back, seq-aligned, with the
  current step's message marked and no pass/phase controls anywhere in the
  transport bar.
"""

from __future__ import annotations

from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node

# The `h()`-compatible fake DOM (`test_ui_review.py`'s own shape): a real
# `classList`, `dataset`, `childNodes`/`insertBefore`/`removeChild`, and a
# `textContent` getter that walks the subtree.
FAKE_DOM = """
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.style = {};
    this.hidden = false;
    this.listeners = {};
    this._text = "";
    this.classList = {
      set: new Set(),
      add: (c) => this.classList.set.add(c),
      remove: (c) => this.classList.set.delete(c),
      toggle: (c, on) => (on ? this.classList.set.add(c) : this.classList.set.delete(c)),
      contains: (c) => this.classList.set.has(c),
    };
  }
  set className(v) {
    this.classList.set = new Set(String(v).split(/\\s+/).filter(Boolean));
    this.attrs.class = v;
  }
  get className() { return [...this.classList.set].join(" "); }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    c.parentNode = this;
    return c;
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  removeEventListener(type, fn) { if (this.listeners[type] === fn) delete this.listeners[type]; }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    if (!this.children.length) return this._text;
    return this.children.map((c) => (c.textContent === undefined ? "" : c.textContent)).join(" ");
  }
  querySelector(sel) {
    const walk = (node) => {
      if (!node) return null;
      if (matches(node, sel)) return node;
      for (const c of node.children || []) {
        const found = walk(c);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      if (matches(node, sel)) out.push(node);
      for (const c of node.children || []) walk(c);
    };
    walk(this);
    return out;
  }
}

function matches(node, sel) {
  if (!node || !node.attrs) return false;
  if (sel.startsWith("#")) return node.attrs.id === sel.slice(1);
  if (sel.startsWith(".")) return node.classList && node.classList.contains(sel.slice(1));
  return node.tagName === sel;
}

globalThis.document = new El("#document");
document.createElement = (t) => new El(t);
document.createTextNode = (t) => {
  const n = new El("#text");
  n.textContent = String(t);
  return n;
};

function section(id) {
  const el = document.createElement("section");
  el.setAttribute("id", id);
  document.appendChild(el);
  return el;
}

function findAll(node, cls) {
  const out = [];
  const walk = (n) => {
    if (n.classList && n.classList.contains(cls)) out.push(n);
    for (const c of n.children || []) walk(c);
  };
  walk(node);
  return out;
}

function click(el) {
  if (el && el.listeners && el.listeners.click) el.listeners.click({ target: el });
}

// `#replay-bar` is a sibling of `#rail-pass` inside `#game`; `#replay-log`
// sits inside `#side-panel`, which sits in `#game` too (the drawer on
// mobile) - the DOM contract R5 adds. `#game` starts hidden, `#lobby`
// starts visible, mirroring the real page before a door is opened.
function mountInto() {
  const lobby = section("lobby");
  const game = section("game");
  game.hidden = true;
  const railPass = document.createElement("div");
  railPass.setAttribute("id", "rail-pass");
  game.appendChild(railPass);
  const bar = document.createElement("div");
  bar.setAttribute("id", "replay-bar");
  game.appendChild(bar);
  const sidePanel = document.createElement("div");
  sidePanel.setAttribute("id", "side-panel");
  game.appendChild(sidePanel);
  const log = document.createElement("div");
  log.setAttribute("id", "replay-log");
  sidePanel.appendChild(log);
  return { lobby, game, bar, log };
}

// A minimal `store.js`-compatible store: synchronous merge, no press guard
// (nothing here presses a button that matters to it), a `history` array of
// every `state.set` this module ever also lets `board`/`panels` see -
// exactly what `mountBoard`/`mountTopbar` (`app.js`'s own callers) read.
function makeStore() {
  let state = {};
  const history = [];
  return {
    get: () => state,
    set: (patch) => {
      state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
      history.push(state);
    },
    history,
  };
}

// A fully-controlled fake clock/timer, injected through `handlers` exactly
// as R4's own contract asks: `setInterval`/`clearInterval`/`now`. `advance`
// moves the clock and fires the one interval callback `mountReplay`
// installs, the same way a real 5000ms tick would - so "5000 fake ms
// advance one step" is asserted with arithmetic, never a real sleep.
function makeClock(start) {
  let now = start;
  let cb = null;
  let cleared = false;
  return {
    handlers: {
      setInterval: (fn, ms) => {
        cb = fn;
        return { ms };
      },
      clearInterval: () => {
        cb = null;
        cleared = true;
      },
      now: () => now,
    },
    advance: (ms) => {
      now += ms;
      if (cb) cb();
    },
    get running() {
      return cb !== null;
    },
    get cleared() {
      return cleared;
    },
  };
}
"""


def snapshot(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "life": {"alice": 20, "claude": 17},
        "pools": {"alice": {}, "claude": {}},
        "battlefield": ["alice: Mountain (tapped)", "claude: Grizzly Bears [2/2]"],
        "hands": {"alice": ["Lightning Bolt"], "claude": ["Forest"]},
        "stack": [],
        "graveyards": {"alice": [], "claude": []},
        "exile": [],
        "combat": [],
    }
    base.update(over)
    return base


def entry(seq: int, action: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "seq": seq,
        "ts": f"2026-09-08T20:{seq:02d}:00+00:00",
        "action": action,
        "turn": 4,
        "step": "precombat_main",
        "phase": "precombat_main",
        "detail": {},
        "reasoning": None,
        "snapshot": snapshot(),
        "active_player": "claude",
        "priority_holder": "claude",
    }
    row.update(over)
    return row


#: One of the engine's own refusals, quoted exactly as `audit.py` recorded it.
REFUSAL = "claude cannot play a land: 1 of the 1 they may play this turn already used"

# Four steps: a cast, a pass folded into a decide, a refused land drop, and
# game over. `exact_frames: [102]` marks only the middle step exact, so the
# other three exercise the fallback path and 102 exercises the real one.
ENTRIES = [
    entry(
        101,
        "cast",
        reasoning="a 2/2 now is worth more than the same 2/2 two turns later",
        detail={"player": "claude", "card": "Grizzly Bears", "allowed": True},
    ),
    entry(102, "decide", detail={"player": "alice", "allowed": True}),
    entry(
        103,
        "play_land",
        reasoning="I wanted the fourth land",
        detail={"player": "claude", "card": "Forest", "allowed": False, "reason": REFUSAL},
    ),
    entry(104, "game_over", detail={"winner": "claude"}),
]

REVIEW = {
    "game_id": "g1",
    "players": ["alice", "claude"],
    "created_at": "2026-09-08T20:00:00+00:00",
    "ended_at": None,
    "is_over": True,
    "winner": "claude",
    "drawn": False,
    "ended_seq": 104,
    "declarations": [],
    "exact_frames": [102],
    "entries": ENTRIES,
}

CARD_ANSWER = {
    "requested": "Grizzly Bears",
    "name": "Grizzly Bears",
    "images": {"normal": "http://img/bears.png", "small": "http://img/bears-s.png"},
    "ok": True,
    "card": {"name": "Grizzly Bears", "type_line": "Creature - Bear", "oracle_text": ""},
}

#: The exact frame for seq 102 - deliberately a different shape than what
#: `snapshotView` would fall back to (a `marker`, and a fatter hand for
#: alice), so a test can tell "the exact view landed" apart from "the
#: fallback is still showing" by more than luck.
EXACT_102 = {
    "marker": "exact-102",
    "players": [
        {"username": "alice", "life": 20, "hand_count": 2},
        {"username": "claude", "life": 17, "hand_count": 1},
    ],
    "battlefield": ["alice: Mountain (tapped)", "claude: Grizzly Bears [2/2]"],
    "hands": {"alice": ["Lightning Bolt", "Swamp"], "claude": ["Forest"]},
    "graveyards": {"alice": [], "claude": []},
    "exile": [],
    "stack": [],
    "turn_number": 4,
    "step": "precombat_main",
    "active_player": "claude",
    "priority_holder": "claude",
    "play_permissions": {},
    "known_cards": {},
}


def drive(body: str, **bindings: Any) -> Any:
    """Mount the replay against the fake DOM and run `body` after it has
    settled, with `mountReplay` already constructed."""
    return run_module("render/replay.js", FAKE_DOM + body, **bindings)


# The default fetch stub: `/review/frames/<seq>` answers from `frames` (or
# refuses if `seq` is in `frameFailures`), a bare `/review?` answers from
# `review` (or refuses with `status`/`detailText`), and anything else (the
# card catalogue) answers from `card`. Every call lands in `calls` in order.
FETCH = """
          globalThis.fetch = async (path) => {
            calls.push(path);
            if (path.includes("/review/frames/")) {
              const seq = Number(path.split("/frames/")[1].split("?")[0]);
              if ((frameFailures || []).includes(seq)) {
                return {
                  ok: false,
                  status: 500,
                  json: async () => ({ detail: "frame lookup failed" }),
                };
              }
              const view = (frames || {})[seq];
              return { ok: true, status: 200, json: async () => view };
            }
            if (path.includes("/review?")) {
              if (status !== 200) {
                return { ok: false, status, json: async () => ({ detail: detailText }) };
              }
              return { ok: true, status: 200, json: async () => review };
            }
            return { ok: true, status: 200, json: async () => card };
          };
"""


def test_open_hides_lobby_shows_game_and_seeds_frame_zero() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = { 102: EXACT_102 };
          const frameFailures = [];
          """
        + FETCH
        + """
          const backCalls = [];
          const api = m.mountReplay(document, store, {
            ...clock.handlers,
            onBack: () => backCalls.push(1),
          });
          await api.open({ gameId: "g1", uid: "u1" });
          return {
            calls,
            lobbyHidden: els.lobby.hidden,
            gameHidden: els.game.hidden,
            replaying: els.game.classList.contains("replaying"),
            replay: store.get().replay,
            state: store.get().state,
            cardCount: findAll(document, "card").length,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        EXACT_102=EXACT_102,
    )
    assert result["calls"][0] == "/api/games/g1/review?uid=u1"
    assert result["lobbyHidden"] is True
    assert result["gameHidden"] is False
    assert result["replaying"] is True
    assert result["replay"] == {
        "gameId": "g1",
        "uid": "u1",
        "index": 0,
        "playing": True,
        "count": 4,
        "exactFrames": [102],
    }
    # frame 0 (seq 101) is never exact, so the store's state right after
    # `open()` is the fallback view - never a card node, only a plain view.
    assert result["state"].get("marker") is None
    assert result["state"]["turn_number"] == 4
    assert result["state"]["active_player"] == "claude"
    assert result["cardCount"] == 0


def test_a_reconstructed_review_shows_the_notice_in_the_logs_own_head() -> None:
    """The one behaviour `render/review.js`'s own
    `test_a_reconstructed_review_says_so_and_quotes_the_engines_refusal`
    pinned that this module must still promise now that the scroll page is
    gone (R5 acceptance (5)): `review.reconstructed` means this build could
    not decode the board this game was saved with, and a viewer must not be
    left thinking a final board was read rather than the ledger rebuilt -
    the engine's own refusal text, quoted verbatim, is what proves it. Shown
    once, at the head of `#replay-log`, ahead of every step message rather
    than folded into one of them - it is a fact about the whole review, not
    about any one step."""
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = {};
          const frameFailures = [];
          """
        + FETCH
        + """
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          return { logText: els.log.textContent };
        })()
        """,
        review={
            **REVIEW,
            "reconstructed": True,
            "unloadable": (
                "Game snapshot was written at schema_version=3, but this build "
                "decodes schema_version=5. Refusing to decode a mismatched snapshot."
            ),
            "unestablished": ["drawn", "is_over", "winner"],
        },
        card=CARD_ANSWER,
    )
    assert "schema_version=3" in result["logText"], (
        "the engine's own words, where the viewer can see them - never a paraphrase"
    )
    assert "Rebuilt from the recorded ledger" in result["logText"]
    assert "drawn" in result["logText"] and "winner" in result["logText"], (
        "what the ledger could not establish is named, not silently dropped"
    )


def test_a_decodable_review_shows_no_reconstructed_notice() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = {};
          const frameFailures = [];
          """
        + FETCH
        + """
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          return { logText: els.log.textContent };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
    )
    assert "Rebuilt from the recorded ledger" not in result["logText"]


def test_a_server_error_shows_its_own_text_with_a_working_back_button_and_no_board() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 409;
          const detailText = "this game is still in progress";
          const frames = {};
          const frameFailures = [];
          """
        + FETCH
        + """
          const backCalls = [];
          const api = m.mountReplay(document, store, {
            ...clock.handlers,
            onBack: () => backCalls.push(1),
          });
          await api.open({ gameId: "g1", uid: "u1" });
          const logText = els.log.textContent;
          const backBtn = findAll(els.log, "replay-error-back")[0] || null;
          if (backBtn) click(backBtn);
          return {
            logText,
            hadBackButton: Boolean(backBtn),
            backCalls: backCalls.length,
            state: store.get().state,
            lobbyHiddenAfterBack: els.lobby.hidden,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
    )
    assert "still in progress" in result["logText"]
    assert result["hadBackButton"] is True
    assert result["backCalls"] == 1
    assert result["state"] is None
    assert result["lobbyHiddenAfterBack"] is False


def test_stepping_forward_and_back_moves_the_frame_and_the_log() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = { 102: EXACT_102 };
          const frameFailures = [];
          """
        + FETCH
        + """
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });

          const fwd = findAll(els.bar, "replay-step-forward")[0];
          const back = findAll(els.bar, "replay-step-back")[0];

          const logAt0 = findAll(els.log, "replay-msg").length;
          click(fwd);
          const indexAt1 = store.get().replay.index;
          const logAt1 = findAll(els.log, "replay-msg").length;
          click(fwd);
          const logAt2 = findAll(els.log, "replay-msg").length;
          const refusalText = els.log.textContent;
          click(back);
          const indexAfterBack = store.get().replay.index;
          const logAfterBack = findAll(els.log, "replay-msg").length;

          return { logAt0, indexAt1, logAt1, logAt2, refusalText, indexAfterBack, logAfterBack };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        EXACT_102=EXACT_102,
    )
    assert result["indexAt1"] == 1
    assert result["logAt1"] >= result["logAt0"]
    assert result["logAt2"] > result["logAt1"], "the log grows as the transport steps forward"
    assert REFUSAL in result["refusalText"], "a refused step shows the engine's own words verbatim"
    assert result["indexAfterBack"] == 1
    assert result["logAfterBack"] < result["logAt2"], "stepping back removes messages"


def test_the_current_step_s_message_is_marked_and_seat_coloured() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = { 102: EXACT_102 };
          const frameFailures = [];
          """
        + FETCH
        + """
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          const current = findAll(els.log, "replay-msg--current");
          const youMsgs = findAll(els.log, "replay-msg--you").length;
          const oppMsgs = findAll(els.log, "replay-msg--opp").length;
          return {
            currentCount: current.length,
            currentText: current.map((el) => el.textContent).join(" | "),
            anyColoured: youMsgs + oppMsgs > 0,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        EXACT_102=EXACT_102,
    )
    assert result["currentCount"] >= 1
    assert "a 2/2 now is worth more" in result["currentText"]
    assert result["anyColoured"] is True


def test_autoplay_advances_one_step_per_5000_fake_ms_and_stops_at_the_end() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = { 102: EXACT_102 };
          const frameFailures = [];
          """
        + FETCH
        + """
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          const indices = [store.get().replay.index];
          for (let i = 0; i < 5; i += 1) {
            clock.advance(5000);
            indices.push(store.get().replay.index);
          }
          return { indices, playingAtEnd: store.get().replay.playing };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        EXACT_102=EXACT_102,
    )
    # 4 steps (index 0..3): each 5000ms tick advances one, and it holds at 3
    # (the last index) rather than running off the end.
    assert result["indices"] == [0, 1, 2, 3, 3, 3]
    assert result["playingAtEnd"] is False


def test_pause_holds_the_frame_across_ticks() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = { 102: EXACT_102 };
          const frameFailures = [];
          """
        + FETCH
        + """
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          const playPause = findAll(els.bar, "replay-play-pause")[0];
          const labelPlaying = playPause.textContent;
          click(playPause); // pause
          // `renderBar` rebuilds the bar from scratch on every render (this
          // codebase's own convention), so the button above is now a
          // detached, stale node - re-query for the current one.
          const labelPaused = findAll(els.bar, "replay-play-pause")[0].textContent;
          const indexAfterPause = store.get().replay.index;
          clock.advance(5000);
          clock.advance(5000);
          return {
            labelPlaying,
            labelPaused,
            indexAfterPause,
            indexAfterTicks: store.get().replay.index,
            playingAfterPause: store.get().replay.playing,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        EXACT_102=EXACT_102,
    )
    assert "pause" in result["labelPlaying"].lower()
    assert "play" in result["labelPaused"].lower()
    assert result["playingAfterPause"] is False
    assert result["indexAfterTicks"] == result["indexAfterPause"], (
        "paused, autoplay ticks do nothing"
    )


def test_the_scrubber_seeks_to_the_dragged_index() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = { 102: EXACT_102 };
          const frameFailures = [];
          """
        + FETCH
        + """
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          const scrubber = findAll(els.bar, "replay-scrubber")[0];
          scrubber.value = "3";
          scrubber.listeners.change({ target: scrubber });
          return { index: store.get().replay.index, state: store.get().state };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        EXACT_102=EXACT_102,
    )
    assert result["index"] == 3
    assert result["state"]["active_player"] == "claude"


def test_an_exact_frame_is_fetched_only_for_its_seq_and_replaces_the_fallback() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = { 102: EXACT_102 };
          const frameFailures = [];
          """
        + FETCH
        + """
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          const stateAtStep0 = store.get().state;
          const fwd = findAll(els.bar, "replay-step-forward")[0];
          click(fwd); // -> step 1, seq 102, the one exact frame
          const fallbackImmediately = store.get().state;
          await new Promise((r) => setTimeout(r, 20));
          const afterFetch = store.get().state;
          const frameCalls = calls.filter((c) => c.includes("/review/frames/"));
          return {
            stateAtStep0Marker: stateAtStep0.marker || null,
            fallbackImmediatelyMarker: fallbackImmediately.marker || null,
            afterFetchMarker: afterFetch.marker || null,
            afterFetchHandCount: (afterFetch.players || []).find((p) => p.username === "alice")
              .hand_count,
            frameCalls,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        EXACT_102=EXACT_102,
    )
    assert result["stateAtStep0Marker"] is None, "seq 101 is not exact - never fetched"
    assert result["fallbackImmediatelyMarker"] is None, "the fallback shows first, synchronously"
    assert result["afterFetchMarker"] == "exact-102"
    assert result["afterFetchHandCount"] == 2, "the exact frame's own data, not the fallback's"
    assert result["frameCalls"] == ["/api/games/g1/review/frames/102?uid=u1"]


def test_a_failed_frame_fetch_keeps_the_fallback() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = {};
          const frameFailures = [102];
          """
        + FETCH
        + """
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          const fwd = findAll(els.bar, "replay-step-forward")[0];
          click(fwd);
          await new Promise((r) => setTimeout(r, 20));
          const state = store.get().state;
          return { marker: state.marker || null, turnNumber: state.turn_number };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
    )
    assert result["marker"] is None
    assert result["turnNumber"] == 4


def test_a_stale_frame_response_is_ignored() -> None:
    """The viewer steps forward past the exact-frame step before that
    fetch resolves, then steps back to it again and a *second*, later fetch
    is issued and resolved - only that one may land, never the first."""
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frameFailures = [];
          const resolvers = {};
          let seenCount = 0;
          globalThis.fetch = async (path) => {
            calls.push(path);
            if (path.includes("/review/frames/102")) {
              seenCount += 1;
              const mySeen = seenCount;
              return new Promise((resolve) => {
                resolvers[mySeen] = () =>
                  resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ ...EXACT_102, marker: `exact-102-${mySeen}` }),
                  });
              });
            }
            if (path.includes("/review?")) {
              return { ok: true, status: 200, json: async () => review };
            }
            return { ok: true, status: 200, json: async () => card };
          };
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          const fwd = findAll(els.bar, "replay-step-forward")[0];
          const back = findAll(els.bar, "replay-step-back")[0];
          click(fwd); // step 1 (seq 102, exact) - first fetch in flight
          click(fwd); // step 2 - the viewer has already left step 1
          click(back); // back to step 1 - a second fetch for seq 102 goes out
          await new Promise((r) => setTimeout(r, 5));
          // Resolve the SECOND request first (the current one), then the
          // stale first one - the first must never overwrite the second.
          resolvers[2]();
          await new Promise((r) => setTimeout(r, 5));
          resolvers[1]();
          await new Promise((r) => setTimeout(r, 5));
          return { marker: store.get().state.marker, index: store.get().replay.index };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        EXACT_102=EXACT_102,
    )
    assert result["index"] == 1
    assert result["marker"] == "exact-102-2", "the stale first response must not win"


def test_back_calls_onBack_and_clears_the_timer() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = { 102: EXACT_102 };
          const frameFailures = [];
          """
        + FETCH
        + """
          const backCalls = [];
          const api = m.mountReplay(document, store, {
            ...clock.handlers,
            onBack: () => backCalls.push(1),
          });
          await api.open({ gameId: "g1", uid: "u1" });
          const runningBeforeClose = clock.running;
          const backBtn = findAll(els.bar, "replay-back")[0];
          click(backBtn);
          return {
            backCalls: backCalls.length,
            runningBeforeClose,
            clearedAfterClose: clock.cleared,
            lobbyHidden: els.lobby.hidden,
            gameHidden: els.game.hidden,
            replaying: els.game.classList.contains("replaying"),
            replay: store.get().replay,
            state: store.get().state,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        EXACT_102=EXACT_102,
    )
    assert result["backCalls"] == 1
    assert result["runningBeforeClose"] is True
    assert result["clearedAfterClose"] is True
    assert result["lobbyHidden"] is False
    assert result["gameHidden"] is True
    assert result["replaying"] is False
    assert result["replay"] is None
    assert result["state"] is None


def test_the_transport_bar_has_no_pass_or_phase_controls() -> None:
    result = drive(
        """
        (async () => {
          const els = mountInto();
          const store = makeStore();
          const clock = makeClock(1000);
          const calls = [];
          const status = 200;
          const detailText = "";
          const frames = { 102: EXACT_102 };
          const frameFailures = [];
          """
        + FETCH
        + """
          const api = m.mountReplay(document, store, { ...clock.handlers, onBack: () => {} });
          await api.open({ gameId: "g1", uid: "u1" });
          return {
            barText: els.bar.textContent,
            promptBox: findAll(els.bar, "prompt-box").length,
            castingStrip: findAll(els.bar, "casting-strip").length,
            priorityBox: findAll(els.bar, "priority-box").length,
            stopsBox: findAll(els.bar, "stops-box").length,
          };
        })()
        """,
        review=REVIEW,
        card=CARD_ANSWER,
        EXACT_102=EXACT_102,
    )
    assert result["promptBox"] == 0
    assert result["castingStrip"] == 0
    assert result["priorityBox"] == 0
    assert result["stopsBox"] == 0
    lowered = result["barText"].lower()
    for banned in (
        "pass priority",
        "hold priority",
        "declare attackers",
        "declare blockers",
        "end step stop",
    ):
        assert banned not in lowered
