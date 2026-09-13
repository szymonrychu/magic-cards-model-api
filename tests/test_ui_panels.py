"""`ui/render/panels.js`'s `mountTopbar` against a fake DOM (the pattern from
`tests/test_ui_ribbon.py`), which had no tests of its own before this whole-
branch review.

Smoke tests: the AI panel's fallback rendering (`ai_seat.py`'s
`mechanism: "fallback"` - the shape it actually reports once a spawned
runner's own process fails to start, `status["state"] == "error"` alongside
it, per that module's own `_start` - never `state: "not_started"`, which
`renderAiPanel`'s own early-return guard reads as the plainer "not started"
pill instead) shows the "Run it by hand" button and, on a click, opens the
modal with the fallback command's full prompt text; the AI-stall banner
offering a "Pass priority for <player>" button once the AI seat has reported
an error; and the AI-loading banner (`renderAiLoadingBanner`, wired to the
pure `aiSeatLoadingNotice` in `ui/store.js` - see `test_ui_ai_loading.py` for
that function's own exhaustive per-state coverage) showing and clearing
itself as a Kubernetes seat's status moves from `pending` to `thinking`.
"""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

# A fake DOM minimal enough for `dom.js`'s `h`/`clear` plus what `panels.js`
# itself needs: `querySelector` by id (`mountTopbar` looks up its boxes that
# way), a `textContent` getter that walks the whole subtree, and an
# `addEventListener` that actually records the listener so a test can invoke
# the handler `h()` wired to a rendered button - `click(el)` below does
# exactly that, same as `tests/test_ui_ribbon.py`'s own copy.
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
    this.classList = {
      set: new Set(),
      add: (c) => this.classList.set.add(c),
      remove: (c) => this.classList.set.delete(c),
      toggle: (c, on) => (on ? this.classList.set.add(c) : this.classList.set.delete(c)),
      contains: (c) => this.classList.set.has(c),
    };
  }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    c.parentNode = this;
    return c;
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  removeEventListener(type, fn) { if (this.listeners[type] === fn) delete this.listeners[type]; }
  contains(other) {
    if (other === this) return true;
    return this.children.some((c) => (c.contains ? c.contains(other) : false));
  }
  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0 }; }
  get firstChild() { return this.children[0] || null; }
  get childNodes() { return this.children; }
  get textContent() {
    return this.children.map((c) => (c.textContent === undefined ? "" : c.textContent)).join("");
  }
  querySelector(sel) {
    if (!sel.startsWith("#")) return null;
    const id = sel.slice(1);
    const walk = (node) => {
      if (!node) return null;
      if (node.attrs && node.attrs.id === id) return node;
      for (const c of node.children || []) {
        const found = walk(c);
        if (found) return found;
      }
      return null;
    };
    return walk(this);
  }
}

function findByText(node, tag, text) {
  const content = typeof node.textContent === "string" ? node.textContent : "";
  if (node.tagName === tag && content.includes(text)) return node;
  for (const c of node.children || []) {
    const found = findByText(c, tag, text);
    if (found) return found;
  }
  return null;
}

function click(el) {
  if (el && el.listeners && el.listeners.click) el.listeners.click({ target: el });
}

globalThis.window = { scrollX: 0, scrollY: 0 };
globalThis.document = new El("#document");
document.createElement = (t) => new El(t);
document.createTextNode = (s) => ({ nodeType: 3, textContent: s });
document.createDocumentFragment = () => new El("#fragment");

function boxed(id, hidden) {
  const el = document.createElement("div");
  el.setAttribute("id", id);
  el.hidden = Boolean(hidden);
  document.appendChild(el);
  return el;
}
boxed("ai-panel");
boxed("ai-loading-banner", true);
boxed("ai-stall-banner", true);
boxed("modal", true);

function makeStore(initial) {
  let state = initial;
  const listeners = [];
  return {
    get: () => state,
    set(patch) {
      state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) };
      for (const fn of listeners) fn(state);
    },
    subscribe(fn) {
      listeners.push(fn);
      return () => {};
    },
  };
}

const NOOP = () => {};

