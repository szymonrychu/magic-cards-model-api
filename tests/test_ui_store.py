"""`ui/store.js`'s press guard: the store's listener fan-out is deferred for
as long as a pointer or key is held down, so no re-render can replace the
button a player is pressing between their mousedown and their mouseup.

Every renderer in `ui/render/*` rebuilds its box from scratch (`clear(el)`
then a fresh `h(...)` tree - `renderPriorityBox` is the one exercised here),
so a poll that lands mid-press detaches the very node the browser is holding
the press on, and the `mouseup`/`click` that follow have nothing to land on:
the click is silently eaten. Fixing that in the renderers would mean keyed
reconciliation in every one of them; deferring the fan-out fixes it once, in
the one place every render is driven from.

The real `createStore` is driven here through the real `mountRibbon` and
`mountTopbar` against the fake DOM `tests/test_ui_ribbon.py` already builds
for the ribbon, extended with a `document`/`window` that record the
capturing listeners `installPressGuard` registers so a test can fire them.
Nothing is restated: the button identity assertion is against the actual
node the shipped renderer produced.
"""

from __future__ import annotations

from test_ui_ribbon import FAKE_DOM
from ui_node import UI, requires_node, run_module

pytestmark = requires_node

RIBBON_URL = (UI / "render" / "ribbon.js").as_uri()
PANELS_URL = (UI / "render" / "panels.js").as_uri()

# What the ribbon's fake DOM does not have, because nothing in `ribbon.js`
# needed it: `document`/`window` listeners that a test can reach. The `El`
# class already records per-element listeners by type; `document` is an `El`,
# so it gets the same recorder spelled out explicitly - including the third
# `capture` argument, which is load-bearing here (`ui/store.js` registers
# every one of these capturing so that a handler stopping propagation cannot
# hide a press from the guard) and so is recorded rather than dropped.
# `window` - a plain object in that fixture - gets a recorder of its own.
# Plus the boxes `mountTopbar` renders into, so step 7 can read `#turn-info`
# off the real topbar rather than a restatement of it.
PRESS_DOM = (
    FAKE_DOM
    + """
document.listeners = {};
document.captures = {};
document.addEventListener = (t, fn, capture) => {
  document.listeners[t] = fn;
  // `Boolean(...)`, so "registered without a capture argument" comes back as
  // false rather than as an `undefined` JSON would drop on the way out.
  document.captures[t] = Boolean(capture);
};
globalThis.window.listeners = {};
globalThis.window.captures = {};
globalThis.window.addEventListener = (t, fn, capture) => {
  window.listeners[t] = fn;
  window.captures[t] = Boolean(capture);
};

boxed("turn-info");
boxed("phase-track");
boxed("ai-panel");
boxed("ai-loading-banner", true);
boxed("ai-stall-banner", true);
boxed("modal", true);

const TOPBAR_HANDLERS = {
  onLeaveGame: NOOP, onPassForAiSeat: NOOP, onReportProblem: NOOP,
};

// Mount the real ribbon and the real topbar on a real `createStore`, with the
// press guard installed exactly the way `app.js`'s `main()` installs it, and
// a render counter subscribed last.
async function mounted() {
  const R = await import(RIBBON_URL);
  const P = await import(PANELS_URL);
  const store = m.createStore({ log: { entries: [], lastSeq: 0 } });
  R.mountRibbon(document, store, HANDLERS);
  P.mountTopbar(document, store, TOPBAR_HANDLERS);
  m.installPressGuard(store);
  const counter = { renders: 0 };
  store.subscribe(() => { counter.renders += 1; });
  return { store, counter };
}

const passButton = () => findByText(document.querySelector("#priority-box"), "button", "Pass");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
"""
)


