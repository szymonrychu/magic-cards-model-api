"""CR 106.6's "Spend this mana only to ..." tag, carried onto the wire.

The engine has held this on the mana itself for a while - `ManaPool.restricted`
beside `ManaPool.floating`, `ManaPool.spend_effects` beside it - and no surface
published it. Two restricted `{C}` in a pool serialised as `floating {"C": 2}`
and were indistinguishable from two unrestricted `{C}`: a UI reading that view
showed the player two mana that would pay for anything, when in fact one of
them (an unread restriction, `understood=False`) pays for nothing at all.

`restricted` is a **subset** of `floating`, not a replacement for it - CR
106.4a still has the player announce all the mana that is there, and the
restriction narrows only what may be *done* with it - so each entry carries
both the sentence and how much of the pool is under it.

`understood` is published rather than hidden for the same reason: an
unreadable restriction is fail-closed in the engine, so the mana exists, is
announced, and refuses every spend. A view that showed the mana without that
flag would be lying to the player about what they can cast.

The abilities half is the same claim one step earlier: an `AbilityView` says
what activating it would produce, so it has to say what that mana would be
tied to before the player taps.

Hidden information: nothing here needs a `visibility.redact_view` pass. A
player's mana pool is public (CR 106.4a - "that player announces what mana is
still there"), and `_redact_player` already leaves the whole pool block alone;
a battlefield card's `abilities` are public the same way (see
`test_mana_ability_visibility.py`), and a *hand* card's whole entry is dropped
for every viewer but its owner, field and all. Both claims are pinned below.
"""

from __future__ import annotations

import pytest
from conftest import add_card, find, state
from magic_cards_model.models import Game

CAVERN_RESTRICTION = (
    "Spend this mana only to cast a creature spell of the chosen type, "
    "and that spell can't be countered."
)
HALFLING_RESTRICTION = (
    "Spend this mana only to cast a legendary spell, and that spell can't be countered."
)
TEMPLE_RESTRICTION = (
    "Spend this mana only to cast colorless Eldrazi spells or activate "
    "abilities of colorless Eldrazi."
)
ARENA_SPEND_EFFECT = "If that mana is spent on a creature spell, it gains haste until end of turn."


def pool_of(client, game, username="alice", uid=None):
    board = state(client, game, uid=uid) if uid else state(client, game)
    return next(p for p in board["players"] if p["username"] == username)["mana_pool"]


