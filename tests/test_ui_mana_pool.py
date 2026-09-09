"""`ui/symbols.js`'s `manaPoolNodes` against a fake DOM (the pattern from
`tests/test_ui_ribbon.py`, which `tests/test_ui_board.py` already copies
verbatim) - the pool renderer had no dedicated suite of its own before this.

What it pins: CR 106.6-restricted mana is *not* interchangeable with the rest
of the pool, and until now it was drawn as though it were. `ManaPool.floating`
carries the whole amount of a colour and `ManaPool.restricted` says how much
of that carries a "Spend this mana only to ..." sentence, so two restricted
{C} and two unrestricted {C} used to arrive at the renderer as the same
`floating: {C: 2}` and came out as the same `2 {C}` pip with a title of just
"2 C". A player paying a cost had no way to see that the pool in front of them
would be refused.

The distinction rendered here is deliberately *not* a hue: WCAG 2.2 SC 1.4.1
(Level A) and the Game Accessibility Guidelines both rule out colour as the
sole carrier of information, so the restricted slice is its own
`.pool-group.restricted` with a marker glyph and a border, all of which
survive greyscale. `understood: false` - a sentence the engine could not parse,
which `ManaRestriction.allows` then refuses for everything - gets its own
marker and its own wording, because "narrowed" and "pays for nothing at all"
are not the same news.

Shape note: `pool.restricted` is `magic_cards_model.models.mana.ManaPool.
restricted` as the API publishes it - a list of `{restriction: {text,
understood}, amounts: {<color>: <count>}}` - which task API1 adds to
`ManaPoolView`. These tests build that payload directly, so they hold whether
or not the serializer has caught up.
"""

from __future__ import annotations

from ui_node import requires_node, run_module

pytestmark = requires_node

# The `tests/test_ui_board.py` fake DOM, minus the reconciler-only parts
# `manaPoolNodes` never reaches (it builds a fresh tree every call and never
# patches one): `h()` needs `createElement`/`createTextNode`/`appendChild`/
# `setAttribute`, and `className` and `classList` stay two views of one token
# set the way a browser keeps them, so a test can ask either.
FAKE_DOM = """
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attrs = {};
    this.style = {};
    this.classList = {
      set: new Set(),
      add: (c) => this.classList.set.add(c),
      remove: (c) => this.classList.set.delete(c),
      contains: (c) => this.classList.set.has(c),
    };
  }
  set className(value) {
    this.classList.set = new Set(String(value || "").split(/\\s+/).filter(Boolean));
  }
  get className() {
    return [...this.classList.set].join(" ");
  }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener(type, fn) { this.listeners = { ...(this.listeners || {}), [type]: fn }; }
  get textContent() {
    return this.children.map((c) => (c.textContent === undefined ? "" : c.textContent)).join("");
  }
}

globalThis.document = new El("#document");
document.createElement = (t) => new El(t);
document.createTextNode = (s) => ({ nodeType: 3, textContent: s });
document.createDocumentFragment = () => new El("#fragment");

// One rendered node as plain data - tag, its classes as a sorted list, its
// `title`, the text it shows, and the same for every child. Asserting on this
// is asserting on the structure the browser gets, not on a snapshot string.
function describe(node) {
  if (!node || node.nodeType === 3) {
    return { tag: "#text", classes: [], title: null, text: node ? node.textContent : "" };
  }
  return {
    tag: node.tagName,
    classes: [...node.classList.set].sort(),
    title: node.attrs.title === undefined ? null : node.attrs.title,
    text: node.textContent,
    children: (node.children || []).map(describe),
  };
}

// No symbology map in these tests, so every pip falls back to `symbolNode`'s
// own text pip (`span.ms-text` with the bare letter) - which is exactly what
// a cold symbol cache gives a real browser on first run.
const NO_SYMBOLS = {};
"""


def render(pool: object) -> dict:
    return run_module(
        "symbols.js",
        FAKE_DOM + "describe(m.manaPoolNodes(NO_SYMBOLS, POOL))",
        POOL=pool,
    )


def groups(rendered: dict) -> list[dict]:
    return [c for c in rendered["children"] if "pool-group" in c["classes"]]


