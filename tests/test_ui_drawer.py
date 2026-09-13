"""`ui/drawer.js` and the mobile shell around it.

In mobile mode - a coarse pointer in landscape, or any window 1100px wide or
less - the board takes the whole width, the zone ribbon is hidden, and the
turn block, the banners and the side panel live in a drawer that slides in
from the right over the board. A fixed tab toggles it. The drawer opens by
itself when something asks this seat for an answer (a decision, the pregame,
a cast being announced, priority), and once the player closes it, it stays
closed until a different question comes.

A coarse pointer held upright gets a full-screen "rotate your device"
overlay instead of a board.

The DOM half runs the real module under Node against
`tests/test_ui_preview.py`'s fake DOM; the CSS and markup halves read
`ui/style.css` and `ui/index.html`. `tests/test_ui_viewport_layout.py` checks
the geometry in a real browser.
"""

from __future__ import annotations

import re

from test_ui_preview import FAKE_DOM
from ui_node import UI, requires_node, run_module

MOBILE_QUERY = "@media (max-width: 1100px), (pointer: coarse) and (orientation: landscape)"
PORTRAIT_QUERY = "@media (pointer: coarse) and (orientation: portrait)"

SETUP = """
const GAME_EL = box("game");
const DRAWER = box("drawer", GAME_EL);
const TOGGLE = document.createElement("button");
TOGGLE.setAttribute("id", "drawer-toggle");
GAME_EL.appendChild(TOGGLE);

const seat = (username) => ({
  username, life: 20, hand_count: 0, library_count: 40, mana_pool: {},
});
const BASE = (extra) => ({
  you: "alice", active_player: "bob", priority_holder: "bob", step: "upkeep", turn_number: 3,
  players: [seat("alice"), seat("bob")], stack: [], battlefield: [], hands: { alice: [] },
  pending_decision: null, pregame: null, winner: null, drawn: false,
  ...(extra || {}),
});
const decision = (seq, player, kind) => ({
  kind: kind || "yes_no", player: player || "alice", seq, generation: seq, cards: [],
  source_card_id: null, prompt: "Pay {1}?",
});
const app = (state, extra) => ({ state, action: null, run: null, ...(extra || {}) });
const isOpen = () => GAME_EL.classList.contains("drawer-open");
const expanded = () => TOGGLE.getAttribute("aria-expanded");

async function mountWith(state) {
  const S = await import(STORE_URL);
  const store = S.createStore(app(state));
  const drawer = m.mountDrawer(document, store);
  store.set({});
  return { store, drawer };
}
"""


def run(expression: str):
    return run_module(
        "drawer.js", FAKE_DOM + SETUP + expression, STORE_URL=(UI / "store.js").as_uri()
    )


# ------------------------------------------------------- what asks for it --


