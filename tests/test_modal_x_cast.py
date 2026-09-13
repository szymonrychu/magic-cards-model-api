"""A modal spell whose cost has an {X}, driven the whole way through the API.

The card is Kozilek's Command, `{X}{C}{C}`, "Choose two -" over four bullets.
It is the case where the two hard parts of announcement meet: CR 700.2a has
the controller choose the modes as part of casting, and CR 107.3a has them
choose the value of X as part of casting, and each answer changes what the
other one is allowed to be.

WHAT BROKE. Engine commit 859e540. `Casting` had a branch that read a spell
whose mana cost contains `{X}` as one non-modal blob, and a modal card reached
it before its modes were announced. Reading Kozilek's Command whole counts the
targets of every bullet it has - three, across four modes - for a spell that
will only ever need the targets of the two modes actually chosen. CR 700.2c is
the rule that branch walked past: "If a spell or ability targets one or more
targets only if a particular mode is chosen for it, its controller will need
to choose those targets only if they chose that mode." The demand for three
targets then met CR 601.2c enforcement downstream, which refused the cast
outright - before the caster was ever asked which two modes they wanted. In
the browser that is a modal spell you simply cannot cast.

WHY THIS LIVES IN THE API REPO. The engine has its own scenario cover for the
`Casting` object (`tests/scenarios/test_modal_spells.py`). Nothing pinned the
HTTP chain the browser actually walks, which is four separate answers that all
have to agree:

    card view `cast` hints  ->  affordance (no modes)  ->  affordance (modes,
    X)  ->  POST /actions/cast

`ui/app.js` (and the render modules it drives - `ui/render/ribbon.js`,
`ui/render/board.js`) reads every one of them, so each test below names the
function it is standing behind. A regression in any single link empties a
control in the browser without failing anything else.

AND THE SAME DEFECT AGAIN, ON THIS SIDE. Writing these tests turned up a
second live copy of the bug, in `affordances._profile_for`: the identical
"substitute X into the whole oracle text" branch, equally unguarded against a
modal card, reached by a different door - `x_value` supplied while `modes` is
still empty, which is precisely what the browser sends when a player types X
before clicking their mode chips. That one is pinned by
`test_announcing_x_before_the_modes_still_asks_for_no_targets`, and unlike the
rest of this file it genuinely fails without its fix.

No mocks: real Scryfall printings out of the engine's `tests/fixtures/cards`
pool, a real board, and the real engine underneath the real app.
"""

from __future__ import annotations

from typing import Any

from conftest import add_card, find, state
from fastapi.testclient import TestClient

KOZILEK = "Kozilek's Command"

#: `{X}{C}{C}` with X=1. Three Wastes, because {C} pays the generic {1} too.
WASTES_FOR_X_ONE = 3