def test_a_press_defers_the_rerender_until_the_click_has_landed() -> None:
    """The primary regression: a state change arriving between `pointerdown`
    and `pointerup` must not replace the pressed button, must not fan out at
    all, and must still be readable synchronously through `get()` - and the
    render it deferred must happen once the press ends."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const { store, counter } = await mounted();
          store.set(baseApp(baseState({ turn_number: 3 })));
          const pressed = passButton();
          const rendersBefore = counter.renders;

          document.listeners.pointerdown({ target: pressed });
          store.set(baseApp(baseState({ turn_number: 4 })));

          const during = {
            sameNode: passButton() === pressed,
            renders: counter.renders,
            // The synchronous-merge invariant: `poll()` reads `store.get()`
            // straight after a `set()` to decide whether its reads are stale
            // (ui/app.js:285), so a deferred fan-out must never defer the
            // merge itself.
            turnInState: store.get().state.turn_number,
            turnInfo: document.querySelector("#turn-info").textContent,
          };

          document.listeners.pointerup({ target: pressed });
          await sleep(30);

          return {
            rendersBefore,
            during,
            after: {
              renders: counter.renders,
              turnInfo: document.querySelector("#turn-info").textContent,
            },
          };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result["rendersBefore"] == 1
    # Mid-press: the pressed button is the same node object, nothing rendered,
    # and the new state is already merged.
    assert result["during"]["sameNode"] is True
    assert result["during"]["renders"] == 1
    assert result["during"]["turnInState"] == 4
    assert "turn 3" in result["during"]["turnInfo"]
    # Once the press ends, exactly one deferred render lands, carrying the
    # state that arrived during it.
    assert result["after"]["renders"] == 2
    assert "turn 4" in result["after"]["turnInfo"]


def test_many_sets_during_one_press_flush_as_a_single_render() -> None:
    """However many state changes land during a press - a poll, a seat poll
    and an affordance refresh can all arrive inside one - the flush notifies
    once, with the state as it finally stands."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const { store, counter } = await mounted();
          store.set(baseApp(baseState({ turn_number: 3 })));
          const pressed = passButton();

          document.listeners.pointerdown({ target: pressed });
          store.set(baseApp(baseState({ turn_number: 4 })));
          store.set(baseApp(baseState({ turn_number: 5 })));
          store.set({ connection: "ok" });
          const during = counter.renders;

          document.listeners.pointerup({ target: pressed });
          await sleep(30);
          const turnInfo = document.querySelector("#turn-info").textContent;
          return { during, after: counter.renders, turnInfo };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result["during"] == 1
    assert result["after"] == 2
    assert "turn 5" in result["turnInfo"]


def test_a_press_that_never_ends_still_flushes_on_the_one_second_cap() -> None:
    """The hard cap, and the mitigation for this design's one real risk: a
    `pointerup` swallowed by another capturing handler (`ribbon.js`'s popover
    installs one), or a tab switched away mid-press, must not freeze
    rendering for the rest of the game.

    Driven off the exported `PRESS_CAP_MS` rather than a hardcoded 1000, so
    the test is about the cap the shipped module actually applies - and the
    export has the consumer it was missing. Both sides are asserted: still
    holding at half the cap, flushed past it."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const { store, counter } = await mounted();
          store.set(baseApp(baseState({ turn_number: 3 })));
          const pressed = passButton();

          document.listeners.pointerdown({ target: pressed });
          store.set(baseApp(baseState({ turn_number: 4 })));
          const during = counter.renders;

          // No pointerup at all.
          await sleep(m.PRESS_CAP_MS / 2);
          const halfway = counter.renders;
          await sleep(m.PRESS_CAP_MS / 2 + 200);
          const turnInfo = document.querySelector("#turn-info").textContent;
          return { during, halfway, after: counter.renders, turnInfo, cap: m.PRESS_CAP_MS };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result["cap"] == 1000, "a cap this far from a real click's length wants re-arguing"
    assert result["during"] == 1
    assert result["halfway"] == 1, "the cap fired early - a real press would have been cut short"
    assert result["after"] == 2
    assert "turn 4" in result["turnInfo"]


def test_every_press_listener_is_registered_capturing() -> None:
    """Capture is load-bearing, not a default: a handler that stops
    propagation - `ribbon.js`'s popover installs one on `document` - would
    otherwise hide the press from the guard entirely, and the guard would
    silently protect nothing. `window`'s `blur` is the exception, and is
    meant to be: focus events do not bubble, so there is nothing to capture
    ahead of."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const { store } = await mounted();
          return { document: document.captures, window: window.captures };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    for event in ("pointerdown", "keydown", "pointerup", "pointercancel", "keyup", "click"):
        assert result["document"][event] is True, f"{event} is not registered capturing"
    assert result["window"] == {"blur": False}


