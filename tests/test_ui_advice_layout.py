"""Where the advisor panel lives, and that the game log panel is gone.

The owner asked for the advisor to "reuse bottom-right corner, where game log
lives right now". `ui/style.css`'s `#game` grid puts `#side-panel` in the
right-hand column's bottom row, so its children ARE that corner. The log
first moved out into a topbar-button overlay, then came back into
`#side-panel` as `#log-box` when that button was removed, and is now removed
from the play UI entirely: `#advice-box` is the column's last child and takes
all of the height the other panels leave.

Only the panel went. The log POLL stays: `app.js` still reads
`GET /api/games/{id}/log?since=` into `app.log` every tick, because the
advisor's proposals are ledger rows on that very read and
`ui/advice_model.js` builds the panel from them
(`tests/test_ui_advice.py` covers that the poll still feeds the panel).

These read `ui/index.html`, `ui/style.css` and `ui/app.js` directly rather
than through Node, because the grid placement and the specificity trap below
are decided in those files and nowhere else - and because a layout guarantee
should not be skipped on a machine with no node.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "ui"

SIDE_PANEL = re.compile(r'<aside id="side-panel">(.*?)</aside>', re.S)


def _index() -> str:
    return (UI / "index.html").read_text()


def _css() -> str:
    return (UI / "style.css").read_text()


def _rule(selector: str) -> str | None:
    block = re.search(r"\n" + re.escape(selector) + r" \{(.*?)\}", _css(), re.S)
    return block.group(1) if block else None


def test_the_log_panel_is_gone_and_the_advisor_is_the_ribbons_last_panel() -> None:
    """`#side-panel` is the right-hand column's bottom row (`style.css`'s
    grid areas). The game log panel is removed from the play UI entirely, so
    the advisor panel is that column's last child."""
    html = _index()
    assert 'id="log-box"' not in html, "the game log panel should be removed from the play UI"
    side = SIDE_PANEL.search(html)
    assert side, "could not find #side-panel in ui/index.html"
    children = re.findall(r'<div id="([\w-]+)"', side.group(1))
    assert children[-1] == "advice-box", children


def test_log_modal_is_gone_entirely() -> None:
    """`#log-modal` (the persistent overlay behind the removed topbar Log
    button) no longer exists either."""
    html = _index()
    assert 'id="log-modal"' not in html, (
        "#log-modal should have been removed along with the Log button"
    )


def test_the_log_panels_renderer_and_styles_are_deleted() -> None:
    """Nothing is left behind for a panel that no longer exists: no
    `render/log.js`, and no `#log-box` or `.log-line` rules in the
    stylesheet."""
    assert not (UI / "render" / "log.js").exists(), "ui/render/log.js should be deleted"
    css = _css()
    assert "#log-box" not in css, "style.css still styles the removed #log-box"
    assert ".log-line" not in css, "style.css still styles the removed log lines"


def test_the_advisor_panel_takes_the_remaining_height() -> None:
    """`#advice-box` is the one flexible child of that column: `flex: 1 1
    auto` absorbs whatever height the other panels leave, which is what
    keeps the column exactly one viewport tall, and `min-height: 0` with
    `overflow-y: auto` lets a long rationale scroll inside the panel rather
    than pushing the ribbon past the window."""
    body = _rule("#advice-box")
    assert body is not None, "could not find the #advice-box rule in ui/style.css"
    for declaration in ("flex: 1 1 auto", "min-height: 0", "overflow-y: auto"):
        assert declaration in body, f"#advice-box is missing `{declaration}`"


def test_no_other_panel_in_the_ribbon_competes_for_that_height() -> None:
    """Every other child of `#side-panel` keeps its content height: none of
    them carries a `flex` rule of its own, so the advisor alone grows."""
    css = _css()
    side = SIDE_PANEL.search(_index())
    assert side
    for child in re.findall(r'<div id="([\w-]+)"', side.group(1)):
        if child == "advice-box":
            continue
        for block in re.findall(
            r"\n([^{}\n]*#" + re.escape(child) + r"\b[^{}\n]*)\{(.*?)\}", css, re.S
        ):
            assert not re.search(r"(^|[\s;])flex(-grow)?\s*:", block[1]), (
                f"{block[0].strip()} gives #{child} a flex rule, competing with #advice-box"
            )


def test_the_side_panel_fills_the_mobile_drawer_too() -> None:
    """In mobile mode `#side-panel` is a flex child of `#drawer`; it takes
    the drawer's remaining height and scrolls inside itself, so the advisor
    panel's own `flex: 1 1 auto` fills the drawer the way it fills the
    desktop column."""
    body = _rule("  #drawer > #side-panel")
    assert body is not None, "could not find the mobile #drawer > #side-panel rule"
    for declaration in ("flex: 1 1 auto", "min-height: 0", "overflow-y: auto"):
        assert declaration in body, f"#drawer > #side-panel is missing `{declaration}`"


def test_no_advisor_rule_walks_into_the_documented_specificity_trap() -> None:
    """`#side-panel > div` is an id AND a type selector, so it outranks a
    bare `#advice-box` - the trap `style.css` already documents above
    `#prompt-box`/`#priority-box`, both of which silently lost their
    `border-color` to it. Any advisor declaration that collides with that
    rule has to be written at matching specificity
    (`#side-panel > #advice-box`, the shape `#casting-strip` already uses)."""
    outranking = _rule("#side-panel > div")
    assert outranking, "could not find the #side-panel > div rule in ui/style.css"
    taken = {line.split(":")[0].strip() for line in outranking.split(";") if ":" in line}
    bare = _rule("#advice-box")
    assert bare
    mine = {line.split(":")[0].strip() for line in bare.split(";") if ":" in line}
    assert not (taken & mine), (
        f"a bare #advice-box rule sets {sorted(taken & mine)}, which "
        "`#side-panel > div` outranks - "
        "write those at `#side-panel > #advice-box` instead"
    )


def test_a_stale_proposal_is_actually_dimmed_rather_than_merely_classed() -> None:
    """The renderer puts `.advice-stale` on the summary and rationale; the
    reduced opacity the acceptance asks for has to exist in the stylesheet
    for that class to mean anything."""
    block = _rule(".advice-stale")
    assert block, "could not find the .advice-stale rule in ui/style.css"
    opacity = re.search(r"opacity:\s*([0-9.]+)", block)
    assert opacity, ".advice-stale sets no opacity"
    assert 0 < float(opacity.group(1)) < 1, "a stale proposal is either invisible or not dimmed"


def test_the_app_mounts_the_advisor_panel_and_no_log_panel() -> None:
    """The advisor panel is mounted; the log panel is not, and its module is
    not imported. The log poll itself stays - `app.log` is what the advisor
    panel reads."""
    app = (UI / "app.js").read_text()
    assert "mountAdvice(document, store" in app
    assert "mountLog" not in app, "app.js still mounts the removed log panel"
    assert "render/log.js" not in app, "app.js still imports the removed log panel's module"
    assert "log?since=" in app, "the log poll the advisor panel reads must stay"
