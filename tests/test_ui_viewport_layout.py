"""The play page in a real browser, at real window sizes - phones included.

`#game` must fill the viewport with no page-level scroll at every width, not
only on a desktop: a phone in landscape (844x390) and in portrait, a small
tablet, a laptop. The seven board rows must fit the board's height without it
scrolling, every card must fit inside its row, and the zone ribbon, the side
panel and the turn block must stay on screen and out from under the fixed
toolbar.

CSS layout cannot be checked from a regex, so this serves `ui/` over HTTP,
mounts the real `render/board.js` on the real `#game` markup and `style.css`
with a crowded board, and loads that page into iframes of exact sizes inside
one headless Chrome (an iframe's viewport is exactly its box, which a
headless window's is not). The harness page measures every frame and writes
the numbers into the DOM, which `--dump-dom` hands back.

Skipped when no Chrome/Chromium is installed.
"""

from __future__ import annotations

import functools
import html
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
    (1280, 480),
    (1440, 900),
    (1920, 1080),
]

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
    return state


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
const state = {json.dumps(_crowded_state())};
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
$("#turn-info").textContent = "Turn 7 - alice, precombat main - priority: alice";
const track = $("#phase-track");
const steps = ["untap", "upkeep", "draw", "main 1", "begin combat", "attackers", "blockers",
  "damage", "end combat", "main 2", "end", "cleanup"];
for (const step of steps) {{
  const li = document.createElement("li"); li.textContent = step; track.appendChild(li);
}}
$("#conn-status-game").textContent = "connected";
$("#concede-slot").innerHTML = "<button>Concede - click again to confirm</button>";
$("#prompt-box").textContent = "You have priority in your precombat main phase.";
$("#priority-box").innerHTML = "<button>Pass priority</button> <button>Resolve top</button>";
$("#stops-box").textContent = "Stops: upkeep, draw, attackers, blockers, end";
$("#advice-box").textContent = "Buddy proposes attacking with every untapped bear. ".repeat(30);
const lines = Array.from({{ length: 80 }}, (_, i) => `<div>log line ${{i}}</div>`);
$("#log-box").innerHTML = lines.join("");
store.set({{}});
</script>
</body></html>"""


def _harness_page() -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8" /></head><body style="margin:0">
<pre id="result">pending</pre>
<div id="frames"></div>
<script>
const SIZES = {json.dumps(VIEWPORTS)};
const ROWS = {json.dumps(SEVEN_ROWS)};
const REGIONS = ["#topbar", "#banners", "#zone-ribbon", "#side-panel", "#mtg-board"];
const report = (value) => {{
  document.getElementById("result").textContent = JSON.stringify(value);
}};
const box = (el) => {{
  if (!el) return null;
  const {{ left, top, right, bottom, width, height }} = el.getBoundingClientRect();
  return {{ left, top, right, bottom, width, height }};
}};
function measure(frame, size) {{
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
  return {{
    size,
    viewport: [w.innerWidth, w.innerHeight],
    page: [d.scrollingElement.scrollWidth, d.scrollingElement.scrollHeight],
    board: {{
      box: box(board), scrollHeight: board.scrollHeight, clientHeight: board.clientHeight,
    }},
    rows,
    regions: Object.fromEntries(REGIONS.map((s) => [s, box($(s))])),
    toolbar: box($("#game-toolbar")),
    stackCards: $("#stack-row").querySelectorAll(".card").length,
  }};
}}
const frames = SIZES.map(([width, height]) => {{
  const f = document.createElement("iframe");
  f.style.cssText = `width:${{width}}px;height:${{height}}px;border:0;display:block`;
  f.src = "/__viewport_game.html";
  document.getElementById("frames").appendChild(f);
  return new Promise((resolve) => f.addEventListener("load", () => resolve(f)));
}});
Promise.all(frames).then((loaded) => {{
  requestAnimationFrame(() => requestAnimationFrame(() => {{
    try {{
      report(loaded.map((f, i) => measure(f, SIZES[i])));
    }} catch (err) {{
      report({{ error: String((err && err.stack) || err) }});
    }}
  }}));
}});
</script></body></html>"""


class _Handler(SimpleHTTPRequestHandler):
    pages: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
        page = self.pages.get(self.path)
        if page is None:
            super().do_GET()
            return
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _dump_dom(command: list[str], within: float = 120.0) -> str:
    """Chrome's stdout once `--dump-dom` has written the page. Headless Chrome
    prints the DOM when the virtual time budget runs out but does not always
    exit afterwards, so the output is read as it arrives and Chrome is killed
    once the whole document is in."""
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    chunks: list[str] = []
    pump = threading.Thread(
        target=lambda: chunks.extend(iter(proc.stdout.readline, "")), daemon=True
    )
    pump.start()
    deadline = time.monotonic() + within
    try:
        while time.monotonic() < deadline and proc.poll() is None:
            if "</html>" in "".join(chunks):
                break
            time.sleep(0.1)
    finally:
        proc.kill()
        proc.wait()
        pump.join(timeout=5)
    return "".join(chunks)


@pytest.fixture(scope="module")
def measurements(tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    _Handler.pages = {
        "/__viewport_game.html": _game_page(),
        "/__viewport_harness.html": _harness_page(),
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=str(UI)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output = _dump_dom(
            [
                str(CHROME),
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={tmp_path_factory.mktemp('chrome-profile')}",
                "--window-size=2000,1200",
                "--virtual-time-budget=5000",
                "--dump-dom",
                f"http://127.0.0.1:{server.server_address[1]}/__viewport_harness.html",
            ]
        )
    finally:
        server.shutdown()
    found = re.search(r'<pre id="result">(.*?)</pre>', output, re.S)
    assert found, f"no result from Chrome: {output[-2000:]}"
    data = json.loads(html.unescape(found.group(1)))
    assert not isinstance(data, dict), data
    return data


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


@EACH
def test_every_region_stays_inside_the_viewport(measurements: list[dict], index: int) -> None:
    m = measurements[index]
    width, height = m["size"]
    for name, rect in m["regions"].items():
        assert rect["left"] >= -0.5 and rect["top"] >= -0.5, (name, rect)
        assert rect["right"] <= width + 0.5 and rect["bottom"] <= height + 0.5, (
            name,
            rect,
            m["size"],
        )
    assert m["regions"]["#side-panel"]["height"] >= 60, m["regions"]["#side-panel"]
    assert m["regions"]["#zone-ribbon"]["height"] >= 24, m["regions"]["#zone-ribbon"]


@EACH
def test_nothing_in_the_grid_sits_under_the_toolbar(measurements: list[dict], index: int) -> None:
    m = measurements[index]
    bar = m["toolbar"]
    for name in ("#topbar", "#zone-ribbon", "#side-panel", "#mtg-board"):
        rect = m["regions"][name]
        overlap_x = min(bar["right"], rect["right"]) - max(bar["left"], rect["left"])
        overlap_y = min(bar["bottom"], rect["bottom"]) - max(bar["top"], rect["top"])
        assert overlap_x <= 0.5 or overlap_y <= 0.5, (name, rect, bar, m["size"])


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
