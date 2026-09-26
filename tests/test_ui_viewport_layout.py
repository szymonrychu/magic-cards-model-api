"""The play page in a real browser, at real window sizes - phones included.

`#game` must fill the viewport with no page-level scroll at every width, not
only on a desktop: a phone in landscape (844x390) and in portrait, a small
tablet, a laptop. The seven board rows must fit the board's height without it
scrolling, every card must fit inside its row, and each seat's player badge
must sit in its land row without covering a card.

On a desktop the zone ribbon, the side panel and the turn block stay on
screen and out from under the fixed toolbar. In mobile mode (1100px wide or
less here - headless Chrome has a fine pointer) the zone ribbon is hidden,
the board takes the width, and the turn block and side panel live in a
drawer that is off screen until opened; one extra 844x390 frame is measured
with the drawer open. The crowded board has this seat holding priority, so in
mobile mode the rail's Pass controls (`#rail-pass`) are on screen with the
drawer closed, clear of every card, the toolbar and the toggle; on a desktop
they are not shown. No frame ever shows the rotate overlay, which is only
for a coarse pointer held upright.

CSS layout cannot be checked from a regex, so this serves `ui/` over HTTP,
mounts the real `render/board.js` on the real `#game` markup and `style.css`
with a crowded board, and loads that page into iframes of exact sizes inside
one headless Chrome (an iframe's viewport is exactly its box, which a
headless window's is not). The harness page measures every frame and POSTs
the numbers back to the test server; the test waits on that report against a
real-time deadline, so a busy machine only makes it slower, never wrong.

Skipped when no Chrome/Chromium is installed.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"

#: This module's own `measurements` fixture docstring already names the
#: failure mode `-n 6` reopens: "near-certainly [fails] when several copies
#: of this file race each other" - a real headless-Chrome flake, not
#: something a retry budget invented. Without this, xdist's default
#: scheduling can hand different tests from this one module to different
#: workers, so up to six of them launch their own concurrent Chrome copy at
#: once even though `measurements` (`scope="module"`) means only one COULD
#: ever be needed per worker. `xdist_group` pins every test below to a
#: single worker, so at most one Chrome copy from this file ever runs at a
#: time - exactly the serial suite's own concurrency, restored under `-n 6`.
pytestmark = pytest.mark.xdist_group(name="ui_viewport_layout")

#: (width, height) of every viewport checked. Phones in landscape are the
#: sizes the old stacked layout scrolled on; the rest cover each breakpoint.
VIEWPORTS = [
    (844, 390),
    (667, 375),
    (740, 360),
    (932, 430),
    (390, 844),
    (1024, 768),
    (1100, 700),
    (1280, 800),
    (1280, 720),
    (1280, 480),
    (1440, 900),
    (1920, 1080),
]

#: The one extra frame measured with the drawer open.
DRAWER_OPEN = (844, 390)

#: Frames measured with CR 601.3 play permissions on the board, so
#: `#permitted-row` draws cards under the hand.
PERMITTED = [(1920, 1080), (844, 390)]

#: How many cards the permitted frames' `play_permissions` name.
PERMITTED_CARDS = 3

#: The one frame measured with an iPhone's landscape safe-area insets (the
#: notch on both sides, the home indicator along the bottom). Chrome has no
#: notch to report through `env()`, so the frame sets the `--safe-*` tokens
#: `style.css` reads those insets into.
SAFE_AREA = (844, 390)
SAFE_INSETS = {"top": 0, "right": 47, "bottom": 21, "left": 47}

#: The one frame where the decision window is open on a phone with its
#: remembered spot at the very top edge (`localStorage`, as a drag there used
#: to leave it), mounted twice as a reload would. Its title bar is the only
#: way to move it, so it must never sit under the top row.
DECISION_TOP = (844, 390)

MOBILE_QUERY = "(max-width: 1100px), (pointer: coarse) and (orientation: landscape)"

SEVEN_ROWS = [
    "opp-hand",
    "opp-lands",
    "opp-battlefield",
    "stack-row",
    "my-battlefield",
    "my-lands",
    "my-hand",
]


def _chrome() -> str | None:
    candidates = [
        os.environ.get("CHROME"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


CHROME = _chrome()

pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium installed")


def _card(card_id: str, controller: str, type_line: str, zone: str, **over: object) -> dict:
    return {
        "id": card_id,
        "name": f"{type_line.split(' ')[0]} {card_id}",
        "controller": controller,
        "owner": controller,
        "tapped": False,
        "summoning_sick": False,
        "type_line": type_line,
        "zone": zone,
        "abilities": [],
        **over,
    }


def _crowded_state() -> dict:
    """A mid-game board with every row busy: eight creatures and seven lands a
    side (some tapped), full hands, two spells on the stack, and cards in both
    graveyards and exiles."""
    state = json.loads((ROOT / "fixtures" / "payloads" / "mid_game_full_state.json").read_text())
    seats = ["alice", "bob"]
    battlefield = []
    for seat in seats:
        for i in range(8):
            battlefield.append(
                _card(f"{seat}-c{i}", seat, "Creature - Bear", "battlefield", tapped=i < 2)
            )
        for i in range(7):
            battlefield.append(
                _card(f"{seat}-l{i}", seat, "Basic Land - Forest", "battlefield", tapped=i < 3)
            )
    state["you"] = "alice"
    state["battlefield"] = battlefield
    state["hands"] = {"alice": [_card(f"alice-h{i}", "alice", "Instant", "hand") for i in range(7)]}
    state["stack"] = [
        _card("s1", "bob", "Instant", "stack", targets=["alice-c3"], x_value=3),
        _card("s2", "alice", "Instant", "stack", targets=[]),
    ]
    state["graveyards"] = {
        seat: [_card(f"{seat}-g{i}", seat, "Instant", "graveyard") for i in range(5)]
        for seat in seats
    }
    if isinstance(state.get("exile"), dict):
        state["exile"] = {
            seat: [_card(f"{seat}-e{i}", seat, "Instant", "exile") for i in range(2)]
            for seat in seats
        }
    for player in state["players"]:
        player["hand_count"] = 7
    state["pending_decision"] = None
    state["play_permissions"] = {}
    # Late in the turn, so on a window whose track has to scroll the current
    # step starts out past its right edge.
    state["step"] = "end_step"
    return state


def _permissions() -> dict:
    """`state.play_permissions` naming `PERMITTED_CARDS` exiled cards, in the
    shape `serialize.py`'s `_play_permission_view` writes."""
    return {
        "alice": [
            {
                "card": _card(f"alice-p{i}", "alice", "Instant", "exile"),
                "granted_by": "Bloodbraid Elf",
                "from_zone": "exile",
                "usable_now": i != 1,
                "why_not": None if i != 1 else "not your main phase",
                "rule": "CR 601.3",
            }
            for i in range(PERMITTED_CARDS)
        ]
    }


