"""Naming *which* mana ability a payment taps a source for.

`mana_sources` used to offer two shapes and neither could name an ability: a
bare card id ("this source, engine picks the colour") and `{card_id, colors}`
("this source, this colour"). Both are fine for a land with one mana ability.
They are not fine for a source that prints two - Eldrazi Temple, Cavern of
Souls, Delighted Halfling and Sunken Citadel each pair a plain `{T}: Add ...`
with a second, CR 106.6-restricted one - because nothing in the payload says
which of the two the player meant.

`ManaSource.ability_index` says it, in the same index space `AbilityView.index`
and `ActivateRequest.ability_index` already use, and `engine_api.pay_with`
then hands `CostPayment.tap_for_mana` the restriction **explicitly** rather
than letting it re-derive one from what the mana happens to be
(`restriction_for_production`).

That is a correctness point, and the case that proves it is **Sunken
Citadel**, not the tie between a restricted and an unrestricted ability that
could each have made the same mana. There is no such tie in this pool: "add
one mana of any color" is CR 106.1a's five colours, and colorless is a type of
mana that is not a color at all (CR 106.1b), so Cavern of Souls' and Delighted
Halfling's `{C}` can only ever have come from their plain ability and
inference gets both of them right. Sunken Citadel's two abilities both read
"the chosen color" - `restriction_for_production` asks `ManaAbility.
effective()` with no board, "the chosen color" narrows to nothing without one,
neither ability matches, and mana that may only be spent on abilities of land
sources is reported as carrying no restriction at all.
`test_the_named_abilitys_restriction_beats_what_inference_would_have_said`
below is that divergence, both answers taken off the same board.

Backward compatibility is the other half and is not optional: a bare uuid and
a `{card_id, colors}` entry with no index must hit exactly the branches they
hit before, and `ability_index=None` must mean today's behaviour precisely.
"""

from __future__ import annotations

from uuid import UUID

from conftest import add_card, find, state
from magic_cards_model.models.card_state import Choice, ChoiceKind

from magic_cards_model_api.store import store

HALFLING_RESTRICTION = (
    "Spend this mana only to cast a legendary spell, and that spell can't be countered."
)
CITADEL_RESTRICTION = "Spend this mana only to activate abilities of land sources."


def battlefield_id(client, game, name):
    return find(state(client, game)["battlefield"], name)["id"]


def cast(client, game, card, sources, **extra):
    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": card, "mana_sources": sources, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


def wake_up(client, game, card_id):
    """CR 302.6: a creature tapped for mana has to have been around since the
    turn began. Delighted Halfling is a creature, so every test that taps one
    clears the flag the same way `test_flow.py` and `test_autopass.py` do."""
    response = client.patch(f"/api/games/{game}/cards/{card_id}", json={"summoning_sick": False})
    assert response.status_code == 200, response.text


# -- backward compatibility -------------------------------------------------


