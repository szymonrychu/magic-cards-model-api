"""`ui/render/zone_viewer.js`: a seat's graveyard, exile or command zone as
cards, opened from the zone counts on that seat's player badge.

On a phone the zone ribbon beside the board is hidden, so the badge's counts
are the only way into a zone. The viewer lists that zone's cards as real
`cardNode`s, patched live on every poll while it is open, and closes on its
close button, on Escape and on a tap on the backdrop. Its cards behave like
the ribbon's: a click targets the card or casts it under a play permission,
and a hover or a long press previews it.

Driven under Node with the real `render/board.js`, `store.js` and
`preview.js`, against `tests/test_ui_preview.py`'s fake DOM (real
capture/target/bubble dispatch and fake timers).
"""

from __future__ import annotations

import json

from test_ui_board import ONE_TARGET_ACTION
from test_ui_preview import FAKE_DOM
from ui_node import UI, requires_node, run_module

pytestmark = requires_node

URLS = {
    "STORE_URL": (UI / "store.js").as_uri(),
    "BOARD_URL": (UI / "render" / "board.js").as_uri(),
    "PREVIEW_URL": (UI / "preview.js").as_uri(),
}

SETUP = """
for (const id of ["opp-hand", "opp-battlefield", "my-battlefield"]) box(id, BOARD);
for (const side of ["opp", "my"]) {
  const row = box(side + "-lands", BOARD);
  box(side + "-badge", row);
  box(side + "-lands-cards", row);
}
const VIEWER = box("zone-viewer");
VIEWER.hidden = true;
// The page ships `#modal` hidden; a visible one would take Escape first.
MODAL.hidden = true;

const seat = (username) => ({
  username, life: 20, hand_count: 0, library_count: 40, graveyard_count: 0, mana_pool: {},
});
const zoned = (id, owner, zone) => ({ ...CARD(id), owner, controller: owner, zone, abilities: [] });
const GAME = (extra) => STATE({
  active_player: "alice", priority_holder: "alice", step: "precombat_main",
  players: [seat("alice"), seat("bob")], stack: [], combat: { attacks: [] },
  graveyards: {
    alice: [zoned("ag1", "alice", "graveyard")],
    bob: [zoned("bg1", "bob", "graveyard"), zoned("bg2", "bob", "graveyard")],
  },
  exile: [zoned("be1", "bob", "exile")],
  command: [],
  ...(extra || {}),
});

async function mountAll(state, over) {
  const S = await import(STORE_URL);
  const B = await import(BOARD_URL);
  const store = S.createStore({
    state, action: null, decisionDraft: {}, combatDraft: {}, preview: null, symbols: {},
    ...(over || {}),
  });
  const calls = { player: [], permanent: [], permission: [] };
  const handlers = {
    onPlayer: (u) => calls.player.push(u),
    onPermanent: (card) => calls.permanent.push(card.id),
    onPermission: (p) => calls.permission.push(p.card.id),
    onHandCard() {},
    onStackObject() {},
  };
  const viewer = m.mountZoneViewer(document, store, handlers);
  B.mountBoard(document, store, { ...handlers, onOpenZone: (key) => viewer.open(key) });
  store.set({});
  return { S, store, viewer, calls };
}

const zoneButton = (side, zone) =>
  document.querySelector("#" + side + "-badge").querySelector(".zone-" + zone);
const viewerCards = () => VIEWER.querySelector(".zone-viewer-cards").children
  .filter((c) => c.dataset && c.dataset.cardId)
  .map((c) => c.dataset.cardId);
const viewerCard = (id) => VIEWER.querySelector(".zone-viewer-cards").children
  .find((c) => c.dataset.cardId === id);
const title = () => VIEWER.querySelector(".zone-viewer-title").textContent;
"""


def run(expression: str, **bindings):
    return run_module("render/zone_viewer.js", FAKE_DOM + SETUP + expression, **URLS, **bindings)