# --------------------------------------------------------------- unchanged ---
#
# The two shapes that existed before restricted mana was rendered at all must
# come out byte-for-byte the way they did: an empty pool, and a pool with
# nothing but ordinary (and snow) mana in it.


def test_an_empty_pool_still_renders_the_empty_marker() -> None:
    for pool in ({}, {"floating": {}, "snow": {}}, {"floating": {}, "snow": {}, "restricted": []}):
        rendered = render(pool)
        assert rendered["tag"] == "span"
        assert rendered["classes"] == ["empty", "pool"]
        assert rendered["text"] == "empty pool"


def test_a_pool_of_only_unrestricted_mana_is_unchanged() -> None:
    """Three red and one white, plus a snow green: one `.pool-group` each,
    each with the count then the pip, and the plain `<count> <color>` title
    the renderer has always given them. No marker anywhere - nothing here is
    restricted, so nothing may imply that it is."""
    rendered = render({"floating": {"R": 3, "W": 1, "G": 1}, "snow": {"G": 1}, "restricted": []})
    assert rendered["classes"] == ["pool"]
    shown = [(g["classes"], g["title"], g["text"]) for g in groups(rendered)]
    assert shown == [
        (["pool-group"], "3 R", "3R"),
        (["pool-group"], "1 W", "1W"),
        (["pool-group"], "1 G", "1G"),
        (["pool-group"], "1 snow G", "1S"),
    ]
    assert "restricted" not in rendered["text"]
    assert all("badge" not in c["classes"] for g in groups(rendered) for c in g["children"])


# ------------------------------------------------- the restricted slice ------


ELDRAZI = "Spend this mana only to cast Eldrazi spells."


def test_restricted_and_unrestricted_mana_of_the_same_colour_are_distinct_groups() -> None:
    """Three colourless floating, two of them restricted: the player must see
    "1 unrestricted, 2 restricted", not "3". The two groups carry different
    classes *and* different glyphs, so the distinction survives greyscale
    (WCAG 2.2 SC 1.4.1), and the restriction's own sentence is reachable from
    the group and from its marker."""
    rendered = render(
        {
            "floating": {"C": 3},
            "snow": {},
            "restricted": [
                {"restriction": {"text": ELDRAZI, "understood": True}, "amounts": {"C": 2}}
            ],
        }
    )
    plain, restricted = groups(rendered)

    assert plain["classes"] == ["pool-group"]
    assert plain["title"] == "1 C"
    assert plain["text"] == "1C"

    assert restricted["classes"] == ["pool-group", "restricted"]
    assert restricted["title"] == f"2 C - {ELDRAZI}"
    assert ELDRAZI in restricted["title"]

    marks = [c for c in restricted["children"] if "badge-restricted" in c["classes"]]
    assert len(marks) == 1, "the restricted group carries exactly one marker glyph"
    assert marks[0]["text"].strip(), "the marker is a glyph, not an empty (colour-only) node"
    assert marks[0]["title"] == ELDRAZI, "the restriction sentence is reachable from the marker too"

    plain_marks = [c for c in plain["children"] if "badge-restricted" in c["classes"]]
    assert plain_marks == [], "unrestricted mana carries no marker"
    assert marks[0]["text"] not in plain["text"], "the two groups' markers differ"


def test_a_colour_that_is_wholly_restricted_leaves_no_unrestricted_group() -> None:
    """`floating` holds the whole amount and `restricted` a slice of it, so a
    pool whose every {C} is restricted must not also draw a phantom
    unrestricted {C} beside it."""
    rendered = render(
        {
            "floating": {"C": 2},
            "snow": {},
            "restricted": [
                {"restriction": {"text": ELDRAZI, "understood": True}, "amounts": {"C": 2}}
            ],
        }
    )
    shown = [(g["classes"], g["title"]) for g in groups(rendered)]
    assert shown == [(["pool-group", "restricted"], f"2 C - {ELDRAZI}")]


