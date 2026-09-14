"""`ui/fullscreen.js`, the home-screen app head, and the safe-area shell.

A phone in landscape loses a strip of height to the browser's own bars. Where
the Fullscreen API exists (`document.fullscreenEnabled`: Chrome and Firefox on
Android, Samsung Internet, iPadOS, desktop browsers) a small button first in
`#game-toolbar` takes the page fullscreen and asks for landscape; iPhone Safari
has no element fullscreen at all, so there the button is hidden and Add to
Home Screen (the manifest and apple meta tags in `index.html`) is the way to
reclaim the space.

A reload always leaves fullscreen and the API needs a user gesture, so a
remembered preference re-requests it from the next click anywhere, once,
without touching that click. The DOM half runs the real module under Node
against `tests/test_ui_preview.py`'s fake DOM; the markup and CSS halves read
`ui/index.html` and `ui/style.css`. `tests/test_ui_viewport_layout.py` checks
the geometry, the button and the safe-area insets in a real browser.
"""

from __future__ import annotations

import re

from test_ui_preview import FAKE_DOM
from ui_node import UI, requires_node, run_module

SETUP = """
const calls = { request: [], exit: 0, lock: [], errors: [], unhandled: 0, listeners: [] };
process.on("unhandledRejection", () => { calls.unhandled += 1; });
console.error = (...a) => calls.errors.push(a.join(" "));
console.warn = (...a) => calls.errors.push(a.join(" "));

const TOOLBAR = document.createElement("div");
TOOLBAR.setAttribute("id", "game-toolbar");
const CONCEDE = document.createElement("div");
CONCEDE.setAttribute("id", "concede-slot");
TOOLBAR.appendChild(CONCEDE);
document.body.appendChild(TOOLBAR);

const TARGET = document.createElement("div");
TARGET.className = "card";
let cardClicks = 0;
TARGET.onclick = () => { cardClicks += 1; };
document.body.appendChild(TARGET);

const realAdd = document.addEventListener.bind(document);
document.addEventListener = (type, fn, opts) => {
  calls.listeners.push({ type, opts });
  realAdd(type, fn, opts);
};

document.documentElement = document.createElement("html");
document.fullscreenEnabled = true;
document.fullscreenElement = null;
let requestOutcome = "resolve";
let lockOutcome = "reject";
document.documentElement.requestFullscreen = (opts) => {
  calls.request.push(opts);
  if (requestOutcome === "reject") {
    return Promise.reject(new TypeError("Permissions check failed"));
  }
  document.fullscreenElement = document.documentElement;
  dispatch(document, "fullscreenchange");
  return Promise.resolve();
};
document.exitFullscreen = () => {
  calls.exit += 1;
  document.fullscreenElement = null;
  dispatch(document, "fullscreenchange");
  return Promise.resolve();
};

const saved = new Map();
let storageThrows = false;
const guard = () => { if (storageThrows) throw new Error("denied"); };
const storage = {
  getItem: (k) => { guard(); return saved.has(k) ? saved.get(k) : null; },
  setItem: (k, v) => { guard(); saved.set(k, String(v)); },
  removeItem: (k) => { guard(); saved.delete(k); },
};
Object.defineProperty(globalThis, "localStorage", {
  value: storage, configurable: true, writable: true,
});
Object.defineProperty(globalThis, "screen", {
  value: { orientation: { lock: (o) => {
    calls.lock.push(o);
    if (lockOutcome === "reject") return Promise.reject(new Error("NotSupportedError"));
    return Promise.resolve();
  } } },
  configurable: true,
  writable: true,
});

const settle = () => new Promise((r) => setImmediate(r));
const button = () => TOOLBAR.children[0];
const pref = () => saved.get("mcm.fullscreen") || null;
const view = () => ({
  first: button() !== CONCEDE,
  tag: button().tagName,
  id: button().id,
  hidden: button().hidden,
  pressed: button().getAttribute("aria-pressed"),
  label: button().getAttribute("aria-label"),
  request: calls.request,
  exit: calls.exit,
  lock: calls.lock,
  errors: calls.errors,
  unhandled: calls.unhandled,
  pref: pref(),
  cardClicks,
});
"""


