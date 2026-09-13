"""The board draws what this seat knows about hidden cards.

`state.known_cards` is keyed by the player who knows and is redacted to the
viewer's own entry (`serialize.py`'s `_known_cards_view`), so the opponent's
known hand cards are `known_cards[you].hand` owned by the opponent, and a
known library top card is a `known_cards[you].library` record with
`index_from_top` 0, owned by whichever player's library it sits in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_ui_board import FAKE_DOM
from ui_node import requires_node, run_module

pytestmark = requires_node

STYLE = Path(__file__).resolve().parents[1] / "ui" / "style.css"

# Mounts the board with `known` as alice's `known_cards` entry and bob holding
# `handCount` cards, captures every `console.warn`, and hands back what the
# opponent's hand, both hands and both player badges ended up drawn as.
MOUNT = """
function mountWith(known, handCount, myHand) {
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  const state = baseState({
    known_cards: { alice: known },
    hands: { alice: myHand || [] },
  });
  state.players[1].hand_count = handCount;
  state.players[0].hand_count = (myHand || []).length;
  const store = makeStore(baseApp(state));
  m.mountBoard(document, store, {
    onHandCard: NOOP, onPlayer: NOOP, onStackObject: NOOP,
    onPermission: NOOP, onPermanent: NOOP,
  });
  store.set({});
  console.warn = originalWarn;
  const describe = (el) => ({
    classes: [...el.classList.set].sort(),
    cardId: el.dataset.cardId || null,
  });
  const chip = (id) => {
    const found = document.querySelector(id).querySelector(".known-top");
    return found ? { text: found.textContent, title: found.attrs.title } : null;
  };
  return {
    oppHand: document.querySelector("#opp-hand").children.map(describe),
    myHand: document.querySelector("#my-hand").children.map(describe),
    oppChip: chip("#opp-badge"),
    youChip: chip("#my-badge"),
    warnings,
  };
}

function bobCard(id, name) {
  return { id, name, owner: "bob", zone: "hand", type_line: "Instant", abilities: [] };
}

function libraryRecord(index, source, card) {
  return { index_from_top: index, source, card: { ...card, zone: "library" } };
}