def test_two_different_restrictions_on_one_colour_are_two_groups() -> None:
    artifacts = "Spend this mana only to cast artifact spells."
    rendered = render(
        {
            "floating": {"C": 4},
            "snow": {},
            "restricted": [
                {"restriction": {"text": ELDRAZI, "understood": True}, "amounts": {"C": 2}},
                {"restriction": {"text": artifacts, "understood": True}, "amounts": {"C": 1}},
            ],
        }
    )
    assert [g["title"] for g in groups(rendered)] == [
        "1 C",
        f"2 C - {ELDRAZI}",
        f"1 C - {artifacts}",
    ]


# ------------------------------------------------------- understood: false ---


UNREADABLE = "Spend this mana only to pay for something this engine cannot read."


def test_mana_under_an_unread_restriction_is_called_unusable() -> None:
    """`ManaRestriction.understood` is false when the printed sentence parsed
    into no clauses at all, and `allows` then answers False for *everything* -
    the mana exists, empties with the pool, and pays for nothing. Saying
    "restricted" there would be a lie by omission, so it gets its own class,
    its own glyph, and wording that says unusable."""
    rendered = render(
        {
            "floating": {"U": 1},
            "snow": {},
            "restricted": [
                {"restriction": {"text": UNREADABLE, "understood": False}, "amounts": {"U": 1}}
            ],
        }
    )
    (group,) = groups(rendered)
    assert group["classes"] == ["pool-group", "restricted", "unusable"]
    title = group["title"]
    assert title.startswith("1 U - ")
    assert "unusable" in title.lower()
    assert UNREADABLE in title, "the printed sentence is still quoted verbatim"
    assert "cannot be spent on anything" in title

    (mark,) = [c for c in group["children"] if "badge-restricted" in c["classes"]]
    assert mark["text"].strip()
    assert "unusable" in (mark["title"] or "").lower()


def test_the_unusable_marker_differs_from_the_merely_restricted_one() -> None:
    """Both survive greyscale, and they must not be the same glyph - "narrowed
    to Eldrazi spells" and "pays for nothing at all" are different news."""

    def marker(understood: bool) -> str:
        rendered = render(
            {
                "floating": {"C": 1},
                "snow": {},
                "restricted": [
                    {
                        "restriction": {"text": ELDRAZI, "understood": understood},
                        "amounts": {"C": 1},
                    }
                ],
            }
        )
        (group,) = groups(rendered)
        (mark,) = [c for c in group["children"] if "badge-restricted" in c["classes"]]
        return mark["text"]

    assert marker(True) != marker(False)


def test_restricted_rows_are_exposed_for_the_casting_tray() -> None:
    """`restrictedManaRows` is the shared reading of a pool's restricted slice
    - the pool renderer and `ui/render/ribbon.js`'s casting-tray step both go
    through it, so the two can never disagree about what a restriction says."""
    rows = run_module(
        "symbols.js",
        FAKE_DOM
        + """
        m.restrictedManaRows({
          floating: { C: 3, U: 1 },
          snow: {},
          restricted: [
            { restriction: { text: ELDRAZI, understood: true }, amounts: { C: 2 } },
            { restriction: { text: UNREADABLE, understood: false }, amounts: { U: 1 } },
          ],
        })
        """.replace("ELDRAZI", repr(ELDRAZI).replace("'", '"')).replace(
            "UNREADABLE", repr(UNREADABLE).replace("'", '"')
        ),
    )
    assert [(r["color"], r["count"], r["understood"]) for r in rows] == [
        ("C", 2, True),
        ("U", 1, False),
    ]
    assert rows[0]["sentence"] == ELDRAZI
    assert "unusable" in rows[1]["sentence"].lower()
    assert UNREADABLE in rows[1]["sentence"]
    assert rows[0]["marker"] != rows[1]["marker"]


def test_a_pool_with_no_restricted_field_at_all_still_renders() -> None:
    """The serializer that publishes `restricted` (task API1) may not be
    deployed beside every client, and `null` is what an older payload sends."""
    rendered = render({"floating": {"R": 1}, "snow": {}, "restricted": None})
    assert [(g["classes"], g["title"]) for g in groups(rendered)] == [(["pool-group"], "1 R")]