def run(expression: str):
    return run_module("fullscreen.js", FAKE_DOM + SETUP + expression)


# ------------------------------------------------------------ the button ---


@requires_node
def test_the_button_is_hidden_where_the_api_is_missing() -> None:
    """iPhone Safari: `document.fullscreenEnabled` is not there at all."""
    for enabled in ("false", "undefined"):
        result = run(
            f"""(async () => {{
              document.fullscreenEnabled = {enabled};
              m.mountFullscreen(document);
              return view();
            }})()"""
        )
        assert result["first"] is True, result
        assert result["tag"] == "button", result
        assert result["hidden"] is True, (enabled, result)


@requires_node
def test_the_button_is_shown_first_in_the_toolbar_where_the_api_exists() -> None:
    result = run(
        """(async () => {
          m.mountFullscreen(document);
          return view();
        })()"""
    )
    assert result["first"] is True and result["tag"] == "button", result
    assert result["id"] == "fullscreen-toggle", result
    assert result["hidden"] is False, result
    assert result["pressed"] == "false", result
    assert result["label"] == "Enter fullscreen", result


@requires_node
def test_a_click_requests_fullscreen_hides_the_navigation_and_locks_landscape() -> None:
    result = run(
        """(async () => {
          m.mountFullscreen(document);
          dispatch(button(), "click");
          await settle();
          await settle();
          return view();
        })()"""
    )
    assert result["request"] == [{"navigationUI": "hide"}], result
    assert result["lock"] == ["landscape"], result
    assert result["unhandled"] == 0 and result["errors"] == [], result
    assert result["pressed"] == "true", result
    assert result["label"] == "Exit fullscreen", result
    assert result["pref"] == "1", result


@requires_node
def test_a_refused_request_is_swallowed_and_forgets_the_preference() -> None:
    result = run(
        """(async () => {
          requestOutcome = "reject";
          m.mountFullscreen(document);
          dispatch(button(), "click");
          await settle();
          await settle();
          return view();
        })()"""
    )
    assert result["request"] == [{"navigationUI": "hide"}], result
    assert result["lock"] == [], result
    assert result["unhandled"] == 0 and result["errors"] == [], result
    assert result["pressed"] == "false", result
    assert result["pref"] is None, result


@requires_node
def test_a_request_that_throws_or_returns_nothing_is_still_quiet() -> None:
    """Older engines return `undefined`, and a sandboxed frame can throw."""
    result = run(
        """(async () => {
          m.mountFullscreen(document);
          document.documentElement.requestFullscreen = (opts) => {
            calls.request.push(opts);
            throw new TypeError("nope");
          };
          dispatch(button(), "click");
          await settle();
          const afterThrow = view();
          document.documentElement.requestFullscreen = (opts) => {
            calls.request.push(opts);
            return undefined;
          };
          screen.orientation.lock = () => { throw new Error("no lock"); };
          dispatch(button(), "click");
          await settle();
          return { afterThrow, after: view() };
        })()"""
    )
    assert result["afterThrow"]["unhandled"] == 0 and result["afterThrow"]["errors"] == [], result
    assert result["afterThrow"]["pref"] is None, result
    assert len(result["after"]["request"]) == 2, result
    assert result["after"]["unhandled"] == 0 and result["after"]["errors"] == [], result


@requires_node
def test_a_click_while_fullscreen_exits_and_turns_the_preference_off() -> None:
    result = run(
        """(async () => {
          m.mountFullscreen(document);
          dispatch(button(), "click");
          await settle();
          const entered = view();
          dispatch(button(), "click");
          await settle();
          return { entered, exited: view() };
        })()"""
    )
    assert result["entered"]["pref"] == "1", result
    assert result["exited"]["exit"] == 1, result
    assert len(result["exited"]["request"]) == 1, result
    assert result["exited"]["pressed"] == "false", result
    assert result["exited"]["pref"] is None, result