def test_a_zone_count_opens_the_viewer_with_that_zones_cards() -> None:
    result = run(
        """
        (async () => {
          const { viewer, calls } = await mountAll(GAME());
          const before = { hidden: VIEWER.hidden, open: viewer.isOpen() };
          dispatch(zoneButton("opp", "graveyard"), "click");
          const opp = { hidden: VIEWER.hidden, cards: viewerCards(), title: title() };
          dispatch(zoneButton("my", "graveyard"), "click");
          const mine = { cards: viewerCards(), title: title() };
          const sizes = VIEWER.querySelector(".zone-viewer-cards").children
            .map((c) => [...c._classes].includes("card-fit"));
          return { before, opp, mine, sizes, player: calls.player };
        })()
        """
    )
    assert result["before"] == {"hidden": True, "open": False}
    assert result["opp"]["hidden"] is False
    assert result["opp"]["cards"] == ["bg1", "bg2"]
    assert "bob" in result["opp"]["title"] and "graveyard" in result["opp"]["title"]
    assert result["mine"]["cards"] == ["ag1"]
    assert "alice" in result["mine"]["title"]
    assert result["sizes"] == [True]
    # The count sits inside the badge, and the badge targets its player: a
    # click on the count must open the zone and never reach that handler.
    assert result["player"] == []


def test_the_viewer_opens_exile_and_command_and_shows_an_empty_zone() -> None:
    result = run(
        """
        (async () => {
          await mountAll(GAME({ command: [zoned("ac1", "alice", "command")] }));
          dispatch(zoneButton("opp", "exile"), "click");
          const exile = viewerCards();
          dispatch(zoneButton("my", "command"), "click");
          const command = viewerCards();
          dispatch(zoneButton("my", "exile"), "click");
          const empty = {
            cards: viewerCards(),
            note: Boolean(VIEWER.querySelector(".zone-viewer-empty")) &&
              !VIEWER.querySelector(".zone-viewer-empty").hidden,
          };
          return { exile, command, empty };
        })()
        """
    )
    assert result == {"exile": ["be1"], "command": ["ac1"], "empty": {"cards": [], "note": True}}


def test_the_viewer_closes_on_its_close_button_escape_and_the_backdrop() -> None:
    result = run(
        """
        (async () => {
          const { viewer } = await mountAll(GAME());
          const open = () => dispatch(zoneButton("opp", "graveyard"), "click");
          open();
          dispatch(VIEWER.querySelector(".zone-viewer-close"), "click");
          const byButton = VIEWER.hidden;
          open();
          const escape = dispatch(document.body, "keydown", { key: "Escape" });
          const byEscape = { hidden: VIEWER.hidden, stopped: escape._stop };
          open();
          dispatch(VIEWER.querySelector(".zone-viewer-panel"), "click");
          const panelKeepsIt = !VIEWER.hidden;
          dispatch(viewerCard("bg1"), "click");
          const cardKeepsIt = !VIEWER.hidden;
          dispatch(VIEWER, "click");
          const byBackdrop = VIEWER.hidden;
          const other = dispatch(document.body, "keydown", { key: "Escape" });
          return {
            byButton, byEscape, panelKeepsIt, cardKeepsIt, byBackdrop,
            closedEscapePassesOn: !other._stop, open: viewer.isOpen(),
          };
        })()
        """
    )
    assert result == {
        "byButton": True,
        "byEscape": {"hidden": True, "stopped": True},
        "panelKeepsIt": True,
        "cardKeepsIt": True,
        "byBackdrop": True,
        "closedEscapePassesOn": True,
        "open": False,
    }


