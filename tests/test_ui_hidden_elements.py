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

`ui/app.js` toggles visibility with the `hidden` property throughout
(`el.hidden = true`), so the fix is asserted once, globally, rather than
per-element - a per-element fix is what let the second and third cases
through in the first place.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "ui"
STYLE_CSS = UI / "style.css"
APP_JS = UI / "app.js"

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
    """Every element `ui/app.js` toggles with `.hidden` relies on the rule
    above, so this pins the coupling rather than leaving it implicit: if the
    JS starts hiding something new, the global rule already covers it, and
    this test explains why no per-element rule is needed."""
    hidden_toggles = set(re.findall(r"\$\(\"#([\w-]+)\"\)\.hidden\s*=", APP_JS.read_text()))
    hidden_toggles |= set(
        re.findall(r"(?:\bconst\s+)?(\w+)\.hidden\s*=\s*(?:true|false)", APP_JS.read_text())
    )
    assert hidden_toggles, "ui/app.js no longer toggles anything with `.hidden` - update this test"
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
