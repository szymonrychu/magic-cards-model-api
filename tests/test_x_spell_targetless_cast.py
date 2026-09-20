"""An {X} spell whose only "target" word sits on an ability it is not casting.

REPORTED, from a live game: "Walking Ballista got broken. On cast it asks for
target, but when I pick one I get error, that it can't take more than 0
targets - it renders it uncastable."

Walking Ballista is `{X}{X}`, "This creature enters with X +1/+1 counters on
it.", and its only "any target" is on an activated ability - "Remove a +1/+1
counter from this creature: It deals 1 damage to any target." A creature spell
takes no targets on cast; that ability is a separate object on the stack and
chooses its own targets when it is activated.

THE MECHANIC, not the card. Every one of these is a spell announced with an X
whose printed text carries a targeting line that is not part of casting it:

  - Walking Ballista, Chamber Sentry - an ACTIVATED ability's line.
  - Ajani's Anguish - a TRIGGERED ability's line ("When this enchantment
    enters, it deals X damage to any target").

Those three are the whole class in the shipped printing pool, measured: for
every other `{X}` non-modal printing the two readings below already agree.

WHICH READING. The engine has two. `machines.targeting._spell_text` drops the
lines that belong to an ability, and `targeting_profile` /
`spell_profile_for_text` both go through it - that is the read
`machines.casting.Casting.targeting` uses, so it is the read the cast is
enforced against. `profile_for_text` keeps the text whole, because its job is
one ability's *own* words. `Game.announcement_profile`'s X branch
(`models/game.py`, "if modal is None and x_value is not None") reaches for the
second one, so the affordance the browser draws its target picker from
demanded a target the cast then refused: "too many targets: 1 chosen where the
spell takes at most 0". That is a spell a player cannot cast at all.

NOT the wider "every targetless spell" case. The branch is gated on
`x_value is not None`, and `ui/actions.js`'s `affordanceQuery()` only sends
`x_value` once the player has typed one into a strip that is drawn only for
`cast.needs_x`. A targetless spell without an X goes through
`targeting_profile` and is answered correctly - pinned below by Acidic Slime,
whose "destroy target artifact, enchantment, or land" is an enters trigger and
which is one of 150 such non-X printings in the pool.

The same defect one door along is pinned by `test_modal_x_cast.py`'s
`test_announcing_x_before_the_modes_still_asks_for_no_targets`: that one is a
modal card read whole before its modes are chosen, this one any card read
whole including the lines that are not the spell.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, find, state
from fastapi.testclient import TestClient

#: Each card is an X spell with NO cast target, whose printed text still says
#: "any target" on a line belonging to an ability. `land`/`lands` are what the
#: listed `x_value` needs to be payable.
TARGETLESS_X_SPELLS = [
    ("Walking Ballista", 1, "Wastes", 2),
    ("Chamber Sentry", 1, "Wastes", 1),
    ("Ajani's Anguish", 1, "Mountain", 2),
]
IDS = ["activated-ability-reported", "activated-ability-other-card", "triggered-ability"]

SPELL_PARAMS = [
    pytest.param(*row, id=name) for row, name in zip(TARGETLESS_X_SPELLS, IDS, strict=True)
]


def affordances(
    client: TestClient, game: str, card_id: str, player: str = "alice", **params: Any
) -> dict:
    response = client.get(
        f"/api/games/{game}/affordances/{card_id}", params={"player": player, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def board(client: TestClient, game: str, name: str, land: str, lands: int) -> dict:
    """Alice holds `name` over `lands` copies of `land`; bob has a Memnite to
    be the target the browser must NOT be offered."""
    for _ in range(lands):
        add_card(client, game, land, "alice", "battlefield")
    add_card(client, game, name, "alice", "hand")
    add_card(client, game, "Memnite", "bob", "battlefield")
    return state(client, game)


def assert_no_cast_target(answer: dict, name: str) -> None:
    assert answer["requirements"] == [], (
        f"{name} takes no targets on cast - its 'any target' is on an ability's"
        " own line, and that ability chooses its targets when it goes on the stack"
    )
    assert answer["maximum"] == 0
    assert answer["minimum"] == 0
    assert answer["targets_anything"] is False
    assert answer["candidates"] == []


@pytest.mark.parametrize(("name", "x_value", "land", "lands"), SPELL_PARAMS)
def test_announcing_x_asks_for_no_cast_target(client, game, name, x_value, land, lands):
    """The affordance `ui/app.js`'s `refreshAffordance()` fetches the moment
    the player types X. `ui/render/board.js`'s `targetingOpts()` and
    `ui/actions.js`'s `slotsFromAffordance()` read this answer and nothing
    else, so a requirement here IS the target picker the player saw - and a
    slot with `minimum: 1` is also what keeps `ready()` false, which is the
    "uncastable" half of the report.
    """
    cards = board(client, game, name, land, lands)
    spell = find(cards["hands"]["alice"], name)

    assert_no_cast_target(affordances(client, game, spell["id"], x_value=x_value), name)


@pytest.mark.parametrize(("name", "x_value", "land", "lands"), SPELL_PARAMS)
def test_without_an_announced_x_the_same_card_is_read_correctly(
    client, game, name, x_value, land, lands
):
    """The control that places the fault: the very same card, the very same
    route, asked without `x_value`, is answered correctly. So nothing about
    the card's text or this api's relay of it is wrong - only the branch
    `x_value` selects.
    """
    cards = board(client, game, name, land, lands)
    spell = find(cards["hands"]["alice"], name)

    assert_no_cast_target(affordances(client, game, spell["id"]), name)


def test_a_targetless_spell_without_an_x_is_not_affected(client, game):
    """The wider case, ruled out with a card that is neither Walking Ballista
    nor an X spell. Acidic Slime is `{3}{G}{G}` and prints "When this creature
    enters, destroy target artifact, enchantment, or land" - a triggered
    ability, same shape as Ajani's Anguish's, on a spell that takes no targets
    on cast. It is answered correctly, which is why the report is about X and
    not about every targetless spell.
    """
    for _ in range(5):
        add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Acidic Slime", "alice", "hand")
    add_card(client, game, "Memnite", "bob", "battlefield")
    slime = find(state(client, game)["hands"]["alice"], "Acidic Slime")

    assert_no_cast_target(affordances(client, game, slime["id"]), "Acidic Slime")


@pytest.mark.parametrize(("name", "x_value", "land", "lands"), SPELL_PARAMS)
def test_it_casts_with_no_targets(client, game, name, x_value, land, lands):
    """End to end: the request the browser builds once no picker is drawn -
    `x_value` set, no `targets` key at all - puts the spell on the stack.

    Passing today: the cast path already reads the card the right way. Only
    the affordance the picker is drawn from does not, which is exactly how a
    player ends up unable to send the one request that works.
    """
    cards = board(client, game, name, land, lands)
    spell = find(cards["hands"]["alice"], name)
    sources = [card["id"] for card in cards["battlefield"] if card["name"] == land]

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": spell["id"],
            "x_value": x_value,
            "mana_sources": sources,
        },
    )
    assert response.status_code == 200, response.text
    assert [item["name"] for item in state(client, game)["stack"]] == [name]


@pytest.mark.parametrize(("name", "x_value", "land", "lands"), SPELL_PARAMS)
def test_a_target_sent_anyway_is_refused_in_the_engines_own_words(
    client, game, name, x_value, land, lands
):
    """The other half of the contract, and the refusal the owner actually saw.
    The affordance offering nothing and the cast accepting nothing have to be
    one answer; this pins the second half so a future "fix" that widens the
    cast instead of narrowing the affordance fails here.
    """
    cards = board(client, game, name, land, lands)
    spell = find(cards["hands"]["alice"], name)
    memnite = find(cards["battlefield"], "Memnite")
    sources = [card["id"] for card in cards["battlefield"] if card["name"] == land]

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": spell["id"],
            "x_value": x_value,
            "targets": [memnite["id"]],
            "mana_sources": sources,
        },
    )
    assert response.status_code == 200, response.text
    outcome = response.json()
    assert outcome["allowed"] is False, outcome
    assert "at most 0" in (outcome["reason"] or ""), outcome