def activate(client, game, card_id, ability_index, **extra):
    response = client.post(
        f"/api/games/{game}/actions/activate",
        json={
            "player": "alice",
            "card_id": card_id,
            "ability_index": ability_index,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["allowed"] is True, body
    return body


def battlefield_id(client, game, name):
    return find(state(client, game)["battlefield"], name)["id"]


# -- the pool ---------------------------------------------------------------


def test_the_pool_says_how_much_of_it_carries_a_restriction(client, game):
    """Cavern of Souls' second ability, activated for {W}. The pool reports one
    white mana floating AND that all of it is under CR 106.6's sentence."""
    add_card(client, game, "Cavern of Souls", "alice", "battlefield")
    activate(client, game, battlefield_id(client, game, "Cavern of Souls"), 1, color="W")

    pool = pool_of(client, game)
    assert pool["floating"] == {"W": 1}
    assert pool["restricted"] == [
        {
            "restriction": {"text": CAVERN_RESTRICTION, "understood": False},
            "amounts": {"W": 1},
        }
    ]
    assert pool["spend_effects"] == []


def test_restricted_mana_is_distinguishable_from_the_same_colour_unrestricted(client, game):
    """The exact defect: without `restricted`, two restricted {C} and one
    unrestricted {C} are one `floating {"C": 3}` and nothing tells them apart.

    Eldrazi Temple's second ability adds {C}{C} under a restriction; its first
    adds a plain {C}. Both land in the same pool, and the view has to say which
    two of the three are restricted.
    """
    add_card(client, game, "Eldrazi Temple", "alice", "battlefield")
    add_card(client, game, "Eldrazi Temple", "alice", "battlefield")
    temples = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Eldrazi Temple"]

    activate(client, game, temples[0], 1)
    activate(client, game, temples[1], 0)

    pool = pool_of(client, game)
    assert pool["floating"] == {"C": 3}
    assert pool["restricted"] == [
        {
            "restriction": {"text": TEMPLE_RESTRICTION, "understood": True},
            "amounts": {"C": 2},
        }
    ]


def test_an_unread_restriction_is_published_as_understood_false(client, game):
    """`understood=False` means the engine will refuse that mana for
    everything, so it must reach the player.

    Cavern of Souls read with no creature type chosen is exactly that case -
    "the chosen type" has no answer, so the sentence cannot be turned into
    anything checkable and fails closed. Delighted Halfling's "a legendary
    spell" needs nothing off the permanent and reads fine, so the two sit in
    one pool with different flags and the view distinguishes them.
    """
    add_card(client, game, "Cavern of Souls", "alice", "battlefield")
    add_card(client, game, "Delighted Halfling", "alice", "battlefield")
    halfling = battlefield_id(client, game, "Delighted Halfling")
    client.patch(f"/api/games/{game}/cards/{halfling}", json={"summoning_sick": False})

    activate(client, game, battlefield_id(client, game, "Cavern of Souls"), 1, color="W")
    activate(client, game, halfling, 1, color="U")

    pool = pool_of(client, game)
    assert pool["floating"] == {"W": 1, "U": 1}
    flags = {
        entry["restriction"]["text"]: entry["restriction"]["understood"]
        for entry in pool["restricted"]
    }
    assert flags == {CAVERN_RESTRICTION: False, HALFLING_RESTRICTION: True}


def test_the_pool_says_which_mana_carries_a_spend_effect(client, game):
    """CR 106.6's *other* clause - "have an additional effect that affects the
    spell or ability that mana is spent on". Arena of Glory prints the only one
    in this pool, and it narrows nothing, so it is its own list rather than a
    restriction with a permissive reading."""
    add_card(client, game, "Arena of Glory", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    arena = battlefield_id(client, game, "Arena of Glory")
    mountain = battlefield_id(client, game, "Mountain")

    activate(client, game, arena, 1, mana_sources=[mountain])

    pool = pool_of(client, game)
    assert pool["floating"] == {"R": 2}
    assert pool["restricted"] == []
    assert pool["spend_effects"] == [{"text": ARENA_SPEND_EFFECT, "amounts": {"R": 2}}]


def test_an_untouched_pool_still_carries_both_keys(client, game):
    """Shape stability: a client reading `restricted`/`spend_effects` must not
    have to guess whether an absent key means "none" or "this build does not
    report it"."""
    pool = pool_of(client, game)
    assert pool == {"floating": {}, "snow": {}, "restricted": [], "spend_effects": []}


def test_the_pools_restriction_is_public_to_the_opponent(client, game):
    """CR 106.4a: a player announces what mana is still in their pool, so the
    restriction on it is public information - the opponent's view of the block
    is the identical one, not a reduced one."""
    add_card(client, game, "Cavern of Souls", "alice", "battlefield")
    activate(client, game, battlefield_id(client, game, "Cavern of Souls"), 1, color="W")

    assert pool_of(client, game, uid="alice") == pool_of(client, game, uid="bob")
    assert pool_of(client, game, uid="bob")["restricted"][0]["amounts"] == {"W": 1}


# -- the abilities ----------------------------------------------------------


def test_a_battlefield_ability_publishes_the_restriction_it_would_attach(client, game):
    """The read-only half one step earlier: before tapping, the ability itself
    says what the mana it makes would be tied to. The unrestricted ability
    beside it says `None`, which is what makes the pair readable at all."""
    add_card(client, game, "Eldrazi Temple", "alice", "battlefield")
    temple = find(state(client, game)["battlefield"], "Eldrazi Temple")

    plain, restricted = temple["abilities"]
    assert plain["restriction"] is None
    assert restricted["restriction"] == {"text": TEMPLE_RESTRICTION, "understood": True}


def test_a_battlefield_ability_reports_an_unread_restriction_too(client, game):
    """Cavern of Souls with no creature type chosen: the ability is offered,
    and the flag says the mana it would make pays for nothing."""
    add_card(client, game, "Cavern of Souls", "alice", "battlefield")
    cavern = find(state(client, game)["battlefield"], "Cavern of Souls")

    assert cavern["abilities"][0]["restriction"] is None
    assert cavern["abilities"][1]["restriction"] == {
        "text": CAVERN_RESTRICTION,
        "understood": False,
    }


def test_an_off_battlefield_ability_carries_the_key(client, game):
    """`list_abilities`' first branch - a card outside the battlefield reads
    `machines.keywords.off_zone_abilities` and renumbers from zero. Angelsong's
    cycling is the only off-zone activated ability in this fixture pool and it
    prints no CR 106.6 sentence, so the assertion is that the key is *there*
    and honestly `None`, not that it is absent."""
    add_card(client, game, "Angelsong", "alice", "hand")
    angelsong = find(state(client, game)["hands"]["alice"], "Angelsong")

    (cycling,) = angelsong["abilities"]
    assert cycling["text"] == "{2}, Discard this card: Draw a card."
    assert "restriction" in cycling
    assert cycling["restriction"] is None


def test_the_intrinsic_mana_fallback_carries_the_restriction_too(client, game, monkeypatch):
    """`list_abilities`' third branch - the printed-mana fallback for a card
    whose computed abilities carry no mana ability at all.

    Nothing in the card pool is shaped like that (the branch's own docstring
    says so), so the only way to reach it is to make `Game.activated_abilities`
    answer with nothing for one permanent. That is exactly the shape the branch
    exists for, and a branch nothing can reach is a branch nobody notices is
    unfilled - so it is reached deliberately here rather than left untested.
    """
    add_card(client, game, "Cavern of Souls", "alice", "battlefield")
    real = Game.activated_abilities
    monkeypatch.setattr(
        Game,
        "activated_abilities",
        lambda self, permanent: (
            [] if permanent.name == "Cavern of Souls" else real(self, permanent)
        ),
    )

    cavern = find(state(client, game)["battlefield"], "Cavern of Souls")
    assert [(a["index"], a["mana"], a["is_mana_ability"]) for a in cavern["abilities"]] == [
        (0, True, True),
        (1, True, True),
    ]
    assert cavern["abilities"][0]["restriction"] is None
    assert cavern["abilities"][1]["restriction"] == {
        "text": CAVERN_RESTRICTION,
        "understood": False,
    }
    # The same branch's colour options, which were silently never reported:
    # the fallback reads `parse_mana_abilities`, whose entries are
    # `ManaAbility` objects with no `.mana` attribute to find.
    assert cavern["abilities"][1]["mana_options"] == ["B", "G", "R", "U", "W"]


def test_a_hand_cards_abilities_stay_hidden_from_the_opponent(client, game):
    """The `redact_view` question this change had to ask: does a restriction on
    a card in a hidden zone leak? It cannot - `redact_view` replaces every hand
    that is not the viewer's own with `[]`, card and all, which is strictly
    more than dropping one key. Asserted over the whole serialised body, the
    way the CR 118.9 keyword seam (commit 3eadbd9) asserted its own."""
    add_card(client, game, "Angelsong", "alice", "hand")

    for params in ({"uid": "bob"}, None):
        response = client.get(f"/api/games/{game}", params=params)
        assert response.status_code == 200, response.text
        assert response.json()["hands"]["alice"] == []
        assert "Angelsong" not in response.text


@pytest.mark.parametrize("name", ["Cavern of Souls", "Delighted Halfling", "Eldrazi Temple"])
def test_every_restricted_source_in_the_pool_reports_its_sentence(client, game, name):
    """Generality, not one card: every printing in this fixture pool that
    prints a CR 106.6 sentence reports it on the ability that prints it, and
    reports nothing on the plain ability beside it. Nothing here is keyed on a
    card name - the sentence comes off the ability's own effect text."""
    add_card(client, game, name, "alice", "battlefield")
    card = find(state(client, game)["battlefield"], name)

    restrictions = [a["restriction"] for a in card["abilities"]]
    assert restrictions[0] is None
    assert restrictions[1] is not None
    assert restrictions[1]["text"].startswith("Spend this mana only to ")