function baseState(overrides) {
  return {
    you: "alice",
    winner: null,
    drawn: false,
    turn_number: 3,
    active_player: "alice",
    priority_holder: "claude",
    step: "precombat_main",
    ...overrides,
  };
}
"""


def test_ai_panel_shows_a_fallback_status_with_the_command_and_opens_it_on_click() -> None:
    """`ai_seat.py`'s real fallback shape once a spawned runner's process
    itself failed: `mechanism: "fallback"` alongside `state: "error"` (never
    `"not_started"` - that earlier, distinct case reads as plain "AI seat:
    not started" through `renderAiPanel`'s own first guard) and a `prompt`
    carrying the full command a human would run by hand."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + """
        (() => {
          const store = makeStore({
            state: baseState(),
            aiStatus: null,
            aiUid: "ai-uid",
            connection: "ok",
          });
          m.mountTopbar(document, store, {
            onOpenZones: NOOP, onLeaveGame: NOOP, onPassForAiSeat: NOOP, onReportProblem: NOOP,
          });
          store.set({
            aiStatus: {
              uid: "ai-uid", player: "claude", mechanism: "fallback", state: "error",
              command: 'claude --print --dangerously-skip-permissions --allowedTools Bash -- "..."',
              prompt: "the full agent prompt, including the game id and the opponent's uid",
            },
          });
          const panel = document.querySelector("#ai-panel");
          const before = {
            panelText: panel.textContent,
            modalHiddenBefore: document.querySelector("#modal").hidden,
          };
          const button = findByText(panel, "button", "Run it by hand");
          click(button);
          const modal = document.querySelector("#modal");
          return {
            ...before,
            found: button !== null,
            modalHiddenAfter: modal.hidden,
            modalText: modal.textContent,
          };
        })()
        """,
    )
    assert "AI seat: no agent running" in result["panelText"]
    assert result["modalHiddenBefore"] is True
    assert result["found"] is True
    assert result["modalHiddenAfter"] is False
    assert (
        "the full agent prompt, including the game id and the opponent's uid" in result["modalText"]
    )


def test_the_stall_banner_offers_a_pass_priority_button_for_the_ai_seat() -> None:
    """`aiStallNotice` (`ui/store.js`) reports a message the instant the AI
    seat's own status carries an error - `renderAiStallBanner` turns that
    into the banner and its "Pass priority for <player>" button, wired to
    `handlers.onPassForAiSeat(aiPlayer)`."""
    calls = run_module(
        "render/panels.js",
        FAKE_DOM
        + """
        (() => {
          const calls = [];
          const store = makeStore({
            state: baseState({ priority_holder: "claude" }),
            aiStatus: null,
            aiUid: "ai-uid",
            connection: "ok",
          });
          m.mountTopbar(document, store, {
            onOpenZones: NOOP, onLeaveGame: NOOP, onReportProblem: NOOP,
            onPassForAiSeat: (player) => calls.push(player),
          });
          store.set({
            aiStatus: { player: "claude", state: "error", last_error: "agent crashed" },
          });
          const banner = document.querySelector("#ai-stall-banner");
          const button = findByText(banner, "button", "Pass priority for claude");
          click(button);
          return { visible: banner.hidden === false, found: button !== null, calls };
        })()
        """,
    )
    assert calls["found"] is True
    assert calls["visible"] is True
    assert calls["calls"] == ["claude"]


def test_live_activity_hides_the_stall_banner_but_the_pill_still_speaks() -> None:
    """The false accusation this whole fix exists for: the board sits
    unchanged for 215s while the agent is genuinely mid-turn.
    `renderAiStallBanner` must go quiet (`aiStallNotice` defers to the same
    activity the pill already shows), and the human must not be left staring
    at a blank topbar - `renderAiPanel` is unconditional, so the pill keeps
    saying "making its play" the whole time. Once the activity record goes
    stale, the banner speaks up again."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + """
        (() => {
          let now = 1_000_000;
          Date.now = () => now;
          const store = makeStore({
            state: baseState({ priority_holder: "claude" }),
            aiStatus: { player: "claude", state: "dispatched" },
            aiUid: "ai-uid",
            connection: "ok",
          });
          m.mountTopbar(document, store, {
            onOpenZones: NOOP, onLeaveGame: NOOP, onReportProblem: NOOP,
            onPassForAiSeat: NOOP,
          });
          const banner = document.querySelector("#ai-stall-banner");
          const panel = document.querySelector("#ai-panel");

          // A render at t0 to start the "unchanged since" clock (module-level
          // in `renderAiStallBanner`) before time moves on.
          store.set({});

          now += 215_000;
          store.set({
            aiStatus: {
              player: "claude", state: "dispatched",
              activity: { state: "acting", seconds: 2, seq: 9 },
            },
          });
          const whileLive = { hidden: banner.hidden, pillText: panel.textContent };

          store.set({
            aiStatus: {
              player: "claude", state: "dispatched",
              activity: { state: "acting", seconds: 1000, seq: 9 },
            },
          });
          const onceStale = { hidden: banner.hidden, bannerText: banner.textContent };

          return { whileLive, onceStale };
        })()
        """,
    )
    assert result["whileLive"]["hidden"] is True
    assert "making its play" in result["whileLive"]["pillText"]
    assert result["onceStale"]["hidden"] is False
    assert "claude" in result["onceStale"]["bannerText"]
    assert "215s" in result["onceStale"]["bannerText"]


def test_the_loading_banner_shows_while_booting_and_clears_once_ready() -> None:
    """A kubernetes seat's own status moves `pending` (image pull,
    initContainers, the agent CLI's own boot) -> `thinking` (the driver's
    first dispatch) as its pod comes up. `renderAiLoadingBanner` shows a
    message while it is still `pending` and clears the banner - `hidden`
    again, no leftover text - the moment it is not."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + """
        (() => {
          const store = makeStore({
            state: baseState(),
            aiStatus: null,
            aiUid: "ai-uid",
            connection: "ok",
          });
          m.mountTopbar(document, store, {
            onOpenZones: NOOP, onLeaveGame: NOOP, onPassForAiSeat: NOOP, onReportProblem: NOOP,
          });
          const banner = document.querySelector("#ai-loading-banner");
          const beforeStart = { hidden: banner.hidden };
          store.set({
            aiStatus: {
              uid: "ai-uid", player: "claude", mechanism: "kubernetes",
              state: "pending", pod: null,
            },
          });
          const whilePending = { hidden: banner.hidden, text: banner.textContent };
          store.set({
            aiStatus: {
              uid: "ai-uid", player: "claude", mechanism: "kubernetes",
              state: "thinking", pod: "Running",
            },
          });
          const onceReady = { hidden: banner.hidden, text: banner.textContent };
          return { beforeStart, whilePending, onceReady };
        })()
        """,
    )
    assert result["beforeStart"]["hidden"] is True
    assert result["whilePending"]["hidden"] is False
    assert "starting the agent" in result["whilePending"]["text"]
    assert result["onceReady"]["hidden"] is True
    assert result["onceReady"]["text"] == ""


def test_the_loading_banner_marks_a_genuine_failure_distinctly() -> None:
    """The driver-grace backstop firing (`k8s_seats.py`) reports
    `state: "error"` - the banner must say so, not keep claiming the agent
    is still starting up."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + """
        (() => {
          const store = makeStore({
            state: baseState(),
            aiStatus: null,
            aiUid: "ai-uid",
            connection: "ok",
          });
          m.mountTopbar(document, store, {
            onOpenZones: NOOP, onLeaveGame: NOOP, onPassForAiSeat: NOOP, onReportProblem: NOOP,
          });
          store.set({
            aiStatus: {
              uid: "ai-uid", player: "claude", mechanism: "kubernetes",
              state: "error", reason: "driver unreachable",
            },
          });
          const banner = document.querySelector("#ai-loading-banner");
          return {
            hidden: banner.hidden,
            bad: banner.classList.contains("bad"),
            text: banner.textContent,
          };
        })()
        """,
    )
    assert result["hidden"] is False
    assert result["bad"] is True
    assert "driver unreachable" in result["text"]


def test_the_ai_pill_says_what_the_agent_is_actually_doing() -> None:
    """The pill is wired to `aiSeatActivityLabel` (`ui/store.js` - see
    `test_ui_ai_activity.py` for that function's own per-state coverage), so
    a seat whose driver reported an observed activity reads as that activity
    rather than the flat "thinking" it used to show for a whole match."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + """
        (() => {
          const store = makeStore({
            state: baseState(),
            aiStatus: null,
            aiUid: "ai-uid",
            connection: "ok",
          });
          m.mountTopbar(document, store, {
            onOpenZones: NOOP, onLeaveGame: NOOP, onPassForAiSeat: NOOP, onReportProblem: NOOP,
          });
          const panel = document.querySelector("#ai-panel");
          const seen = [];
          const show = (activity) => {
            store.set({
              aiStatus: {
                uid: "ai-uid", player: "claude", mechanism: "kubernetes",
                state: "thinking", pod: "Running", activity,
              },
            });
            seen.push(panel.textContent);
          };
          show({ state: "waiting", seconds: 0.4, seq: 1 });
          show({ state: "reading_cards", seconds: 0.2, seq: 2 });
          show({ state: "thinking", seconds: 0.1, seq: 3 });
          show({ state: "acting", seconds: 0.1, seq: 4 });
          // Nothing new observed for a while: the last real activity stands.
          show({ state: "acting", seconds: 20, seq: 4 });
          return { seen, className: panel.firstChild.className };
        })()
        """,
    )
    assert result["seen"] == [
        "AI (claude): waiting for priority",
        "AI (claude): reading cards",
        "AI (claude): thinking",
        "AI (claude): making its play",
        "AI (claude): making its play",
    ]
    assert "state-thinking" in result["className"], (
        "the pill's styling stays keyed to the seat's own state, not to the activity"
    )


def test_the_ai_pill_falls_back_to_the_seat_state_when_nothing_was_observed() -> None:
    """A `subprocess` seat, a driver too old to report activity, an
    unreachable wrapper: the pill reads exactly as it always did."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + """
        (() => {
          const store = makeStore({
            state: baseState(),
            aiStatus: null,
            aiUid: "ai-uid",
            connection: "ok",
          });
          m.mountTopbar(document, store, {
            onOpenZones: NOOP, onLeaveGame: NOOP, onPassForAiSeat: NOOP, onReportProblem: NOOP,
          });
          const panel = document.querySelector("#ai-panel");
          store.set({
            aiStatus: {
              uid: "ai-uid", player: "claude", mechanism: "subprocess", state: "thinking",
            },
          });
          const plain = panel.textContent;
          store.set({
            aiStatus: {
              uid: "ai-uid", player: "claude", mechanism: "kubernetes", state: "unreachable",
              activity: { state: "reading_cards", seconds: 1 },
            },
          });
          return { plain, unreachable: panel.textContent };
        })()
        """,
    )
    assert result["plain"] == "AI (claude): thinking"
    assert result["unreachable"] == "AI (claude): unreachablewhy"


# ------------------------------------------------------ the log overlay ----
#
# The game log used to be the bottom panel of `#side-panel`; the mtg-buddy
# advisor took that corner (the owner asked for exactly that), so the log
# moved into `#log-modal` behind a new topbar button. These cover the three
# things that move can get wrong.

LOG_MODAL = """
  const modal = document.createElement("div");
  modal.setAttribute("id", "log-modal");
  modal.hidden = true;
  const box = document.createElement("div");
  box.setAttribute("id", "log-box");
  box.scrollTop = 0;
  box.scrollHeight = 0;
  box.clientHeight = 40;
  const line = document.createElement("div");
  line.setAttribute("id", "a-log-line");
  box.appendChild(line);
  modal.appendChild(box);
  document.appendChild(modal);
"""


def test_toggling_the_log_overlay_only_ever_hides_and_shows_it() -> None:
    """The whole reason `#log-modal` is not built through `showModal`:
    `closeModal` empties `#modal`'s content, and `render/log.js`'s `mountLog`
    subscribes ONCE at boot and keyed-patches one `#log-box` element for the
    life of the page. Empty this one and that subscription spends the rest of
    the game patching a detached node - the log silently stops growing. So
    the content survives every toggle, and visibility moves through `hidden`
    rather than `style.display` (`style.css`'s global
    `[hidden] { display: none !important }` is what beats `.modal`'s own
    `display: flex`)."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + LOG_MODAL
        + """
        (() => {
          const seen = [];
          m.toggleLogModal(document);
          seen.push({
            hidden: modal.hidden,
            lines: box.children.length,
            open: m.logModalIsOpen(document),
          });
          m.toggleLogModal(document);
          seen.push({
            hidden: modal.hidden,
            lines: box.children.length,
            open: m.logModalIsOpen(document),
          });
          m.toggleLogModal(document);
          m.closeLogModal(document);
          seen.push({
            hidden: modal.hidden,
            lines: box.children.length,
            open: m.logModalIsOpen(document),
          });
          return {
            seen,
            display: modal.style.display || null,
            sameBox: document.querySelector("#log-box") === box,
          };
        })()
        """,
    )
    assert [s["hidden"] for s in result["seen"]] == [False, True, True]
    assert [s["open"] for s in result["seen"]] == [True, False, False]
    assert [s["lines"] for s in result["seen"]] == [1, 1, 1], (
        "toggling the overlay destroyed the log's content - mountLog's element must survive"
    )
    assert result["sameBox"] is True, "#log-box was replaced, so mountLog is patching a dead node"
    assert not result["display"], "the overlay was toggled with style.display instead of `hidden`"


