"""The click-to-cast path, whole: a real board, the real card view the
browser is served, the real `ui/*.js` modules under Node, and the real API
endpoints the requests they build are posted to. No stubbed payloads
anywhere in this file - every card is a real Scryfall printing out of the
engine's own packaged corpus, and every assertion is about what the served
page would actually do with what the served API actually says.

Three defects, all of them in this one path, and all of them silently WRONG
rather than merely missing - which is why they are pinned end to end here
rather than only at the unit seams (`tests/test_ui_app.py`,
`tests/test_ui_actions.py`, `tests/test_ui_menus.py`,
`tests/test_affordances.py` each hold their own half):

1. **A modal double-faced card was played as a land when the player meant to
   cast it.** `planHandCardClick` decided landness with
   `/land/i.test(card.type_line)`, and a modal double-faced card's
   `type_line` off the battlefield is the printing's COMBINED line -
   "Creature - Human Warlock // Land" for Witch Enchanter //
   Witch-Blessed Meadow. So the plan was `{kind: "land"}`, the menu offered
   only Play, and clicking a four-of in the esper-blink pool burned it as a
   tapped land with its creature half unreachable from the browser at all.
   CR 712.8a is the rule the front face reading comes from: "While a
   double-faced card is outside the game or in a zone other than the
   battlefield or stack, it has only the characteristics of its front face."
   CR 712.12 is the permission the land half is played under.

2. **A non-mana X was never asked for.** CR 107.3a counts "a mana cost,
   alternative cost, additional cost, and/or activation cost with an {X},
   [-X], or X in it", and Toxic Deluge's X is in its printed additional cost
   ("pay X life"), not its mana cost. `cast.needs_x` published only the mana
   cost's half, so no X box appeared, no `x_value` was sent, and the wrath
   resolved as -0/-0 with 0 life paid.

3. **A split card was cast with no half chosen.** CR 709.3: the caster
   "chooses which half of a split card they are casting". `CastRequest.
   spell_face` and `Action.spell_face` both existed; the browser sent
   neither, so the cast kept the printing's combined cost and both halves'
   targeting.

Every CR number above was read in full, in the session that wrote this file,
out of the Comprehensive Rules text `mise run rules:pull` downloads in the
engine repo (effective September 25, 2026) with `mise run rules:find
<number>` - 107.3a, 118.8a, 709.3/709.3a/709.3b, 709.4, 712.2, 712.8a,
712.12 and 715.3/715.3a. None is restated here from memory, and each one is
the same rule the engine's own code cites where this change reads it
(`machines/legal_actions._cast_action`, `models/card.Card.land_face`,
`models/game.play_land`, `machines/casting.castable_faces`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient
from ui_node import requires_node, run_module

from magic_cards_model_api.store import store

pytestmark = requires_node

UI = Path(__file__).resolve().parents[1] / "ui"
APP_URL = (UI / "app.js").as_uri()

#: What `app.js` needs on `globalThis` before it can even be imported under
#: Node: its own module-level `createStore({... P.loadStops(localStorage)})`
#: runs at import time. `fetch` throws so a path that accidentally reaches
#: the network fails loudly instead of hanging - nothing in this file lets
#: the browser modules make their own requests; every request here is posted
#: by the test, through the real `TestClient`.
_NODE_PRELUDE = """
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.fetch = () => { throw new Error("the page must not call out here"); };
"""


def _seat_uid(game_id: str, player: str) -> str:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}[player]


def legal_actions(client: TestClient, game: str, player: str = "alice") -> list[dict[str, Any]]:
    response = client.get(
        f"/api/games/{game}/legal-actions",
        params={"player": player, "uid": _seat_uid(game, player)},
    )
    assert response.status_code == 200, response.text
    return response.json()["actions"]


def hand_menu(card: dict, rows: list[dict] | None = None) -> list[dict]:
    """The menu the real page opens on a click on `card`: `app.js`'s own
    `planHandCardClick` fed into `menus.js`'s `handMenuItems`, then enriched
    with the engine's own `legal-actions` answer exactly as `app.js` does."""
    return run_module(
        "menus.js",
        _NODE_PRELUDE
        + """(async () => {
          const app = await import(appUrl);
          const items = m.handMenuItems(app.planHandCardClick(card, { action: null }), card);
          return rows === null ? items : m.enrichItems(items, rows);
        })()""",
        card=card,
        rows=rows,
        appUrl=APP_URL,
    )


