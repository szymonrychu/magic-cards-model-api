"""`bodyFor` in `ui/actions.js`, exercised for real under Node.

This is the regression guard for the bug that motivated the whole affordances
layer. `POST .../actions/cast` (and `.../actions/activate`) distinguishes
three answers for `targets`:

- no `targets` field at all - the card does not target anything, so the
  question never arises;
- `targets: []` - the card targets, but the slots built from its affordance
  hold nothing, a real and legal answer for an "up to N" requirement
  (CR 115.6);
- `targets: [...]` - these ones.

The old `ui/app.js`'s `sendTargets` had a fourth state: an untouched picker on
a mandatory target also sent no field at all, so the engine would ask rather
than accept an empty announcement it could never make legal. `bodyFor` does
not carry that distinction any more - it always answers `[]` for an unfilled
slot, whatever the requirement's minimum, and leaves "is this legal to fire"
to `readyToFire` (which checks `isSatisfied`, and for an "up to"/unbounded
slot, `done`) instead of baking it into the request body; there is no
`touchedTargets` flag on `Action` at all. This file keeps the old test names
so the regression story - Karn, the Great Creator's `+1` going on the stack
targeting nothing every time it was activated - stays attached to the code
that fixed it, renaming only the handful whose names asserted the old
`undefined` answer where `bodyFor` now answers `[]`.

A click that lands before the affordance does (the candidate fetch is
asynchronous) is not lost either: `chooseTarget` records it in
`pendingTargets` when there are no slots yet, and `withAffordance` replays
those clicks through the normal slot fill once slots exist. That is a
different mechanism from `addFreeTarget`, which is for a targeting profile
the parser could never structure into slots at all (`understood: false`).

Like the old file, this loads and runs the actual shipped JS rather than a
Python re-description of it: `ui/actions.js` has no build step, so this is
the exact module `ui/index.html` imports.
"""

from __future__ import annotations

from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node


def card(
    *, minimum: int, maximum: int | None, anything: bool = True, understood: bool = True
) -> dict[str, Any]:
    """A card whose cast-level targeting hint has the given shape. Used
    whenever a test never fills a slot: `bodyFor` never looks past
    `targets_anything` for those, so no affordance is needed either."""
    return {
        "id": "c1",
        "name": "Test Card",
        "zone": "hand",
        "controller": "alice",
        "type_line": "Instant",
        "cast": {
            "mana_cost": "{1}",
            "needs_x": False,
            "modes": None,
            "targeting": {
                "targets_anything": anything,
                "understood": understood,
                "minimum": minimum,
                "maximum": maximum,
                "requirements": [],
            },
        },
        "abilities": [],
    }