def _game_page() -> str:
    index = (UI / "index.html").read_text()
    section = re.search(r'<section id="game" hidden>.*?</section>', index, re.S)
    assert section, "could not find #game in ui/index.html"
    markup = section.group(0).replace('<section id="game" hidden>', '<section id="game">', 1)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<link rel="stylesheet" href="/style.css" />
</head><body>
{markup}
<script type="module">
import {{ mountBoard }} from "/render/board.js";
import {{ mountTopbar }} from "/render/panels.js";
import * as ribbon from "/render/ribbon.js";
import {{ mountFullscreen }} from "/fullscreen.js";
import {{ mountDecisionWindow, WINDOW_POSITION_KEY }} from "/render/decision_window.js";
try {{
if (location.search.includes("safe=1")) {{
  const insets = document.createElement("style");
  insets.textContent = ":root {{ " + Object.entries({json.dumps(SAFE_INSETS)})
    .map(([side, px]) => `--safe-${{side}}: ${{px}}px;`).join(" ") + " }}";
  document.head.appendChild(insets);
}}
const state = {json.dumps(_crowded_state())};
if (location.search.includes("permitted=1")) state.play_permissions = {json.dumps(_permissions())};
let app = {{
  state, action: null, decisionDraft: {{}}, combatDraft: {{}}, preview: null, symbols: {{}},
}};
const listeners = [];
const store = {{
  get: () => app,
  set(patch) {{ app = {{ ...app, ...patch }}; for (const fn of listeners) fn(app); }},
  subscribe(fn) {{ listeners.push(fn); return () => {{}}; }},
}};
const noop = () => {{}};
const $ = (sel) => document.querySelector(sel);
mountBoard(document, store, {{
  onHandCard: noop, onPermanent: noop, onPlayer: noop,
  onStackObject: noop, onPermission: noop, onLand: noop,
}});
// The real turn line, phase track and connection pill, as `app.js` mounts them.
mountTopbar(document, store, {{ onLeaveGame: noop, onPassForAiSeat: noop, onReportProblem: noop }});
$("#concede-slot").innerHTML = "<button>Concede - click again to confirm</button>";
// The fullscreen button as an Android phone shows it: a frame has no
// fullscreen permission of its own, so the API is reported present here.
Object.defineProperty(document, "fullscreenEnabled", {{ value: true, configurable: true }});
mountFullscreen(document);
$("#prompt-box").textContent = "You have priority in your precombat main phase.";
$("#priority-box").innerHTML = "<button>Pass priority</button> <button>Resolve top</button>";
$("#stops-box").textContent = "Stops: upkeep, draw, attackers, blockers, end";
$("#advice-box").textContent = "Buddy proposes attacking with every untapped bear. ".repeat(90);
if (location.search.includes("drawer=open")) {{
  // Measured at rest, not part way through the slide-in.
  const still = document.createElement("style");
  still.textContent = "#drawer {{ transition: none !important; }}";
  document.head.appendChild(still);
  $("#game").classList.add("drawer-open");
}}
// This seat holds priority on the crowded board: the rail's Pass controls
// render from the real `ribbon.js`, as `mountRibbon` draws them.
const rail = $("#rail-pass");
if (rail) store.subscribe((a) => ribbon.renderRailPass(rail, a, {{ onVerb: noop }}));
store.set({{}});
window.__phaseTrackAfterSet = document.querySelectorAll("#phase-track li").length;
if (location.search.includes("decision=top")) {{
  const spot = JSON.stringify({{ left: 40, top: 0 }});
  try {{ localStorage.setItem(WINDOW_POSITION_KEY, spot); }} catch {{}}
  const scry = {{
    generation: 1, player: state.you, kind: "scry", prompt: "Scry 2.", source: "Opt",
    amount: 2, exact: false, cards: ["x1", "x2"], card_labels: ["Opt", "Island"],
    targets: [], target_minimum: 0, target_maximum: null, affirmative_is_legal: true,
    options: [], moved_label: "", kept_label: "", default_cards: null,
  }};
  const handlers = {{ onDecisionDraft: noop, onDecide: noop, onPregame: noop }};
  // Mounted, shown, then mounted again: the reload a saved spot survives.
  mountDecisionWindow(document, store, handlers);
  store.set({{ state: {{ ...state, pending_decision: scry }} }});
  mountDecisionWindow(document, store, handlers);
}}
}} catch (err) {{
  window.__mountError = String((err && err.stack) || err);
}}
</script>
</body></html>"""


def _harness_page() -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8" /></head><body style="margin:0">
<pre id="result">pending</pre>
<div id="frames"></div>
<script>
const SIZES = {json.dumps(VIEWPORTS)};
const FRAMES = [...SIZES.map((size) => ({{ size, open: false, permitted: false }})),
  {{ size: {json.dumps(list(DRAWER_OPEN))}, open: true, permitted: false }},
  ...{json.dumps(PERMITTED)}.map((size) => ({{ size, open: false, permitted: true }})),
  {{ size: {json.dumps(list(SAFE_AREA))}, open: false, permitted: false, safe: true }},
  {{ size: {json.dumps(list(DECISION_TOP))}, open: false, permitted: false, decision: true }}];
const MOBILE = {json.dumps(MOBILE_QUERY)};
const ROWS = {json.dumps(SEVEN_ROWS)};
const REGIONS = ["#topbar", "#banners", "#zone-ribbon", "#side-panel", "#mtg-board"];
const report = (value) => {{
  const body = JSON.stringify(value);
  document.getElementById("result").textContent = body;
  fetch("/__result", {{ method: "POST", body }});
}};
const box = (el) => {{
  if (!el) return null;
  const {{ left, top, right, bottom, width, height }} = el.getBoundingClientRect();
  return {{ left, top, right, bottom, width, height }};
}};
function measure(frame, {{ size, open, permitted, safe, decision }}) {{
  const w = frame.contentWindow;
  const d = frame.contentDocument;
  const $ = (sel) => d.querySelector(sel);
  const board = $("#mtg-board");
  const rows = ROWS.map((id) => {{
    const row = $("#" + id);
    const style = w.getComputedStyle(row);
    const inner = box(row);
    inner.top += parseFloat(style.borderTopWidth);
    inner.bottom -= parseFloat(style.borderBottomWidth);
    return {{
      id,
      box: box(row),
      inner,
      cardW: style.getPropertyValue("--card-w").trim(),
      scroll: row.classList.contains("fit-scroll"),
      children: [...row.children].map(box),
      cards: [...row.querySelectorAll(".card, .card-back, .stack-caption")].map(box),
    }};
  }});
  const permittedRow = $("#permitted-row");
  const permittedStyle = w.getComputedStyle(permittedRow);
  const permittedInner = box(permittedRow);
  permittedInner.left += parseFloat(permittedStyle.borderLeftWidth);
  permittedInner.right -= parseFloat(permittedStyle.borderRightWidth);
  permittedInner.top += parseFloat(permittedStyle.borderTopWidth);
  permittedInner.bottom -= parseFloat(permittedStyle.borderBottomWidth);
  const permittedCards = [...permittedRow.querySelectorAll(".card")];
  w.scrollTo(100000, 100000);
  const scrolled = [w.scrollX, w.scrollY];
  const display = (sel) => ($(sel) ? w.getComputedStyle($(sel)).display : null);
  // The side panel's own content box, its visible children in order, and the
  // advisor panel that should fill what they leave - then the same again with
  // the advisor panel hidden, as it is while there is no proposal.
  const side = $("#side-panel");
  const advice = $("#advice-box");
  const sidePanel = () => {{
    const style = w.getComputedStyle(side);
    const outer = box(side);
    return {{
      box: outer,
      inner: {{
        top: outer.top + parseFloat(style.borderTopWidth) + parseFloat(style.paddingTop),
        bottom:
          outer.bottom - parseFloat(style.borderBottomWidth) - parseFloat(style.paddingBottom),
      }},
      scrollHeight: side.scrollHeight,
      clientHeight: side.clientHeight,
      children: [...side.children]
        .filter((c) => w.getComputedStyle(c).display !== "none")
        .map((c) => ({{ id: c.id, ...box(c) }})),
    }};
  }};
  const sideShown = sidePanel();
  const adviceShown = {{
    box: box(advice),
    overflowY: w.getComputedStyle(advice).overflowY,
    scrollHeight: advice.scrollHeight,
    clientHeight: advice.clientHeight,
  }};
  advice.hidden = true;
  const sideHidden = sidePanel();
  const pageHidden = [d.scrollingElement.scrollWidth, d.scrollingElement.scrollHeight];
  w.scrollTo(100000, 100000);
  const scrolledHidden = [w.scrollX, w.scrollY];
  w.scrollTo(0, 0);
  advice.hidden = false;
  return {{
    size,
    open,
    permitted,
    safe: Boolean(safe),
    decision: Boolean(decision),
    decisionWindowHidden: $("#decision-window").hidden,
    decisionHandle: box($("#decision-window-handle")),
    fullscreen: box($("#fullscreen-toggle")),
    fullscreenDisplay: display("#fullscreen-toggle"),
    fullscreenFirst: $("#game-toolbar").firstElementChild === $("#fullscreen-toggle"),
    permittedRow: {{
      box: box(permittedRow),
      inner: permittedInner,
      display: permittedStyle.display,
      cardW: permittedStyle.getPropertyValue("--card-w").trim(),
      scrollWidth: permittedRow.scrollWidth,
      clientWidth: permittedRow.clientWidth,
      cards: permittedCards.map(box),
      classes: permittedCards.map((c) => c.className),
    }},
    mobile: w.matchMedia(MOBILE).matches,
    sidePanel: sideShown,
    advice: adviceShown,
    adviceHidden: {{ sidePanel: sideHidden, page: pageHidden, scrolled: scrolledHidden }},
    scrolled,
    viewport: [w.innerWidth, w.innerHeight],
    page: [d.scrollingElement.scrollWidth, d.scrollingElement.scrollHeight],
    board: {{
      box: box(board), scrollHeight: board.scrollHeight, clientHeight: board.clientHeight,
    }},
    rows,
    regions: Object.fromEntries(REGIONS.map((s) => [s, box($(s))])),
    toolbar: box($("#game-toolbar")),
    phaseTrack: (() => {{
      const track = $("#phase-track");
      return {{
        box: box(track),
        display: w.getComputedStyle(track).display,
        inToolbar: $("#game-toolbar").contains(track),
        inTopbar: $("#topbar").contains(track),
        items: [...track.children].map(box),
        current: box(track.querySelector("li.current")),
        scrollWidth: track.scrollWidth,
        clientWidth: track.clientWidth,
      }};
    }})(),
    toolbarButtons: [...$("#game-toolbar").querySelectorAll("button")].map(box),
    zoneRibbonDisplay: display("#zone-ribbon"),
    rotateDisplay: display("#rotate-overlay"),
    drawer: box($("#drawer")),
    drawerVisibility: w.getComputedStyle($("#drawer")).visibility,
    toggle: box($("#drawer-toggle")),
    toggleDisplay: display("#drawer-toggle"),
    railPass: box($("#rail-pass")),
    railPassDisplay: display("#rail-pass"),
    railButtons: $("#rail-pass")
      ? [...$("#rail-pass").querySelectorAll("button")]
          .map((b) => ({{ ...box(b), text: b.textContent }}))
      : [],
    badges: {{ opp: box($("#opp-badge")), my: box($("#my-badge")) }},
    badgeFit: Object.fromEntries(["opp", "my"].map((side) => {{
      const el = $("#" + side + "-badge");
      return [side, [el.scrollHeight, el.clientHeight]];
    }})),
    badgeRows: {{ opp: box($("#opp-lands")), my: box($("#my-lands")) }},
    boardCards: [...board.querySelectorAll(".card, .card-back")].map(box),
    stackCards: $("#stack-row").querySelectorAll(".card").length,
  }};
}}
// Loading and settling every frame AT ONCE - append all 13-odd iframes, each
// mounting its own full copy of the board plus module imports, then race
// `requestAnimationFrame`s against all of them together - multiplies peak
// CPU/memory work by the frame count right when the machine is already
// busiest (the full suite running many pytest workers at once, each
// spawning its own headless Chrome). Under real contention a frame has been
// observed still rendering with `#phase-track` empty two animation frames
// after its own "load" fired. So this instead loads and settles frames ONE
// AT A TIME: only ever one iframe is actively parsing/executing/painting,
// which caps peak load to a single frame's worth regardless of how many
// pytest workers are competing for the CPU, and each frame is polled real
// time until `#phase-track` (set once per mount, off `store.set({{}})` inside
// the same synchronous module script "load" waits for, and never cleared
// afterwards) actually has its step items rather than assumed settled after
// a fixed frame count - bounded so a page that never settles still fails
// instead of hanging.
async function loadFrame({{ size: [width, height], open, permitted, safe, decision }}) {{
  const f = document.createElement("iframe");
  f.style.cssText = `width:${{width}}px;height:${{height}}px;border:0;display:block`;
  const query = [
    open ? "drawer=open" : "",
    permitted ? "permitted=1" : "",
    safe ? "safe=1" : "",
    decision ? "decision=top" : "",
  ].filter(Boolean).join("&");
  // `src` is set before the iframe joins the document on purpose: appending
  // a `src`-less iframe first briefly navigates it to `about:blank`, which
  // fires its own "load" event - `poll` below would then read that empty
  // document forever, real page or not, and reliably time out (this cost a
  // whole debugging pass to a machine-load red herring the first time
  // around). Setting `src` first means the one "load" this fires is for the
  // real page.
  f.src = "/__viewport_game.html" + (query ? "?" + query : "");
  document.getElementById("frames").appendChild(f);
  await new Promise((resolve, reject) => {{
    f.addEventListener("load", () => {{
      const deadline = performance.now() + 20000;
      const poll = () => {{
        const w = f.contentWindow;
        if (w && w.__mountError) {{
          reject(new Error(f.src + ": " + w.__mountError));
          return;
        }}
        const track = f.contentDocument && f.contentDocument.querySelector("#phase-track");
        if (track && track.children.length > 0) {{
          resolve();
          return;
        }}
        if (performance.now() > deadline) {{
          const has = Boolean(f.contentDocument && f.contentDocument.querySelector("#phase-track"));
          const state = f.contentDocument ? f.contentDocument.readyState : null;
          const afterSet = w ? w.__phaseTrackAfterSet : "no window";
          reject(new Error(
            f.src + ": #phase-track never gained items (element present: " + has +
              ", readyState: " + state + ", items right after store.set: " + afterSet + ")"
          ));
          return;
        }}
        requestAnimationFrame(poll);
      }};
      poll();
    }});
  }});
  // Two more paints once `#phase-track` itself is populated, for whatever
  // else `measure` reads that settles a frame later still (layout from a
  // stylesheet applied this same tick, for one).
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  return f;
}}
(async () => {{
  try {{
    const loaded = [];
    for (const frame of FRAMES) loaded.push(await loadFrame(frame));
    report(loaded.map((f, i) => measure(f, FRAMES[i])));
  }} catch (err) {{
    report({{ error: String((err && err.stack) || err) }});
  }}
}})();
</script></body></html>"""