def affordance(client: TestClient, game: str, card_id: str, **params: Any) -> dict:
    response = client.get(
        f"/api/games/{game}/affordances/{card_id}", params={"player": "alice", **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def untapped_lands(client: TestClient, game: str) -> list[str]:
    return [
        card["id"]
        for card in state(client, game)["battlefield"]
        if card["controller"] == "alice"
        and "Land" in (card.get("type_line") or "")
        and not card["tapped"]
    ]


def post(client: TestClient, game: str, endpoint: str, body: dict) -> dict:
    response = client.post(f"/api/games/{game}/{endpoint}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# -- (A) CR 712.12: an MDFC is two options, not one ------------------------


def test_a_modal_dfc_offers_both_halves_off_the_real_card_view(client, game):
    """The reported card. Both options, each named, each carrying the engine
    row it would take - and the engine's own affordability on each: with no
    lands out the creature half says why it cannot be paid for, while the
    land drop beside it stays live."""
    add_card(client, game, "Witch Enchanter", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Witch Enchanter")
    assert card["type_line"] == "Creature — Human Warlock // Land", (
        "the combined line is what made a `/land/i` test match"
    )

    items = hand_menu(card, legal_actions(client, game))

    assert [item["label"] for item in items] == [
        "Cast Witch Enchanter",
        "Play Witch-Blessed Meadow",
    ]
    assert items[0]["disabled"] is True, "no lands out, so the creature half is unpayable"
    assert items[0]["sublabel"], "the engine's own reason is what the option prints"
    assert "disabled" not in items[1], "the land drop is legal and payable"


def test_a_different_modal_dfc_layout_offers_both_halves_too(client, game):
    """Generality, on a card that is neither the reported one nor from the
    same deck: Sink into Stupor // Soporific Springs is `Instant // Land`
    where Witch Enchanter is `Creature // Land`. Nothing in the fix reads a
    card name or a type."""
    add_card(client, game, "Sink into Stupor", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Sink into Stupor")

    assert [item["label"] for item in hand_menu(card)] == [
        "Cast Sink into Stupor",
        "Play Soporific Springs",
    ]


def test_a_single_faced_land_still_offers_exactly_one_play(client, game):
    add_card(client, game, "Mountain", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Mountain")

    assert hand_menu(card) == [
        {
            "key": "play",
            "label": "Play",
            "sublabel": "",
            "match": {"card_id": card["id"], "kind": "play_land", "ability_index": None},
        }
    ]


def test_choosing_the_land_half_plays_the_land_face(client, game):
    """CR 712.12, through the real endpoint: the land half goes to
    `actions/play-land`, and what arrives on the battlefield is the LAND -
    `Game.play_land` chooses the face (`Card.land_face`, which reads the one
    land face a modal double-faced card has), so the request needs no field
    of its own to name it."""
    add_card(client, game, "Witch Enchanter", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Witch Enchanter")

    (play,) = [item for item in hand_menu(card) if item["key"] == "play"]
    assert play["match"]["kind"] == "play_land"
    result = post(client, game, "actions/play-land", {"player": "alice", "card_id": card["id"]})
    # The land face's own "As this land enters, you may pay 3 life" is an
    # ordinary replacement-effect question (CR 614.12a), asked as the land
    # enters - which is itself proof that the LAND is what is being played.
    assert result["paused"] is True, result
    assert result["pending_decision"]["kind"] == "pay_life"
    answer = client.post(
        f"/api/games/{game}/decide",
        params={"uid": _seat_uid(game, "alice")},
        json={"player": "alice", "accept": False},
    )
    assert answer.status_code == 200, answer.text

    (permanent,) = state(client, game)["battlefield"]
    assert permanent["name"] == "Witch-Blessed Meadow"
    assert permanent["type_line"] == "Land"


def test_choosing_the_spell_half_casts_the_creature(client, game):
    """The half that could not be reached from the browser at all: the same
    card, the same click, the other option - and a creature spell on the
    stack rather than a land on the battlefield."""
    for _ in range(4):
        add_card(client, game, "Plains", "alice", "battlefield")
    add_card(client, game, "Witch Enchanter", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Witch Enchanter")

    (cast,) = [
        item for item in hand_menu(card, legal_actions(client, game)) if item["key"] == "cast"
    ]
    assert "disabled" not in cast, "four Plains pay {3}{W}"
    body = run_module(
        "actions.js",
        "m.bodyFor(m.begin('cast', card, {}), 'alice', sources)",
        card=card,
        sources=untapped_lands(client, game),
    )
    assert "spell_face" not in body, "a modal double-faced card is cast as its front face"
    result = post(client, game, "actions/cast", body)
    assert result["allowed"] is True, result

    (spell,) = state(client, game)["stack"]
    assert spell["name"] == "Witch Enchanter"


# -- (B) CR 107.3a: an X that is not in the mana cost -----------------------


def test_toxic_deluge_asks_for_an_x_and_sends_the_one_it_is_given(client, game):
    """The engine has always said there is an X here - its row carries
    `x_range` - and now so does the card view the page reads. The end of the
    chain is the request: `x_value` on the cast, and the wrath resolving at
    that X rather than at 0."""
    for _ in range(3):
        add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Toxic Deluge", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    card = find(state(client, game)["hands"]["alice"], "Toxic Deluge")

    (row,) = [r for r in legal_actions(client, game) if r["kind"] == "cast"]
    assert row["x_range"] == [0, None], "the engine's own CR 107.3a reading"
    assert card["cast"]["needs_x"] is True, "and the card view now agrees with it"

    staged = run_module(
        "actions.js",
        "(() => { let a = m.begin('cast', card, {}); const before = m.readyToFire(a); "
        "a = m.setX(a, 2); return [before, m.readyToFire(a), m.bodyFor(a, 'alice', sources)]; })()",
        card=card,
        sources=untapped_lands(client, game),
    )
    assert staged[0] is False, "the page must not fire a cast with no X announced"
    assert staged[1] is True
    body = staged[2]
    assert body["x_value"] == 2

    assert post(client, game, "actions/cast", body)["allowed"] is True
    assert client.post(f"/api/games/{game}/resolve-top").status_code == 200

    after = state(client, game)
    alice = (
        find(after["players"], "alice")
        if False
        else next(p for p in after["players"] if p["username"] == "alice")
    )
    assert alice["life"] == 18, "X life was actually paid (CR 107.3a, CR 118.8a)"
    assert not [c for c in after["battlefield"] if c["name"] == "Grizzly Bears"], (
        "a 2/2 dies to -2/-2; at the old X=0 it would have survived"
    )


# -- (C) CR 709.3: which half of a split card ------------------------------


def test_a_split_card_offers_its_halves_and_casts_the_one_picked(client, game):
    """The whole chain for the chosen half: the menu offers both, each item
    is enriched from that half's OWN row (one Mountain pays for Wear and not
    for Tear), the affordance asked for the chosen half answers that half's
    targeting (CR 709.3b), and the cast that goes out names it - so what
    lands on the stack is one half, not the printing."""
    for _ in range(2):
        add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Wear // Tear", "alice", "hand")
    add_card(client, game, "Blood Moon", "bob", "battlefield")
    add_card(client, game, "Aether Spellbomb", "bob", "battlefield")
    card = find(state(client, game)["hands"]["alice"], "Wear")

    items = hand_menu(card, legal_actions(client, game))
    assert [item["label"] for item in items] == ["Cast Wear", "Cast Tear"]
    assert [item["match"]["spell_face"] for item in items] == ["Wear", "Tear"]
    assert "disabled" not in items[0], "two Mountains pay {1}{R}"
    assert items[1]["disabled"] is True, "and does not pay {W}"

    aff = affordance(client, game, card["id"], spell_face="Tear")
    assert [r["phrase"] for r in aff["requirements"]] == ["target enchantment"]
    assert [c["name"] for c in aff["candidates"]] == ["Blood Moon"]

    body = run_module(
        "actions.js",
        "(() => { let a = m.begin('cast', card, { spellFace: 'Tear' }); "
        "a = m.withAffordance(a, aff); a = m.chooseTarget(a, target); "
        "return m.bodyFor(a, 'alice', sources); })()",
        card=card,
        aff=aff,
        target=find(state(client, game)["battlefield"], "Blood Moon")["id"],
        sources=[],
    )
    assert body["spell_face"] == "Tear"

    # Tear costs {W}; the Mountains in play are there to tell the two halves'
    # affordability apart, so the cast itself is paid for by a Plains.
    add_card(client, game, "Plains", "alice", "battlefield")
    body["mana_sources"] = untapped_lands(client, game)
    assert post(client, game, "actions/cast", body)["allowed"] is True

    (spell,) = state(client, game)["stack"]
    assert spell["name"] == "Tear", "the announced spell is the chosen half (CR 709.3a)"