def test_a_bare_uuid_source_pays_exactly_as_it_did(client, game):
    """The oldest shape in the payload, and the one `seat_cli` sends by
    default: a card id and nothing else. It must still reach `auto_tap`."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    mountain = battlefield_id(client, game, "Mountain")

    body = cast(client, game, "Shock", [mountain], targets=["bob"])
    assert body["allowed"] is True, body
    assert find(state(client, game)["battlefield"], "Mountain")["tapped"] is True


def test_a_card_id_and_colors_entry_pays_exactly_as_it_did(client, game):
    """The explicit-colour branch, which until now had **no** server-side test
    at all - every payload test in this suite sends bare ids, and the only
    coverage of `mana_colors` was through the UI's own fixtures.

    Sacred Foundry can make either {R} or {W}, so the colour named is the whole
    question: naming {R} pays for Shock and naming {W} does not, which is what
    proves the caller's choice reached `tap_for_mana` rather than being
    re-solved by `auto_tap`.
    """
    add_card(client, game, "Sacred Foundry", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    foundry = battlefield_id(client, game, "Sacred Foundry")

    refused = cast(
        client,
        game,
        "Shock",
        [{"card_id": foundry, "colors": ["W"]}],
        targets=["bob"],
        dry_run=True,
    )
    assert refused["allowed"] is False
    assert "{R}" in refused["reason"], refused

    allowed = cast(client, game, "Shock", [{"card_id": foundry, "colors": ["R"]}], targets=["bob"])
    assert allowed["allowed"] is True, allowed
    assert find(state(client, game)["battlefield"], "Sacred Foundry")["tapped"] is True


def test_an_explicit_null_ability_index_is_todays_behaviour(client, game):
    """`ability_index=None` is spelled out in the payload here rather than
    omitted, because "absent" and "explicitly null" must not diverge: both mean
    "do what you did before"."""
    add_card(client, game, "Sacred Foundry", "alice", "battlefield")
    add_card(client, game, "Shock", "alice", "hand")
    foundry = battlefield_id(client, game, "Sacred Foundry")

    body = cast(
        client,
        game,
        "Shock",
        [{"card_id": foundry, "colors": ["R"], "ability_index": None}],
        targets=["bob"],
    )
    assert body["allowed"] is True, body


def test_a_bare_uuid_still_reaches_the_unrestricted_ability_of_a_two_ability_source(client, game):
    """Cavern of Souls with no index named: `auto_tap` drops the restricted
    ability (its sentence cannot be read with no creature type chosen, so it
    permits nothing) and taps the plain `{T}: Add {C}.` instead. Sol Ring costs
    `{1}`, which that {C} pays."""
    add_card(client, game, "Cavern of Souls", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "alice", "hand")
    cavern = battlefield_id(client, game, "Cavern of Souls")

    body = cast(client, game, "Sol Ring", [cavern])
    assert body["allowed"] is True, body


# -- naming the ability -----------------------------------------------------


def test_naming_cavern_of_souls_restricted_ability_yields_restricted_mana(client, game):
    """Cavern of Souls, both abilities, one board.

    Index 1 is the restricted one. With no creature type chosen its sentence is
    unreadable, so the engine fails it closed and the mana pays for nothing -
    which is exactly what makes it observable here: the same Sol Ring that the
    plain {C} pays for is refused when the mana came from the restricted
    ability. Index 0 still pays, so the difference is the ability named and
    nothing else.
    """
    add_card(client, game, "Cavern of Souls", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "alice", "hand")
    cavern = battlefield_id(client, game, "Cavern of Souls")

    restricted = cast(
        client,
        game,
        "Sol Ring",
        [{"card_id": cavern, "ability_index": 1, "colors": ["R"]}],
        dry_run=True,
    )
    assert restricted["allowed"] is False, restricted

    plain = cast(client, game, "Sol Ring", [{"card_id": cavern, "ability_index": 0}])
    assert plain["allowed"] is True, plain


def test_naming_delighted_halflings_restricted_ability_yields_restricted_mana(client, game):
    """Delighted Halfling's sentence *is* readable - "a legendary spell" needs
    nothing off the permanent - so this proves the restriction both ways round:
    the named mana pays for Shadowspear (Legendary Artifact) and refuses Sol
    Ring (not legendary), while the unrestricted `{T}: Add {C}.` beside it pays
    for either."""
    add_card(client, game, "Delighted Halfling", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "alice", "hand")
    add_card(client, game, "Shadowspear", "alice", "hand")
    halfling = battlefield_id(client, game, "Delighted Halfling")
    wake_up(client, game, halfling)

    named = {"card_id": halfling, "ability_index": 1, "colors": ["R"]}
    refused = cast(client, game, "Sol Ring", [dict(named)], dry_run=True)
    assert refused["allowed"] is False, refused

    allowed = cast(client, game, "Shadowspear", [dict(named)])
    assert allowed["allowed"] is True, allowed


def test_omitting_the_index_on_delighted_halfling_still_yields_the_plain_mana(client, game):
    """The other half of the pair: with no index, Sol Ring is paid for - the
    restricted ability does not permit it, so `auto_tap` reaches for the
    unrestricted {C} instead."""
    add_card(client, game, "Delighted Halfling", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "alice", "hand")
    halfling = battlefield_id(client, game, "Delighted Halfling")
    wake_up(client, game, halfling)

    body = cast(client, game, "Sol Ring", [halfling])
    assert body["allowed"] is True, body


def test_a_named_fixed_ability_needs_no_colours_at_all(client, game):
    """Eldrazi Temple's second ability adds `{C}{C}` and offers no choice, so
    naming it is enough - the production comes off the ability itself rather
    than off the caller."""
    add_card(client, game, "Eldrazi Temple", "alice", "battlefield")
    add_card(client, game, "Thought-Knot Seer", "alice", "hand")
    add_card(client, game, "Eldrazi Temple", "alice", "battlefield")
    temples = [c["id"] for c in state(client, game)["battlefield"] if c["name"] == "Eldrazi Temple"]

    body = cast(
        client,
        game,
        "Thought-Knot Seer",
        [{"card_id": temples[0], "ability_index": 1}, {"card_id": temples[1], "ability_index": 1}],
        dry_run=True,
    )
    assert body["allowed"] is True, body


# -- refusals, in the engine's own words ------------------------------------


def test_naming_a_choice_ability_with_no_colour_is_refused_not_guessed(client, game):
    """A choice must be *made*, never inferred: picking a colour on the
    player's behalf would put the tie-break back exactly where naming the
    ability was supposed to take it out of. The refusal is the engine's own
    sentence, so a seat learns the rule from the engine."""
    add_card(client, game, "Cavern of Souls", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "alice", "hand")
    cavern = battlefield_id(client, game, "Cavern of Souls")

    body = cast(client, game, "Sol Ring", [{"card_id": cavern, "ability_index": 1}])
    assert body["allowed"] is False
    assert "pass the colour chosen" in body["reason"], body
    assert find(state(client, game)["battlefield"], "Cavern of Souls")["tapped"] is False


def test_naming_a_colour_the_ability_cannot_make_is_refused(client, game):
    """The named ability is the authority on what it produces: Eldrazi Temple's
    first ability adds `{C}` and nothing else, so asking it for {R} is refused
    rather than quietly satisfied by the other ability on the same card."""
    add_card(client, game, "Eldrazi Temple", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "alice", "hand")
    temple = battlefield_id(client, game, "Eldrazi Temple")

    body = cast(
        client, game, "Sol Ring", [{"card_id": temple, "ability_index": 0, "colors": ["R"]}]
    )
    assert body["allowed"] is False
    assert "R" in body["reason"], body


def test_naming_an_index_that_is_not_a_mana_ability_is_refused(client, game):
    """Nurturing Peatland's second ability draws a card; it is a real ability
    at a real index, and it makes no mana."""
    add_card(client, game, "Nurturing Peatland", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "alice", "hand")
    peatland = battlefield_id(client, game, "Nurturing Peatland")

    body = cast(client, game, "Sol Ring", [{"card_id": peatland, "ability_index": 1}])
    assert body["allowed"] is False
    assert "not a mana ability" in body["reason"], body


def test_naming_an_index_the_source_does_not_have_is_refused(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Sol Ring", "alice", "hand")
    mountain = battlefield_id(client, game, "Mountain")

    body = cast(client, game, "Sol Ring", [{"card_id": mountain, "ability_index": 7}])
    assert body["allowed"] is False
    assert "7" in body["reason"], body


# -- the ability index space is the one the view publishes ------------------


def test_the_index_named_here_is_the_index_the_board_view_publishes(client, game):
    """One index space, asserted rather than assumed: the restricted ability's
    `AbilityView.index` is the number `mana_sources` takes, and it is the
    number `ActivateRequest.ability_index` takes too."""
    for name in ("Cavern of Souls", "Eldrazi Temple", "Delighted Halfling"):
        add_card(client, game, name, "alice", "battlefield")

    for card in state(client, game)["battlefield"]:
        restricted = [a for a in card["abilities"] if a["restriction"] is not None]
        assert len(restricted) == 1, card["name"]
        assert restricted[0]["index"] == 1, card["name"]


def test_a_named_ability_pays_alongside_the_older_shapes(client, game):
    """All three entry shapes in one `mana_sources` list, because a real seat
    mixes them: a bare uuid, a `{card_id, colors}` entry, and a named ability.
    Thought-Knot Seer costs `{3}{C}`."""
    add_card(client, game, "Eldrazi Temple", "alice", "battlefield")
    add_card(client, game, "Sacred Foundry", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Thought-Knot Seer", "alice", "hand")
    board = state(client, game)["battlefield"]
    temple = find(board, "Eldrazi Temple")["id"]
    foundry = find(board, "Sacred Foundry")["id"]
    mountain = find(board, "Mountain")["id"]

    body = cast(
        client,
        game,
        "Thought-Knot Seer",
        [
            {"card_id": temple, "ability_index": 1},
            {"card_id": foundry, "colors": ["R"]},
            mountain,
        ],
    )
    assert body["allowed"] is True, body


# -- the divergence the named ability actually exists to close --------------


def mana_ability_index(client, game, name, *, restricted):
    """The `AbilityView.index` of `name`'s restricted (or unrestricted) mana
    ability, read off the board view rather than assumed - the same index
    space `ManaSource.ability_index` takes."""
    card = find(state(client, game)["battlefield"], name)
    found = [
        ability["index"]
        for ability in card["abilities"]
        if ability["is_mana_ability"] and (ability["restriction"] is not None) is restricted
    ]
    assert len(found) == 1, card["abilities"]
    return found[0]


def citadel_with_red_chosen(client, game):
    """Sunken Citadel on the battlefield with CR 614.12a's colour answered.

    `add_card` stages a board rather than playing a land, so no replacement
    effect ran to ask "as it enters, choose a color" - set straight on the
    live engine object, the same reach-past-the-API test setup
    `conftest.force_turn_position` documents.
    """
    add_card(client, game, "Sunken Citadel", "alice", "battlefield")
    citadel = battlefield_id(client, game, "Sunken Citadel")
    live = store.get(game).find_by_id(UUID(citadel))
    live.choice = Choice(kind=ChoiceKind.COLOR, value="R")
    return citadel


def test_the_named_abilitys_restriction_beats_what_inference_would_have_said(client, game):
    """The one board where naming the ability and inferring it disagree, with
    both answers taken from the same tap.

    Left to itself `tap_for_mana` re-derives the CR 106.6 restriction from what
    the mana turned out to be (`machines.cost.restriction_for_production`),
    asking which of the source's `{T}` abilities could have produced it - and
    it asks `ManaAbility.effective()` with **no board at all**. Both of Sunken
    Citadel's abilities read "the chosen color", which board-blind narrows to
    nothing, so neither matches, the match set comes back empty, and inference
    answers "no restriction" for mana that is very much restricted.

    So: the same two red mana, sent twice. Named, the restriction is the one
    printed on the ability the player named, and Sol Ring - a spell, not an
    ability of a land source - cannot be paid for with it. Unnamed, inference
    sees no restriction and pays for Sol Ring quite happily. The second half is
    today's engine-side behaviour rather than an aspiration, and it is exactly
    why this branch passes `restriction=` explicitly instead of letting it be
    guessed.
    """
    citadel = citadel_with_red_chosen(client, game)
    add_card(client, game, "Sol Ring", "alice", "hand")
    restricted = mana_ability_index(client, game, "Sunken Citadel", restricted=True)

    named = cast(
        client,
        game,
        "Sol Ring",
        [{"card_id": citadel, "ability_index": restricted}],
        dry_run=True,
    )
    assert named["allowed"] is False, named
    assert "'R': 2" in named["reason"], named

    inferred = cast(client, game, "Sol Ring", [{"card_id": citadel, "colors": ["R", "R"]}])
    assert inferred["allowed"] is True, inferred


def test_naming_sunken_citadels_restricted_ability_yields_restricted_mana(client, game):
    """The pair, on one board: index 1's two red mana may only be spent on
    abilities of land sources, index 0's one red mana on anything. Sol Ring is
    a spell, so the difference is the ability named and nothing else."""
    citadel = citadel_with_red_chosen(client, game)
    add_card(client, game, "Sol Ring", "alice", "hand")

    restricted = cast(
        client,
        game,
        "Sol Ring",
        [{"card_id": citadel, "ability_index": 1}],
        dry_run=True,
    )
    assert restricted["allowed"] is False, restricted
    assert CITADEL_RESTRICTION in find(
        state(client, game)["battlefield"], "Sunken Citadel"
    )["abilities"][1]["restriction"]["text"]

    plain = cast(client, game, "Sol Ring", [{"card_id": citadel, "ability_index": 0}])
    assert plain["allowed"] is True, plain