def test_a_touch_press_holds_until_the_synthetic_click_has_been_dispatched() -> None:
    """The input path the guard still had the bug on. A mouse dispatches
    `pointerup`, `mouseup` and `click` in one task, so flushing on the next
    macrotask is safely after all three. Touch and pen do not: the synthetic
    `click` follows the `pointerup` by around 300ms, in a task of its own, so
    a flush scheduled on the release fans out first, the renderer rebuilds
    the button under the finger, and the click is eaten by a detached node -
    exactly the defect this guard exists to prevent, on the one input the
    fix had not covered.

    So a touch release is not the end of the press: the click is."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const out = {};
          for (const pointerType of ["touch", "pen"]) {
            const { store, counter } = await mounted();
            store.set(baseApp(baseState({ turn_number: 3 })));
            const pressed = passButton();

            document.listeners.pointerdown({ pointerType, target: pressed });
            store.set(baseApp(baseState({ turn_number: 4 })));
            document.listeners.pointerup({ pointerType, target: pressed });
            // Where a mouse's click would already have happened, and where
            // the flush used to land.
            await sleep(30);
            const afterRelease = { renders: counter.renders, sameNode: passButton() === pressed };

            // The synthetic click, a third of a second later - on the node
            // the finger went down on, which must still be the one on screen.
            document.listeners.click({ target: pressed });
            await sleep(30);
            out[pointerType] = {
              afterRelease,
              after: counter.renders,
              turnInfo: document.querySelector("#turn-info").textContent,
            };
          }
          return out;
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    for pointerType in ("touch", "pen"):
        got = result[pointerType]
        assert got["afterRelease"] == {"renders": 1, "sameNode": True}, (
            f"{pointerType}: the button was rebuilt before its click was dispatched: {got}"
        )
        assert got["after"] == 2, f"{pointerType}: the deferred render never landed"
        assert "turn 4" in got["turnInfo"]


def test_a_touch_press_whose_click_never_comes_is_still_bounded() -> None:
    """The other side of waiting for the click: a tap that produces none at
    all - on nothing clickable, or one the browser simply drops - must not
    hold rendering open until the press cap. It waits `TOUCH_CLICK_MS`, which
    is the delay it is there to cover plus headroom, and then gives up."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const { store, counter } = await mounted();
          store.set(baseApp(baseState({ turn_number: 3 })));

          document.listeners.pointerdown({ pointerType: "touch", target: passButton() });
          store.set(baseApp(baseState({ turn_number: 4 })));
          document.listeners.pointerup({ pointerType: "touch" });
          await sleep(m.TOUCH_CLICK_MS / 2);
          const waiting = counter.renders;
          await sleep(m.TOUCH_CLICK_MS / 2 + 100);
          return { waiting, after: counter.renders, wait: m.TOUCH_CLICK_MS, cap: m.PRESS_CAP_MS };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result["waiting"] == 1, "the wait for the click was not honoured"
    assert result["after"] == 2, "a touch press with no click never flushed"
    assert result["wait"] < result["cap"], "waiting for a click may never outlast the press cap"


def test_a_cancelled_touch_press_does_not_wait_for_a_click_that_cannot_come() -> None:
    """`pointercancel` is the browser taking the pointer away - a scroll or a
    gesture claimed it - and no click follows a cancelled press. Waiting the
    full `TOUCH_CLICK_MS` for one would stall every render behind a page
    scroll."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const { store, counter } = await mounted();
          store.set(baseApp(baseState({ turn_number: 3 })));
          document.listeners.pointerdown({ pointerType: "touch", target: passButton() });
          store.set(baseApp(baseState({ turn_number: 4 })));
          document.listeners.pointercancel({ pointerType: "touch" });
          await sleep(30);
          return counter.renders;
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result == 2, "a cancelled touch press held the fan-out anyway"


def test_a_mouse_click_after_the_flush_never_starts_a_press_of_its_own() -> None:
    """The click listener the touch path needs is on `document` for every
    input, so it has to be inert outside a touch or pen release: a mouse
    click arrives after its own press has already flushed, and must not
    re-open a window nothing will close."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const { store, counter } = await mounted();
          store.set(baseApp(baseState({ turn_number: 3 })));
          const pressed = passButton();
          document.listeners.pointerdown({ pointerType: "mouse", target: pressed });
          document.listeners.pointerup({ pointerType: "mouse", target: pressed });
          await sleep(30);
          document.listeners.click({ target: pressed });
          const afterClick = counter.renders;
          store.set(baseApp(baseState({ turn_number: 4 })));
          return { afterClick, immediately: counter.renders };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result["afterClick"] == 1
    assert result["immediately"] == 2, "a stray click left the store's fan-out held"


