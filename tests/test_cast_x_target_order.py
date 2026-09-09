"""Postmortem Lunge ("{X}{B/P}", "Return target creature card with mana value
X from your graveyard to the battlefield. It gains haste. Exile it at the
beginning of the next end step." - oracle text verified with `resolve_cards`
in the session that wrote this file) is the reported gap: a seat cast it with
`--x 2`, targeting a mana-value-2 creature card in its own graveyard, and got
refused every time - "illegal target: target creature card needs mana value
0" - even though `--x` was given and even though the no-target case refused
for a different, correct reason (announcement itself works).

**The cause.** `engine_api._drive_cast` only calls `Casting.choose_modes(...,
x_value=req.x_value)` `if req.modes:` - so a non-modal X spell like Postmortem
Lunge, cast with no modes (it has none to give), never has `x_value` set on
the `Casting` at all until `determine_cost` runs, several lines later. But
`choose_targets`, which reads `Casting.targeting` - and that property's own
docstring says outright "X is announced before targets, so it is always known
here" (CR 107.3a/CR 601.2b/601.2c order) - runs *before* `determine_cost`.
So `self.x_value` is still its unset default (0) at the exact moment targets
are checked, no matter what `x_value` the request carried: every non-modal X
spell's targeting was silently checked against X=0.

This is the same defect shape as the `activate` gap this same session already
fixed (`test_activate_x_cost.py`) - a seat's chosen X silently not reaching
the moment that needed it - but the other end of the pipeline: `cast`
already exposed `--x`/`x_value` on every seat surface, so this was never a
missing parameter. It was `engine_api._drive_cast` reading it too late for a
spell with no modes to carry it.

The fix: announce `x_value` before targets whenever one was given, whether or
not the spell is modal - `Casting.choose_modes([], x_value=...)` is exactly
how a non-modal spell already announces X (CR 700.2a routes X's announcement
through the same call a modal spell's mode choice uses), so no new engine
call is needed, only calling the one that already existed at the right
moment.

An activated ability never showed this symptom: `Game.activate_ability` sets
`x_value` and `targets` in one call, so there is no two-phase window for the
order to matter - the window exists only because a `Casting` is built up over
several calls.

Every card here is a real Scryfall printing loaded through `conftest.add_card`
(Postmortem Lunge copied in from a prior session's `.scryfall-cache` capture
of the live game that reported this bug); no mocks in this file's
board-building."""

from __future__ import annotations

from conftest import add_card, find, state


def stock_lunge_board(client, game_id: str) -> dict[str, str]:
    """alice: Postmortem Lunge in hand, Grizzly Bears (mana value 2) in her
    graveyard, and three untapped Swamps for `{X}{B/P}` at X=2 (2 generic
    plus the {B/P} leg paid in black)."""
    add_card(client, game_id, "Postmortem Lunge", "alice", "hand")
    add_card(client, game_id, "Grizzly Bears", "alice", "graveyard")
    add_card(client, game_id, "Swamp", "alice", "battlefield")
    add_card(client, game_id, "Swamp", "alice", "battlefield")
    add_card(client, game_id, "Swamp", "alice", "battlefield")
    board = state(client, game_id)
    return {
        "lunge_id": find(board["hands"]["alice"], "Postmortem Lunge")["id"],
        "bears_id": find(board["graveyards"]["alice"], "Grizzly Bears")["id"],
        "swamps": [c["id"] for c in board["battlefield"] if c["name"] == "Swamp"],
    }


def test_postmortem_lunge_cast_for_x_2_at_a_mana_value_2_creature_succeeds(client, game):
    """The exact reported case, driven through this repo's own cast path (the
    engine harness already passes this - `../magic-cards-model`'s own
    `tests/scenarios/test_postmortem_lunge.py` - so a regression here is this
    repo's own plumbing, not the engine)."""
    rig = stock_lunge_board(client, game)

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": rig["lunge_id"],
            "x_value": 2,
            "targets": [rig["bears_id"]],
            "mana_sources": rig["swamps"],
        },
    )
    body = response.json()
    assert body["allowed"] is True, body

    mid = state(client, game)
    assert mid["stack"][0]["kind"] == "spell"
    assert mid["stack"][0]["name"] == "Postmortem Lunge"

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"]

    final = state(client, game)
    assert final["stack"] == []
    assert any(c["name"] == "Grizzly Bears" for c in final["battlefield"]), (
        "X=2 must permit returning a mana value 2 creature card"
    )
    assert all(c["name"] != "Grizzly Bears" for c in final["graveyards"]["alice"])


def test_postmortem_lunge_cast_for_x_5_at_a_mana_value_5_creature_also_succeeds(client, game):
    """A second X, the other value the reported seat tried - "mana value X",
    not "X or less" (Fiend Artisan's own wording, and a different card), so
    this needs its own mana-value-5 target rather than reusing Grizzly Bears:
    Elder Gargaroth, in a second graveyard slot alongside it."""
    rig = stock_lunge_board(client, game)
    add_card(client, game, "Elder Gargaroth", "alice", "graveyard")
    # Three more Swamps, since X=5 needs 5 generic plus the {B/P} leg.
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Swamp", "alice", "battlefield")
    add_card(client, game, "Swamp", "alice", "battlefield")
    board = state(client, game)
    swamps = [c["id"] for c in board["battlefield"] if c["name"] == "Swamp"]
    gargaroth_id = find(board["graveyards"]["alice"], "Elder Gargaroth")["id"]

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": rig["lunge_id"],
            "x_value": 5,
            "targets": [gargaroth_id],
            "mana_sources": swamps,
        },
    )
    body = response.json()
    assert body["allowed"] is True, body


def test_postmortem_lunge_cast_with_no_x_still_refuses_for_the_right_reason(client, game):
    """The no-target-announced case already refused correctly before this fix
    (per the report); this pins that it still does, so the fix does not paper
    over a genuinely illegal target."""
    rig = stock_lunge_board(client, game)

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": rig["lunge_id"],
            "targets": [rig["bears_id"]],
            "mana_sources": rig["swamps"],
        },
    )
    body = response.json()
    assert body["allowed"] is False
    assert "mana value" in body["reason"].lower()