def test_the_viewer_follows_the_zone_while_it_is_open() -> None:
    result = run(
        """
        (async () => {
          const { store } = await mountAll(GAME());
          dispatch(zoneButton("opp", "graveyard"), "click");
          const first = viewerCards();
          const kept = viewerCard("bg1");
          const next = GAME();
          next.graveyards.bob = [...next.graveyards.bob, zoned("bg3", "bob", "graveyard")];
          store.set({ state: next });
          const grown = { cards: viewerCards(), kept: viewerCard("bg1") === kept, title: title() };
          const emptied = GAME();
          emptied.graveyards.bob = [];
          store.set({ state: emptied });
          return { first, grown, emptied: viewerCards(), stillOpen: !VIEWER.hidden };
        })()
        """
    )
    assert result["first"] == ["bg1", "bg2"]
    assert result["grown"]["cards"] == ["bg1", "bg2", "bg3"]
    assert result["grown"]["kept"] is True
    assert "3" in result["grown"]["title"]
    assert result["emptied"] == []
    assert result["stillOpen"] is True


def test_a_viewer_card_clicks_like_a_zone_ribbon_card() -> None:
    """A graveyard card is a legal target of the in-flight cast (it lights up
    and a click fills the slot through `onPermanent`, as in `#zone-ribbon`),
    and a card a play permission names begins that cast instead."""
    action = json.loads(json.dumps(ONE_TARGET_ACTION))
    action["affordance"]["candidates"] = [
        {"id": "bg2", "kind": "card", "name": "Card bg2", "requirements": [0]}
    ]
    result = run(
        """
        (async () => {
          const permission = {
            card: zoned("ag1", "alice", "graveyard"), from_zone: "graveyard",
            granted_by: "Snapcaster Mage", usable_now: true, rule: "",
          };
          const { calls } = await mountAll(
            GAME({ play_permissions: { alice: [permission] } }), { action }
          );
          dispatch(zoneButton("opp", "graveyard"), "click");
          const lit = [...viewerCard("bg2")._classes].includes("targetable");
          const dim = [...viewerCard("bg1")._classes].includes("dimmed");
          dispatch(viewerCard("bg2"), "click");
          dispatch(zoneButton("my", "graveyard"), "click");
          dispatch(viewerCard("ag1"), "click");
          return { lit, dim, calls };
        })()
        """,
        action=action,
    )
    assert result["lit"] is True and result["dim"] is True
    assert result["calls"] == {"player": [], "permanent": ["bg2"], "permission": ["ag1"]}


def test_viewer_cards_preview_on_hover_and_on_a_long_press() -> None:
    result = run(
        """
        (async () => {
          const { store, calls } = await mountAll(GAME());
          const P = await import(PREVIEW_URL);
          P.mountCardPreview(document, store, window);
          dispatch(zoneButton("opp", "graveyard"), "click");
          const card = viewerCard("bg2");
          mouse(card, "pointerover");
          const hover = { shown: shown(), image: zoomImage() };
          mouse(card, "pointerout", { relatedTarget: document.body });
          touch(card, "pointerdown");
          advance(400);
          const press = { shown: shown(), image: zoomImage() };
          touch(card, "pointerup");
          dispatch(card, "click");
          advance(1000);
          return { hover, press, afterRelease: shown(), permanent: calls.permanent };
        })()
        """
    )
    assert result["hover"] == {"shown": True, "image": "/large/bg2"}
    assert result["press"] == {"shown": True, "image": "/large/bg2"}
    assert result["afterRelease"] is False
    assert result["permanent"] == []


def test_app_js_mounts_the_viewer_and_opens_it_from_the_badges() -> None:
    app = (UI / "app.js").read_text()
    assert 'from "./render/zone_viewer.js"' in app
    assert "mountZoneViewer(document, store" in app
    assert "onOpenZone" in app


def test_index_html_ships_the_viewer_hidden_and_it_is_a_preview_source() -> None:
    html = (UI / "index.html").read_text()
    assert '<div id="zone-viewer" class="zone-viewer"' in html
    tag = html.split('<div id="zone-viewer"', 1)[1].split(">", 1)[0]
    assert "hidden" in tag and 'role="dialog"' in tag
    preview = (UI / "preview.js").read_text()
    assert "#zone-viewer" in preview.split("PREVIEW_SOURCES =", 1)[1].split(";", 1)[0]