def test_pointercancel_keyup_and_window_blur_all_end_a_press() -> None:
    """Every way a press can end without a `pointerup`: the browser taking the
    pointer away (scroll/gesture), a keyboard activation's `keyup`, and the
    window losing focus mid-press."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const out = {};
          for (const [name, fire] of [
            ["pointercancel", () => document.listeners.pointercancel({})],
            ["keyup", () => document.listeners.keyup({})],
            ["blur", () => window.listeners.blur({})],
          ]) {
            const { store, counter } = await mounted();
            store.set(baseApp(baseState({ turn_number: 3 })));
            const starter =
              name === "keyup" ? document.listeners.keydown : document.listeners.pointerdown;
            starter({ target: passButton() });
            store.set(baseApp(baseState({ turn_number: 4 })));
            const during = counter.renders;
            fire();
            await sleep(30);
            out[name] = { during, after: counter.renders };
          }
          return out;
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    for name in ("pointercancel", "keyup", "blur"):
        assert result[name]["during"] == 1, name
        assert result[name]["after"] == 2, name


def test_outside_a_press_set_notifies_synchronously_exactly_as_before() -> None:
    """No press in flight is the overwhelmingly common case, and it must be
    unchanged: `set()` notifies inside the call, before it returns."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const { store, counter } = await mounted();
          store.set(baseApp(baseState({ turn_number: 3 })));
          const immediately = counter.renders;
          store.set(baseApp(baseState({ turn_number: 4 })));
          return {
            immediately,
            afterSecond: counter.renders,
            turnInfo: document.querySelector("#turn-info").textContent,
            differentNode: passButton() !== null,
          };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result["immediately"] == 1
    assert result["afterSecond"] == 2
    assert "turn 4" in result["turnInfo"]
    assert result["differentNode"] is True


def test_a_second_press_after_a_flush_defers_again() -> None:
    """The guard is not one-shot: it rearms for every press, and a flush from
    the previous press never leaks into the next one."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const { store, counter } = await mounted();
          store.set(baseApp(baseState({ turn_number: 3 })));

          document.listeners.pointerdown({ target: passButton() });
          store.set(baseApp(baseState({ turn_number: 4 })));
          document.listeners.pointerup({});
          await sleep(30);
          const firstRound = counter.renders;

          const pressed = passButton();
          document.listeners.pointerdown({ target: pressed });
          store.set(baseApp(baseState({ turn_number: 5 })));
          const during = { renders: counter.renders, sameNode: passButton() === pressed };
          document.listeners.pointerup({});
          await sleep(30);
          return { firstRound, during, after: counter.renders };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result["firstRound"] == 2
    assert result["during"]["renders"] == 2
    assert result["during"]["sameNode"] is True
    assert result["after"] == 3


def test_a_press_with_no_state_change_flushes_nothing() -> None:
    """A press that nothing arrives during must not manufacture a render on
    release - the flush is for deferred work, not a heartbeat."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const { store, counter } = await mounted();
          store.set(baseApp(baseState({ turn_number: 3 })));
          document.listeners.pointerdown({ target: passButton() });
          document.listeners.pointerup({});
          await sleep(30);
          return counter.renders;
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result == 1