@requires_node
def test_fullscreenchange_updates_aria_pressed() -> None:
    result = run(
        """(async () => {
          m.mountFullscreen(document);
          document.fullscreenElement = document.documentElement;
          dispatch(document, "fullscreenchange");
          const on = button().getAttribute("aria-pressed");
          document.fullscreenElement = null;
          dispatch(document, "fullscreenchange");
          return { on, off: button().getAttribute("aria-pressed") };
        })()"""
    )
    assert result == {"on": "true", "off": "false"}, result


@requires_node
def test_leaving_by_the_system_back_or_escape_turns_the_preference_off() -> None:
    result = run(
        """(async () => {
          m.mountFullscreen(document);
          dispatch(button(), "click");
          await settle();
          const entered = pref();
          document.fullscreenElement = null;
          dispatch(document, "fullscreenchange");
          return { entered, after: view() };
        })()"""
    )
    assert result["entered"] == "1", result
    assert result["after"]["pref"] is None, result
    assert result["after"]["exit"] == 0, result


@requires_node
def test_the_exit_a_reload_causes_does_not_forget_the_preference() -> None:
    """The page leaving fullscreen because it is unloading is not the player
    asking to leave it."""
    result = run(
        """(async () => {
          m.mountFullscreen(document);
          dispatch(button(), "click");
          await settle();
          dispatch(window, "pagehide");
          document.fullscreenElement = null;
          dispatch(document, "fullscreenchange");
          return view();
        })()"""
    )
    assert result["pref"] == "1", result


# ------------------------------------------------------ after a reload ---


@requires_node
def test_after_a_reload_the_first_click_anywhere_re_requests_once_and_still_lands() -> None:
    result = run(
        """(async () => {
          saved.set("mcm.fullscreen", "1");
          m.mountFullscreen(document);
          const before = calls.request.length;
          const ev = dispatch(TARGET, "click");
          await settle();
          const first = { ...view(), prevented: ev.defaultPrevented, stopped: ev._stop };
          document.fullscreenElement = null;
          dispatch(document, "fullscreenchange");
          saved.set("mcm.fullscreen", "1");
          dispatch(TARGET, "click");
          await settle();
          const clickListeners = calls.listeners.filter((l) => l.type === "click");
          return { before, first, second: view(), clickListeners };
        })()"""
    )
    assert result["before"] == 0, result
    first = result["first"]
    assert first["request"] == [{"navigationUI": "hide"}], result
    assert first["cardClicks"] == 1, "the card's own click handler must still run"
    assert first["prevented"] is False and first["stopped"] is False, result
    assert first["lock"] == ["landscape"], result
    assert first["unhandled"] == 0 and first["errors"] == [], result
    assert len(result["second"]["request"]) == 1, "only the first click re-requests"
    assert result["second"]["cardClicks"] == 2, result
    assert result["clickListeners"], result
    for listener in result["clickListeners"]:
        opts = listener["opts"]
        assert opts["capture"] is True and opts["passive"] is True, listener


@requires_node
def test_nothing_is_re_requested_when_the_preference_is_off() -> None:
    result = run(
        """(async () => {
          m.mountFullscreen(document);
          dispatch(TARGET, "click");
          await settle();
          return view();
        })()"""
    )
    assert result["request"] == [] and result["cardClicks"] == 1, result


@requires_node
def test_nothing_is_re_requested_where_the_api_is_missing() -> None:
    result = run(
        """(async () => {
          saved.set("mcm.fullscreen", "1");
          document.fullscreenEnabled = false;
          m.mountFullscreen(document);
          dispatch(TARGET, "click");
          await settle();
          return view();
        })()"""
    )
    assert result["request"] == [] and result["cardClicks"] == 1, result


@requires_node
def test_a_first_click_on_the_button_itself_requests_only_once() -> None:
    result = run(
        """(async () => {
          saved.set("mcm.fullscreen", "1");
          m.mountFullscreen(document);
          dispatch(button(), "click");
          await settle();
          return view();
        })()"""
    )
    assert result["request"] == [{"navigationUI": "hide"}], result
    assert result["exit"] == 0 and result["pressed"] == "true", result


