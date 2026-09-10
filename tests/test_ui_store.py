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

from pathlib import Path

from test_ui_ribbon import FAKE_DOM
from ui_node import UI, requires_node, run_module

pytestmark = requires_node

RIBBON_URL = (UI / "render" / "ribbon.js").as_uri()
PANELS_URL = (UI / "render" / "panels.js").as_uri()

# What the ribbon's fake DOM does not have, because nothing in `ribbon.js`
# needed it: `document`/`window` listeners that a test can reach. The `El`
# class already records per-element listeners by type; `document` is an `El`,
# so it gets the same recorder spelled out explicitly (the third `capture`
# argument is irrelevant to a fake with no real dispatch), and `window` - a
# plain object in that fixture - gets one of its own. Plus the boxes
# `mountTopbar` renders into, so step 7 can read `#turn-info` off the real
# topbar rather than a restatement of it.
PRESS_DOM = (
    FAKE_DOM
    + """
document.listeners = {};
document.addEventListener = (t, fn) => (document.listeners[t] = fn);
globalThis.window.listeners = {};
globalThis.window.addEventListener = (t, fn) => (window.listeners[t] = fn);

boxed("turn-info");
boxed("phase-track");
boxed("ai-panel");
boxed("ai-loading-banner", true);
boxed("ai-stall-banner", true);
boxed("modal", true);

const TOPBAR_HANDLERS = {
  onOpenZones: NOOP, onLeaveGame: NOOP, onPassForAiSeat: NOOP, onReportProblem: NOOP,
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
    rendering for the rest of the game."""
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

          // No pointerup at all. Past the cap, the flush must have fired.
          await sleep(1200);
          const turnInfo = document.querySelector("#turn-info").textContent;
          return { during, after: counter.renders, turnInfo };
        })()
        """,
        RIBBON_URL=RIBBON_URL,
        PANELS_URL=PANELS_URL,
    )
    assert result["during"] == 1
    assert result["after"] == 2
    assert "turn 4" in result["turnInfo"]


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
    """The wiring is one call from `main()`, not listener registration
    scattered across render modules."""
    app_js = Path(UI / "app.js").read_text()
    assert "installPressGuard" in app_js
    main_body = app_js.split("async function main() {", 1)[1]
    assert "installPressGuard(store)" in main_body
