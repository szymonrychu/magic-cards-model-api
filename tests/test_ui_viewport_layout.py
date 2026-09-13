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
with the drawer open. No frame ever shows the rotate overlay, which is only
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
    (1280, 720),
    (1280, 480),
    (1440, 900),
    (1920, 1080),
]

#: The one extra frame measured with the drawer open.
DRAWER_OPEN = (844, 390)

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
if (location.search.includes("drawer=open")) {{
  // Measured at rest, not part way through the slide-in.
  const still = document.createElement("style");
  still.textContent = "#drawer {{ transition: none !important; }}";
  document.head.appendChild(still);
  $("#game").classList.add("drawer-open");
}}
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
const FRAMES = [...SIZES.map((size) => ({{ size, open: false }})),
  {{ size: {json.dumps(list(DRAWER_OPEN))}, open: true }}];
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
function measure(frame, {{ size, open }}) {{
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
  w.scrollTo(100000, 100000);
  const scrolled = [w.scrollX, w.scrollY];
  const display = (sel) => ($(sel) ? w.getComputedStyle($(sel)).display : null);
  return {{
    size,
    open,
    mobile: w.matchMedia(MOBILE).matches,
    scrolled,
    viewport: [w.innerWidth, w.innerHeight],
    page: [d.scrollingElement.scrollWidth, d.scrollingElement.scrollHeight],
    board: {{
      box: box(board), scrollHeight: board.scrollHeight, clientHeight: board.clientHeight,
    }},
    rows,
    regions: Object.fromEntries(REGIONS.map((s) => [s, box($(s))])),
    toolbar: box($("#game-toolbar")),
    toolbarButtons: [...$("#game-toolbar").querySelectorAll("button")].map(box),
    zoneRibbonDisplay: display("#zone-ribbon"),
    rotateDisplay: display("#rotate-overlay"),
    drawer: box($("#drawer")),
    drawerVisibility: w.getComputedStyle($("#drawer")).visibility,
    toggle: box($("#drawer-toggle")),
    toggleDisplay: display("#drawer-toggle"),
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
const frames = FRAMES.map(({{ size: [width, height], open }}) => {{
  const f = document.createElement("iframe");
  f.style.cssText = `width:${{width}}px;height:${{height}}px;border:0;display:block`;
  f.src = "/__viewport_game.html" + (open ? "?drawer=open" : "");
  document.getElementById("frames").appendChild(f);
  return new Promise((resolve) => f.addEventListener("load", () => resolve(f)));
}});
Promise.all(frames).then((loaded) => {{
  requestAnimationFrame(() => requestAnimationFrame(() => {{
    try {{
      report(loaded.map((f, i) => measure(f, FRAMES[i])));
    }} catch (err) {{
      report({{ error: String((err && err.stack) || err) }});
    }}
  }}));
}});
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


@pytest.fixture(scope="module")
def measurements(tmp_path_factory: pytest.TempPathFactory) -> list[dict]:
    data = _collect(
        {"/__viewport_game.html": _game_page(), "/__viewport_harness.html": _harness_page()},
        "/__viewport_harness.html",
        tmp_path_factory.mktemp("chrome-profile"),
    )
    assert not isinstance(data, dict), data
    assert len(data) == len(VIEWPORTS) + 1, len(data)
    return data


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
    if m["mobile"]:
        assert not _overlap(bar, m["toggle"]), (bar, m["toggle"], m["size"])


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


def test_at_1920x1080_the_desktop_keeps_the_zone_ribbon_and_no_overlay(
    measurements: list[dict],
) -> None:
    m = measurements[VIEWPORTS.index((1920, 1080))]
    assert m["mobile"] is False
    assert m["zoneRibbonDisplay"] != "none" and m["regions"]["#zone-ribbon"]["height"] >= 24
    assert m["rotateDisplay"] == "none"
    assert m["toggleDisplay"] == "none"


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
    for button in m["toolbarButtons"]:
        assert _inside(button, width, height), button
        assert not _overlap(button, m["drawer"]), (button, m["drawer"])