@requires_node
def test_storage_that_throws_never_breaks_the_button() -> None:
    result = run(
        """(async () => {
          storageThrows = true;
          m.mountFullscreen(document);
          dispatch(button(), "click");
          await settle();
          dispatch(button(), "click");
          await settle();
          return view();
        })()"""
    )
    assert len(result["request"]) == 1 and result["exit"] == 1, result
    assert result["errors"] == [] and result["unhandled"] == 0, result


def test_app_js_mounts_the_fullscreen_button() -> None:
    app = (UI / "app.js").read_text()
    assert re.search(r'import \{ mountFullscreen \} from "\./fullscreen\.js";', app)
    assert "mountFullscreen(document)" in app


# ------------------------------------------------------ the page head ---


def _head() -> str:
    html = (UI / "index.html").read_text()
    head = re.search(r"<head>(.*?)</head>", html, re.S)
    assert head, "could not find <head> in ui/index.html"
    return head.group(1)


def test_the_head_links_the_manifest_relatively_with_credentials() -> None:
    """Behind oauth2-proxy every non-/api/ request needs the session cookie, and
    a browser fetches the manifest without cookies unless the link asks for
    them - the proxy would answer with the login page and the manifest would
    be dropped (no install prompt, no fullscreen, no icon)."""
    link = '<link rel="manifest" href="manifest.webmanifest" crossorigin="use-credentials" />'
    assert link in _head()


def test_the_head_carries_the_home_screen_meta_tags() -> None:
    head = _head()
    assert '<meta name="theme-color" content="#10131a" />' in head
    assert '<meta name="mobile-web-app-capable" content="yes" />' in head
    assert '<meta name="apple-mobile-web-app-capable" content="yes" />' in head
    assert (
        '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />' in head
    )
    assert '<link rel="apple-touch-icon" href="icons/icon-192.png" />' in head


def test_the_viewport_reaches_under_the_notch() -> None:
    assert (
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />'
        in _head()
    )


def test_the_theme_colour_matches_the_dark_board() -> None:
    css = (UI / "style.css").read_text()
    bg = re.search(r"--bg:\s*(#[0-9a-f]{6});", css).group(1)
    assert f'<meta name="theme-color" content="{bg}" />' in _head()


# ----------------------------------------------------- dvh and insets ---


def _css() -> str:
    return (UI / "style.css").read_text()


def test_the_game_shell_is_one_dynamic_viewport_tall_with_a_vh_fallback_first() -> None:
    block = re.search(r"\n#game \{(.*?)\n\}", _css(), re.S).group(1)
    heights = re.findall(r"^\s*height:\s*(.+);", block, re.M)
    assert len(heights) == 2, heights
    assert "100vh" in heights[0] and "100dvh" not in heights[0], heights
    assert "100dvh" in heights[1], heights


def test_the_insets_are_read_once_and_pad_the_page() -> None:
    css = _css()
    for side in ("top", "right", "bottom", "left"):
        assert re.search(rf"--safe-{side}:\s*env\(safe-area-inset-{side},\s*0px\);", css), side
    body = re.search(r"/\* -+ fullscreen.*", css, re.S)
    assert body, "no delimited fullscreen section at the end of ui/style.css"
    section = body.group(0)
    assert re.search(
        r"body \{[^}]*padding:\s*var\(--safe-top\) var\(--safe-right\) "
        r"var\(--safe-bottom\) var\(--safe-left\);",
        section,
    ), section
    game = re.search(r"\n#game \{(.*?)\n\}", css, re.S).group(1)
    assert "var(--safe-top)" in game and "var(--safe-bottom)" in game, (
        "#game's height must leave room for the body's inset padding or the page scrolls"
    )


def test_the_fixed_controls_are_kept_out_of_the_insets() -> None:
    section = re.search(r"/\* -+ fullscreen.*", _css(), re.S).group(0)
    for selector in ("#game-toolbar", "#rail-pass", "#drawer-toggle"):
        rule = re.search(rf"{re.escape(selector)}[^{{]*\{{([^}}]*)\}}", section)
        assert rule, selector
        assert "var(--safe-right)" in rule.group(1), (selector, rule.group(1))