def test_install_press_guard_is_wired_from_app_main() -> None:
    """The wiring, proved by driving it rather than by reading it: a commented
    -out call, or one made against some other store, passes a source search
    and ships the bug.

    `app.js` runs `main()` on import when the page has a `document` and a
    `window`, so the module is imported here with a fake DOM already in place
    (under a cache-busting query, since the module registry is per-URL). Only
    the first line of `main()` is being tested - it installs the guard before
    anything mounts - so everything after it is free to fail against a DOM
    with no board, no lobby and no server; the rejection that produces is
    swallowed deliberately, and would not be if this test cared about it.

    What is asserted is the behaviour end to end: a `pointerdown` on the real
    `document` holds the real exported `store`'s fan-out, and the release
    pays it off.

    Importing `app.js` at all needs `localStorage` on `globalThis` before the
    `import` resolves - its own top-level `createStore({... stops:
    P.loadStops(localStorage) ...})` runs at import time and reads it
    immediately - the same stub `test_ui_app.py` uses for the same reason.
    Whether the bare identifier `localStorage` even exists is a Node-version
    fact, not something this test is about: on node v22.23.2 (confirmed
    locally) referencing it throws `ReferenceError: localStorage is not
    defined` straight out of the import, while a newer Node declares it (as
    an experimental, unbacked global) so the same reference just evaluates
    without throwing. Stubbing it here makes the import - and the assertion
    that follows - work the same on any Node."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        process.on("unhandledRejection", () => {});
        globalThis.localStorage = {
          getItem: () => null,
          setItem() {},
          removeItem() {},
        };
        globalThis.fetch = () => Promise.resolve({ ok: true, json: async () => ({}) });
        globalThis.location = { pathname: "/", search: "", href: "http://t/" };
        globalThis.history = { pushState() {}, replaceState() {} };
        (async () => {
          const App = await import(APP_URL + "?boot=press-guard");
          const seen = [];
          App.store.subscribe((s) => seen.push(s.connection));
          if (!document.listeners.pointerdown || !document.listeners.pointerup) {
            return { installed: false, registered: Object.keys(document.listeners) };
          }
          document.listeners.pointerdown({ target: document.querySelector("#priority-box") });
          App.store.set({ connection: "held" });
          const during = seen.length;
          document.listeners.pointerup({});
          await sleep(30);
          return { during, after: seen.length, last: seen[seen.length - 1] };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
        APP_URL=(UI / "app.js").as_uri(),
    )
    assert result.get("installed") is not False, (
        f"main() registered no press listeners on the page's document: {result}"
    )
    assert result["during"] == 0, "a press did not hold the app store's fan-out"
    assert result["after"] == 1, "the press ended and the deferred fan-out never landed"
    assert result["last"] == "held"


def test_a_hold_keeps_renders_back_past_the_press_cap_until_it_is_released() -> None:
    """`ui/preview.js`'s long-press preview is a press that is meant to last:
    the finger stays down for as long as the player is reading the card, and
    the card under it must not be rebuilt meanwhile. `holdStart` keeps the
    fan-out back past `PRESS_CAP_MS`; `holdEnd` pays it off at once. The cap
    itself is untouched for a press nothing is holding. Fake timers, so the
    cap passing is exact rather than slept."""
    result = run_module(
        "store.js",
        PRESS_DOM
        + """
        (async () => {
          const clock = { now: 0, seq: 0, timers: [] };
          globalThis.setTimeout = (fn, ms) => {
            const id = ++clock.seq;
            clock.timers.push({ id, at: clock.now + (ms || 0), fn });
            return id;
          };
          globalThis.clearTimeout = (id) => {
            clock.timers = clock.timers.filter((t) => t.id !== id);
          };
          const advance = (ms) => {
            const end = clock.now + ms;
            for (;;) {
              const due = clock.timers.filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
              if (!due) break;
              clock.timers = clock.timers.filter((t) => t !== due);
              clock.now = due.at;
              due.fn();
            }
            clock.now = end;
          };

          const { store, counter } = await mounted();
          document.listeners.pointerdown({ pointerType: "touch", target: passButton() });
          store.set(baseApp(baseState({ turn_number: 3 })));
          advance(350);
          store.holdStart();
          advance(m.PRESS_CAP_MS * 3);
          const held = counter.renders;
          store.set(baseApp(baseState({ turn_number: 4 })));
          const stillHeld = counter.renders;
          store.holdEnd();
          const released = counter.renders;
          store.holdEnd();
          const idempotent = counter.renders;

          document.listeners.pointerdown({ pointerType: "mouse", target: passButton() });
          store.set(baseApp(baseState({ turn_number: 5 })));
          advance(m.PRESS_CAP_MS);
          return { held, stillHeld, released, idempotent, capped: counter.renders,
                   turnInfo: document.querySelector("#turn-info").textContent };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result["held"] == 0, "the press cap flushed a render the hold was keeping back"
    assert result["stillHeld"] == 0
    assert result["released"] == 1, "holdEnd did not pay off the held render"
    assert result["idempotent"] == 1
    assert result["capped"] == 2, "an ordinary press no longer flushes on the cap"
    assert "turn 5" in result["turnInfo"]
