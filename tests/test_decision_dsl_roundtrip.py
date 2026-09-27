"""FD4: every reachable decision converts to a play that validates, lints
clean and executes - a real `TestClient` game for every action route and
every decision kind this task's requirement names, converted through the
review's own `decision_dsl.decision_rows` (via `GET /api/games/{id}/review`,
the production path) rather than a hand-built ledger.

Every card here is a real Scryfall printing - the api's own cached fixtures
under `tests/fixtures/scryfall/`, or (Bonecrusher Giant, Burst Lightning,
Postmortem Lunge, Urza's Saga) the engine's own bundled printing corpus at
`magic_cards_model/data/printings/` (2606 printings, read-only, no network
touched - see `scryfall.py`'s own module docstring for the tiers). No hand-
written card data, no mocks, matching the api repo's own "ordinary practice"
- a real board, real routes, real engine outcomes.

**`choose_a_quality` naming an actual card** (review finding 5): Pithing
Needle's own "As Pithing Needle enters, name a card" is a real printing in
the engine's own bundled corpus (`magic_cards_model/data/printings/
pithing_needle.json`, alongside Runed Halo/Disruptor Flute/Phyrexian
Revoker's own `ChoiceKind.CARD_NAME` choices), and `test_group_3b_...`
exercises it for real - casting it, naming a Grizzly Bears the board can
actually see, and asserting the converted answer carries `choice_from`
(never the printed name). `tests/test_decision_dsl.py::
test_decide_choose_a_quality_naming_a_card_gives_choice_from` stays the
synthetic-row proof of the same branch's own logic (Meddling Mage's own
"name a nonland card" ETB trigger, which - unlike Pithing Needle's
replacement-effect choice - genuinely has no printing in either fixture
pool); this file's own `choose_a_quality` coverage (Utopia Sprawl's colour,
`test_group_3_...`; Pithing Needle's card name, `test_group_3b_...`) proves
the kind as a whole converts, validates, lints and executes both ways -
G15's "does not resolve to a card" branch and its "does" branch alike.

**Execution.** Every converted play here is also run through the real
`runtime.run_script` (FD3). The one execution assertion still marked
`xfail(strict=True)` says why on its own marker; conversion,
`schema.play_errors` and lint are never behind that marker."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import add_card, find, state
from fastapi.testclient import TestClient
from playscript_testkit import make_root, put
from test_autopass import a_bear_that_can_attack, stock_libraries
from test_battle3_ledger_and_reads import answer_everything, sagas_paused_on_their_chapter_triggers
from test_decision_kinds import stock_library_top
from test_decisions import advance_into_cleanup, stuff_hand
from test_mulligans import (
    LEYLINE_DECK,
    PLAIN_DECK,
    SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    boot,
    declare,
    finish_opening_hand,
)
from test_playscript_runtime import (
    hooked_seat,
    opponent_passes,
    register,
    seat_uid,
    wired,
)

from magic_cards_model_api.playscripts import runtime, schema
from magic_cards_model_api.playscripts.lint import check_report
from magic_cards_model_api.playscripts.runtime import Exit, run_script
from magic_cards_model_api.store import store

REASONING = "FD4 round-trip coverage"


def seats_for(client: TestClient, game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def _review_decisions(
    client: TestClient, game_id: str, admin_key: str, player: str
) -> list[dict[str, Any]]:
    """`GET /api/games/{id}/review`'s own `decisions[player]` - the review's
    own `decision_dsl.decision_rows` conversion, over the real ledger and
    real per-seq frames, exactly as a reviewer reads it."""
    response = client.get(f"/api/games/{game_id}/review", params={"key": admin_key})
    assert response.status_code == 200, response.text
    return response.json()["decisions"][player]


def _end_with_concede(client: TestClient, game_id: str, loser: str = "bob") -> None:
    conceded = client.post(
        f"/api/games/{game_id}/actions/concede",
        json={"player": loser, "reasoning": "handing the round-trip coverage its ending"},
    )
    assert conceded.status_code == 200, conceded.text


def _one(records: list[dict[str, Any]], action: str) -> dict[str, Any]:
    matches = [r for r in records if r["action"] == action]
    assert len(matches) == 1, (action, records)
    return matches[0]


def _play_of(record: dict[str, Any]) -> dict[str, Any]:
    assert record["unexpressible"] is None, record
    assert record["dsl"] is not None, record
    errors = schema.play_errors(record["dsl"])
    assert errors == [], errors
    return record["dsl"]


def _lint_clean(play: dict[str, Any], tmp_path: Path) -> None:
    """The play, alone in a fresh guides root, lints with no error - the
    same gate `mcp-seat plays check`/the corpus admit path applies."""
    root = make_root(tmp_path / play["id"])
    put(root, play)
    errors, _warnings = check_report(root)
    assert errors == [], errors


# ===========================================================================
# Group 1: cast (targets, X, kicker, Adventure, convoke), play_land, activate
# (a sacrifice cost, a mana-colour choice), keyword-action routes.
# ===========================================================================


def test_group_1_cast_play_land_activate_and_keyword_actions(
    client: TestClient, game: str, tmp_path: Path
) -> None:
    game_id = game
    # `game` fixture's own admin key lives in conftest's stash; read it back
    # the same way `state()` does.
    from conftest import _ADMIN_KEYS

    admin_key = _ADMIN_KEYS[game_id]
    uids = seats_for(client, game_id)
    stock_libraries(client, game_id)  # cycling/foretell draw a real card

    # -- cast with real targets: Shock at bob -------------------------------
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Shock", "alice", "hand")
    mountain = find(state(client, game_id)["battlefield"], "Mountain")["id"]
    shock = find(state(client, game_id)["hands"]["alice"], "Shock")["id"]
    cast_shock = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": shock,
            "mana_sources": [mountain],
            "targets": ["bob"],
            "reasoning": REASONING,
        },
    )
    assert cast_shock.json()["allowed"] is True, cast_shock.text
    client.post(f"/api/games/{game_id}/resolve-top")

    # -- cast with X and a target: Postmortem Lunge on a graveyard bear -----
    for _ in range(3):
        add_card(client, game_id, "Swamp", "alice", "battlefield")
    add_card(client, game_id, "Grizzly Bears", "alice", "graveyard")
    add_card(client, game_id, "Postmortem Lunge", "alice", "hand")
    swamps = [c["id"] for c in state(client, game_id)["battlefield"] if c["name"] == "Swamp"]
    bear = find(state(client, game_id)["graveyards"]["alice"], "Grizzly Bears")["id"]
    lunge = find(state(client, game_id)["hands"]["alice"], "Postmortem Lunge")["id"]
    cast_lunge = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": lunge,
            "mana_sources": swamps,
            "x_value": 2,
            "targets": [bear],
            "reasoning": REASONING,
        },
    )
    assert cast_lunge.json()["allowed"] is True, cast_lunge.text
    client.post(f"/api/games/{game_id}/resolve-top")

    # -- cast kicked: Burst Lightning at bob (optional_costs) ---------------
    for _ in range(5):
        add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Burst Lightning", "alice", "hand")
    burst_sources = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Mountain" and not c["tapped"]
    ]
    burst = find(state(client, game_id)["hands"]["alice"], "Burst Lightning")["id"]
    cast_burst = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": burst,
            "mana_sources": burst_sources,
            "targets": ["bob"],
            "optional_costs": [{"keyword": "Kicker"}],
            "reasoning": REASONING,
        },
    )
    assert cast_burst.json()["allowed"] is True, cast_burst.text
    client.post(f"/api/games/{game_id}/resolve-top")

    # -- cast an Adventure face: Bonecrusher Giant's Stomp ------------------
    for _ in range(2):
        add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Bonecrusher Giant", "alice", "hand")
    stomp_sources = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Mountain" and not c["tapped"]
    ]
    giant = find(state(client, game_id)["hands"]["alice"], "Bonecrusher Giant")["id"]
    cast_stomp = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": giant,
            "spell_face": "Stomp",
            "mana_sources": stomp_sources,
            "targets": ["bob"],
            "reasoning": REASONING,
        },
    )
    assert cast_stomp.json()["allowed"] is True, cast_stomp.text
    client.post(f"/api/games/{game_id}/resolve-top")

    # -- cast with convoke: Conclave Tribunal, convoked by a Bear -----------
    add_card(client, game_id, "Grizzly Bears", "alice", "battlefield")
    convoke_bear = find(
        [c for c in state(client, game_id)["battlefield"] if c["name"] == "Grizzly Bears"],
        "Grizzly Bears",
    )["id"]
    client.patch(f"/api/games/{game_id}/cards/{convoke_bear}", json={"summoning_sick": False})
    add_card(client, game_id, "Conclave Tribunal", "alice", "hand")
    add_card(client, game_id, "Watchwolf", "bob", "battlefield")
    watchwolf = find(state(client, game_id)["battlefield"], "Watchwolf")["id"]
    for _ in range(3):
        add_card(client, game_id, "Plains", "alice", "battlefield")
    plains = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Plains" and not c["tapped"]
    ]
    tribunal = find(state(client, game_id)["hands"]["alice"], "Conclave Tribunal")["id"]
    cast_tribunal = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": tribunal,
            "mana_sources": plains,
            "convoke": [{"card_id": convoke_bear}],
            "reasoning": REASONING,
        },
    )
    assert cast_tribunal.json()["allowed"] is True, cast_tribunal.text
    client.post(f"/api/games/{game_id}/resolve-top")
    # The ETB trigger's own `choose_targets` - answered here so the review
    # sees a committing `decide` row, folded into the cast's own move
    # (FD4 (f)/G2) since the cast paused on it.
    client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "targets": [watchwolf], "reasoning": REASONING},
    )
    client.post(f"/api/games/{game_id}/resolve-top")

    # -- play_land -----------------------------------------------------------
    add_card(client, game_id, "Forest", "alice", "hand")
    forest = find(state(client, game_id)["hands"]["alice"], "Forest")["id"]
    played_land = client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "alice", "card_id": forest, "reasoning": REASONING},
    )
    assert played_land.json()["allowed"] is True, played_land.text

    # -- activate with a sacrifice cost: Goblin Bombardment -----------------
    add_card(client, game_id, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, game_id, "Grizzly Bears", "alice", "battlefield")
    bombardment = find(state(client, game_id)["battlefield"], "Goblin Bombardment")["id"]
    fodder = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Grizzly Bears" and c["id"] != convoke_bear
    ][0]
    activate_sac = client.post(
        f"/api/games/{game_id}/actions/activate",
        json={
            "player": "alice",
            "card_id": bombardment,
            "ability_index": 0,
            "sacrificing": [fodder],
            "targets": ["bob"],
            "reasoning": REASONING,
        },
    )
    assert activate_sac.json()["allowed"] is True, activate_sac.text
    client.post(f"/api/games/{game_id}/resolve-top")

    # -- activate with a mana-colour choice: Birds of Paradise --------------
    add_card(client, game_id, "Birds of Paradise", "alice", "battlefield")
    birds = find(state(client, game_id)["battlefield"], "Birds of Paradise")["id"]
    client.patch(f"/api/games/{game_id}/cards/{birds}", json={"summoning_sick": False})
    activate_color = client.post(
        f"/api/games/{game_id}/actions/activate",
        json={
            "player": "alice",
            "card_id": birds,
            "ability_index": 0,
            "color": "G",
            "reasoning": REASONING,
        },
    )
    assert activate_color.json()["allowed"] is True, activate_color.text

    # -- suspend: Living End --------------------------------------------------
    add_card(client, game_id, "Living End", "alice", "hand")
    for _ in range(4):
        add_card(client, game_id, "Swamp", "alice", "battlefield")
    suspend_sources = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Swamp" and not c["tapped"]
    ]
    living_end = find(state(client, game_id)["hands"]["alice"], "Living End")["id"]
    suspended = client.post(
        f"/api/games/{game_id}/actions/suspend",
        json={
            "player": "alice",
            "card_id": living_end,
            "mana_sources": suspend_sources,
            "reasoning": REASONING,
        },
    )
    assert suspended.json()["allowed"] is True, suspended.text

    # -- plot: Slickshot Show-Off ---------------------------------------------
    add_card(client, game_id, "Slickshot Show-Off", "alice", "hand")
    for _ in range(2):
        add_card(client, game_id, "Mountain", "alice", "battlefield")
    plot_sources = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Mountain" and not c["tapped"]
    ]
    show_off = find(state(client, game_id)["hands"]["alice"], "Slickshot Show-Off")["id"]
    plotted = client.post(
        f"/api/games/{game_id}/actions/plot",
        json={
            "player": "alice",
            "card_id": show_off,
            "mana_sources": plot_sources,
            "reasoning": REASONING,
        },
    )
    assert plotted.json()["allowed"] is True, plotted.text

    # -- foretell: Lupine Harbingers ------------------------------------------
    add_card(client, game_id, "Lupine Harbingers", "alice", "hand")
    for _ in range(2):
        add_card(client, game_id, "Forest", "alice", "battlefield")
    foretell_sources = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Forest" and not c["tapped"]
    ]
    harbingers = find(state(client, game_id)["hands"]["alice"], "Lupine Harbingers")["id"]
    foretold = client.post(
        f"/api/games/{game_id}/actions/foretell",
        json={
            "player": "alice",
            "card_id": harbingers,
            "mana_sources": foretell_sources,
            "reasoning": REASONING,
        },
    )
    assert foretold.json()["allowed"] is True, foretold.text

    # -- cycle: Angelsong -------------------------------------------------------
    add_card(client, game_id, "Angelsong", "alice", "hand")
    for _ in range(2):
        add_card(client, game_id, "Plains", "alice", "battlefield")
    cycle_sources = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Plains" and not c["tapped"]
    ]
    angelsong = find(state(client, game_id)["hands"]["alice"], "Angelsong")["id"]
    cycled = client.post(
        f"/api/games/{game_id}/actions/cycle",
        json={
            "player": "alice",
            "card_id": angelsong,
            "ability_index": 0,
            "mana_sources": cycle_sources,
            "reasoning": REASONING,
        },
    )
    assert cycled.json()["allowed"] is True, cycled.text
    client.post(f"/api/games/{game_id}/resolve-top")

    # -- equip: Shadowspear ------------------------------------------------------
    add_card(client, game_id, "Shadowspear", "alice", "battlefield")
    add_card(client, game_id, "Grizzly Bears", "alice", "battlefield")
    spear = find(state(client, game_id)["battlefield"], "Shadowspear")["id"]
    equip_target = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Grizzly Bears" and c["id"] not in (convoke_bear, fodder)
    ][0]
    for _ in range(2):
        add_card(client, game_id, "Mountain", "alice", "battlefield")
    equip_sources = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Mountain" and not c["tapped"]
    ]
    equipped = client.post(
        f"/api/games/{game_id}/actions/equip",
        json={
            "player": "alice",
            "card_id": spear,
            "target": equip_target,
            "mana_sources": equip_sources,
            "reasoning": REASONING,
        },
    )
    assert equipped.json()["allowed"] is True, equipped.text

    # -- crew: Smuggler's Copter --------------------------------------------
    add_card(client, game_id, "Smuggler's Copter", "alice", "battlefield")
    add_card(client, game_id, "Grizzly Bears", "alice", "battlefield")
    copter = find(state(client, game_id)["battlefield"], "Smuggler's Copter")["id"]
    crewer = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Grizzly Bears" and c["id"] not in (convoke_bear, fodder, equip_target)
    ][0]
    crewed = client.post(
        f"/api/games/{game_id}/actions/crew",
        json={"player": "alice", "card_id": copter, "crewing": [crewer], "reasoning": REASONING},
    )
    assert crewed.json()["allowed"] is True, crewed.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")

    # -- assert conversion, schema validity and lint for every kind above ---
    casts = [r for r in decisions if r["action"] == "cast"]
    assert len(casts) == 5, casts
    for record in casts:
        play = _play_of(record)
        _lint_clean(play, tmp_path)
    targeted = [r for r in casts if r["dsl"]["moves"][0].get("targets")]
    assert len(targeted) >= 4  # shock, lunge, burst, stomp - already logged pre-FD5

    # `x_value`/`optional_costs`/`convoke` (cast) and `sacrifice`/`color`
    # (activate) are FD5 part 1's own job to add to the ledger - review
    # finding 4: asserting their *absence* here locked in that gap instead
    # of testing FD4's own requirement. `test_group_1b_...` below asserts
    # the positive shape.
    #
    # Bonecrusher Giant (Stomp) is a genuinely different, permanent case:
    # it is an Adventure card, and the view has no per-face `type_line` for
    # an Adventure card at all (only transform/MDFC cards get one, and
    # `_faces_view` does not break an Adventure card's own two faces out) -
    # see `_add_cast_fields`'s own docstring. That gap is not FD5's to
    # close, so it stays a plain (non-`xfail`) assertion.
    assert all("face" not in r["dsl"]["moves"][0] for r in casts)

    land_record = _one(decisions, "play_land")
    land_play = _play_of(land_record)
    _lint_clean(land_play, tmp_path)

    activates = [r for r in decisions if r["action"] == "activate"]
    assert len(activates) == 2, activates
    for record in activates:
        play = _play_of(record)
        _lint_clean(play, tmp_path)
    for action in ("suspend", "plot", "foretell", "cycle", "equip", "crew"):
        record = _one(decisions, action)
        play = _play_of(record)
        assert play["moves"][0]["action"] == action
        _lint_clean(play, tmp_path)
    assert _one(decisions, "equip")["dsl"]["moves"][0]["target"]["side"] == "me"
    assert _one(decisions, "crew")["dsl"]["moves"][0]["crew_with"]["count"] == 1

    # bob conceded, not alice - his own decisions carry the concede row.
    bob_decisions = _review_decisions(client, game_id, admin_key, "bob")
    concede_record = _one(bob_decisions, "concede")
    assert concede_record["dsl"] is None
    assert concede_record["unexpressible"] == "concede"

    # Every other kind above is expressible - concede is the only exception.
    all_decisions = decisions + bob_decisions
    non_concede = [r for r in all_decisions if r["action"] != "concede"]
    assert all(r["unexpressible"] is None for r in non_concede), [
        (r["action"], r["unexpressible"]) for r in non_concede if r["unexpressible"] is not None
    ]


def test_group_1b_cast_and_activate_rich_fields_once_fd5_logs_them(
    client: TestClient, game: str
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]

    # X spell with a target: Postmortem Lunge on a graveyard bear.
    for _ in range(3):
        add_card(client, game_id, "Swamp", "alice", "battlefield")
    add_card(client, game_id, "Grizzly Bears", "alice", "graveyard")
    add_card(client, game_id, "Postmortem Lunge", "alice", "hand")
    swamps = [c["id"] for c in state(client, game_id)["battlefield"] if c["name"] == "Swamp"]
    bear = find(state(client, game_id)["graveyards"]["alice"], "Grizzly Bears")["id"]
    lunge = find(state(client, game_id)["hands"]["alice"], "Postmortem Lunge")["id"]
    client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": lunge,
            "mana_sources": swamps,
            "x_value": 2,
            "targets": [bear],
            "reasoning": REASONING,
        },
    )
    client.post(f"/api/games/{game_id}/resolve-top")

    # A kicked cast: Burst Lightning at bob (optional_costs).
    for _ in range(5):
        add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Burst Lightning", "alice", "hand")
    burst_sources = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Mountain" and not c["tapped"]
    ]
    burst = find(state(client, game_id)["hands"]["alice"], "Burst Lightning")["id"]
    client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": burst,
            "mana_sources": burst_sources,
            "targets": ["bob"],
            "optional_costs": [{"keyword": "Kicker"}],
            "reasoning": REASONING,
        },
    )
    client.post(f"/api/games/{game_id}/resolve-top")

    # Convoke: Conclave Tribunal, convoked by a Bear.
    add_card(client, game_id, "Grizzly Bears", "alice", "battlefield")
    convoke_bear = find(
        [c for c in state(client, game_id)["battlefield"] if c["name"] == "Grizzly Bears"],
        "Grizzly Bears",
    )["id"]
    client.patch(f"/api/games/{game_id}/cards/{convoke_bear}", json={"summoning_sick": False})
    add_card(client, game_id, "Conclave Tribunal", "alice", "hand")
    for _ in range(3):
        add_card(client, game_id, "Plains", "alice", "battlefield")
    plains = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Plains" and not c["tapped"]
    ]
    tribunal = find(state(client, game_id)["hands"]["alice"], "Conclave Tribunal")["id"]
    client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": tribunal,
            "mana_sources": plains,
            "convoke": [{"card_id": convoke_bear}],
            "reasoning": REASONING,
        },
    )
    client.post(f"/api/games/{game_id}/resolve-top")

    # Activate with a sacrifice cost and a target: Goblin Bombardment.
    add_card(client, game_id, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, game_id, "Grizzly Bears", "alice", "battlefield")
    bombardment = find(state(client, game_id)["battlefield"], "Goblin Bombardment")["id"]
    fodder = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Grizzly Bears" and c["id"] != convoke_bear
    ][0]
    client.post(
        f"/api/games/{game_id}/actions/activate",
        json={
            "player": "alice",
            "card_id": bombardment,
            "ability_index": 0,
            "sacrificing": [fodder],
            "targets": ["bob"],
            "reasoning": REASONING,
        },
    )
    client.post(f"/api/games/{game_id}/resolve-top")

    # Activate with a mana-colour choice: Birds of Paradise.
    add_card(client, game_id, "Birds of Paradise", "alice", "battlefield")
    birds = find(state(client, game_id)["battlefield"], "Birds of Paradise")["id"]
    client.patch(f"/api/games/{game_id}/cards/{birds}", json={"summoning_sick": False})
    client.post(
        f"/api/games/{game_id}/actions/activate",
        json={
            "player": "alice",
            "card_id": birds,
            "ability_index": 0,
            "color": "G",
            "reasoning": REASONING,
        },
    )

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    casts = [r for r in decisions if r["action"] == "cast"]
    activates = [r for r in decisions if r["action"] == "activate"]

    lunge_move = next(r["dsl"]["moves"][0] for r in casts if r["dsl"]["moves"][0].get("x") == 2)
    assert lunge_move["x"] == 2
    burst_move = next(
        r["dsl"]["moves"][0] for r in casts if r["dsl"]["moves"][0].get("optional_costs")
    )
    assert burst_move["optional_costs"] == [{"text_has": "Kicker"}]
    tribunal_move = next(r["dsl"]["moves"][0] for r in casts if r["dsl"]["moves"][0].get("convoke"))
    assert tribunal_move["convoke"]["count"] == 1

    sac_move = next(
        r["dsl"]["moves"][0] for r in activates if r["dsl"]["moves"][0].get("sacrifice")
    )
    assert sac_move["sacrifice"]["count"] == 1
    color_move = next(r["dsl"]["moves"][0] for r in activates if r["dsl"]["moves"][0].get("color"))
    assert color_move["color"] == "G"


# ===========================================================================
# Group 2: combat (declare_attackers, declare_blockers via /decide's own
# `pairs`, assign_combat_damage) and order_triggers.
# ===========================================================================


def test_group_2_combat_and_order_triggers(client: TestClient, game: str, tmp_path: Path) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]

    bear = a_bear_that_can_attack(client, game_id, "alice")
    client.put(f"/api/games/{game_id}/step", json={"step": "beginning_of_combat"})
    uids = seats_for(client, game_id)
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    attacked = client.post(
        f"/api/games/{game_id}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": bear, "defender": "bob"}], "reasoning": REASONING},
    )
    assert attacked.json()["allowed"] is True, attacked.text
    # bob declares no blockers, through the /decide `pairs` route (G12).
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    declined = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["bob"]},
        json={"player": "bob", "pairs": [], "reasoning": REASONING},
    )
    assert declined.json()["accepted"] is True, declined.text

    _end_with_concede(client, game_id, loser="bob")
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    attack_record = _one(decisions, "declare_attackers")
    attack_play = _play_of(attack_record)
    _lint_clean(attack_play, tmp_path)
    assert attack_play["moves"][0]["attackers"][0]["count"] == 1

    # bob's own declare_blockers reached the ledger through /decide's own
    # `pairs` field (FD4 (g)) - its record's own `action` is still "decide"
    # (the raw ledger action never changes), so it is picked out by its
    # move's own kind, the same way the other decide-sourced kinds below are.
    bob_decisions = _review_decisions(client, game_id, admin_key, "bob")
    (block_record,) = [
        r
        for r in bob_decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0]["kind"] == "declare_blockers"
    ]
    block_play = _play_of(block_record)
    _lint_clean(block_play, tmp_path)
    assert block_play["moves"] == [{"kind": "declare_blockers", "blocks": []}]


def test_group_2_assign_combat_damage(client: TestClient, game: str, tmp_path: Path) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    harbingers = add_card(client, game_id, "Lupine Harbingers", "alice", "battlefield")["added"][
        "card_id"
    ]
    bears_id = add_card(client, game_id, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    mystic_id = add_card(client, game_id, "Elvish Mystic", "bob", "battlefield")["added"]["card_id"]
    uids = seats_for(client, game_id)

    client.put(f"/api/games/{game_id}/step", json={"step": "beginning_of_combat"})
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    client.post(
        f"/api/games/{game_id}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": harbingers, "defender": "bob"}], "reasoning": REASONING},
    )
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    client.post(
        f"/api/games/{game_id}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={
            "player": "bob",
            "blocks": [
                {"blocker": bears_id, "blocked": harbingers},
                {"blocker": mystic_id, "blocked": harbingers},
            ],
            "reasoning": REASONING,
        },
    )
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    assigned = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "damage": [
                {"target": bears_id, "amount": 2},
                {"target": mystic_id, "amount": 1},
                {"target": "bob", "amount": 1},
            ],
            "reasoning": REASONING,
        },
    )
    assert assigned.json()["accepted"] is True, assigned.text

    _end_with_concede(client, game_id, loser="bob")
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    damage_records = [
        r
        for r in decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0]["answers"][0]["decision"] == "assign_combat_damage"
    ]
    assert len(damage_records) == 1, decisions
    play = _play_of(damage_records[0])
    _lint_clean(play, tmp_path)
    answer = play["moves"][0]["answers"][0]
    assert len(answer["damage"]["tiers"]) == 2
    assert answer["damage"]["rest_to"] == "player"


def test_group_2_order_triggers(client: TestClient, game: str, tmp_path: Path) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    sagas_paused_on_their_chapter_triggers(client, game_id)
    answer_everything(client, game_id, "alice")

    _end_with_concede(client, game_id, loser="bob")
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    order_records = [
        r
        for r in decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0]["answers"][0]["decision"] == "order_triggers"
    ]
    assert len(order_records) == 1, decisions
    play = _play_of(order_records[0])
    _lint_clean(play, tmp_path)
    assert len(play["moves"][0]["answers"][0]["order"]) == 2


# ===========================================================================
# Group 3: scry, cleanup discard, a name-a-quality choice (colour, not a
# card - see the module docstring), a real "choose zero" (surveil).
# ===========================================================================


def test_group_3_scry_cleanup_discard_quality_and_choose_zero(
    client: TestClient, game: str, tmp_path: Path
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    uids = seats_for(client, game_id)

    # -- scry: Preordain -------------------------------------------------------
    add_card(client, game_id, "Island", "alice", "battlefield")
    add_card(client, game_id, "Preordain", "alice", "hand")
    stock_library_top(client, game_id, "alice", ["Mountain", "Forest", "Grizzly Bears"])
    island = find(state(client, game_id)["battlefield"], "Island")["id"]
    preordain = find(state(client, game_id)["hands"]["alice"], "Preordain")["id"]
    cast_pre = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": preordain,
            "mana_sources": [island],
            "reasoning": REASONING,
        },
    )
    assert cast_pre.json()["allowed"] is True, cast_pre.text
    resolved = client.post(f"/api/games/{game_id}/resolve-top").json()
    assert resolved["paused"] is True, resolved
    # Keep everything on top, unchanged - the bare answer, needing no board
    # data. A scry that moves specific cards needs their own selectors
    # resolved against the library, and `serialize.frame_view`'s own
    # docstring is explicit that a review frame never carries library
    # contents at all (CR 401.2, "nobody may look at a library, not even
    # their own" - true even omnisciently, post-game): a real ledger row
    # naming specific cards moved by a scry is genuinely unresolvable from
    # any frame this task's review mechanism can ever supply, by a design
    # this task does not own (R2/DR2). `tests/test_decision_dsl.py::
    # test_decide_scry_keep_one_bottom_one_gives_to_bottom_and_top_order` is
    # the synthetic-row proof that `_scry_or_surveil_answer` resolves cards
    # correctly whenever a state view does carry them.
    scryed = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "moved": [], "top_order": [], "reasoning": REASONING},
    )
    assert scryed.json()["accepted"] is True, scryed.text

    # -- choose_a_quality naming a colour: Utopia Sprawl ------------------------
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Utopia Sprawl", "alice", "hand")
    forests = [c["id"] for c in state(client, game_id)["battlefield"] if c["name"] == "Forest"]
    sprawl = find(state(client, game_id)["hands"]["alice"], "Utopia Sprawl")["id"]
    cast_sprawl = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": sprawl,
            "targets": [forests[0]],
            "mana_sources": [forests[1]],
            "reasoning": REASONING,
        },
    )
    assert cast_sprawl.json()["allowed"] is True, cast_sprawl.text
    resolved_sprawl = client.post(f"/api/games/{game_id}/resolve-top").json()
    assert resolved_sprawl["paused"] is True, resolved_sprawl
    quality = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "G", "reasoning": REASONING},
    )
    assert quality.json()["accepted"] is True, quality.text

    # -- choose zero: Contingency Plan's surveil 5, keeping everything --------
    add_card(client, game_id, "Island", "alice", "battlefield")
    add_card(client, game_id, "Island", "alice", "battlefield")
    add_card(client, game_id, "Contingency Plan", "alice", "hand")
    stock_library_top(
        client, game_id, "alice", ["Mountain", "Mountain", "Mountain", "Mountain", "Mountain"]
    )
    islands = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Island" and not c["tapped"]
    ][-2:]
    plan = find(state(client, game_id)["hands"]["alice"], "Contingency Plan")["id"]
    cast_plan = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": plan,
            "mana_sources": islands,
            "reasoning": REASONING,
        },
    )
    assert cast_plan.json()["allowed"] is True, cast_plan.text
    resolved_plan = client.post(f"/api/games/{game_id}/resolve-top").json()
    assert resolved_plan["paused"] is True, resolved_plan
    surveilled = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "moved": [], "top_order": [], "reasoning": REASONING},
    )
    assert surveilled.json()["accepted"] is True, surveilled.text

    # -- cleanup discard ---------------------------------------------------------
    stuff_hand(client, game_id, "alice", 7)  # over the 7-card maximum with 1 to spare
    paused = advance_into_cleanup(game_id)
    assert paused is not None, "the discard should have asked, not defaulted silently"
    # `stuff_hand`'s own 7 Mountains, plus the 1 leftover from the scenario
    # above, put alice exactly 1 over the 7-card maximum.
    hand = state(client, game_id, uid=uids["alice"])["hands"]["alice"]
    mountains = [c["id"] for c in hand if c["name"] == "Mountain"]
    to_discard = mountains[:1]
    discarded = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": to_discard, "reasoning": REASONING},
    )
    assert discarded.json()["accepted"] is True, discarded.text

    _end_with_concede(client, game_id, loser="bob")
    decisions = _review_decisions(client, game_id, admin_key, "alice")

    def _decide_records(decision_kind: str) -> list[dict[str, Any]]:
        return [
            r
            for r in decisions
            if r["action"] == "decide"
            and r["dsl"]
            and r["dsl"]["moves"][0].get("answers")
            and r["dsl"]["moves"][0]["answers"][0]["decision"] == decision_kind
        ]

    (scry_record,) = _decide_records("scry")
    scry_play = _play_of(scry_record)
    _lint_clean(scry_play, tmp_path)

    quality_records = _decide_records("choose_a_quality")
    assert len(quality_records) == 1, decisions
    quality_play = _play_of(quality_records[0])
    _lint_clean(quality_play, tmp_path)
    assert quality_play["moves"][0]["answers"][0] == {"decision": "choose_a_quality", "choice": "G"}

    surveil_records = _decide_records("surveil")
    assert len(surveil_records) == 1, decisions
    surveil_play = _play_of(surveil_records[0])
    _lint_clean(surveil_play, tmp_path)
    assert surveil_play["moves"][0]["answers"][0] == {"decision": "surveil"}

    discard_records = _decide_records("choose_cards")
    assert len(discard_records) == 1, decisions
    discard_play = _play_of(discard_records[0])
    _lint_clean(discard_play, tmp_path)
    assert discard_play["when"]["steps"] == ["cleanup"]


def test_group_3b_choose_a_quality_naming_a_real_card_gives_choice_from(
    client: TestClient, game: str, tmp_path: Path
) -> None:
    # Review finding 5: Pithing Needle's own "As Pithing Needle enters, name
    # a card" (CR 614.12a's own `ChoiceKind.CARD_NAME`, `machines.
    # replacement._choose_a_quality`) is a real printing, in the engine's
    # own bundled corpus (`magic_cards_model/data/printings/pithing_needle.
    # json`) exactly like Bonecrusher Giant/Burst Lightning/Postmortem
    # Lunge/Urza's Saga already are elsewhere in this file - the module
    # docstring's older claim that no such printing exists was wrong.
    # `choice_from` needs the *named* string to itself resolve to a real
    # card in the board this row's frame carries (`_choice_answer`), so a
    # Grizzly Bears is put somewhere visible (alice's own hand) before it is
    # named - naming a card nothing on the board matches would keep the
    # plain `choice`, already covered by `test_group_3_...` above (Utopia
    # Sprawl's colour) and by `tests/test_decision_dsl.py`'s own
    # synthetic-row tests.
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    uids = seats_for(client, game_id)

    add_card(client, game_id, "Grizzly Bears", "alice", "hand")
    for _ in range(2):
        add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Pithing Needle", "alice", "hand")
    sources = [
        c["id"]
        for c in state(client, game_id)["battlefield"]
        if c["name"] == "Mountain" and not c["tapped"]
    ]
    needle = find(state(client, game_id)["hands"]["alice"], "Pithing Needle")["id"]
    cast_needle = client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": needle,
            "mana_sources": sources,
            "reasoning": REASONING,
        },
    )
    assert cast_needle.json()["allowed"] is True, cast_needle.text
    resolved = client.post(f"/api/games/{game_id}/resolve-top").json()
    assert resolved["paused"] is True, resolved
    named = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "Grizzly Bears", "reasoning": REASONING},
    )
    assert named.json()["accepted"] is True, named.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    quality_records = [
        r
        for r in decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0].get("answers")
        and r["dsl"]["moves"][0]["answers"][0]["decision"] == "choose_a_quality"
    ]
    assert len(quality_records) == 1, decisions
    play = _play_of(quality_records[0])
    _lint_clean(play, tmp_path)
    answer = play["moves"][0]["answers"][0]
    assert "choice_from" in answer
    assert "choice" not in answer
    assert "grizzly" not in json.dumps(answer).lower()
    assert "bears" not in json.dumps(answer).lower()


# ===========================================================================
# Group 4: mulligan with bottom, and a CR 103.6 opening-hand action.
# ===========================================================================


def test_group_4_mulligan_with_bottom_and_opening_hand_action(
    client: TestClient, tmp_path: Path
) -> None:
    info = boot(
        client,
        alice_deck=LEYLINE_DECK,
        bob_deck=PLAIN_DECK,
        seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND,
    )
    game_id = info["game_id"]
    try:
        # The seed only guarantees a Leyline in alice's own *first* dealt
        # hand (CR 103.5's London mulligan draws a fresh seven on a
        # mulligan, so mulliganing away that hand would lose the guarantee)
        # - alice keeps hers straight away; bob is the one who mulligans
        # once, then keeps and bottoms one card, for the fuller shape.
        alice_keep = declare(client, info, "alice", take=False, reasoning="a fine seven")
        assert alice_keep.status_code == 200, alice_keep.text
        bob_first = declare(client, info, "bob", take=True, reasoning="need more colour")
        assert bob_first.status_code == 200, bob_first.text
        bob_new_hand = client.get(
            f"/api/games/{game_id}", params={"uid": info["seat_uids"]["bob"]}
        ).json()["hands"]["bob"]
        bottom_card = bob_new_hand[0]["id"]
        bob_keep = declare(
            client, info, "bob", take=False, bottom=[bottom_card], reasoning="this one works"
        )
        assert bob_keep.status_code == 200, bob_keep.text

        # alice's opening hand carries a Leyline of the Void (the seed); take
        # its CR 103.6 action.
        alice_hand = client.get(
            f"/api/games/{game_id}", params={"uid": info["seat_uids"]["alice"]}
        ).json()["hands"]["alice"]
        leyline = next(c["id"] for c in alice_hand if c["name"] == "Leyline of the Void")
        acted = client.post(
            f"/api/games/{game_id}/pregame/opening-hand/action",
            params={"uid": info["seat_uids"]["alice"]},
            json={"player": "alice", "card_id": leyline, "reasoning": "free hate on the board"},
        )
        assert acted.status_code == 200, acted.text
        finished_alice = finish_opening_hand(client, info, "alice")
        assert finished_alice.status_code == 200, finished_alice.text
        finished_bob = finish_opening_hand(client, info, "bob")
        assert finished_bob.status_code == 200, finished_bob.text

        conceded = client.post(
            f"/api/games/{game_id}/actions/concede",
            json={"player": "bob", "reasoning": "handing the round-trip coverage its ending"},
        )
        assert conceded.status_code == 200, conceded.text

        decisions = _review_decisions(client, game_id, info["admin_key"], "alice")
        (alice_mulligan,) = [r for r in decisions if r["action"] == "mulligan"]
        alice_move = _play_of(alice_mulligan)["moves"][0]
        _lint_clean(_play_of(alice_mulligan), tmp_path)
        assert alice_move == {"kind": "mulligan", "keep": True}

        bob_decisions = _review_decisions(client, game_id, info["admin_key"], "bob")
        bob_mulligans = [r for r in bob_decisions if r["action"] == "mulligan"]
        # One row per declaration: take=True, then take=False (the keep).
        assert len(bob_mulligans) == 2, bob_decisions
        keep_moves = [_play_of(r)["moves"][0] for r in bob_mulligans]
        _lint_clean(_play_of(bob_mulligans[0]), tmp_path)
        _lint_clean(_play_of(bob_mulligans[1]), tmp_path)
        assert {m["keep"] for m in keep_moves} == {True, False}

        opening_record = _one(decisions, "opening_hand_action")
        opening_play = _play_of(opening_record)
        _lint_clean(opening_play, tmp_path)
        assert opening_play["when"]["steps"] == ["pregame_actions"]
        assert opening_play["moves"][0]["answers"][0] == {
            "decision": "opening_hand_action",
            "accept": True,
        }
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# ===========================================================================
# Execution: restored to the pre-action board, through `runtime.run_script`.
# ===========================================================================


def _register_and_run(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    game_id: str,
    player: str,
    play: dict[str, Any],
) -> runtime.RunResult:
    uid = seat_uid(game_id, player)
    register(client, game_id, uid, play)
    seats = wired(monkeypatch, game_id)
    seat, _hook = hooked_seat(seats, uid)
    with opponent_passes(game_id, "bob" if player == "alice" else "alice"):
        return run_script(
            seat, script_id=play["id"], reasoning=REASONING, corpora=seats.script_corpora
        )


def test_a_converted_cast_with_targets_executes_to_the_same_outcome(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FD3-free proof: `cast`'s own targets/mana-auto shape is already
    something `runtime.cast_body` supports - this one needs no FD3 gap at
    all, so it executes for real today."""
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    add_card(client, game_id, "Shock", "alice", "hand")
    mountain = find(state(client, game_id)["battlefield"], "Mountain")["id"]
    shock = find(state(client, game_id)["hands"]["alice"], "Shock")["id"]
    client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": shock,
            "mana_sources": [mountain],
            "targets": ["bob"],
            "reasoning": REASONING,
        },
    )
    client.post(f"/api/games/{game_id}/resolve-top")
    _end_with_concede(client, game_id)

    decisions = _review_decisions(client, game_id, admin_key, "alice")
    play = _play_of(_one(decisions, "cast"))

    # A fresh, identical board (the pre-action one) to run the converted
    # play against - never the same game the manual cast already committed.
    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    try:
        add_card(client, game2, "Mountain", "alice", "battlefield")
        add_card(client, game2, "Shock", "alice", "hand")
        before = {p["username"]: p["life"] for p in state(client, game2)["players"]}
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        client.post(f"/api/games/{game2}/resolve-top")
        after = {p["username"]: p["life"] for p in state(client, game2)["players"]}
        assert after["bob"] == before["bob"] - 2
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_suspend_executes_once_fd3_lands(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    add_card(client, game_id, "Living End", "alice", "hand")
    for _ in range(4):
        add_card(client, game_id, "Swamp", "alice", "battlefield")
    sources = [c["id"] for c in state(client, game_id)["battlefield"]]
    living_end = find(state(client, game_id)["hands"]["alice"], "Living End")["id"]
    client.post(
        f"/api/games/{game_id}/actions/suspend",
        json={
            "player": "alice",
            "card_id": living_end,
            "mana_sources": sources,
            "reasoning": REASONING,
        },
    )
    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    play = _play_of(_one(decisions, "suspend"))

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    try:
        add_card(client, game2, "Living End", "alice", "hand")
        for _ in range(4):
            add_card(client, game2, "Swamp", "alice", "battlefield")
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        assert result.script_exit.exit is Exit.CHAIN_COMPLETE
        after = state(client, game2)
        assert any(c["name"] == "Living End" for c in after["exile"])
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_mulligan_move_executes_once_fd3_lands(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The play itself: `_mulligan_declared_play`'s own converted shape for
    a plain keep (`tests/test_decision_dsl.py::
    test_mulligan_declared_keep_gives_a_mulligan_move` proves this is what a
    real `mulligan_declared` row converts to). Built directly rather than
    read back from a live game's own review, so this test's own point stays
    the runtime executor alone (`test_group_4_mulligan_with_bottom_and_
    opening_hand_action` above covers the live row)."""
    play = {
        "schema_version": 1,
        "id": "decision-mulligan",
        "name": "decision-mulligan",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "either", "steps": ["mulligan"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [{"kind": "mulligan", "keep": True}],
        "next": [],
    }
    info = boot(client, alice_deck=PLAIN_DECK, bob_deck=PLAIN_DECK, seed=1)
    game_id = info["game_id"]
    try:
        uid = info["seat_uids"]["alice"]
        register(client, game_id, uid, play)
        seats = wired(monkeypatch, game_id)
        seat, _hook = hooked_seat(seats, uid)
        result = run_script(
            seat, script_id=play["id"], reasoning=REASONING, corpora=seats.script_corpora
        )
        assert result.finished is True, result.to_dict()
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# -- review finding 3: activate/play_land/pass/combat declarations and every -----
# -- decide answer kind each get their own execution assertion, real or ----------
# -- xfail(strict) - never left with none at all ---------------------------------


def test_a_converted_play_land_executes_to_the_same_outcome(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    add_card(client, game_id, "Forest", "alice", "hand")
    forest = find(state(client, game_id)["hands"]["alice"], "Forest")["id"]
    played = client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "alice", "card_id": forest, "reasoning": REASONING},
    )
    assert played.json()["allowed"] is True, played.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    play = _play_of(_one(decisions, "play_land"))

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    try:
        add_card(client, game2, "Forest", "alice", "hand")
        before = len(state(client, game2)["battlefield"])
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        after = state(client, game2)["battlefield"]
        assert len(after) == before + 1
        assert any(c["name"] == "Forest" for c in after)
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_activate_executes_to_the_same_outcome(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain `{T}: Add {G}.` mana ability - no sacrifice cost, no color
    # choice, no target - none of `sacrificing`/`color` (FD5 part 1, review
    # finding 4) is needed to convert or replay this one, so it proves the
    # activate move's own execution wiring for real, on today's ledger.
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    add_card(client, game_id, "Forest", "alice", "battlefield")
    forest = find(state(client, game_id)["battlefield"], "Forest")["id"]
    activated = client.post(
        f"/api/games/{game_id}/actions/activate",
        json={
            "player": "alice",
            "card_id": forest,
            "ability_index": 0,
            "reasoning": REASONING,
        },
    )
    assert activated.json()["allowed"] is True, activated.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    play = _play_of(_one(decisions, "activate"))

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    try:
        add_card(client, game2, "Forest", "alice", "battlefield")
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        after = find(state(client, game2)["battlefield"], "Forest")
        assert after["tapped"] is True
    finally:
        client.delete(f"/api/games/{game2}")


def _pass_priority_to(client: TestClient, game_id: str, active: str) -> None:
    """Pass priority for real, seat by seat, until `active` holds it in one
    of their own main phases (CR 505.1a/b - the only steps a land can be
    played in, which is all this helper's own caller needs) - answering any
    `declare_attackers`/`declare_blockers` pause met on the way with an
    empty (always legal) declaration, exactly like `advance_into_cleanup`
    does. Polls the *live*
    board (`GET .../state`), never the ledger: a ledger row's own `step` is
    the pre-action step and its own `active_player` can be the post-action
    one (exactly the split G16 is about), so a row that itself ends a turn
    can read `step="end_step", active_player=active` simultaneously and stop
    this loop one iteration too early. Used only by FD4-r1's own
    turn-boundary test below; every other test in this file that needs to
    cross a turn plays through `advance_into_cleanup` instead, which this
    module already imports."""
    for _ in range(200):
        board = state(client, game_id)
        if board["active_player"] == active and board["step"] in (
            "precombat_main",
            "postcombat_main",
        ):
            return
        holder = board.get("priority_holder") or "alice"
        response = client.post(
            f"/api/games/{game_id}/actions/pass-priority",
            json={"player": holder, "reasoning": REASONING},
        )
        body = response.json()
        pending = body.get("pending_decision") if body.get("paused") else None
        if pending is None:
            continue
        if pending["kind"] == "declare_attackers":
            uid = seats_for(client, game_id)[pending["player"]]
            client.post(
                f"/api/games/{game_id}/actions/attack",
                params={"uid": uid},
                json={"attackers": [], "reasoning": REASONING},
            )
        elif pending["kind"] == "declare_blockers":
            uid = seats_for(client, game_id)[pending["player"]]
            client.post(
                f"/api/games/{game_id}/actions/declare-blockers",
                params={"uid": uid},
                json={"player": pending["player"], "blocks": [], "reasoning": REASONING},
            )
        else:  # pragma: no cover - not reached on this bare board
            raise AssertionError(f"unexpected pending decision: {body}")
    raise AssertionError(f"never reached {active}'s own turn")  # pragma: no cover


def test_own_first_action_of_a_new_turn_reads_whose_turn_mine(
    client: TestClient, game: str
) -> None:
    """FD4-r1/G16: over a *real* ledger (`GET .../review`'s own production
    path, not a hand-built row), bob's own first action of his brand new
    turn reads `when.whose_turn == "mine"` - the carried-forward
    `active_player` approximation used to apply uniformly to every action
    kind rather than only to the `pass` whose own action can change it, and
    would misread this `play_land` as "theirs" instead."""
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    stock_libraries(client, game_id, cards=10)
    add_card(client, game_id, "Forest", "bob", "hand")

    _pass_priority_to(client, game_id, "bob")
    forest = find(state(client, game_id)["hands"]["bob"], "Forest")["id"]
    played_land = client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "bob", "card_id": forest, "reasoning": REASONING},
    )
    assert played_land.json()["allowed"] is True, played_land.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "bob")
    play = _play_of(_one(decisions, "play_land"))
    assert play["when"]["whose_turn"] == "mine"


def test_a_converted_pass_executes_to_the_same_outcome(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    passed = client.post(
        f"/api/games/{game_id}/actions/pass-priority",
        json={"player": "alice", "reasoning": REASONING},
    )
    assert passed.json()["allowed"] is True, passed.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    play = _play_of(_one(decisions, "pass"))

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    try:
        before = state(client, game2)["battlefield"]
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
        assert state(client, game2)["battlefield"] == before
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_declare_attackers_executes_to_the_same_outcome(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    bear = a_bear_that_can_attack(client, game_id, "alice")
    client.put(f"/api/games/{game_id}/step", json={"step": "beginning_of_combat"})
    uids = seats_for(client, game_id)
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    attacked = client.post(
        f"/api/games/{game_id}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": bear, "defender": "bob"}], "reasoning": REASONING},
    )
    assert attacked.json()["allowed"] is True, attacked.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    play = _play_of(_one(decisions, "declare_attackers"))

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    try:
        a_bear_that_can_attack(client, game2, "alice")
        client.put(f"/api/games/{game2}/step", json={"step": "beginning_of_combat"})
        uids2 = seats_for(client, game2)
        client.post(
            f"/api/games/{game2}/advance",
            params={"uid": uids2["alice"]},
            json={"player": "alice"},
        )
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        after = find(state(client, game2)["battlefield"], "Grizzly Bears")
        assert after["tapped"] is True  # CR 508.1f - declared an attacker, no vigilance
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_declare_blockers_executes_to_the_same_outcome(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    bear = a_bear_that_can_attack(client, game_id, "alice")
    client.put(f"/api/games/{game_id}/step", json={"step": "beginning_of_combat"})
    uids = seats_for(client, game_id)
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    client.post(
        f"/api/games/{game_id}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": bear, "defender": "bob"}], "reasoning": REASONING},
    )
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    declined = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["bob"]},
        json={"player": "bob", "pairs": [], "reasoning": REASONING},
    )
    assert declined.json()["accepted"] is True, declined.text

    _end_with_concede(client, game_id)
    bob_decisions = _review_decisions(client, game_id, admin_key, "bob")
    (block_record,) = [
        r
        for r in bob_decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0]["kind"] == "declare_blockers"
    ]
    play = _play_of(block_record)

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    try:
        bear2 = a_bear_that_can_attack(client, game2, "alice")
        client.put(f"/api/games/{game2}/step", json={"step": "beginning_of_combat"})
        uids2 = seats_for(client, game2)
        # Alice's own attack is driven directly (not backgrounded) - it is
        # what puts bob's own declare_blockers decision on the table at all,
        # which `opponent_passes` alone could never do for her.
        client.post(
            f"/api/games/{game2}/advance",
            params={"uid": uids2["alice"]},
            json={"player": "alice"},
        )
        client.post(
            f"/api/games/{game2}/actions/attack",
            params={"uid": uids2["alice"]},
            json={"attackers": [{"card_id": bear2, "defender": "bob"}], "reasoning": REASONING},
        )
        client.post(
            f"/api/games/{game2}/advance",
            params={"uid": uids2["alice"]},
            json={"player": "alice"},
        )
        uid_bob = seat_uid(game2, "bob")
        register(client, game2, uid_bob, play)
        seats = wired(monkeypatch, game2)
        seat, _hook = hooked_seat(seats, uid_bob)
        with opponent_passes(game2, "alice"):
            result = run_script(
                seat, script_id=play["id"], reasoning=REASONING, corpora=seats.script_corpora
            )
        assert result.finished is True, result.to_dict()
        assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_scry_answer_executes_to_the_same_outcome(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    add_card(client, game_id, "Island", "alice", "battlefield")
    add_card(client, game_id, "Preordain", "alice", "hand")
    stock_library_top(client, game_id, "alice", ["Mountain", "Forest", "Grizzly Bears"])
    island = find(state(client, game_id)["battlefield"], "Island")["id"]
    preordain = find(state(client, game_id)["hands"]["alice"], "Preordain")["id"]
    client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": preordain,
            "mana_sources": [island],
            "reasoning": REASONING,
        },
    )
    client.post(f"/api/games/{game_id}/resolve-top")
    uids = seats_for(client, game_id)
    scryed = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "moved": [], "top_order": [], "reasoning": REASONING},
    )
    assert scryed.json()["accepted"] is True, scryed.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    (scry_record,) = [
        r
        for r in decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0].get("answers")
        and r["dsl"]["moves"][0]["answers"][0]["decision"] == "scry"
    ]
    play = _play_of(scry_record)

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    admin_key2 = second.json()["admin_key"]
    try:
        add_card(client, game2, "Island", "alice", "battlefield")
        add_card(client, game2, "Preordain", "alice", "hand")
        stock_library_top(client, game2, "alice", ["Mountain", "Forest", "Grizzly Bears"])
        island2 = find(state(client, game2, uid=admin_key2)["battlefield"], "Island")["id"]
        preordain2 = find(state(client, game2, uid=admin_key2)["hands"]["alice"], "Preordain")["id"]
        client.post(
            f"/api/games/{game2}/actions/cast",
            json={
                "player": "alice",
                "card_id": preordain2,
                "mana_sources": [island2],
                "reasoning": REASONING,
            },
        )
        client.post(f"/api/games/{game2}/resolve-top")
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_cleanup_discard_executes_to_the_same_outcome(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    stuff_hand(client, game_id, "alice", 8)  # over the 7-card maximum
    paused = advance_into_cleanup(game_id)
    assert paused is not None, "the discard should have asked, not defaulted silently"
    uids = seats_for(client, game_id)
    hand = state(client, game_id, uid=uids["alice"])["hands"]["alice"]
    to_discard = [c["id"] for c in hand if c["name"] == "Mountain"][:1]
    discarded = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "card_ids": to_discard, "reasoning": REASONING},
    )
    assert discarded.json()["accepted"] is True, discarded.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    (discard_record,) = [
        r
        for r in decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0].get("answers")
        and r["dsl"]["moves"][0]["answers"][0]["decision"] == "choose_cards"
    ]
    play = _play_of(discard_record)

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    try:
        stuff_hand(client, game2, "alice", 8)
        paused2 = advance_into_cleanup(game2)
        assert paused2 is not None
        before = len(state(client, game2, uid=seat_uid(game2, "alice"))["hands"]["alice"])
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        after = len(state(client, game2, uid=seat_uid(game2, "alice"))["hands"]["alice"])
        assert after == before - 1
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_choose_zero_answer_executes_to_the_same_outcome(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    add_card(client, game_id, "Island", "alice", "battlefield")
    add_card(client, game_id, "Island", "alice", "battlefield")
    add_card(client, game_id, "Contingency Plan", "alice", "hand")
    stock_library_top(
        client, game_id, "alice", ["Mountain", "Mountain", "Mountain", "Mountain", "Mountain"]
    )
    islands = [c["id"] for c in state(client, game_id)["battlefield"] if c["name"] == "Island"]
    plan = find(state(client, game_id)["hands"]["alice"], "Contingency Plan")["id"]
    client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": plan,
            "mana_sources": islands,
            "reasoning": REASONING,
        },
    )
    client.post(f"/api/games/{game_id}/resolve-top")
    uids = seats_for(client, game_id)
    surveilled = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "moved": [], "top_order": [], "reasoning": REASONING},
    )
    assert surveilled.json()["accepted"] is True, surveilled.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    (surveil_record,) = [
        r
        for r in decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0].get("answers")
        and r["dsl"]["moves"][0]["answers"][0]["decision"] == "surveil"
    ]
    play = _play_of(surveil_record)

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    admin_key2 = second.json()["admin_key"]
    try:
        add_card(client, game2, "Island", "alice", "battlefield")
        add_card(client, game2, "Island", "alice", "battlefield")
        add_card(client, game2, "Contingency Plan", "alice", "hand")
        stock_library_top(
            client, game2, "alice", ["Mountain", "Mountain", "Mountain", "Mountain", "Mountain"]
        )
        islands2 = [
            c["id"]
            for c in state(client, game2, uid=admin_key2)["battlefield"]
            if c["name"] == "Island"
        ]
        plan2 = find(state(client, game2, uid=admin_key2)["hands"]["alice"], "Contingency Plan")[
            "id"
        ]
        client.post(
            f"/api/games/{game2}/actions/cast",
            json={
                "player": "alice",
                "card_id": plan2,
                "mana_sources": islands2,
                "reasoning": REASONING,
            },
        )
        client.post(f"/api/games/{game2}/resolve-top")
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_choice_answer_executes_to_the_same_outcome(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Utopia Sprawl", "alice", "hand")
    forests = [c["id"] for c in state(client, game_id)["battlefield"] if c["name"] == "Forest"]
    sprawl = find(state(client, game_id)["hands"]["alice"], "Utopia Sprawl")["id"]
    client.post(
        f"/api/games/{game_id}/actions/cast",
        json={
            "player": "alice",
            "card_id": sprawl,
            "targets": [forests[0]],
            "mana_sources": [forests[1]],
            "reasoning": REASONING,
        },
    )
    client.post(f"/api/games/{game_id}/resolve-top")
    uids = seats_for(client, game_id)
    quality = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={"player": "alice", "choice": "G", "reasoning": REASONING},
    )
    assert quality.json()["accepted"] is True, quality.text

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    (quality_record,) = [
        r
        for r in decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0].get("answers")
        and r["dsl"]["moves"][0]["answers"][0]["decision"] == "choose_a_quality"
    ]
    play = _play_of(quality_record)

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    admin_key2 = second.json()["admin_key"]
    try:
        add_card(client, game2, "Forest", "alice", "battlefield")
        add_card(client, game2, "Forest", "alice", "battlefield")
        add_card(client, game2, "Utopia Sprawl", "alice", "hand")
        forests2 = [
            c["id"]
            for c in state(client, game2, uid=admin_key2)["battlefield"]
            if c["name"] == "Forest"
        ]
        sprawl2 = find(state(client, game2, uid=admin_key2)["hands"]["alice"], "Utopia Sprawl")[
            "id"
        ]
        client.post(
            f"/api/games/{game2}/actions/cast",
            json={
                "player": "alice",
                "card_id": sprawl2,
                "targets": [forests2[0]],
                "mana_sources": [forests2[1]],
                "reasoning": REASONING,
            },
        )
        client.post(f"/api/games/{game2}/resolve-top")
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_order_triggers_executes_once_fd3_lands(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    sagas_paused_on_their_chapter_triggers(client, game_id)
    answer_everything(client, game_id, "alice")

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    (order_record,) = [
        r
        for r in decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0].get("answers")
        and r["dsl"]["moves"][0]["answers"][0]["decision"] == "order_triggers"
    ]
    play = _play_of(order_record)

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    try:
        sagas_paused_on_their_chapter_triggers(client, game2)
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    finally:
        client.delete(f"/api/games/{game2}")


def test_a_converted_assign_combat_damage_executes_once_fd3_lands(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import _ADMIN_KEYS

    game_id = game
    admin_key = _ADMIN_KEYS[game_id]
    harbingers = add_card(client, game_id, "Lupine Harbingers", "alice", "battlefield")["added"][
        "card_id"
    ]
    bears_id = add_card(client, game_id, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    mystic_id = add_card(client, game_id, "Elvish Mystic", "bob", "battlefield")["added"]["card_id"]
    uids = seats_for(client, game_id)

    client.put(f"/api/games/{game_id}/step", json={"step": "beginning_of_combat"})
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    client.post(
        f"/api/games/{game_id}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": harbingers, "defender": "bob"}], "reasoning": REASONING},
    )
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    client.post(
        f"/api/games/{game_id}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={
            "player": "bob",
            "blocks": [
                {"blocker": bears_id, "blocked": harbingers},
                {"blocker": mystic_id, "blocked": harbingers},
            ],
            "reasoning": REASONING,
        },
    )
    client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uids["alice"]},
        json={
            "player": "alice",
            "damage": [
                {"target": bears_id, "amount": 2},
                {"target": mystic_id, "amount": 1},
                {"target": "bob", "amount": 1},
            ],
            "reasoning": REASONING,
        },
    )

    _end_with_concede(client, game_id)
    decisions = _review_decisions(client, game_id, admin_key, "alice")
    (damage_record,) = [
        r
        for r in decisions
        if r["action"] == "decide"
        and r["dsl"]
        and r["dsl"]["moves"][0].get("answers")
        and r["dsl"]["moves"][0]["answers"][0]["decision"] == "assign_combat_damage"
    ]
    play = _play_of(damage_record)

    second = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    game2 = second.json()["game_id"]
    try:
        harbingers2 = add_card(client, game2, "Lupine Harbingers", "alice", "battlefield")["added"][
            "card_id"
        ]
        bears2_id = add_card(client, game2, "Grizzly Bears", "bob", "battlefield")["added"][
            "card_id"
        ]
        mystic2_id = add_card(client, game2, "Elvish Mystic", "bob", "battlefield")["added"][
            "card_id"
        ]
        client.put(f"/api/games/{game2}/step", json={"step": "beginning_of_combat"})
        uids2 = seats_for(client, game2)
        client.post(
            f"/api/games/{game2}/advance", params={"uid": uids2["alice"]}, json={"player": "alice"}
        )
        client.post(
            f"/api/games/{game2}/actions/attack",
            params={"uid": uids2["alice"]},
            json={
                "attackers": [{"card_id": harbingers2, "defender": "bob"}],
                "reasoning": REASONING,
            },
        )
        client.post(
            f"/api/games/{game2}/advance", params={"uid": uids2["alice"]}, json={"player": "alice"}
        )
        client.post(
            f"/api/games/{game2}/actions/declare-blockers",
            params={"uid": uids2["bob"]},
            json={
                "player": "bob",
                "blocks": [
                    {"blocker": bears2_id, "blocked": harbingers2},
                    {"blocker": mystic2_id, "blocked": harbingers2},
                ],
                "reasoning": REASONING,
            },
        )
        client.post(
            f"/api/games/{game2}/advance", params={"uid": uids2["alice"]}, json={"player": "alice"}
        )
        result = _register_and_run(client, monkeypatch, game2, "alice", play)
        assert result.finished is True, result.to_dict()
        assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    finally:
        client.delete(f"/api/games/{game2}")