def aff(
    *,
    minimum: int,
    maximum: int | None,
    anything: bool = True,
    requirements: list[dict[str, Any]] | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """An affordance (the shape `GET /affordances` returns) with the given
    per-slot requirements, understood and division-free."""
    return {
        "targets_anything": anything,
        "understood": True,
        "minimum": minimum,
        "maximum": maximum,
        "requirements": requirements or [],
        "division": None,
        "candidates": candidates or [],
    }


def requirement(phrase: str, *, count: int = 1, minimum: int = 1) -> dict[str, Any]:
    return {
        "phrase": phrase,
        "count": count,
        "minimum": minimum,
        "unbounded": False,
        "variable_count": False,
        "mode": None,
        "mode_text": None,
    }


# -- the Karn case: an unfilled "up to one" slot answers [], not nothing ----


def test_an_up_to_one_ability_with_nothing_chosen_sends_an_empty_list() -> None:
    """Karn, the Great Creator's `+1`, exactly as it broke: "up to one target
    noncreature artifact" has `minimum: 0`. `bodyFor` answers `[]` here - an
    unfilled slot on a card that targets is always `[]`, whatever the
    minimum. What kept this from firing before the player had looked is
    `readyToFire`'s "up to"/unbounded check (CR 115.6) - the sibling case
    below, with an exact ("any target") requirement instead of an "up to"
    one, shows the same gate."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', c, {}), affordance); "
        "return [m.readyToFire(a), m.bodyFor(a, 'alice', []).targets]; })()",
        c=card(minimum=0, maximum=1),
        affordance=aff(
            minimum=0, maximum=1, requirements=[requirement("up to one target", minimum=0)]
        ),
    )
    assert result == [False, []]


def test_a_mandatory_target_with_nothing_chosen_sends_an_empty_list() -> None:
    """Lightning Bolt's "any target": `minimum: 1`, nothing chosen yet, and no
    affordance fetched yet either - `bodyFor` falls back to the card's own
    `cast.targeting` hint. It still answers `[]`, the same unfilled-slot
    answer as every other case in this file, since the minimum no longer
    changes what `bodyFor` sends."""
    targets = run_module(
        "actions.js",
        "m.bodyFor(m.begin('cast', c, {}), 'alice', []).targets",
        c=card(minimum=1, maximum=1),
    )
    assert targets == []


# -- a deliberate "none", which is only ever legal at minimum zero ----------


def test_a_touched_picker_left_empty_on_an_up_to_one_ability_sends_an_empty_list() -> None:
    """CR 115.6's real answer: the player opened the picker and chose none.
    There is no "touched" flag on `Action` any more - `withAffordance`
    building empty slots is what this now looks like, and it answers the
    same `[]` whether or not the player ever engaged the picker."""
    targets = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', c, {}), affordance); "
        "return m.bodyFor(a, 'alice', []).targets; })()",
        c=card(minimum=0, maximum=1),
        affordance=aff(
            minimum=0, maximum=1, requirements=[requirement("up to one target", minimum=0)]
        ),
    )
    assert targets == []


def test_a_mandatory_target_left_empty_still_sends_an_empty_list() -> None:
    """A mandatory slot left unfilled: `bodyFor` still answers `[]`, same as
    every other unfilled-slot case in this file now that the mandatory/
    optional distinction lives in `readyToFire`'s `isSatisfied` check rather
    than in what `bodyFor` sends - so this action also fails `readyToFire`,
    which is what stands between this body and a live request."""
    result = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', c, {}), affordance); "
        "return [m.readyToFire(a), m.bodyFor(a, 'alice', []).targets]; })()",
        c=card(minimum=1, maximum=1),
        affordance=aff(minimum=1, maximum=1, requirements=[requirement("any target")]),
    )
    assert result == [False, []]


# -- chosen targets always go ------------------------------------------------


def test_chosen_targets_are_sent_verbatim() -> None:
    chosen = ["11111111-1111-1111-1111-111111111111", "alice"]
    targets = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', c, {}), affordance); "
        "a = m.chooseTarget(a, chosen[0]); a = m.chooseTarget(a, chosen[1]); "
        "return m.bodyFor(a, 'alice', []).targets; })()",
        c=card(minimum=1, maximum=2),
        affordance=aff(
            minimum=1,
            maximum=2,
            requirements=[requirement("any target", count=2)],
            candidates=[
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "kind": "card",
                    "name": "Something",
                    "requirements": [0],
                },
                {"id": "alice", "kind": "player", "name": "alice", "requirements": [0]},
            ],
        ),
        chosen=chosen,
    )
    assert targets == chosen


def test_chosen_targets_are_sent_even_before_the_affordance_arrives() -> None:
    """The candidate fetch is asynchronous; a player who clicked a target
    before it landed must not have that click dropped. `chooseTarget` with no
    slots yet and no affordance records the click in `pendingTargets`, and
    `withAffordance` replays it through the normal slot fill once real slots
    exist - so the eventual body carries it exactly as if the click had
    happened after the fetch landed."""
    targets = run_module(
        "actions.js",
        "(() => { let a = m.chooseTarget(m.begin('cast', c, {}), 'bob'); "
        "a = m.withAffordance(a, affordance); "
        "return m.bodyFor(a, 'alice', []).targets; })()",
        c=card(minimum=1, maximum=1),
        affordance=aff(
            minimum=1,
            maximum=1,
            requirements=[requirement("any target")],
            candidates=[{"id": "bob", "kind": "player", "name": "bob", "requirements": [0]}],
        ),
    )
    assert targets == ["bob"]


def test_an_unreadable_targeting_profile_uses_free_targets_instead_of_slots() -> None:
    """A profile the parser could never structure into slots (`understood:
    false`) has no slots to record a click into at all, so this is
    `addFreeTarget`'s job, not `chooseTarget`'s `pendingTargets` - a
    different escape hatch for a genuinely different reason (the affordance
    exists and says "I can't enumerate this", rather than "hasn't arrived
    yet")."""
    targets = run_module(
        "actions.js",
        "(() => { let a = m.addFreeTarget(m.begin('cast', c, {}), 'alice'); "
        "return m.bodyFor(a, 'alice', []).targets; })()",
        c=card(minimum=0, maximum=None, anything=False, understood=False),
    )
    assert targets == ["alice"]


# -- something that does not target ------------------------------------------


def test_a_non_targeting_ability_sends_no_field() -> None:
    """Karn's `-2` does not target. Sending `targets: []` for it would be
    harmless but meaningless; sending nothing is the honest answer, and it is
    the only case left where `bodyFor` omits the field entirely."""
    body = run_module(
        "actions.js",
        "m.bodyFor(m.begin('cast', c, {}), 'alice', [])",
        c=card(minimum=0, maximum=0, anything=False),
    )
    assert "targets" not in body


def test_a_non_targeting_ability_sends_no_field_even_with_an_affordance_loaded() -> None:
    """Even with an affordance loaded (`targets_anything: False` on it too),
    a non-targeting card still gets no `targets` field."""
    body = run_module(
        "actions.js",
        "(() => { let a = m.withAffordance(m.begin('cast', c, {}), affordance); "
        "return m.bodyFor(a, 'alice', []); })()",
        c=card(minimum=0, maximum=0, anything=False),
        affordance=aff(minimum=0, maximum=0, anything=False),
    )
    assert "targets" not in body
