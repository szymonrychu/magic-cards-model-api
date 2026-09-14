"""`ui/fit.js`'s `fitCardWidth`: how wide a card may be in one board row.

Pure arithmetic, no DOM - `render/board.js` measures a row and applies the
answer as `--card-w`. The geometry it has to respect:

- a card is 63:88 (width:height), so a card of width `w` is `w * 88 / 63`
  tall;
- a tapped card reserves a landscape box, so its footprint across the row is
  the card's height, not its width;
- every card tucked under a permanent shifts `TUCK_X * w` right and
  `TUCK_Y * w` down, and the pile reserves that room;
- a card never shrinks below the readable floor - past that the row scrolls
  sideways instead.
"""

from __future__ import annotations

import re
from pathlib import Path

from ui_node import requires_node, run_module

pytestmark = requires_node

STYLE = Path(__file__).resolve().parents[1] / "ui" / "style.css"
ASPECT = 88 / 63


def fit(**kwargs: object) -> dict:
    return run_module("fit.js", "m.fitCardWidth(args)", args=kwargs)


def constants() -> dict:
    return run_module(
        "fit.js",
        "({ tuckX: m.TUCK_X, tuckY: m.TUCK_Y, floor: m.MIN_CARD_WIDTH, aspect: m.CARD_ASPECT })",
    )


def test_cards_shrink_so_every_footprint_fits_the_row_width() -> None:
    few = fit(width=1000, height=10_000, count=4, gap=0)
    many = fit(width=1000, height=10_000, count=10, gap=0)
    assert few == {"cardWidth": 250, "scroll": False}
    assert many == {"cardWidth": 100, "scroll": False}


def test_the_gap_between_cards_is_part_of_the_row_width() -> None:
    result = fit(width=1000, height=10_000, count=10, gap=10)
    assert result["cardWidth"] * 10 + 10 * 9 <= 1000
    assert result["cardWidth"] == 91
    assert result["scroll"] is False


def test_card_width_is_capped_by_the_row_height() -> None:
    result = fit(width=5000, height=140, count=2, gap=0)
    assert result["cardWidth"] == 100
    assert result["cardWidth"] * ASPECT <= 140
    assert result["scroll"] is False


def test_reaching_the_floor_stops_shrinking_and_signals_a_scroll() -> None:
    result = fit(width=300, height=10_000, count=20, gap=0)
    assert result == {"cardWidth": constants()["floor"], "scroll": True}
    assert constants()["floor"] == 44


def test_a_floor_width_that_exactly_fits_does_not_scroll() -> None:
    result = fit(width=440, height=10_000, count=10, gap=0)
    assert result == {"cardWidth": 44, "scroll": False}


def test_a_tapped_card_takes_its_height_across_the_row() -> None:
    upright = fit(width=600, height=10_000, count=4, tapped=0, gap=0)
    tapped = fit(width=600, height=10_000, count=4, tapped=2, gap=0)
    w = tapped["cardWidth"]
    assert upright["cardWidth"] == 150
    assert 2 * w + 2 * w * ASPECT <= 600
    # The largest whole-pixel width that still fits.
    assert 2 * (w + 1) + 2 * (w + 1) * ASPECT > 600
    assert tapped["scroll"] is False


def test_an_all_tapped_row_is_capped_by_the_landscape_height() -> None:
    """A row of nothing but tapped cards stands every card on its long
    edge, so each box is only `w` tall - the height cap is the height itself."""
    result = fit(width=5000, height=100, count=3, tapped=3, gap=0)
    assert result["cardWidth"] == 100


def test_tucked_cards_add_their_offsets_to_the_footprint() -> None:
    c = constants()
    alone = fit(width=600, height=10_000, count=4, depths=[0, 0, 0, 0], gap=0)
    piled = fit(width=600, height=10_000, count=4, depths=[2, 0, 1, 0], gap=0)
    w = piled["cardWidth"]
    assert alone["cardWidth"] == 150
    assert 4 * w + 3 * c["tuckX"] * w <= 600
    assert 4 * (w + 1) + 3 * c["tuckX"] * (w + 1) > 600


def test_tucked_cards_count_against_the_row_height() -> None:
    c = constants()
    result = fit(width=5000, height=300, count=1, depths=[2], gap=0)
    w = result["cardWidth"]
    assert w * ASPECT + 2 * c["tuckY"] * w <= 300
    assert (w + 1) * ASPECT + 2 * c["tuckY"] * (w + 1) > 300


def test_zero_cards_is_the_height_cap_and_never_scrolls() -> None:
    result = fit(width=500, height=140, count=0, gap=5)
    assert result == {"cardWidth": 100, "scroll": False}


def test_a_row_too_short_for_the_floor_still_gets_the_floor() -> None:
    result = fit(width=5000, height=20, count=1, gap=0)
    assert result == {"cardWidth": 44, "scroll": False}


def floor(**kwargs: object) -> int:
    return run_module("fit.js", "m.fitFloor(args)", args=kwargs)


def test_a_board_tall_enough_for_seven_floor_cards_keeps_the_readable_floor() -> None:
    assert floor(height=1000, rows=7) == 44


def test_a_short_board_lowers_the_floor_so_all_seven_rows_fit_its_height() -> None:
    """A landscape phone has about 250px for seven rows: seven 44px-wide cards
    are 430px tall, so the shared floor drops until one card per row fits."""
    got = floor(height=294, rows=7)
    assert got == 30
    assert 7 * got * ASPECT <= 294
    assert 7 * (got + 1) * ASPECT > 294


def test_the_floor_never_drops_below_the_smallest_card() -> None:
    assert floor(height=50, rows=7) == run_module("fit.js", "m.MIN_FLOOR_WIDTH")
    assert run_module("fit.js", "m.MIN_FLOOR_WIDTH") == 20


def test_a_board_with_no_rows_keeps_the_readable_floor() -> None:
    assert floor(height=0, rows=0) == 44


def test_the_css_tuck_offsets_scale_with_the_same_ratios_fit_js_uses() -> None:
    """`fit.js` sizes a pile with `TUCK_X`/`TUCK_Y`, and `style.css` draws the
    pile - the two have to be the same numbers or a fitted pile overhangs."""
    css = STYLE.read_text()
    c = constants()
    rule = re.search(r"\n\.perm-stack \{(.*?)\}", css, re.S)
    assert rule is not None
    x = re.search(r"--tuck-x:\s*calc\(var\(--card-w, 76px\) \* (\d+) / (\d+)\)", rule.group(1))
    y = re.search(r"--tuck-y:\s*calc\(var\(--card-w, 76px\) \* (\d+) / (\d+)\)", rule.group(1))
    assert x is not None and y is not None, rule.group(1)
    assert int(x.group(1)) / int(x.group(2)) == c["tuckX"]
    assert int(y.group(1)) / int(y.group(2)) == c["tuckY"]
    assert c["aspect"] == ASPECT