def test_opening_the_log_shows_its_newest_entries_not_its_oldest() -> None:
    """`mountLog`'s auto-scroll reads real scroll geometry, and a hidden
    element has none: while the overlay is closed, `scrollHeight` is 0, so
    every patch pins `scrollTop` to 0. Opened, that would show entry #1 of
    500 and leave the player scrolling to find the turn they are on.

    Fixed here rather than in `mountLog`, which is untouched: the element's
    layout only exists once this function has made it visible, so this is the
    only place that can read the real height. Reading `scrollHeight` right
    after clearing `hidden` forces the browser's own synchronous reflow."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + LOG_MODAL
        + """
        (() => {
          // What a browser has after laying the box out: taller content than
          // viewport, and a scroll position still at the top.
          box.scrollHeight = 900;
          m.toggleLogModal(document);
          return { scrollTop: box.scrollTop };
        })()
        """,
    )
    assert result["scrollTop"] == 900, "opening the log left it scrolled to the oldest entries"


def test_the_topbars_log_button_toggles_the_overlay_through_its_handler() -> None:
    """The wiring, end to end: `mountTopbar` binds `#open-log`, and a click
    calls `handlers.onToggleLog` - which is what `app.js` points at
    `toggleLogModal`."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + """
        (() => {
          const button = document.createElement("button");
          button.setAttribute("id", "open-log");
          document.appendChild(button);
          let toggles = 0;
          const store = makeStore({ state: null, connection: "ok" });
          m.mountTopbar(document, store, {
            onOpenZones: NOOP,
            onToggleLog: () => { toggles += 1; },
            onLeaveGame: NOOP,
            onPassForAiSeat: NOOP,
            onReportProblem: NOOP,
          });
          button.onclick();
          button.onclick();
          return toggles;
        })()
        """,
    )
    assert result == 2


def test_clicking_beside_the_log_closes_it_but_clicking_the_log_does_not() -> None:
    """The overlay covers the whole viewport, topbar included, so the Log
    button that opened it is not reachable to close it again - without this,
    Escape would be the only way out of a panel a mouse opened. Clicking the
    dimmed backdrop closes it; a click that lands on the log itself (reading
    it, selecting a line) must not, which is what the `e.target === modal`
    test distinguishes."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + LOG_MODAL
        + """
        (() => {
          const store = makeStore({ state: null, connection: "ok" });
          m.mountTopbar(document, store, {
            onOpenZones: NOOP, onToggleLog: NOOP, onLeaveGame: NOOP,
            onPassForAiSeat: NOOP, onReportProblem: NOOP,
          });
          m.toggleLogModal(document);
          const openAfterInsideClick = (() => {
            modal.listeners.click({ target: box, currentTarget: modal });
            return m.logModalIsOpen(document);
          })();
          const openAfterBackdropClick = (() => {
            modal.listeners.click({ target: modal, currentTarget: modal });
            return m.logModalIsOpen(document);
          })();
          return { openAfterInsideClick, openAfterBackdropClick, lines: box.children.length };
        })()
        """,
    )
    assert result["openAfterInsideClick"] is True, "reading the log closed it"
    assert result["openAfterBackdropClick"] is False, "clicking beside the log did not close it"
    assert result["lines"] == 1, "closing it emptied the log"