class _Handler(SimpleHTTPRequestHandler):
    pages: dict[str, str] = {}
    reported: threading.Event
    result: bytes = b""

    def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
        page = self.pages.get(self.path.split("?", 1)[0])
        if page is None:
            super().do_GET()
            return
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - the http.server hook name
        if self.path != "/__result":
            self.send_error(404)
            return
        type(self).result = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(204)
        self.end_headers()
        self.reported.set()

    def log_message(self, *args: object) -> None:
        pass


def _collect(pages: dict[str, str], entry: str, profile: Path, within: float = 120.0) -> object:
    """Serve `pages` (and `ui/`), open `entry` in headless Chrome and return the
    JSON the page POSTs to `/__result`. Waits for that report in real time -
    no virtual time budget, which runs out before the page has reported when
    the machine is busy - and fails with a clear message if it never comes."""
    handler = type("_CollectHandler", (_Handler,), {"pages": pages, "reported": threading.Event()})
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(handler, directory=str(UI)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}{entry}"
    chrome = subprocess.Popen(
        [
            str(CHROME),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            "--window-size=2000,1200",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        reported = handler.reported.wait(within)
    finally:
        chrome.kill()
        chrome.wait()
        server.shutdown()
        server.server_close()
    if not reported:
        pytest.fail(f"{url} did not report a result within {within:g}s", pytrace=False)
    try:
        return json.loads(handler.result)
    except json.JSONDecodeError as err:
        pytest.fail(f"{url} reported something that is not JSON ({err}): {handler.result[:2000]!r}")


#: `measurements` is `scope="module"` - every test function below shares the
#: one collection, so a single flaky Chrome run used to fail all of them at
#: once (a cascade, not thirty independent flakes). The flake itself: a frame
#: occasionally reports fully loaded (`readyState: "complete"`, `#phase-track`
#: present, no script error caught, the marker this same script sets right
#: after its own `store.set({{}})` never reached) with `#phase-track` still
#: empty - reproduces roughly one run in five run back to back in total
#: isolation, and near-certainly when several copies of this file race each
#: other, but the exact mechanism resisted pinning down further (no thrown
#: exception, no rejected promise, the static markup present, only the mount
#: itself silently never reaching its own next line) - a headless-Chrome
#: iframe-navigation quirk is the leading suspect, not anything in this
#: repo's own JS. Since a bad run can apparently make EVERY attempt fail (the
#: three-parallel-copies stress test that found this flake in the first place
#: failed all three retries under the old, lower count), `measurements` also
#: waits a little between attempts rather than retrying back to back - giving
#: whatever transient condition this is room to actually clear.
_COLLECT_ATTEMPTS = 6


@pytest.fixture(scope="module")
def measurements(tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    """Every frame's measurements, collected once for the whole module -
    retried (see `_COLLECT_ATTEMPTS`) rather than letting one flaky Chrome
    run cascade into every test in this file failing together."""
    problems: list[str] = []
    for attempt in range(_COLLECT_ATTEMPTS):
        if attempt > 0:
            time.sleep(2.0)
        try:
            data = _collect(
                {
                    "/__viewport_game.html": _game_page(),
                    "/__viewport_harness.html": _harness_page(),
                },
                "/__viewport_harness.html",
                tmp_path_factory.mktemp(f"chrome-profile-{attempt}"),
            )
        except pytest.fail.Exception as err:
            problems.append(str(err))
            continue
        if isinstance(data, dict):
            problems.append(f"page reported an error: {data}")
            continue
        expected = len(VIEWPORTS) + 1 + len(PERMITTED) + 1 + 1
        if len(data) != expected:
            problems.append(f"expected {expected} frames, got {len(data)}")
            continue
        return data
    pytest.fail(
        f"measurements never collected cleanly in {_COLLECT_ATTEMPTS} attempts, each its "
        f"own Chrome profile and port (not shared state - a transient failure each time): "
        f"{problems}",
        pytrace=False,
    )


def test_a_result_reported_after_the_page_settles_is_still_collected(tmp_path: Path) -> None:
    """The harness reports only after every iframe has loaded and rendered. On
    a busy machine that can come later than any fixed time budget, and the
    fixture must still wait for the report rather than read a page that says
    "pending"."""
    late = """<!doctype html><html><body><pre id="result">pending</pre><script>
setTimeout(() => {
  const value = JSON.stringify({ late: true });
  document.getElementById("result").textContent = value;
  fetch("/__result", { method: "POST", body: value });
}, 6000);
</script></body></html>"""
    assert _collect({"/__late.html": late}, "/__late.html", tmp_path / "profile") == {"late": True}


def test_a_page_that_never_reports_fails_with_a_clear_message(tmp_path: Path) -> None:
    silent = '<!doctype html><html><body><pre id="result">pending</pre></body></html>'
    with pytest.raises(pytest.fail.Exception, match="did not report a result"):
        _collect({"/__silent.html": silent}, "/__silent.html", tmp_path / "profile", within=3)


EACH = pytest.mark.parametrize(
    "index", range(len(VIEWPORTS)), ids=[f"{w}x{h}" for w, h in VIEWPORTS]
)


@EACH
def test_the_frame_is_exactly_the_viewport_size(measurements: list[dict], index: int) -> None:
    m = measurements[index]
    assert m["viewport"] == list(m["size"])


@EACH
def test_the_page_never_scrolls(measurements: list[dict], index: int) -> None:
    m = measurements[index]
    width, height = m["size"]
    assert m["page"][0] <= width, f"page scrolls sideways at {width}x{height}: {m['page']}"
    assert m["page"][1] <= height, f"page scrolls vertically at {width}x{height}: {m['page']}"


def _inside(rect: dict, width: float, height: float) -> bool:
    return (
        rect["left"] >= -0.5
        and rect["top"] >= -0.5
        and rect["right"] <= width + 0.5
        and rect["bottom"] <= height + 0.5
    )


def _clip(rect: dict, bound: dict) -> dict | None:
    """The part of `rect` inside `bound`, or `None` when nothing of it is."""
    left, right = max(rect["left"], bound["left"]), min(rect["right"], bound["right"])
    top, bottom = max(rect["top"], bound["top"]), min(rect["bottom"], bound["bottom"])
    if right <= left or bottom <= top:
        return None
    return {"left": left, "right": right, "top": top, "bottom": bottom}


def _overlap(a: dict, b: dict) -> bool:
    overlap_x = min(a["right"], b["right"]) - max(a["left"], b["left"])
    overlap_y = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
    return overlap_x > 0.5 and overlap_y > 0.5


@EACH
def test_mobile_mode_is_exactly_the_narrow_windows(measurements: list[dict], index: int) -> None:
    m = measurements[index]
    width, _ = m["size"]
    assert m["mobile"] is (width <= 1100), m["size"]


@EACH
def test_every_region_stays_inside_the_viewport(measurements: list[dict], index: int) -> None:
    m = measurements[index]
    width, height = m["size"]
    if m["mobile"]:
        # The drawer is closed: the board is the whole screen and everything
        # else waits off it, behind the toggle.
        assert _inside(m["regions"]["#mtg-board"], width, height), m["regions"]["#mtg-board"]
        assert m["drawer"]["left"] >= width - 0.5, (m["size"], m["drawer"])
        assert m["drawerVisibility"] == "hidden"
        assert m["toggleDisplay"] != "none" and _inside(m["toggle"], width, height), m["toggle"]
        assert not _overlap(m["toggle"], m["regions"]["#mtg-board"]), (m["toggle"], m["size"])
        return
    for name, rect in m["regions"].items():
        assert _inside(rect, width, height), (name, rect, m["size"])
    assert m["regions"]["#side-panel"]["height"] >= 60, m["regions"]["#side-panel"]
    assert m["regions"]["#zone-ribbon"]["height"] >= 24, m["regions"]["#zone-ribbon"]
    assert m["toggleDisplay"] == "none"


@EACH
def test_the_zone_ribbon_shows_on_a_desktop_and_hides_in_mobile_mode(
    measurements: list[dict], index: int
) -> None:
    m = measurements[index]
    if m["mobile"]:
        assert m["zoneRibbonDisplay"] == "none", m["size"]
    else:
        assert m["zoneRibbonDisplay"] != "none", m["size"]


@EACH
def test_no_fine_pointer_window_shows_the_rotate_overlay(
    measurements: list[dict], index: int
) -> None:
    assert measurements[index]["rotateDisplay"] == "none", measurements[index]["size"]


@EACH
def test_the_page_cannot_be_scrolled_at_all(measurements: list[dict], index: int) -> None:
    assert measurements[index]["scrolled"] == [0, 0], measurements[index]["size"]


@EACH
def test_nothing_in_the_grid_sits_under_the_toolbar(measurements: list[dict], index: int) -> None:
    m = measurements[index]
    bar = m["toolbar"]
    width, height = m["size"]
    assert _inside(bar, width, height), (bar, m["size"])
    for name in ("#topbar", "#zone-ribbon", "#side-panel", "#mtg-board"):
        rect = m["regions"][name]
        if m["mobile"] and name != "#mtg-board":
            continue
        assert not _overlap(bar, rect), (name, rect, bar, m["size"])
    assert bar["bottom"] <= m["regions"]["#mtg-board"]["top"] + 0.5, (bar, m["size"])
    for card in m["boardCards"]:
        assert not _overlap(bar, card), (bar, card, m["size"])
    if m["mobile"]:
        assert not _overlap(bar, m["toggle"]), (bar, m["toggle"], m["size"])
        assert not _overlap(bar, m["railPass"]), (bar, m["railPass"], m["size"])


@EACH
def test_the_phase_track_sits_in_the_top_row_and_not_in_the_topbar(
    measurements: list[dict], index: int
) -> None:
    m = measurements[index]
    track, bar = m["phaseTrack"], m["toolbar"]
    assert track["display"] != "none", m["size"]
    assert track["inToolbar"] is True and track["inTopbar"] is False, m["size"]
    # T9: CR 510.4's first-strike combat damage step joined `priority.js`'s
    # `STEPS`, so this is 13 now, not 12.
    assert len(track["items"]) == 13, track["items"]
    assert track["box"]["width"] > 0, (track["box"], m["size"])
    assert track["box"]["left"] >= bar["left"] - 0.5 and track["box"]["right"] <= bar["right"] + 0.5
    assert track["box"]["top"] >= bar["top"] - 0.5 and track["box"]["bottom"] <= bar["bottom"] + 0.5
    # On the left of the row, the buttons on the right.
    for button in m["toolbarButtons"]:
        assert track["box"]["right"] <= button["left"] + 0.5, (track["box"], button, m["size"])


@EACH
def test_the_phase_track_stays_on_one_line(measurements: list[dict], index: int) -> None:
    m = measurements[index]
    track, bar = m["phaseTrack"], m["toolbar"]
    assert track["box"]["height"] <= bar["height"] + 0.5, (track["box"], bar, m["size"])
    tops = {round(item["top"]) for item in track["items"]}
    assert len(tops) == 1, (tops, m["size"])
    for item in track["items"]:
        assert item["bottom"] <= track["box"]["bottom"] + 0.5, (item, track["box"], m["size"])
    for button in m["toolbarButtons"]:
        assert button["top"] >= bar["top"] - 0.5 and button["bottom"] <= bar["bottom"] + 0.5, (
            button,
            bar,
            m["size"],
        )


@EACH
def test_the_phase_track_covers_no_button_card_toggle_or_rail_pass(
    measurements: list[dict], index: int
) -> None:
    m = measurements[index]
    track = m["phaseTrack"]["box"]
    for button in m["toolbarButtons"]:
        assert not _overlap(track, button), (track, button, m["size"])
    for card in m["boardCards"]:
        assert not _overlap(track, card), (track, card, m["size"])
    assert not _overlap(track, m["regions"]["#mtg-board"]), (track, m["size"])
    assert not _overlap(track, m["toggle"]), (track, m["toggle"], m["size"])
    assert not _overlap(track, m["railPass"]), (track, m["railPass"], m["size"])
    for button in m["railButtons"]:
        assert not _overlap(track, button), (track, button, m["size"])


@EACH
def test_the_current_step_is_highlighted_and_scrolled_into_view(
    measurements: list[dict], index: int
) -> None:
    m = measurements[index]
    track = m["phaseTrack"]
    current = track["current"]
    assert current is not None and current["width"] > 0, m["size"]
    assert current["left"] >= track["box"]["left"] - 0.5, (current, track["box"], m["size"])
    assert current["right"] <= track["box"]["right"] + 0.5, (current, track["box"], m["size"])


def test_on_a_phone_the_track_scrolls_inside_itself_rather_than_wrapping(
    measurements: list[dict],
) -> None:
    m = measurements[VIEWPORTS.index((667, 375))]
    track = m["phaseTrack"]
    assert track["scrollWidth"] > track["clientWidth"], track


@EACH
def test_the_top_row_spans_the_page(measurements: list[dict], index: int) -> None:
    m = measurements[index]
    width, _ = m["size"]
    bar = m["toolbar"]
    if m["mobile"]:
        # A thin strip across the whole viewport.
        assert bar["height"] <= 32, (bar, m["size"])
        assert bar["top"] <= 0.5 and bar["left"] <= 0.5 and bar["right"] >= width - 0.5, (
            bar,
            m["size"],
        )
        return
    assert bar["left"] <= 16 and bar["right"] >= width - 16, (bar, m["size"])


@EACH
def test_the_player_badges_sit_in_their_land_rows_and_cover_no_card(
    measurements: list[dict], index: int
) -> None:
    m = measurements[index]
    for side in ("opp", "my"):
        badge, row = m["badges"][side], m["badgeRows"][side]
        assert badge["width"] >= 40 and badge["height"] >= 16, (side, badge, m["size"])
        assert badge["top"] >= row["top"] - 0.5 and badge["bottom"] <= row["bottom"] + 0.5, (
            side,
            badge,
            row,
            m["size"],
        )
        assert badge["left"] >= row["left"] - 0.5, (side, badge, row)
        for card in m["boardCards"]:
            assert not _overlap(badge, card), (side, badge, card, m["size"])
        # Everything on the badge, zone counts included, is on show without
        # scrolling it.
        scroll_height, client_height = m["badgeFit"][side]
        assert scroll_height <= client_height + 1, (side, m["badgeFit"][side], m["size"])


@EACH
def test_the_seven_rows_fit_the_board_without_it_scrolling(
    measurements: list[dict], index: int
) -> None:
    m = measurements[index]
    board = m["board"]
    assert board["scrollHeight"] <= board["clientHeight"] + 1, (m["size"], board)
    rows = m["rows"]
    for upper, lower in zip(rows, rows[1:], strict=False):
        assert upper["box"]["bottom"] <= lower["box"]["top"] + 0.5, (
            m["size"],
            upper["id"],
            lower["id"],
        )
    for row in rows:
        assert row["box"]["height"] > 0, (m["size"], row["id"])


@EACH
def test_every_card_fits_inside_its_row(measurements: list[dict], index: int) -> None:
    m = measurements[index]
    assert m["stackCards"] == 2
    for row in m["rows"]:
        for rect in row["children"] + row["cards"]:
            assert rect["top"] >= row["inner"]["top"] - 0.5, (
                m["size"],
                row["id"],
                row["cardW"],
                rect,
                row["inner"],
            )
            assert rect["bottom"] <= row["inner"]["bottom"] + 0.5, (
                m["size"],
                row["id"],
                row["cardW"],
                rect,
                row["inner"],
            )


def _open_frame(measurements: list[dict]) -> dict:
    m = measurements[len(VIEWPORTS)]
    assert m["open"] is True and m["size"] == list(DRAWER_OPEN)
    return m


def test_at_844x390_the_closed_drawer_leaves_the_whole_board_in_mobile_mode(
    measurements: list[dict],
) -> None:
    m = measurements[VIEWPORTS.index((844, 390))]
    assert m["mobile"] is True
    assert m["scrolled"] == [0, 0] and m["page"] == [844, 390]
    assert m["zoneRibbonDisplay"] == "none"
    assert m["drawerVisibility"] == "hidden"
    # The board spans the screen but for the gutter and the toggle rail.
    assert m["regions"]["#mtg-board"]["width"] >= 844 * 0.85, m["regions"]["#mtg-board"]


@EACH
def test_the_rail_pass_is_on_screen_in_mobile_mode_and_covers_nothing(
    measurements: list[dict], index: int
) -> None:
    m = measurements[index]
    width, height = m["size"]
    if not m["mobile"]:
        assert m["railPassDisplay"] == "none", m["size"]
        return
    assert m["railPassDisplay"] not in (None, "none"), m["size"]
    assert [b["text"] for b in m["railButtons"]] == ["Pass", "Pass until"], m["railButtons"]
    for button in m["railButtons"]:
        assert button["width"] >= 40 and button["height"] >= 28, (button, m["size"])
        assert _inside(button, width, height), (button, m["size"])
        assert not _overlap(button, m["toggle"]), (button, m["toggle"], m["size"])
        assert not _overlap(button, m["toolbar"]), (button, m["toolbar"], m["size"])
        for bar_button in m["toolbarButtons"]:
            assert not _overlap(button, bar_button), (button, bar_button, m["size"])
        assert not _overlap(button, m["regions"]["#mtg-board"]), (button, m["size"])
        # A card past a sideways-scrolling row's edge is clipped by the board
        # (`#mtg-board` is `overflow-x: hidden`), so only its visible part counts.
        for card in m["boardCards"]:
            visible = _clip(card, m["board"]["box"])
            assert visible is None or not _overlap(button, visible), (button, card, m["size"])


def test_at_844x390_with_priority_held_pass_is_on_the_rail_and_the_drawer_stays_closed(
    measurements: list[dict],
) -> None:
    m = measurements[VIEWPORTS.index((844, 390))]
    assert m["mobile"] is True
    assert m["scrolled"] == [0, 0] and m["page"] == [844, 390]
    assert m["drawerVisibility"] == "hidden"
    assert m["railPassDisplay"] not in (None, "none")
    texts = [b["text"] for b in m["railButtons"]]
    assert "Pass" in texts and "Pass until" in texts, texts
    board = m["board"]
    assert board["scrollHeight"] <= board["clientHeight"] + 1, board


def test_at_1920x1080_the_desktop_keeps_the_zone_ribbon_and_no_overlay(
    measurements: list[dict],
) -> None:
    m = measurements[VIEWPORTS.index((1920, 1080))]
    assert m["mobile"] is False
    assert m["zoneRibbonDisplay"] != "none" and m["regions"]["#zone-ribbon"]["height"] >= 24
    assert m["rotateDisplay"] == "none"
    assert m["toggleDisplay"] == "none"
    assert m["railPassDisplay"] == "none"


def test_an_open_drawer_is_on_screen_and_keeps_the_toggle_and_toolbar_reachable(
    measurements: list[dict],
) -> None:
    m = _open_frame(measurements)
    width, height = m["size"]
    assert m["scrolled"] == [0, 0]
    assert m["drawerVisibility"] == "visible"
    assert _inside(m["drawer"], width, height), m["drawer"]
    assert m["drawer"]["width"] >= 200, m["drawer"]
    for name in ("#topbar", "#side-panel"):
        rect = m["regions"][name]
        assert (
            rect["left"] >= m["drawer"]["left"] - 0.5
            and rect["right"] <= m["drawer"]["right"] + 0.5
        )
        assert _inside(rect, width, height), (name, rect)
    assert m["regions"]["#side-panel"]["height"] >= 60, m["regions"]["#side-panel"]
    assert _inside(m["toggle"], width, height) and not _overlap(m["toggle"], m["drawer"])
    for button in m["toolbarButtons"] + m["railButtons"]:
        assert _inside(button, width, height), button
        assert not _overlap(button, m["drawer"]), (button, m["drawer"])
    assert not _overlap(m["phaseTrack"]["box"], m["drawer"]), (m["phaseTrack"], m["drawer"])
    assert m["phaseTrack"]["inToolbar"] is True and m["phaseTrack"]["inTopbar"] is False


@EACH
def test_an_empty_permitted_row_takes_no_board_height(measurements: list[dict], index: int) -> None:
    """No permission, no row: it is not one of the seven, so it collapses to
    nothing rather than taking height from them or painting over a row."""
    m = measurements[index]
    assert m["permitted"] is False
    assert m["permittedRow"]["display"] == "none", m["size"]
    assert m["permittedRow"]["box"]["height"] == 0, (m["size"], m["permittedRow"]["box"])


PERMITTED_EACH = pytest.mark.parametrize(
    "size", PERMITTED, ids=[f"permitted-{w}x{h}" for w, h in PERMITTED]
)


def _permitted_frame(measurements: list[dict], size: tuple[int, int]) -> dict:
    m = measurements[len(VIEWPORTS) + 1 + PERMITTED.index(size)]
    assert m["permitted"] is True and m["size"] == list(size), (m["size"], size)
    return m


@PERMITTED_EACH
def test_permitted_cards_never_scroll_the_page_or_the_board(
    measurements: list[dict], size: tuple[int, int]
) -> None:
    m = _permitted_frame(measurements, size)
    width, height = size
    assert m["scrolled"] == [0, 0], size
    assert m["page"][0] <= width and m["page"][1] <= height, (size, m["page"])
    board = m["board"]
    assert board["scrollHeight"] <= board["clientHeight"] + 1, (size, board)
    assert _inside(m["regions"]["#mtg-board"], width, height), m["regions"]["#mtg-board"]


@PERMITTED_EACH
def test_permitted_cards_sit_inside_their_row_below_the_seven(
    measurements: list[dict], size: tuple[int, int]
) -> None:
    m = _permitted_frame(measurements, size)
    row = m["permittedRow"]
    assert len(row["cards"]) == PERMITTED_CARDS, row
    assert row["box"]["height"] > 0, row["box"]
    assert m["rows"][-1]["box"]["bottom"] <= row["box"]["top"] + 0.5, (m["rows"][-1], row["box"])
    assert row["scrollWidth"] <= row["clientWidth"] + 1, (size, row)
    for card in row["cards"]:
        assert card["top"] >= row["inner"]["top"] - 0.5, (size, card, row["inner"])
        assert card["bottom"] <= row["inner"]["bottom"] + 0.5, (size, card, row["inner"])
        assert card["left"] >= row["inner"]["left"] - 0.5, (size, card, row["inner"])
        assert card["right"] <= row["inner"]["right"] + 0.5, (size, card, row["inner"])
    for seven in m["rows"]:
        assert seven["box"]["height"] > 0, (size, seven["id"])
        for rect in seven["children"] + seven["cards"]:
            assert rect["bottom"] <= seven["inner"]["bottom"] + 0.5, (size, seven["id"], rect)


@PERMITTED_EACH
def test_no_two_cards_overlap_with_permitted_cards_on_the_board(
    measurements: list[dict], size: tuple[int, int]
) -> None:
    m = _permitted_frame(measurements, size)
    cards = m["boardCards"]
    assert len(cards) >= PERMITTED_CARDS + 30, len(cards)
    for i, a in enumerate(cards):
        for b in cards[i + 1 :]:
            assert not _overlap(a, b), (size, a, b)


@PERMITTED_EACH
def test_permitted_cards_are_as_wide_as_the_fitted_card_w(
    measurements: list[dict], size: tuple[int, int]
) -> None:
    m = _permitted_frame(measurements, size)
    row = m["permittedRow"]
    assert row["cardW"].endswith("px"), row["cardW"]
    card_w = float(row["cardW"][:-2])
    for classes in row["classes"]:
        assert "card-fit" in classes.split() and "card-md" not in classes.split(), classes
    for card in row["cards"]:
        assert abs(card["width"] - card_w) <= 0.5, (size, card, row["cardW"])
        assert abs(card["width"] - 108) > 0.5, (size, card)


#: The frames the advisor panel's fill is pinned at: the two desktop sizes
#: named for it, and the 844x390 phone with the drawer open.
ADVICE_FILL = [(1920, 1080), (1280, 720)]
ADVICE_FILL_EACH = pytest.mark.parametrize(
    "frame",
    [*[f"{w}x{h}" for w, h in ADVICE_FILL], "drawer-open-844x390"],
)


def _advice_frame(measurements: list[dict], frame: str) -> dict:
    if frame.startswith("drawer-open"):
        return _open_frame(measurements)
    size = next(s for s in ADVICE_FILL if f"{s[0]}x{s[1]}" == frame)
    return measurements[VIEWPORTS.index(size)]


def _stacked_inside(panel: dict) -> None:
    """The side panel's visible children sit inside it, one under the next,
    with no two overlapping."""
    children = panel["children"]
    for child in children:
        assert child["top"] >= panel["box"]["top"] - 0.5, (child, panel["box"])
        assert child["bottom"] <= panel["box"]["bottom"] + 0.5, (child, panel["box"])
    for upper, lower in zip(children, children[1:], strict=False):
        assert upper["bottom"] <= lower["top"] + 0.5, (upper, lower)


@ADVICE_FILL_EACH
def test_the_advice_panel_fills_the_rest_of_the_side_panel(
    measurements: list[dict], frame: str
) -> None:
    """No game log panel under it any more: `#advice-box` is the side panel's
    last child and reaches down to the side panel's own bottom edge, on a
    desktop and inside the open mobile drawer alike. A long rationale scrolls
    inside it, so the side panel itself never has to scroll."""
    m = _advice_frame(measurements, frame)
    width, height = m["size"]
    panel, advice = m["sidePanel"], m["advice"]
    ids = [child["id"] for child in panel["children"]]
    assert "log-box" not in ids, ids
    assert ids[-1] == "advice-box", ids
    assert _inside(panel["box"], width, height), (panel["box"], m["size"])
    assert advice["box"]["height"] >= 40, (advice["box"], m["size"])
    assert abs(advice["box"]["bottom"] - panel["inner"]["bottom"]) <= 1, (
        advice["box"],
        panel["inner"],
        m["size"],
    )
    assert advice["overflowY"] == "auto", advice
    assert advice["scrollHeight"] > advice["clientHeight"], advice
    assert panel["scrollHeight"] <= panel["clientHeight"] + 1, (panel, m["size"])
    _stacked_inside(panel)
    assert not _overlap(panel["box"], m["toolbar"]), (panel["box"], m["toolbar"])
    assert not _overlap(panel["box"], m["regions"]["#mtg-board"]) or m["open"], panel["box"]


@EACH
def test_a_hidden_advice_panel_breaks_nothing(measurements: list[dict], index: int) -> None:
    """With no proposal the advisor panel is `hidden`: the page still cannot
    scroll, and the side panel keeps its place with its remaining children
    stacked inside it."""
    m = measurements[index]
    width, height = m["size"]
    hidden = m["adviceHidden"]
    assert hidden["scrolled"] == [0, 0], m["size"]
    assert hidden["page"][0] <= width and hidden["page"][1] <= height, (hidden["page"], m["size"])
    ids = [child["id"] for child in hidden["sidePanel"]["children"]]
    assert "advice-box" not in ids, ids
    if m["mobile"]:
        return
    assert hidden["sidePanel"]["box"] == m["sidePanel"]["box"], (hidden["sidePanel"], m["size"])
    _stacked_inside(hidden["sidePanel"])


def test_a_hidden_advice_panel_breaks_nothing_in_the_open_drawer(
    measurements: list[dict],
) -> None:
    m = _open_frame(measurements)
    width, height = m["size"]
    hidden = m["adviceHidden"]
    assert hidden["scrolled"] == [0, 0]
    assert hidden["page"] == [width, height], hidden["page"]
    assert _inside(hidden["sidePanel"]["box"], width, height), hidden["sidePanel"]["box"]
    _stacked_inside(hidden["sidePanel"])


@EACH
def test_the_fullscreen_button_leads_the_toolbar_on_screen_and_covers_nothing(
    measurements: list[dict], index: int
) -> None:
    m = measurements[index]
    width, height = m["size"]
    button = m["fullscreen"]
    assert m["fullscreenFirst"] is True, m["size"]
    assert m["fullscreenDisplay"] not in (None, "none"), m["size"]
    assert button["width"] >= 20 and button["height"] >= 20, (button, m["size"])
    assert _inside(button, width, height), (button, m["size"])
    assert not _overlap(button, m["regions"]["#mtg-board"]), (button, m["size"])
    for other in m["railButtons"] + ([m["toggle"]] if m["mobile"] else []):
        assert not _overlap(button, other), (button, other, m["size"])


def _safe_frame(measurements: list[dict]) -> dict:
    m = measurements[-2]
    assert m["safe"] is True and m["size"] == list(SAFE_AREA), m["size"]
    return m


def test_nothing_a_player_touches_sits_in_the_safe_area_insets(measurements: list[dict]) -> None:
    """The notch and the home indicator never cover a card, the top row's phase
    track or buttons, the rail's Pass or the drawer toggle, and the page still
    does not scroll. The top row stays a thin strip across the whole width with
    the board starting under it."""
    m = _safe_frame(measurements)
    width, height = m["size"]
    assert m["scrolled"] == [0, 0] and m["page"] == [width, height], (m["scrolled"], m["page"])
    board = m["board"]
    assert board["scrollHeight"] <= board["clientHeight"] + 1, board
    safe = {
        "left": SAFE_INSETS["left"],
        "top": SAFE_INSETS["top"],
        "right": width - SAFE_INSETS["right"],
        "bottom": height - SAFE_INSETS["bottom"],
    }

    def within(rect: dict) -> bool:
        return (
            rect["left"] >= safe["left"] - 0.5
            and rect["top"] >= safe["top"] - 0.5
            and rect["right"] <= safe["right"] + 0.5
            and rect["bottom"] <= safe["bottom"] + 0.5
        )

    assert within(m["regions"]["#mtg-board"]), (m["regions"]["#mtg-board"], safe)
    for card in m["boardCards"]:
        visible = _clip(card, board["box"])
        assert visible is None or within(visible), (card, safe)
    # The top row is a full-width strip whose background reaches the edges;
    # what it holds - the phase track and every button - stays clear.
    controls = m["toolbarButtons"] + m["railButtons"] + [m["phaseTrack"]["box"], m["toggle"]]
    assert m["railButtons"] and m["toolbarButtons"], m
    for control in controls:
        assert within(control), (control, safe)
    bar = m["toolbar"]
    assert bar["height"] <= 32 and bar["left"] <= 0.5 and bar["right"] >= width - 0.5, bar
    board_box = m["regions"]["#mtg-board"]
    assert bar["bottom"] <= board_box["top"] + 0.5, (bar, board_box)
    for bar_button in m["toolbarButtons"]:
        for rail_button in m["railButtons"] + [m["toggle"]]:
            assert not _overlap(bar_button, rail_button), (bar_button, rail_button)


def test_a_decision_window_remembered_at_the_top_keeps_its_title_bar_below_the_row(
    measurements: list[dict],
) -> None:
    """At 844x390 a saved spot at top 0, mounted again as a reload does, still
    leaves the whole title bar - the window's only drag handle - below the
    top row that paints over it, and on screen."""
    m = measurements[-1]
    assert m["decision"] is True and m["size"] == list(DECISION_TOP), m["size"]
    width, height = m["size"]
    handle, bar = m["decisionHandle"], m["toolbar"]
    assert m["decisionWindowHidden"] is False, m
    assert handle["height"] > 0, handle
    assert not _overlap(handle, bar), (handle, bar)
    assert handle["top"] >= bar["bottom"] - 0.5, (handle, bar)
    assert _inside(handle, width, height), (handle, m["size"])