@requires_node
def test_prompt_key_names_each_question_this_seat_must_answer() -> None:
    result = run(
        """
        ({
          noState: m.promptKey({ state: null }),
          idle: m.promptKey(app(BASE())),
          mine: m.promptKey(app(BASE({ pending_decision: decision(4) }))),
          sameAgain: m.promptKey(app(BASE({ pending_decision: decision(4) }))) ===
            m.promptKey(app(BASE({ pending_decision: decision(4) }))),
          nextOne: m.promptKey(app(BASE({ pending_decision: decision(5) }))) !==
            m.promptKey(app(BASE({ pending_decision: decision(4) }))),
          theirs: m.promptKey(app(BASE({ pending_decision: decision(4, "bob") }))),
          scry: m.promptKey(app(BASE({ pending_decision: decision(6, "alice", "scry") }))),
          priority: m.promptKey(app(BASE({ priority_holder: "alice" }))),
          prioritySameStep: m.promptKey(app(BASE({ priority_holder: "alice" }))) ===
            m.promptKey(app(BASE({ priority_holder: "alice" }), { action: null })),
          priorityNewStack:
            m.promptKey(app(BASE({ priority_holder: "alice", stack: [{ id: "s1" }] }))) !==
            m.promptKey(app(BASE({ priority_holder: "alice" }))),
          running: m.promptKey(
            app(BASE({ priority_holder: "alice" }), { run: { verb: "pass_until" } })
          ),
          casting: m.promptKey(app(BASE({ priority_holder: "alice" }), {
            action: { kind: "cast", cardId: "h1", abilityIndex: null },
          })),
          over: m.promptKey(app(BASE({ priority_holder: "alice", winner: "bob" }))),
          openingHand: m.promptKey(app(BASE({
            pregame: {
              step: "opening_hand", opening_hand_actor: "alice", opening_hand_options: {},
            },
          }))),
          openingHandTheirs: m.promptKey(app(BASE({
            pregame: { step: "opening_hand", opening_hand_actor: "bob", opening_hand_options: {} },
          }))),
          mulligan: m.promptKey(app(BASE({
            pregame: { step: "mulligan", awaiting_mulligan: ["alice"], mulligans_taken: {} },
          }))),
        })
        """
    )
    assert result["noState"] is None
    assert result["idle"] is None
    assert isinstance(result["mine"], str)
    assert result["sameAgain"] is True and result["nextOne"] is True
    assert result["theirs"] is None
    assert isinstance(result["scry"], str)
    assert isinstance(result["priority"], str)
    assert result["prioritySameStep"] is True and result["priorityNewStack"] is True
    assert result["running"] is None
    assert isinstance(result["casting"], str) and result["casting"] != result["priority"]
    assert result["over"] is None
    assert isinstance(result["openingHand"], str)
    assert result["openingHandTheirs"] is None
    assert isinstance(result["mulligan"], str)


# ------------------------------------------------------- open and close --


@requires_node
def test_the_toggle_opens_and_closes_the_drawer() -> None:
    result = run(
        """
        (async () => {
          const { drawer } = await mountWith(BASE());
          const start = { open: isOpen(), expanded: expanded(), api: drawer.isOpen() };
          dispatch(TOGGLE, "click");
          const opened = { open: isOpen(), expanded: expanded(), api: drawer.isOpen() };
          dispatch(TOGGLE, "click");
          const closed = { open: isOpen(), expanded: expanded() };
          drawer.open();
          const byApi = isOpen();
          drawer.close();
          return { start, opened, closed, byApi, afterApiClose: isOpen() };
        })()
        """
    )
    assert result["start"] == {"open": False, "expanded": "false", "api": False}
    assert result["opened"] == {"open": True, "expanded": "true", "api": True}
    assert result["closed"] == {"open": False, "expanded": "false"}
    assert result["byApi"] is True and result["afterApiClose"] is False


@requires_node
def test_a_new_prompt_opens_the_drawer_once_and_a_closed_drawer_stays_closed() -> None:
    result = run(
        """
        (async () => {
          const { store } = await mountWith(BASE());
          const idle = isOpen();
          store.set({ state: BASE({ pending_decision: decision(4) }) });
          const asked = isOpen();
          dispatch(TOGGLE, "click");
          // The next polls carry the same question: a fresh state object each
          // time, and the player already closed the drawer on it.
          store.set({ state: BASE({ pending_decision: decision(4) }) });
          store.set({ state: BASE({ pending_decision: decision(4) }) });
          const samePrompt = isOpen();
          store.set({ state: BASE({ pending_decision: decision(5) }) });
          const nextPrompt = isOpen();
          dispatch(TOGGLE, "click");
          store.set({ state: BASE() });
          const answered = isOpen();
          store.set({ state: BASE({ priority_holder: "alice", step: "draw" }) });
          const priority = isOpen();
          dispatch(TOGGLE, "click");
          store.set({ state: BASE({ pending_decision: decision(6, "bob") }) });
          const theirs = isOpen();
          return { idle, asked, samePrompt, nextPrompt, answered, priority, theirs };
        })()
        """
    )
    assert result == {
        "idle": False,
        "asked": True,
        "samePrompt": False,
        "nextPrompt": True,
        "answered": False,
        "priority": True,
        "theirs": False,
    }


