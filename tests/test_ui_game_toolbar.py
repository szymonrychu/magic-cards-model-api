"""The top-right game toolbar: Concede, Report and Lobby, moved out of the
in-flow topbar/ribbon into a `position: fixed` corner so all three stay
reachable at every viewport width - and the "Zones" and "Log" buttons that
used to sit beside Report and Lobby in `.topbar-right`, removed at the
owner's request.

Two removed buttons, two places the information they showed had to stay
reachable:

- The "Zones" popover's one exclusive bit of info - the command zone (CR
  408: commanders, emblems) - now draws in the always-visible zone ribbon
  itself (`ui/board_layout.js`'s `zoneRibbon`, covered by
  `tests/test_ui_board_layout.py`). Everything else that popover showed
  (graveyards, shared exile, the CR 406.4 marked exile piles) was already a
  subset of `state.exile`/`state.graveyards`, both already drawn in that same
  ribbon.
- The "Log" button's overlay (`#log-modal`) is gone, and so is the game log
  panel itself; the advisor panel takes its room in `#side-panel`, covered
  by `tests/test_ui_menus.py` and `tests/test_ui_advice_layout.py`.

This file covers what is left: the two buttons are actually gone (from the
markup and from `mountTopbar`'s wiring), and the three that remain sit
together in `#game-toolbar`, pinned via `position: fixed`, with their
handlers unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

from ui_node import requires_node, run_module

pytestmark = requires_node

UI = Path(__file__).resolve().parents[1] / "ui"


def _index() -> str:
    return (UI / "index.html").read_text()


def _css() -> str:
    return (UI / "style.css").read_text()


def _rule(css: str, selector: str) -> str:
    """The body of the first top-level `selector { ... }` rule."""
    found = re.search(r"\n" + re.escape(selector) + r" \{([^}]*)\}", css)
    assert found, f"no top-level `{selector}` rule in ui/style.css"
    return found.group(1)


# --------------------------------------------------------- the removal -----


def test_the_zones_and_log_buttons_are_gone_from_the_markup() -> None:
    html = _index()
    assert 'id="open-zones"' not in html, "the Zones button should have been removed"
    assert 'id="open-log"' not in html, "the Log button should have been removed"
    assert 'id="log-modal"' not in html, "the Log button's overlay should have been removed with it"


def test_panels_js_no_longer_wires_zones_or_log() -> None:
    """`mountTopbar` used to bind `#open-zones`/`#open-log` and call
    `handlers.onOpenZones`/`onToggleLog`; none of that exists any more (the
    functions themselves are gone too - only historical, prose mentions of
    their old names may remain in comments)."""
    panels = (UI / "render" / "panels.js").read_text()
    for gone in (
        '"#open-zones"',
        '"#open-log"',
        "handlers.onOpenZones",
        "handlers.onToggleLog",
        "export function openZonesModal",
        "export function toggleLogModal",
        "export function closeLogModal",
        "export function logModalIsOpen",
    ):
        assert gone not in panels, f"panels.js still references removed `{gone}`"


def test_app_js_no_longer_wires_zones_or_log() -> None:
    app = (UI / "app.js").read_text()
    for gone in (
        "onOpenZones",
        "onToggleLog",
        "openZonesModal",
        "toggleLogModal",
        "closeLogModal",
        "logModalIsOpen",
    ):
        assert gone not in app, f"app.js still references removed `{gone}`"


# ------------------------------------------------------------- the move ----


def test_the_three_buttons_sit_together_in_the_game_toolbar() -> None:
    html = _index()
    toolbar = re.search(r'<div id="game-toolbar"[^>]*>(.*?)\n  </div>', html, re.S)
    assert toolbar, "could not find #game-toolbar in ui/index.html"
    inner = toolbar.group(1)
    assert 'id="concede-slot"' in inner
    assert re.search(r'id="report-problem"[^>]*>Report', inner)
    assert re.search(r'id="leave-game"[^>]*>Lobby', inner)


def test_the_phase_track_sits_in_the_game_toolbar_left_of_the_buttons() -> None:
    """The turn structure moved out of `#topbar` into the top row: the track
    is inside `#game-toolbar`, ahead of Concede, Report and Lobby in the
    markup, and `style.css` orders it first, so a button added as the row's
    first child still lands to the right of the track."""
    html = _index()
    toolbar = re.search(r'<div id="game-toolbar"[^>]*>(.*?)\n  </div>', html, re.S)
    assert toolbar, "could not find #game-toolbar in ui/index.html"
    inner = toolbar.group(1)
    assert '<ol id="phase-track"></ol>' in inner
    assert inner.index('id="phase-track"') < inner.index('id="concede-slot"')
    assert html.count('id="phase-track"') == 1
    track = _rule(_css(), "#phase-track")
    assert re.search(r"order:\s*-1", track), track


def test_the_phase_track_stays_on_one_line_and_scrolls_inside_itself() -> None:
    css = _css()
    track = _rule(css, "#phase-track")
    assert "flex-wrap: nowrap" in track, track
    assert "overflow-x: auto" in track, track
    # A fixed floor, not `min-width: 0`: the track shrinks and scrolls inside
    # itself, but is never squeezed to nothing by the armed Concede label.
    assert re.search(r"min-width:\s*\d+px", track), track
    concede = _rule(css, "#concede-slot")
    assert "min-width: 0" in concede, concede
    bar = re.search(r"\n\.game-toolbar \{(.*?)\}", css, re.S)
    assert bar and "flex-wrap: nowrap" in bar.group(1), (
        "the top row must never wrap into a second line"
    )
    # The track has no click behaviour, so nothing may look clickable on it.
    items = _rule(css, "#phase-track li")
    assert "cursor: pointer" not in items, items
    assert "#phase-track li.target" not in css
    assert "#phase-track { flex-wrap: wrap" not in css


def test_none_of_the_three_are_still_in_the_topbar() -> None:
    """They used to live in `.topbar-right`, beside Zones and Log - now that
    whole button row is gone from the topbar, which keeps only the AI panel
    and the connection pill."""
    html = _index()
    topbar = re.search(r'<header id="topbar">(.*?)</header>', html, re.S)
    assert topbar, "could not find #topbar in ui/index.html"
    for gone in (
        "concede-slot",
        "report-problem",
        "leave-game",
        "open-zones",
        "open-log",
        "phase-track",
    ):
        assert f'id="{gone}"' not in topbar.group(1), f"#{gone} should have moved out of #topbar"


def test_the_game_toolbar_is_pinned_to_the_viewport_at_every_width() -> None:
    """`position: fixed` (against the viewport, not `#game`'s own grid) is
    what makes the corner the same one whether `#game` is the three-column
    desktop grid or the narrow-width single-column stack - and no narrower
    `@media` block may override that, or the guarantee breaks exactly where a
    phone-width player needs it most."""
    css = _css()
    block = re.search(r"\n\.game-toolbar \{(.*?)\}", css, re.S)
    assert block, "could not find the .game-toolbar rule in ui/style.css"
    body = block.group(1)
    assert "position: fixed" in body
    assert re.search(r"top:\s*\S", body)
    assert re.search(r"right:\s*\S", body)
    # Only the one rule for the selector - nothing later in the cascade (a
    # narrower @media block, say) repositions or hides it.
    assert len(re.findall(r"\.game-toolbar\s*\{", css)) == 1


def test_game_reserves_a_top_strip_so_nothing_renders_under_the_toolbar() -> None:
    css = _css()
    block = re.search(r"\n#game \{(.*?)\n\}", css, re.S)
    assert block, "could not find the #game rule in ui/style.css"
    assert re.search(r"padding-top:\s*\d", block.group(1)), (
        "#game does not reserve a top strip - the fixed toolbar could cover the board or the ribbon"
    )


# ---------------------------------------------------- handlers unchanged ---

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
  removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  contains(other) {
    if (other === this) return true;
    return this.children.some((c) => (c.contains ? c.contains(other) : false));
  }
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
    subscribe(fn) {
      listeners.push(fn);
      return () => {};
    },
  };
}
"""


def test_lobby_and_report_keep_their_own_handlers_once_moved() -> None:
    """`mountTopbar` still wires `#leave-game` to `handlers.onLeaveGame` and
    `#report-problem` to the same report form it always opened - unchanged by
    the move out of `.topbar-right` into `#game-toolbar`."""
    result = run_module(
        "render/panels.js",
        FAKE_DOM
        + """
        (() => {
          const leave = document.createElement("button");
          leave.setAttribute("id", "leave-game");
          document.appendChild(leave);
          const report = document.createElement("button");
          report.setAttribute("id", "report-problem");
          document.appendChild(report);

          let left = 0;
          const store = makeStore({ state: null, connection: "ok" });
          m.mountTopbar(document, store, {
            onLeaveGame: () => { left += 1; },
            onPassForAiSeat: () => {},
            onReportProblem: () => {},
          });

          leave.onclick();
          const modal = document.querySelector("#modal");
          const modalHiddenBeforeReport = modal.hidden;
          report.onclick();
          return {
            left,
            modalHiddenBeforeReport,
            modalHiddenAfterReport: modal.hidden,
            modalText: modal.textContent,
          };
        })()
        """,
    )
    assert result["left"] == 1, "clicking #leave-game did not call onLeaveGame"
    assert result["modalHiddenBeforeReport"] is True
    assert result["modalHiddenAfterReport"] is False, (
        "clicking #report-problem did not open the report form"
    )
    assert "Report a problem" in result["modalText"]