const EMPTY = { hand: [], library: [], sideboard: [] };
"""

BACK = {"classes": ["card-back"], "cardId": None}


def known_card(card_id: str) -> dict:
    return {"classes": ["card", "card--known", "card-fit"], "cardId": card_id}


def test_no_known_cards_draws_only_card_backs() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM + MOUNT + "mountWith(EMPTY, 3)",
    )
    assert result["oppHand"] == [BACK, BACK, BACK]
    assert result["oppChip"] is None
    assert result["warnings"] == []


def test_a_board_without_known_cards_at_all_still_draws_card_backs() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + MOUNT
        + """
        (() => {
          const state = baseState();
          const store = makeStore(baseApp(state));
          m.mountBoard(document, store, {
            onHandCard: NOOP, onPlayer: NOOP, onStackObject: NOOP,
            onPermission: NOOP, onPermanent: NOOP,
          });
          store.set({});
          return document.querySelector("#opp-hand").children.length;
        })()
        """,
    )
    assert result == 3


def test_two_known_of_five_draws_the_known_cards_then_three_backs() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + MOUNT
        + """
        mountWith(
          { ...EMPTY, hand: [bobCard("k1", "Lightning Bolt"), bobCard("k2", "Forest")] },
          5
        )
        """,
    )
    assert result["oppHand"] == [known_card("k1"), known_card("k2"), BACK, BACK, BACK]
    assert result["warnings"] == []


def test_a_known_hand_card_of_another_owner_is_not_drawn_in_the_opponents_hand() -> None:
    """Every known hand card is in its owner's hand; only bob's belong here."""
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + MOUNT
        + """
        mountWith(
          { ...EMPTY, hand: [
            bobCard("k1", "Lightning Bolt"), { ...bobCard("x", "Island"), owner: "carol" },
          ] },
          2
        )
        """,
    )
    assert result["oppHand"] == [known_card("k1"), BACK]


def test_the_known_top_of_a_library_is_a_chip_by_that_players_library_count() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + MOUNT
        + """
        mountWith(
          { ...EMPTY, library: [
            libraryRecord(0, "Mishra's Bauble", bobCard("t0", "Lightning Bolt")),
            libraryRecord(1, "Mishra's Bauble", bobCard("t1", "Forest")),
          ] },
          3
        )
        """,
    )
    assert result["oppChip"] is not None
    assert "Lightning Bolt" in result["oppChip"]["text"]
    assert "Lightning Bolt" in result["oppChip"]["title"]
    assert "Mishra's Bauble" in result["oppChip"]["title"]
    assert "Forest" not in result["oppChip"]["text"] + result["oppChip"]["title"]
    assert result["youChip"] is None
    assert result["oppHand"] == [BACK, BACK, BACK]


def test_a_known_top_of_your_own_library_is_a_chip_on_your_own_info_row() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + MOUNT
        + """
        mountWith(
          { ...EMPTY, library: [
            libraryRecord(0, null, { ...bobCard("t0", "Brainstorm"), owner: "alice" }),
          ] },
          3
        )
        """,
    )
    assert result["oppChip"] is None
    assert result["youChip"] is not None
    assert "Brainstorm" in result["youChip"]["text"]
    assert result["youChip"]["title"] == "Brainstorm"


def test_your_own_hand_is_drawn_the_same_whatever_you_know() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + MOUNT
        + """
        (() => {
          const mine = [{ ...bobCard("h1", "Counterspell"), owner: "alice" }];
          const plain = mountWith(EMPTY, 2, mine);
          const knowing = mountWith(
            { ...EMPTY, hand: [bobCard("k1", "Lightning Bolt")], library: [
              libraryRecord(0, "Mishra's Bauble", bobCard("t0", "Forest")),
            ] },
            2,
            mine
          );
          return { plain: plain.myHand, knowing: knowing.myHand, youChip: knowing.youChip };
        })()
        """,
    )
    assert (
        result["plain"] == result["knowing"] == [{"classes": ["card", "card-fit"], "cardId": "h1"}]
    )
    assert result["youChip"] is None


def test_more_known_cards_than_the_hand_holds_is_clamped_with_a_warning() -> None:
    result = run_module(
        "render/board.js",
        FAKE_DOM
        + MOUNT
        + """
        mountWith(
          { ...EMPTY, hand: [
            bobCard("k1", "Lightning Bolt"), bobCard("k2", "Forest"),
            bobCard("k3", "Island"), bobCard("k4", "Swamp"),
          ] },
          2
        )
        """,
    )
    assert result["oppHand"] == [known_card("k1"), known_card("k2")]
    assert len(result["warnings"]) == 1
    assert "4" in result["warnings"][0] and "2" in result["warnings"][0]


def test_known_opponent_hand_cards_are_hoverable() -> None:
    result = run_module(
        "preview.js",
        """
        Object.keys(m.hoverableCards({
          you: "alice",
          hands: { alice: [] },
          known_cards: { alice: {
            hand: [{ id: "k1", name: "Lightning Bolt", owner: "bob" }],
            library: [], sideboard: [],
          } },
        }))
        """,
    )
    assert result == ["k1"]


def _root_vars(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def _scheme_vars(css: str, scheme: str) -> dict[str, str]:
    """The `:root` colour tokens a viewer whose OS prefers `scheme` renders with."""
    base = re.search(r"(?:^|\n):root\s*\{(.*?)\n\}", css, re.S)
    assert base is not None, "no :root token block"
    tokens = _root_vars(base.group(1))
    for media in re.finditer(
        r"@media[^{]*prefers-color-scheme:\s*" + scheme + r"[^{]*\{(.*?)\n\}", css, re.S
    ):
        for root in re.finditer(r":root[^{]*\{(.*?)\}", media.group(1), re.S):
            tokens.update(_root_vars(root.group(1)))
    return tokens


def _contrast(a: str, b: str) -> float:
    def luminance(hex_colour: str) -> float:
        channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _rule(css: str, selector: str) -> str:
    found = re.search(r"(?:^|\n)" + re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert found is not None, f"no {selector} rule"
    return found.group(1)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_known_style_stands_out_on_the_board_in_both_colour_schemes(scheme: str) -> None:
    """WCAG contrast of what each OS colour preference actually renders.

    The chip's name text needs 4.5:1 against the player badge it sits on and
    the "known" label ink the same against its known-hue badge; the 2px ring
    is a non-text outline, so 3:1 against the board and against the card-back
    border it sits beside. The board itself is whatever that scheme's tokens
    make it - an override that only swaps the known hue is judged on the same
    background as every other scheme.
    """
    css = STYLE.read_text()
    tokens = _scheme_vars(css, scheme)
    known, ink = tokens["--known"], tokens["--known-ink"]

    assert "var(--known)" in _rule(css, ".card--known")
    chip = _rule(css, ".player-badge .known-top")
    assert "color: var(--known)" in chip and "border: 1px solid var(--known)" in chip
    badge = _rule(css, ".card--known::after")
    assert "background: var(--known)" in badge and "color: var(--known-ink)" in badge
    back_border = re.search(r"border:\s*\d+px solid (#[0-9a-fA-F]{6})", _rule(css, ".card-back"))
    assert back_border is not None, "no .card-back border colour"

    assert _contrast(known, tokens["--bg-raised"]) >= 4.5, (
        f"known-top chip text on the player badge ({scheme})"
    )
    assert _contrast(ink, known) >= 4.5, f"known label ink on its badge ({scheme})"
    assert _contrast(known, tokens["--bg"]) >= 3, f"known ring on the board ({scheme})"
    assert _contrast(known, back_border.group(1)) >= 3, f"known ring beside a card back ({scheme})"
