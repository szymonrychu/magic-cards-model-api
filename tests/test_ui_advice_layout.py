"""Where the advisor panel lives, and where the game log went.

The owner asked for the advisor to "reuse bottom-right corner, where game log
lives right now". `ui/style.css`'s `#game` grid puts `#side-panel` in the
right-hand column's bottom row, so its children ARE that corner - and reuse
meant the log moved out, into `#log-modal`, a persistent overlay behind a new
topbar button, keeping `render/log.js` and its own tests untouched.

A later task removed that "Zones" and "Log" button pair from the topbar at
the owner's request. The log came back into `#side-panel` as `#log-box`,
always visible now rather than behind an overlay - `#log-modal` is gone
entirely. The tests that pinned the modal's own shape moved to
`tests/test_ui_menus.py` (the six-panel column) and
`tests/test_ui_hidden_elements.py` (the overlay list); what is left here is
what is still true: the advisor's own corner and its flexible-height CSS.

These read `ui/index.html` and `ui/style.css` directly rather than through
Node, because the grid placement and the specificity trap below are decided
in those two files and nowhere else - and because a layout guarantee should
not be skipped on a machine with no node.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "ui"

# --------------------------------------------------------- the structure ---
#
# The panel takes over the bottom-right corner, which is where the game log
# lived (the owner: "For this reuse bottom-right corner, where game log lives
# right now"). Reuse, not replace: the log MOVES, into a persistent modal
# behind the topbar's Log button. These read `ui/index.html` and
# `ui/style.css` directly, because the grid placement and the specificity
# trap below are decided there and nowhere else.

SIDE_PANEL = re.compile(r'<aside id="side-panel">(.*?)</aside>', re.S)


def _index() -> str:
    return (UI / "index.html").read_text()


def _css() -> str:
    return (UI / "style.css").read_text()


def test_the_advisor_panel_and_the_log_share_the_control_ribbons_bottom() -> None:
    """`#side-panel` is the right-hand column's bottom row (`style.css`'s
    grid areas). The advisor panel sits there with the room the rest of the
    column leaves it; the log (`#log-box`) is back in this same column too
    (moved out of a topbar-button overlay once that button was removed), as
    the column's last child so it reads after the advisor's proposal rather
    than before it."""
    side = SIDE_PANEL.search(_index())
    assert side, "could not find #side-panel in ui/index.html"
    assert 'id="advice-box"' in side.group(1)
    assert 'id="log-box"' in side.group(1), (
        "the log is not in the control ribbon - it should be back here, always visible, "
        "now that the topbar Log button that used to hide it behind #log-modal is gone"
    )
    assert re.search(r'id="advice-box".*id="log-box"', side.group(1), re.S), (
        "the log should read after the advisor's proposal, not before it"
    )


def test_log_modal_is_gone_entirely() -> None:
    """`#log-modal` (the persistent overlay behind the removed topbar Log
    button) no longer exists - the log moved back into `#side-panel` as an
    always-visible `#log-box` instead."""
    html = _index()
    assert 'id="log-modal"' not in html, (
        "#log-modal should have been removed along with the Log button"
    )


def test_the_advisor_panel_inherits_the_height_the_log_used_to_absorb() -> None:
    """`#log-box` was the one child of that column with `flex: 1 1 auto` - it
    absorbed whatever height the other panels left, which is what kept the
    column exactly one viewport tall. Whatever sits in that slot has to carry
    the same rule, or the ribbon grows past the window."""
    block = re.search(r"\n#advice-box \{(.*?)\}", _css(), re.S)
    assert block, "could not find the #advice-box rule in ui/style.css"
    body = block.group(1)
    for declaration in ("flex: 1 1 auto", "min-height: 0", "overflow-y: auto"):
        assert declaration in body, f"#advice-box is missing `{declaration}`"


def test_no_advisor_rule_walks_into_the_documented_specificity_trap() -> None:
    """`#side-panel > div` is an id AND a type selector, so it outranks a
    bare `#advice-box` - the trap `style.css` already documents above
    `#prompt-box`/`#priority-box`, both of which silently lost their
    `border-color` to it. Any advisor declaration that collides with that
    rule has to be written at matching specificity
    (`#side-panel > #advice-box`, the shape `#casting-strip` already uses)."""
    css = _css()
    outranking = re.search(r"\n#side-panel > div \{(.*?)\}", css, re.S)
    assert outranking, "could not find the #side-panel > div rule in ui/style.css"
    taken = {line.split(":")[0].strip() for line in outranking.group(1).split(";") if ":" in line}
    bare = re.search(r"\n#advice-box \{(.*?)\}", css, re.S)
    assert bare
    mine = {line.split(":")[0].strip() for line in bare.group(1).split(";") if ":" in line}
    assert not (taken & mine), (
        f"a bare #advice-box rule sets {sorted(taken & mine)}, which "
        "`#side-panel > div` outranks - "
        "write those at `#side-panel > #advice-box` instead"
    )


def test_a_stale_proposal_is_actually_dimmed_rather_than_merely_classed() -> None:
    """The renderer puts `.advice-stale` on the summary and rationale; the
    reduced opacity the acceptance asks for has to exist in the stylesheet
    for that class to mean anything."""
    block = re.search(r"\n\.advice-stale \{(.*?)\}", _css(), re.S)
    assert block, "could not find the .advice-stale rule in ui/style.css"
    opacity = re.search(r"opacity:\s*([0-9.]+)", block.group(1))
    assert opacity, ".advice-stale sets no opacity"
    assert 0 < float(opacity.group(1)) < 1, "a stale proposal is either invisible or not dimmed"


def test_the_app_mounts_the_advisor_panel_and_still_mounts_the_log() -> None:
    """Moving the log must not have dropped it: both renderers are mounted,
    and `mountLog` itself is untouched (its own five tests in
    `tests/test_ui_log.py` build their own `#log-box` root and pass
    unchanged)."""
    app = (UI / "app.js").read_text()
    assert "mountAdvice(document, store" in app
    assert "mountLog(document, store)" in app
