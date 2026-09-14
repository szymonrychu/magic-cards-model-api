"""`[hidden]` must beat every `display` rule in `ui/style.css`.

This guards a bug that shipped and made the whole UI unusable: the player
opened `/play/`, the page looked greyed out, and no button did anything.

The cause is a CSS subtlety worth stating plainly, because it bit three
separate elements in one change. `hidden` is not a CSS feature - it is a
plain HTML attribute, and browsers implement it as a *user-agent stylesheet*
rule (`[hidden] { display: none }`). Any author stylesheet rule that sets
`display` on the same element wins over it, because author styles beat UA
styles regardless of specificity. So an element toggled in JS with
`el.hidden = true` stays visible the moment this file gives it a `display`.

What that cost, in increasing severity:

- `#banners > div` (`display: flex`) left an empty bordered box on the board;
- `#game` (`display: grid`) rendered the game screen on top of the lobby;
- `.modal` (`display: flex`, plus `position: fixed; inset: 0` and a
  translucent dark background) covered the entire viewport while "hidden" and
  swallowed every click - the greyed-out, dead-button symptom exactly.

`ui/app.js` and the buildless ES modules under `ui/` and `ui/render/` toggle
visibility with the `hidden` property throughout (`el.hidden = true`), so the
fix is asserted once, globally, rather than per-element - a per-element fix
is what let the second and third cases through in the first place.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "ui"
STYLE_CSS = UI / "style.css"
INDEX_HTML = UI / "index.html"

#: Every element `ui/index.html` ships as an overlay - each one covers part
#: or all of the viewport when it is up, and each one is toggled from JS with
#: `.hidden`. Listed by id because the failure mode is silent: an overlay
#: that ships WITHOUT the attribute is up from the moment the page loads, and
#: `.modal`'s own `position: fixed; inset: 0` then swallows every click on
#: the board behind it - the greyed-out, dead-button outage this file exists
#: for. `#log-modal` used to be one of these (the game log, behind a topbar
#: button); it is gone now, at the owner's request, and the log lives in
#: `#log-box`, always visible in `#side-panel` rather than as an overlay.
OVERLAYS = ("card-zoom", "zone-viewer", "toast", "popover", "decision-window", "modal")

#: Every module that can toggle `.hidden` - the still-classic `app.js` plus
#: the buildless ES modules under `ui/` and `ui/render/` (`board.js` and
#: whatever else joins it). Globbed rather than named one by one, so a new
#: module that starts hiding an element is covered without editing this file.
JS_MODULES = sorted(UI.glob("*.js")) + sorted(UI.glob("render/*.js"))

#: The global rule, as a regex tolerant of whitespace but not of the
#: `!important` being dropped - without it, any later `display` rule in the
#: file silently wins again.
GLOBAL_RULE = re.compile(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", re.I)


def test_style_css_asserts_hidden_globally() -> None:
    assert GLOBAL_RULE.search(STYLE_CSS.read_text()), (
        "ui/style.css no longer contains a global `[hidden] { display: none !important; }`. "
        "Without it, any rule in the file that sets `display` on an element the JS toggles "
        "with `el.hidden` keeps that element rendered - which previously left the .modal "
        "overlay covering the whole page and swallowing every click."
    )


def test_the_elements_the_js_hides_are_all_covered_by_that_rule() -> None:
    """Every element any `ui/*.js` or `ui/render/*.js` module toggles with
    `.hidden` relies on the rule above, so this pins the coupling rather than
    leaving it implicit: if a module starts hiding something new, the global
    rule already covers it, and this test explains why no per-element rule is
    needed."""
    hidden_toggles: set[str] = set()
    for module in JS_MODULES:
        text = module.read_text()
        hidden_toggles |= set(re.findall(r"\$\(\"#([\w-]+)\"\)\.hidden\s*=", text))
        hidden_toggles |= set(
            re.findall(r"(?:\bconst\s+)?(\w+)\.hidden\s*=\s*(?:true|false)", text)
        )
    assert hidden_toggles, (
        "no `ui/*.js` or `ui/render/*.js` module toggles anything with `.hidden` any more - "
        "update this test"
    )
    # The point is only that the global rule exists to serve them; the
    # per-element names are informational, and deliberately not hard-coded
    # into an allowlist that would need editing on every UI change.
    assert GLOBAL_RULE.search(STYLE_CSS.read_text())


def test_no_element_id_gets_a_display_rule_without_the_global_guard() -> None:
    """A blunt but real check: `.modal` and `#game` are the two rules whose
    `display` values caused the outage, and both must still be preceded in
    the file by the global guard - CSS order does not matter for
    `!important` here, but the guard being *present* does."""
    css = STYLE_CSS.read_text()
    for selector in ("#game {", ".modal {"):
        assert selector in css, f"{selector} vanished from style.css - update this test"
    assert GLOBAL_RULE.search(css)


def test_every_overlay_ships_hidden_in_the_markup() -> None:
    """The other half of the guarantee. The global rule above makes `hidden`
    WORK; this makes sure the overlays actually start out carrying it.

    An overlay is only safe to add to the page because it is hidden at rest:
    `.modal` is `position: fixed; inset: 0` with a dark background, so one
    that shipped visible would cover the whole viewport and intercept every
    click, which is exactly the outage this file was written for."""
    html = INDEX_HTML.read_text()
    for overlay in OVERLAYS:
        tag = re.search(rf'<div id="{overlay}"([^>]*)>', html)
        assert tag, f"#{overlay} is no longer in ui/index.html - update this test"
        assert "hidden" in tag.group(1), (
            f"#{overlay} ships visible; it is toggled with `.hidden`, so it must start hidden"
        )


def test_the_advisor_panel_toggles_through_hidden_and_not_style_display() -> None:
    """The advisor panel (`render/advice.js`, which takes itself out of the
    control ribbon when it has nothing to propose) is the newest thing in
    this UI to appear and disappear, and it does so through `hidden` - so the
    single global rule above covers it with no per-element CSS.

    `style.display` would be outside that guarantee and would also fight it:
    `!important` beats an inline `display` only while the attribute is set,
    so the two mechanisms disagree exactly when both are in play.
    (`ui/cards.js` does set `style.display`, legitimately and not as a
    visibility toggle: it swaps a card's <img> for its text fallback on
    `onerror`, and the fallback needs `display: flex` specifically. Neither
    element is ever `hidden`.)"""
    for module in ("render/panels.js", "render/advice.js"):
        text = (UI / module).read_text()
        assert not re.search(r"\.style\.display\s*=", text), (
            f"ui/{module} toggles visibility with style.display instead of `el.hidden`"
        )
    panels = (UI / "render" / "panels.js").read_text()
    assert re.search(r"modal\.hidden\s*=", panels), "#modal is no longer toggled through `.hidden`"