@requires_node
def test_a_prompt_already_standing_at_mount_opens_the_drawer() -> None:
    result = run(
        """
        (async () => {
          await mountWith(BASE({ pending_decision: decision(9) }));
          return isOpen();
        })()
        """
    )
    assert result is True


@requires_node
def test_app_js_mounts_the_drawer() -> None:
    app = (UI / "app.js").read_text()
    assert 'from "./drawer.js"' in app
    assert "mountDrawer(document, store)" in app


# ------------------------------------------------------------ the markup --


def _index() -> str:
    return (UI / "index.html").read_text()


def _css() -> str:
    return (UI / "style.css").read_text()


def _media_block(css: str, query: str) -> str:
    start = css.find(query + " {")
    assert start >= 0, f"no `{query}` block in ui/style.css"
    depth = 0
    for i in range(css.index("{", start), len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start:i]
    raise AssertionError(f"unterminated `{query}` block")


def _rule(css: str, selector: str) -> str:
    found = re.search(r"(?:^|\n)\s*" + re.escape(selector) + r" \{([^}]*)\}", css)
    assert found, f"no `{selector}` rule"
    return found.group(1)


def test_the_drawer_wraps_the_turn_block_banners_and_side_panel() -> None:
    html = _index()
    drawer = re.search(r'<div id="drawer" class="drawer">(.*?)\n  </div>', html, re.S)
    assert drawer, "no #drawer in ui/index.html"
    inner = drawer.group(1)
    ids = [i for i in ("topbar", "banners", "side-panel") if f'id="{i}"' in inner]
    assert ids == ["topbar", "banners", "side-panel"]
    assert 'id="zone-ribbon"' not in inner and 'id="mtg-board"' not in inner
    assert 'id="game-toolbar"' not in inner
    toggle = re.search(r'<button id="drawer-toggle"([^>]*)>', html)
    assert toggle, "no #drawer-toggle button"
    assert 'aria-controls="drawer"' in toggle.group(1)
    assert 'aria-expanded="false"' in toggle.group(1)


def test_the_rotate_overlay_is_part_of_the_game_screen() -> None:
    html = _index()
    game = re.search(r'<section id="game" hidden>(.*?)</section>', html, re.S)
    assert game and 'id="rotate-overlay"' in game.group(1)


# ------------------------------------------------------------- the style --


def test_desktop_keeps_the_ribbon_and_the_drawer_is_only_a_wrapper() -> None:
    css = _css()
    assert "display: contents;" in _rule(css, "#drawer")
    assert "display: none;" in _rule(css, "#drawer-toggle")
    assert "display: none;" in _rule(css, "#rotate-overlay")
    assert "grid-area: zones;" in _rule(css, "#zone-ribbon")


def test_mobile_mode_hides_the_ribbon_and_slides_the_drawer_over_the_board() -> None:
    block = _media_block(_css(), MOBILE_QUERY)
    game = _rule(block, "#game")
    assert 'grid-template-areas: "board";' in game
    assert "display: none;" in _rule(block, "#zone-ribbon")
    drawer = _rule(block, "#drawer")
    assert "position: fixed;" in drawer and "transform: translateX(" in drawer
    assert "visibility: hidden;" in drawer
    opened = _rule(block, "#game.drawer-open #drawer")
    assert "transform: none;" in opened and "visibility: visible;" in opened
    toggle = _rule(block, "#drawer-toggle")
    assert "position: fixed;" in toggle and "display: flex;" in toggle


def test_a_coarse_pointer_upright_gets_the_rotate_overlay() -> None:
    block = _media_block(_css(), PORTRAIT_QUERY)
    overlay = _rule(block, "#rotate-overlay")
    assert "display: flex;" in overlay
    base = _rule(_css(), "#rotate-overlay")
    assert "position: fixed;" in base and "inset: 0;" in base
    z = re.search(r"z-index:\s*(\d+)", base)
    assert z and int(z.group(1)) >= 100, "the overlay must sit above every other layer"
