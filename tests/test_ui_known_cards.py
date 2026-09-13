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

from test_ui_board import FAKE_DOM
from ui_node import requires_node, run_module

pytestmark = requires_node

STYLE = Path(__file__).resolve().parents[1] / "ui" / "style.css"

# Mounts the board with `known` as alice's `known_cards` entry and bob holding
# `handCount` cards, captures every `console.warn`, and hands back what the
# opponent's hand, both hands and both info rows ended up drawn as.
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
    oppChip: chip("#opp-info"),
    youChip: chip("#you-info"),
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
    return {"classes": ["card", "card--known", "card-sm"], "cardId": card_id}


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
        result["plain"] == result["knowing"] == [{"classes": ["card", "card-lg"], "cardId": "h1"}]
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
        "render/panels.js",
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


def test_the_known_style_is_set_for_both_light_and_dark_rendering() -> None:
    css = STYLE.read_text()
    assert re.search(r"(^|\n)\.card--known\s*\{", css), "no base .card--known rule"
    light = re.search(r"@media \(prefers-color-scheme: light\)\s*\{(.*?)\n\}", css, re.S)
    assert light is not None, "no light colour-scheme block"
    assert "--known" in light.group(1)
    assert "var(--known" in css.split(".card--known", 1)[1].split("}", 1)[0]