def affordances(
    client: TestClient, game: str, card_id: str, player: str = "alice", **params: Any
) -> dict:
    response = client.get(
        f"/api/games/{game}/affordances/{card_id}", params={"player": player, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def board(client: TestClient, game: str) -> dict:
    """Alice holds Kozilek's Command over three Wastes; bob has a Memnite.

    Memnite is the target for the exile mode and is chosen deliberately: it is
    mana value 0, so it stays a legal target for "mana value X or less" even
    at the smallest X worth announcing.
    """
    for _ in range(WASTES_FOR_X_ONE):
        add_card(client, game, "Wastes", "alice", "battlefield")
    add_card(client, game, KOZILEK, "alice", "hand")
    add_card(client, game, "Memnite", "bob", "battlefield")
    return state(client, game)


# -- link 1: the card view's own hints ----------------------------------------


def test_the_card_view_offers_both_an_x_and_every_mode(client, game):
    """The UI contract behind `ui/render/ribbon.js`'s `renderXInput`/
    `renderModeChips`. Both read this one blob off the card view, and neither
    asks the server anything else before drawing its control:

      - `renderXInput` checks `card.cast.needs_x`, and a false here means
        the strip renders no "Announce X" number input at all.
      - `renderModeChips` checks `card.cast.modes`, and a missing block here
        means the strip renders no mode chips at all.

    Either regression is silent: the spell still looks castable, the player
    just has no control to reach the choice with, and the cast goes off with
    a default X of nothing and no modes. So these are asserted as a contract,
    field by field, not merely spot-checked.

    `minimum == maximum == 2` is the parse of CR 700.2's "Choose two -", and
    it is what `toggleMode()` uses to decide when to start evicting an
    already-chosen mode. All four options must be offered even though only
    two may be kept.
    """
    koz = find(board(client, game)["hands"]["alice"], KOZILEK)

    assert koz["mana_cost"] == "{X}{C}{C}"
    hints = koz["cast"]
    assert hints["needs_x"] is True, "no X input would be drawn"

    modes = hints["modes"]
    assert modes is not None, "no mode chips would be drawn"
    assert modes["instruction"] == "Choose two —"
    assert modes["minimum"] == 2
    assert modes["maximum"] == 2
    assert modes["understood"] is True
    assert modes["repeatable"] is False

    # Every bullet, in printed order, numbered from one - `toggleMode()` sends
    # these numbers straight back as the `modes` list.
    assert [option["number"] for option in modes["options"]] == [1, 2, 3, 4]
    assert [option["text"] for option in modes["options"]] == [
        'Target player creates X 0/1 colorless Eldrazi Spawn creature tokens with "Sacrifice'
        ' this token: Add {C}."',
        "Target player scries X, then draws a card.",
        "Exile target creature with mana value X or less.",
        "Exile up to X target cards from graveyards.",
    ]


# -- link 2: the affordance before any mode is chosen -------------------------


def test_no_targets_are_offered_until_the_modes_are_chosen(client, game):
    """CR 700.2c: which targets this spell needs is not a question that has
    an answer yet. Three of the four modes target and they do not agree, so
    the honest reply before a mode is chosen is "not understood", not a
    guess.

    `understood: false` with `targets_anything: false` is what keeps the UI
    honest too: `ui/render/board.js`'s `targetingOpts()` reads this affordance
    and declines to dim the board or open a target picker. The player is asked
    for modes first and only then for targets, which is the order CR 700.2a
    and CR 601.2c put them in.
    """
    koz = find(board(client, game)["hands"]["alice"], KOZILEK)

    answer = affordances(client, game, koz["id"])

    assert answer["understood"] is False
    assert answer["targets_anything"] is False
    assert answer["requirements"] == []
    assert answer["candidates"] == []


# -- link 3: the affordance once they are ------------------------------------


def test_chosen_modes_ask_for_their_own_targets_and_no_others(client, game):
    """Modes 2 and 3 want one target each - a player to scry, a creature to
    exile - so this must be exactly TWO requirements, never three. CR 700.2f
    is why per-mode counting is the only correct reading: "Modal spells and
    abilities may have different targeting requirements for each mode."

    Honest about its own reach: this one does NOT fail if engine commit
    859e540 is reverted. Checked, by reverting `machines/casting.py` to its
    parent and re-running. The API never reaches the branch that commit
    fixed on this path - `affordances._profile_for` answers a modes-supplied
    question through `profile_for_modes`, and `engine_api._drive_cast` calls
    `casting.choose_modes(...)` BEFORE anything reads `casting.targeting`, so
    `Casting`'s pre-modes profile is never consulted. The engine's own
    `tests/scenarios/test_modal_spells.py` is what guards that, and it does
    fail under the revert.

    What this test is really for is the contract itself: two chosen modes,
    two requirements, over the right objects. The API-side copy of the same
    defect is caught by
    `test_announcing_x_before_the_modes_still_asks_for_no_targets` below,
    which does fail without its fix.

    The candidate list is checked as well as the count, because a right count
    over the wrong objects would still put the browser's target picker on the
    wrong cards. Both players are legal for "target player"; only Memnite is
    legal for "target creature with mana value X or less" at X=1 - and it
    being the ONLY creature-slot candidate is what pins the requirement index
    each candidate is offered against, which is how `renderTargetStep()`
    groups the picker.
    """
    cards = board(client, game)
    koz = find(cards["hands"]["alice"], KOZILEK)

    answer = affordances(client, game, koz["id"], modes="2,3", x_value=1)

    assert answer["understood"] is True
    assert answer["targets_anything"] is True
    phrases = [requirement["phrase"] for requirement in answer["requirements"]]
    assert phrases == ["target player", "target creature"], (
        "two chosen modes, two requirements - three is the pre-859e540 bug,"
        " the whole card's targets counted before any mode was picked"
    )
    assert answer["minimum"] == 2
    assert answer["maximum"] == 2

    memnite = find(cards["battlefield"], "Memnite")
    assert {candidate["id"] for candidate in answer["candidates"]} == {
        "alice",
        "bob",
        memnite["id"],
    }
    # Requirement 0 is the player slot, requirement 1 the creature slot.
    by_id = {candidate["id"]: candidate["requirements"] for candidate in answer["candidates"]}
    assert by_id["alice"] == [0]
    assert by_id["bob"] == [0]
    assert by_id[memnite["id"]] == [1]


def test_announcing_x_before_the_modes_still_asks_for_no_targets(client, game):
    """The same defect as engine commit 859e540, in the API's own copy of the
    branch - it lived in `affordances._profile_for` too and outlived the
    engine fix, because the API reaches the branch by a different door.

    This is not a contrived call. It is the browser's, on the ordinary path.
    `ui/app.js` calls `refreshAffordance()` from its ribbon's `onSetX` handler
    just as much as from its `onToggleMode` handler (`actions.js`'s
    `affordanceQuery()` puts `modes` in the query only when
    `action.modes.length` is non-zero). So a player who clicks Kozilek's
    Command and types X BEFORE clicking their two mode chips sends exactly
    this: `x_value` set, `modes` absent.

    The old answer to it was `understood=True` with FOUR requirements and a
    minimum of four - the whole card's four bullets read as one non-modal
    blob with X substituted. `ui/render/board.js`'s `targetingOpts()` believes
    that, so the board dimmed and demanded four targets, graveyard cards among
    them, for a spell that chooses two modes and needs at most two.

    CR 700.2c is what makes `understood=False` the only honest answer here:
    "If a spell or ability targets one or more targets only if a particular
    mode is chosen for it, its controller will need to choose those targets
    only if they chose that mode." Announcing X (CR 107.3a) tells you nothing
    about which modes were chosen, so it cannot unlock the target question.
    """
    koz = find(board(client, game)["hands"]["alice"], KOZILEK)

    answer = affordances(client, game, koz["id"], x_value=1)

    assert answer["understood"] is False, (
        "X is announced, the modes are not - the whole card read as one blob"
        " answered understood=True with four requirements"
    )
    assert answer["targets_anything"] is False
    assert answer["requirements"] == []
    assert answer["candidates"] == []
    assert answer["minimum"] == 0

    # And X is not being ignored - it still governs once the modes arrive.
    with_modes = affordances(client, game, koz["id"], modes="2,3", x_value=1)
    assert with_modes["understood"] is True
    assert with_modes["minimum"] == 2


def test_a_creature_too_expensive_for_the_announced_x_is_not_offered(client, game):
    """X is announced before targets are legal-checked (CR 107.3a, then
    CR 601.2c), so "mana value X or less" is a real filter at announcement -
    and re-announcing X has to move it.

    This is what makes `setXValue()` call `refreshAffordance()` rather than
    just storing the number: at X=0 a mana value 1 creature is not a legal
    target, at X=1 it is, and a picker that did not re-ask would offer a
    target the engine is about to refuse.
    """
    cards = board(client, game)
    add_card(client, game, "Llanowar Elves", "bob", "battlefield")
    koz = find(cards["hands"]["alice"], KOZILEK)
    elves = find(state(client, game)["battlefield"], "Llanowar Elves")
    memnite = find(cards["battlefield"], "Memnite")

    at_zero = affordances(client, game, koz["id"], modes="2,3", x_value=0)
    creatures = {c["id"] for c in at_zero["candidates"] if c["kind"] == "card"}
    assert memnite["id"] in creatures, "Memnite is mana value 0, legal at X=0"
    assert elves["id"] not in creatures, "Llanowar Elves is mana value 1, not X=0 or less"

    at_one = affordances(client, game, koz["id"], modes="2,3", x_value=1)
    creatures = {c["id"] for c in at_one["candidates"] if c["kind"] == "card"}
    assert creatures == {memnite["id"], elves["id"]}


# -- link 4: the cast itself --------------------------------------------------


def test_casting_it_with_two_modes_and_an_x_puts_it_on_the_stack(client, game):
    """End to end, the request `ui/app.js` builds: `modes` and `x_value` in
    the body next to the targets.

    The cost is the proof that X was really announced and really paid, not
    defaulted: `{X}{C}{C}` with X=1 is `{1}{C}{C}`, three mana, which is
    exactly the three Wastes handed over. A cast that quietly took X=0 would
    report `{C}{C}`.
    """
    cards = board(client, game)
    koz = find(cards["hands"]["alice"], KOZILEK)
    memnite = find(cards["battlefield"], "Memnite")
    wastes = [card["id"] for card in cards["battlefield"] if card["name"] == "Wastes"]
    assert len(wastes) == WASTES_FOR_X_ONE

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": koz["id"],
            "modes": [2, 3],
            "x_value": 1,
            "targets": ["alice", memnite["id"]],
            "mana_sources": wastes,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body.pop("log_seq"), int)
    assert sorted(entry["card_id"] for entry in body.pop("tapped")) == sorted(wastes)
    assert body == {
        "allowed": True,
        "reason": None,
        "spell_id": koz["id"],
        "cost": "{1}{C}{C}",
    }

    (spell,) = state(client, game)["stack"]
    assert spell["kind"] == "spell"
    assert spell["name"] == KOZILEK
    assert spell["controller"] == "alice"
    assert spell["targets"] == ["alice", "Memnite"]

    # Every Wastes was tapped to pay for it, and the card left hand for stack.
    board_after = state(client, game)
    assert all(card["tapped"] for card in board_after["battlefield"] if card["name"] == "Wastes")
    assert KOZILEK not in [card["name"] for card in board_after["hands"]["alice"]]
