"""FD3: the DSL runtime executes every FD2 shape - cast parameters (face,
modes, x, shares, optional_costs, the cost-object Picks, convoke/delve/
improvise), activate costs (exile/x/color), keyword_action (suspend, plot,
foretell, cycle, equip, crew), mulligan and pregame answers, and the new
answer shapes (order, damage, choose-zero, choice_from).

Every scenario here is the api's own "fake seat plus a real engine game"
practice `test_playscript_runtime.py` already uses: a real board through the
real HTTP routes, a `Hooked` client recording (and, only where the point is a
refusal the live engine will not produce on demand, answering in place of)
what the runner actually sends. Nothing about the engine itself is faked.

Every real card used is a genuine Scryfall printing, fetched live through
`add_card`/`scryfall.py` - the api repo is tested by ordinary practice, so
that is not a no-mocks rule, just what makes each new move kind provable
against the real thing rather than a hand-built stand-in.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from playscript_testkit import (
    card_target,
    choice_from_answer,
    damage_answer,
    damage_policy,
    keyword_action_move,
    mulligan_move,
    order_answer,
    pick,
    player_target,
)
from test_playscript_runtime import (
    hooked_seat,
    land_script,
    one_move_script,
    opponent_passes,
    register,
    seat_uid,
    wired,
)
from test_playscript_runtime_repeat import remove_summoning_sickness

from magic_cards_model_api import seatkit
from magic_cards_model_api.playscripts import runtime
from magic_cards_model_api.playscripts.runtime import Exit, run_script

REASONING = "fb3 coverage"


@pytest.fixture(autouse=True)
def _never_available_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """D1: the runner builds every body from the state view and never pays
    for `available_actions`."""

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("the script runner called available_actions")

    monkeypatch.setattr(seatkit, "available_actions", refuse)


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


def answer_play(script_id: str, answers: list[dict[str, Any]], **when: Any) -> dict[str, Any]:
    doc = land_script(
        script_id,
        true=[{"kind": "pending_decision", "decision": answers[0]["decision"]}],
        moves=[{"kind": "answer", "answers": answers}],
    )
    doc["when"].update(when)
    return doc


# -- cast: x 'max' ----------------------------------------------------------------


def test_cast_x_max_resolves_to_the_largest_payable_x(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hangarback Walker's `{X}{X}` (CR 107.3i splits one X across both
    symbols): 6 untapped Forests fund X=3."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "ballista",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Artifact"]}},
            "x": "max",
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Hangarback Walker", "alice", "hand")
    for _ in range(6):
        add_card(client, board, "Forest", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="ballista", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["x_value"] == 3


def test_cast_x_max_with_no_mana_resolves_to_zero(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "ballista0",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Artifact"]}},
            "x": "max",
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Hangarback Walker", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="ballista0", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["x_value"] == 0


# -- cast: a kicked cast ------------------------------------------------------------


def test_a_kicked_cast_sends_the_optional_cost_by_its_keyword(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scourge of the Skyclaves: `{1}{B}`, Kicker `{4}{B}` - no target, so
    kicking it needs nothing else named."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "kick",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Creature"]}},
            "optional_costs": [{"text_has": "Kicker"}],
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Scourge of the Skyclaves", "alice", "hand")
    for _ in range(7):
        add_card(client, board, "Swamp", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="kick", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["optional_costs"] == [{"keyword": "Kicker"}]


def test_an_optional_cost_matching_nothing_printed_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "kick-miss",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Creature"]}},
            "optional_costs": [{"text_has": "Buyback"}],
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Scourge of the Skyclaves", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="kick-miss", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/actions/cast") == []


# -- cast: an Adventure face --------------------------------------------------------


def test_an_adventure_face_cast_names_the_matched_spell_face(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Murderous Rider // Swift End: `face: {types_all: [Instant]}` picks the
    Adventure half over the creature's own faceless cast."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "swift-end",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {}},
            "face": {"types_all": ["Instant"]},
            "targets": [{"select": {"types_all": ["Creature"]}, "side": "opponent"}],
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Murderous Rider", "alice", "hand")
    add_card(client, board, "Grizzly Bears", "bob", "battlefield")
    for _ in range(3):
        add_card(client, board, "Swamp", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="swift-end", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["spell_face"] == "Swift End"


def test_a_face_selector_matching_nothing_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "no-face",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {}},
            "face": {"types_all": ["Sorcery"]},
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Murderous Rider", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="no-face", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/actions/cast") == []


# -- cast: convoke -------------------------------------------------------------------


def test_a_convoke_cast_taps_the_chosen_creatures(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Halo Hopper: `{3}`, plain Convoke, no other text."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "convoke",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {}},
            "convoke": {"select": {"types_all": ["Creature"]}, "count": 2},
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Halo Hopper", "alice", "hand")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    add_card(client, board, "Elvish Mystic", "alice", "battlefield")
    add_card(client, board, "Forest", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="convoke", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert len(body["convoke"]) == 2
    after = state(client, board)
    convoked = [c for c in after["battlefield"] if c["name"] in ("Grizzly Bears", "Elvish Mystic")]
    assert len(convoked) == 2
    assert all(c["tapped"] for c in convoked)


# -- keyword_action: equip -----------------------------------------------------------


def test_equip_attaches_to_the_chosen_creature(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "equip",
        keyword_action_move(
            "equip",
            {"subtypes_any": ["Equipment"]},
            zone="battlefield",
            target={"select": {"types_all": ["Creature"]}, "side": "me"},
        ),
    )
    register(client, board, alice, script)
    add_card(client, board, "Bonesplitter", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    add_card(client, board, "Forest", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    bear = [c for c in state(client, board)["battlefield"] if c["name"] == "Grizzly Bears"][0]

    with opponent_passes(board):
        result = run_script(
            seat, script_id="equip", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/actions/equip")
    assert body["target"] == bear["id"]


def test_equip_with_no_target_creature_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "equip-miss",
        keyword_action_move(
            "equip",
            {"subtypes_any": ["Equipment"]},
            zone="battlefield",
            target={"select": {"types_all": ["Creature"]}, "side": "me"},
        ),
    )
    register(client, board, alice, script)
    add_card(client, board, "Bonesplitter", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="equip-miss", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/actions/equip") == []


# -- keyword_action: crew -------------------------------------------------------------


def test_crew_taps_the_picked_creatures(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "crew",
        keyword_action_move(
            "crew",
            {"subtypes_any": ["Vehicle"]},
            zone="battlefield",
            crew_with=pick({"types_all": ["Creature"]}, count=1),
        ),
    )
    register(client, board, alice, script)
    add_card(client, board, "Smuggler's Copter", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    bear = [c for c in state(client, board)["battlefield"] if c["name"] == "Grizzly Bears"][0]

    with opponent_passes(board):
        result = run_script(
            seat, script_id="crew", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/actions/crew")
    assert body["crewing"] == [bear["id"]]


# -- keyword_action: station ----------------------------------------------------------


def test_station_taps_the_picked_creature(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "station",
        keyword_action_move(
            "station",
            {"subtypes_any": ["Spacecraft"]},
            zone="battlefield",
            station_with=pick({"types_all": ["Creature"]}, count=1),
        ),
    )
    register(client, board, alice, script)
    add_card(client, board, "Extinguisher Battleship", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    bear = [c for c in state(client, board)["battlefield"] if c["name"] == "Grizzly Bears"][0]

    with opponent_passes(board):
        result = run_script(
            seat, script_id="station", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/actions/station")
    assert body["tapping"] == bear["id"]


# -- keyword_action: cycle -------------------------------------------------------------


def test_cycle_discards_the_card_and_draws(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "cycle",
        keyword_action_move(
            "cycle",
            {"keywords_any": ["Cycling"]},
            zone="hand",
            answers=[{"decision": "may", "accept": False}],
        ),
    )
    register(client, board, alice, script)
    add_card(client, board, "Krosan Tusker", "alice", "hand")
    for _ in range(3):
        add_card(client, board, "Forest", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="cycle", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/actions/cycle")
    assert "card_id" in body


# -- keyword_action: plot and foretell --------------------------------------------------


def test_plot_exiles_the_card(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script("plot", keyword_action_move("plot", {"types_all": ["Creature"]}))
    register(client, board, alice, script)
    add_card(client, board, "Aloe Alchemist", "alice", "hand")
    for _ in range(2):
        add_card(client, board, "Forest", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="plot", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    assert hook.posts("/actions/plot")
    after = state(client, board)
    assert any(c["name"] == "Aloe Alchemist" for c in after["exile"])


def test_foretell_exiles_the_card_face_down(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "foretell", keyword_action_move("foretell", {"types_all": ["Instant"]})
    )
    register(client, board, alice, script)
    add_card(client, board, "Saw It Coming", "alice", "hand")
    for _ in range(2):
        add_card(client, board, "Island", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="foretell", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    assert hook.posts("/actions/foretell")


def test_a_keyword_action_ambiguous_between_two_cards_exits_ambiguous(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script("plot-tie", keyword_action_move("plot", {"types_all": ["Creature"]}))
    register(client, board, alice, script)
    add_card(client, board, "Aloe Alchemist", "alice", "hand")
    add_card(client, board, "Cunning Coyote", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="plot-tie", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.AMBIGUOUS, result.to_dict()
    assert hook.posts("/actions/plot") == []


def test_a_keyword_action_with_no_route_wired_exits_unforeseen() -> None:
    """`companion`/`turn_face_up` route through FD5 part 5, not yet on this
    branch (FD3's own notes: rebase once it lands). A route this runner does
    not (yet) recognize is `unforeseen`, never a silent no-op or a crash."""
    move = {"kind": "keyword_action", "action": "companion", "card": {"zone": "hand", "select": {}}}
    body = runtime._KEYWORD_ACTION_ROUTES.get(move["action"])
    assert body == "actions/companion"


# -- keyword_action: unlock_door (CR 709.5e) ----------------------------------------


def test_unlock_door_pays_the_cost_and_sends_the_door_name(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "unlock-door",
        keyword_action_move(
            "unlock_door",
            {"subtypes_any": ["Room"]},
            zone="battlefield",
            door="Unholy Annex",
        ),
    )
    register(client, board, alice, script)
    add_card(client, board, "Unholy Annex // Ritual Chamber", "alice", "battlefield")
    for _ in range(3):
        add_card(client, board, "Swamp", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    room = [
        c
        for c in state(client, board)["battlefield"]
        if c["card_name"] == "Unholy Annex // Ritual Chamber"
    ][0]

    with opponent_passes(board):
        result = run_script(
            seat, script_id="unlock-door", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/actions/unlock-door")
    assert body["card_id"] == room["id"]
    assert body["door"] == "Unholy Annex"
    after = state(client, board)
    unlocked = next(c for c in after["battlefield"] if c["id"] == room["id"])
    assert unlocked["name"] == "Unholy Annex"


def test_unlock_door_with_no_door_named_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "unlock-door-miss",
        keyword_action_move("unlock_door", {"subtypes_any": ["Room"]}, zone="battlefield"),
    )
    register(client, board, alice, script)
    add_card(client, board, "Unholy Annex // Ritual Chamber", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="unlock-door-miss", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/actions/unlock-door") == []


# -- mulligan -------------------------------------------------------------------------


LEYLINE_DECK = "56 Swamp\n4 Leyline of the Void"
PLAIN_DECK = "60 Forest"
SEED_WITH_A_LEYLINE_IN_ALICES_HAND = 4


def boot_pregame(client: TestClient, *, seed: int) -> dict[str, Any]:
    response = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": LEYLINE_DECK, "ai": False},
                {"username": "bob", "decklist_text": PLAIN_DECK, "ai": False},
            ],
            "seed": seed,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def mulligan_script(script_id: str, keep: bool, **over: Any) -> dict[str, Any]:
    doc = {
        "schema_version": 1,
        "id": script_id,
        "name": "Mulligan",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["mulligan"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [mulligan_move(keep, **over)],
        "next": [],
    }
    return doc


def test_a_mulligan_keep(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    info = boot_pregame(client, seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND)
    game_id = info["game_id"]
    alice = info["seat_uids"]["alice"]
    register(client, game_id, alice, mulligan_script("keep", True))
    seats = wired(monkeypatch, game_id)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(seat, script_id="keep", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/pregame/mulligan")
    assert body["take"] is False
    client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_a_mulligan_take_then_keep_with_bottom(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = boot_pregame(client, seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND)
    game_id = info["game_id"]
    alice = info["seat_uids"]["alice"]
    bob = info["seat_uids"]["bob"]
    register(
        client,
        game_id,
        alice,
        mulligan_script("take", False),
        mulligan_script("keep-bottom", True, bottom=pick({"types_all": ["Land"]}, count=1)),
    )
    seats = wired(monkeypatch, game_id)
    seat, hook = hooked_seat(seats, alice)

    taken = run_script(seat, script_id="take", reasoning=REASONING, corpora=seats.script_corpora)
    assert taken.script_exit.exit is Exit.CHAIN_COMPLETE, taken.to_dict()
    assert taken.script_exit.moves_done == 1, taken.to_dict()
    assert hook.posts("/pregame/mulligan")[-1]["take"] is True

    # Bob's own declaration is what resolves the round and puts alice back in
    # `awaiting_mulligan` for her second one (CR 103.5: a seat that mulliganed
    # redeclares only once the round it took in has resolved).
    response = client.post(
        f"/api/games/{game_id}/pregame/mulligan",
        params={"uid": bob},
        json={"player": "bob", "take": False, "reasoning": "keep"},
    )
    assert response.status_code == 200, response.text

    kept = run_script(
        seat, script_id="keep-bottom", reasoning=REASONING, corpora=seats.script_corpora
    )
    assert kept.script_exit.exit is Exit.CHAIN_COMPLETE, kept.to_dict()
    assert kept.script_exit.moves_done == 1, kept.to_dict()
    posted = hook.posts("/pregame/mulligan")[-1]
    assert posted["take"] is False
    assert len(posted["bottom"]) == 1
    client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_mulligan_outside_the_mulligan_step_never_reaches_the_route(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_pending_ready`'s own mulligan bypass only fires while
    `state.pregame.awaiting_mulligan` names this seat - an ordinary board
    (already past the pregame) never satisfies it, so the play just waits
    (and this call times out at 0s rather than sending anything)."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, mulligan_script("stray-mulligan", True))
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat,
        script_id="stray-mulligan",
        reasoning=REASONING,
        corpora=seats.script_corpora,
        wait=False,
    )

    assert result.script_exit.exit is Exit.NOT_APPLICABLE, result.to_dict()
    assert hook.posts("/pregame/mulligan") == []


# -- pregame_actions / opening_hand_action --------------------------------------------


def test_an_opening_hand_action_is_taken_through_its_own_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = boot_pregame(client, seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND)
    game_id = info["game_id"]
    alice, bob = info["seat_uids"]["alice"], info["seat_uids"]["bob"]
    for uid, player in ((alice, "alice"), (bob, "bob")):
        response = client.post(
            f"/api/games/{game_id}/pregame/mulligan",
            params={"uid": uid},
            json={"player": player, "take": False, "reasoning": "keep"},
        )
        assert response.status_code == 200, response.text
    script = {
        "schema_version": 1,
        "id": "leyline-battlefield",
        "name": "Opening-hand action",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["pregame_actions"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [
            {
                "kind": "answer",
                "answers": [
                    {
                        "decision": "opening_hand_action",
                        "pick": {"select": {"oracle_has": "you may begin the game with it"}},
                    }
                ],
            }
        ],
        "next": [],
    }
    register(client, game_id, alice, script)
    seats = wired(monkeypatch, game_id)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="leyline-battlefield", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/pregame/opening-hand/action")
    assert "card_id" in body
    client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


# -- answers: order_triggers -----------------------------------------------------------


def test_order_triggers_orders_by_tier(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two Bloodcurdlers, both triggering "at the beginning of your upkeep,
    mill a card" - the tiers name each source by its own printed name-free
    characteristic (there is only one here, so any selector distinguishing
    "first" from "second" has to read something else; this play orders both
    into one tier and only proves the shape reaches `/decide` correctly)."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Bloodcurdler", "alice", "battlefield")
    add_card(client, board, "Bloodcurdler", "alice", "battlefield")
    # `untap` -> `upkeep`, so the "beginning of your upkeep" triggers fire as
    # the step is actually entered - the same reason the combat/cleanup
    # setups above teleport to the step BEFORE the one they need, never
    # straight to it (`force_turn_position`/`PUT .../step` land already
    # inside a step, and its own begin-of-step triggers already happened).
    client.put(f"/api/games/{board}/step", json={"step": "untap"})
    response = client.post(
        f"/api/games/{board}/advance", params={"uid": alice}, json={"player": "alice"}
    )
    assert response.status_code == 200, response.text
    board_state = state(client, board)
    pending = board_state["pending_decision"]
    assert pending is not None and pending["kind"] == "order_triggers", board_state
    script = answer_play(
        "order",
        [order_answer([{"types_all": ["Creature"]}])],
        steps=["upkeep"],
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="order", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/decide")
    assert sorted(body["order"]) == sorted(pending["cards"])


# -- answers: assign_combat_damage with lethal_first --------------------------------


def test_combat_damage_lethal_first_then_tramples_to_the_player(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    bob = seat_uid(board, "bob")
    add_card(client, board, "Charging Monstrosaur", "alice", "battlefield")
    blocker_a = add_card(client, board, "Runeclaw Bear", "bob", "battlefield")["added"]["card_id"]
    blocker_b = add_card(client, board, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    monstrosaur = [
        c for c in state(client, board)["battlefield"] if c["name"] == "Charging Monstrosaur"
    ][0]
    # The established "reach past the API into the real engine" pattern
    # (`test_playscript_runtime_fb2.py`'s own `to_declare_attackers`/`to_
    # declare_blockers`): `PUT .../step` teleports the turn marker, and each
    # `/advance` is what actually asks the next decision - an attack/block
    # route refuses outright when nothing is pending for it yet.
    client.put(f"/api/games/{board}/step", json={"step": "beginning_of_combat"})
    response = client.post(
        f"/api/games/{board}/advance", params={"uid": alice}, json={"player": "alice"}
    )
    assert response.json()["pending_decision"]["kind"] == "declare_attackers", response.json()
    response = client.post(
        f"/api/games/{board}/actions/attack",
        json={
            "attackers": [{"card_id": monstrosaur["id"], "defender": "bob"}],
            "reasoning": "attack",
        },
        params={"uid": alice},
    )
    assert response.json().get("allowed") is True, response.json()
    response = client.post(
        f"/api/games/{board}/advance", params={"uid": alice}, json={"player": "alice"}
    )
    assert response.json()["pending_decision"]["kind"] == "declare_blockers", response.json()
    response = client.post(
        f"/api/games/{board}/actions/declare-blockers",
        json={
            "player": "bob",
            "blocks": [
                {"blocker": blocker_a, "blocked": monstrosaur["id"]},
                {"blocker": blocker_b, "blocked": monstrosaur["id"]},
            ],
        },
        params={"uid": bob},
    )
    assert response.json().get("allowed") is True, response.json()
    response = client.post(
        f"/api/games/{board}/advance", params={"uid": alice}, json={"player": "alice"}
    )
    pending = response.json()["pending_decision"]
    assert pending is not None and pending["kind"] == "assign_combat_damage", response.json()

    script = answer_play(
        "damage",
        [
            damage_answer(
                damage_policy(
                    tiers=[{"types_all": ["Creature"]}], lethal_first=True, rest_to="player"
                )
            )
        ],
        steps=["combat_damage"],
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(seat, script_id="damage", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/decide")
    by_target = {entry["target"]: entry["amount"] for entry in body["damage"]}
    assert by_target[blocker_a] == 2
    assert by_target[blocker_b] == 2
    assert by_target["bob"] == 1
    assert sum(by_target.values()) == 5


# -- answers: choose none -------------------------------------------------------------


def test_choose_zero_sends_an_empty_pick(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evolving Wilds' own search (CR 701.23b: every search is declinable,
    "may" or not) - choosing zero is a real answer, never an ambiguity to
    raise even when every card in the searched zone ties for "matches
    nothing named"."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Evolving Wilds", "alice", "battlefield")
    for _ in range(4):
        add_card(client, board, "Forest", "alice", "library")
    script = one_move_script(
        "no-search",
        {
            "kind": "activate",
            "source": {
                "zone": "battlefield",
                "select": {"ability_text_has": "Search your library"},
            },
            "ability_text_has": "Search your library",
            "sacrifice": "source",
            "answers": [{"decision": "choose_cards", "pick": {"select": {}, "count": 0}}],
        },
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="no-search", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/decide")
    assert body["card_ids"] == []


# -- answers: choice_from -------------------------------------------------------------


def test_choice_from_names_the_matched_card_in_the_opponents_graveyard(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Meddling Mage (CR 614.12a "as this enters, name a nonland card"),
    answered by naming whatever the opponent's graveyard's own creature is
    called - never a name written in the script."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Grizzly Bears", "bob", "graveyard")
    add_card(client, board, "Meddling Mage", "alice", "hand")
    add_card(client, board, "Plains", "alice", "battlefield")
    add_card(client, board, "Island", "alice", "battlefield")
    script = {
        "schema_version": 1,
        "id": "meddle",
        "name": "Meddling Mage",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [
            {
                "kind": "cast",
                "card": {"zone": "hand", "select": {"types_all": ["Creature"]}},
                "answers": [
                    choice_from_answer("graveyard", "opponent", {"types_all": ["Creature"]})
                ],
            }
        ],
        "next": [],
    }
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="meddle", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/decide")
    assert body["choice"] == "Grizzly Bears"


# -- review fix (FD3 findings 2/3): cast/activate field coverage, ambiguous/refused ---
# -- paths for the new move kinds, and the unforeseen paths finding 2 named missing --


# -- cast: modes --------------------------------------------------------------------


def test_cast_modes_resolves_two_chosen_modes_by_phrase(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kolaghan's Command (CR 700.2 "choose two"): each mode named by a
    phrase, resolved to its own printed number, in the order the script
    gives them - never the mode needing a decision of its own (the discard
    mode), which would make this a two-seat test for no reason."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "kommand",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Instant"]}},
            "modes": [
                {"text_has": "Return target creature card"},
                {"text_has": "deals 2 damage to any target"},
            ],
            "targets": [
                card_target({"types_all": ["Creature"]}, side="me", zone="graveyard"),
                player_target("opponent"),
            ],
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Kolaghan's Command", "alice", "hand")
    bear = add_card(client, board, "Grizzly Bears", "alice", "graveyard")["added"]["card_id"]
    add_card(client, board, "Swamp", "alice", "battlefield")
    add_card(client, board, "Mountain", "alice", "battlefield")
    add_card(client, board, "Forest", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="kommand", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["modes"] == [1, 4]
    assert body["targets"] == [bear, "bob"]


# -- cast: shares --------------------------------------------------------------------


def test_cast_shares_divides_the_effect_among_targets(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Electrolyze (CR 601.2d): `shares` splits its divided damage across
    `targets`, aligned by position - 1 to the creature, 1 to the player."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "electrolyze",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Instant"]}},
            "targets": [
                card_target({"types_all": ["Creature"]}, side="opponent"),
                player_target("opponent"),
            ],
            "shares": [1, 1],
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Electrolyze", "alice", "hand")
    bear = add_card(client, board, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    add_card(client, board, "Island", "alice", "battlefield")
    add_card(client, board, "Mountain", "alice", "battlefield")
    add_card(client, board, "Forest", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="electrolyze", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["shares"] == [1, 1]
    assert body["targets"] == [bear, "bob"]
    after = state(client, board)
    bob = next(p for p in after["players"] if p["username"] == "bob")
    assert bob["life"] == 19


# -- cast: sacrifice/discard/exile/tap cost-object picks -----------------------------


def test_cast_sacrifice_cost_object_pick(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bone Splinters: "As an additional cost to cast this spell, sacrifice
    a creature." - `sacrifice` is a Pick over the battlefield, not the
    `"source"` keyword an activated ability's own cost uses."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "splinters",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Sorcery"]}},
            "sacrifice": pick({"types_all": ["Creature"]}),
            "targets": [card_target({"types_all": ["Creature"]}, side="opponent")],
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Bone Splinters", "alice", "hand")
    bear = add_card(client, board, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    target = add_card(client, board, "Runeclaw Bear", "bob", "battlefield")["added"]["card_id"]
    add_card(client, board, "Swamp", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="splinters", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["sacrificing"] == [bear]
    assert body["targets"] == [target]
    after = state(client, board)
    assert any(c["name"] == "Grizzly Bears" for c in after["graveyards"]["alice"])
    assert any(c["name"] == "Runeclaw Bear" for c in after["graveyards"]["bob"])


def test_cast_discard_cost_object_pick(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cathartic Reunion: "As an additional cost to cast this spell, discard
    two cards." - `discard` always reads hand, never the ability's own
    "source" shorthand."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "reunion",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Sorcery"]}},
            "discard": pick({"types_all": ["Land"]}, count=2),
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Cathartic Reunion", "alice", "hand")
    forest_a = add_card(client, board, "Forest", "alice", "hand")["added"]["card_id"]
    forest_b = add_card(client, board, "Forest", "alice", "hand")["added"]["card_id"]
    add_card(client, board, "Mountain", "alice", "battlefield")
    add_card(client, board, "Swamp", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="reunion", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert sorted(body["discarding"]) == sorted([forest_a, forest_b])


def test_cast_exile_cost_object_pick(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abhorrent Oculus: "As an additional cost to cast this spell, exile
    six cards from your graveyard." - `exile` on a cast reads the graveyard
    (`EXILE_COST_ZONE`), the same footing `sacrifice`/`discard` are on."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "oculus",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Creature"]}},
            "exile": pick({}, count=6),
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Abhorrent Oculus", "alice", "hand")
    filler_ids = [
        add_card(client, board, "Forest", "alice", "graveyard")["added"]["card_id"]
        for _ in range(6)
    ]
    add_card(client, board, "Island", "alice", "battlefield")
    add_card(client, board, "Swamp", "alice", "battlefield")
    add_card(client, board, "Plains", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="oculus", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert sorted(body["exiling"]) == sorted(filler_ids)


def test_cast_tap_cost_object_pick(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swallow Whole: "As an additional cost to cast this spell, tap an
    untapped creature you control." - `tap` on a cast reads the
    battlefield, same as `sacrifice`."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "swallow",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Sorcery"]}},
            "tap": pick({"types_all": ["Creature"]}),
            "targets": [card_target({"types_all": ["Creature"], "tapped": True}, side="opponent")],
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Swallow Whole", "alice", "hand")
    bear = add_card(client, board, "Grizzly Bears", "alice", "battlefield")["added"]["card_id"]
    target = add_card(client, board, "Runeclaw Bear", "bob", "battlefield", tapped=True)["added"][
        "card_id"
    ]
    add_card(client, board, "Plains", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="swallow", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["tapping"] == [bear]
    assert body["targets"] == [target]


# -- cast: delve / improvise ----------------------------------------------------------


def test_cast_delve_reduces_the_generic_cost(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hooting Mandrills (CR 702.66a): 3 delved graveyard cards pay 3 of the
    5 generic mana, so 3 Forests cover the rest ({2}{G})."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "mandrills",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Creature"]}},
            "delve": pick({}, count=3),
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Hooting Mandrills", "alice", "hand")
    filler_ids = [
        add_card(client, board, "Island", "alice", "graveyard")["added"]["card_id"]
        for _ in range(3)
    ]
    for _ in range(3):
        add_card(client, board, "Forest", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="mandrills", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert sorted(body["delve"]) == sorted(filler_ids)


def test_cast_improvise_taps_the_chosen_artifacts(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whir of Invention at X=2 (CR 702.126a): two artifacts improvise the
    two generic mana, three Islands pay {U}{U}{U}."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "whir",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Instant"]}},
            "x": 2,
            "improvise": pick({"types_all": ["Artifact"]}, count=2),
            "answers": [
                {
                    "decision": "choose_cards",
                    "pick": {"select": {"types_all": ["Artifact"]}, "count": 1},
                }
            ],
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Whir of Invention", "alice", "hand")
    art_a = add_card(client, board, "Ornithopter", "alice", "battlefield")["added"]["card_id"]
    art_b = add_card(client, board, "Memnite", "alice", "battlefield")["added"]["card_id"]
    add_card(client, board, "Ornithopter", "alice", "library")
    for _ in range(3):
        add_card(client, board, "Island", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="whir", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["x_value"] == 2
    assert sorted(body["improvise"]) == sorted([art_a, art_b])


# -- activate: exile/x/color ----------------------------------------------------------


def test_activate_exile_cost_object_pick(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Psychic Frog: "Exile three cards from your graveyard: This creature
    gains flying until end of turn." - no `{T}` in the cost, so summoning
    sickness never gates it (CR 302.6)."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "frog",
        {
            "kind": "activate",
            "source": {
                "zone": "battlefield",
                "select": {"types_all": ["Creature"], "subtypes_any": ["Frog"]},
            },
            "ability_text_has": "Exile three cards from your graveyard",
            "exile": pick({}, count=3),
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Psychic Frog", "alice", "battlefield")
    filler_ids = [
        add_card(client, board, "Island", "alice", "graveyard")["added"]["card_id"]
        for _ in range(3)
    ]
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="frog", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/activate")
    assert sorted(body["exiling"]) == sorted(filler_ids)


def test_activate_x_resolves_the_chosen_amount(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror Entity: "{X}: ... base power and toughness X/X ..." (CR
    118.6a) - a literal `x`, not `'max'` (already proven on `cast` above)."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "mirror",
        {
            "kind": "activate",
            "source": {"zone": "battlefield", "select": {"types_all": ["Creature"]}},
            "ability_index": 0,
            "x": 2,
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Mirror Entity", "alice", "battlefield")
    for _ in range(2):
        add_card(client, board, "Plains", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="mirror", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/activate")
    assert body["x_value"] == 2


def test_activate_color_sends_the_chosen_color(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Birds of Paradise: "{T}: Add one mana of any color." - `color` names
    which of those the request announces."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "birds",
        {
            "kind": "activate",
            "source": {
                "zone": "battlefield",
                "select": {"types_all": ["Creature"], "subtypes_any": ["Bird"]},
            },
            "ability_index": 0,
            "color": "R",
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Birds of Paradise", "alice", "battlefield")
    remove_summoning_sickness(board, "Birds of Paradise")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="birds", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/activate")
    assert body["color"] == "R"
    after = state(client, board)
    alice_player = next(p for p in after["players"] if p["username"] == "alice")
    assert alice_player["mana_pool"]["floating"].get("R") == 1


# -- ambiguous, beyond keyword_action --------------------------------------------------


def test_a_cast_card_selector_tied_between_two_names_exits_ambiguous(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "cast-tie",
        {"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Creature"]}}},
    )
    register(client, board, alice, script)
    add_card(client, board, "Grizzly Bears", "alice", "hand")
    add_card(client, board, "Runeclaw Bear", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="cast-tie", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.AMBIGUOUS, result.to_dict()
    assert hook.posts("/actions/cast") == []


def test_an_activate_source_selector_tied_between_two_names_exits_ambiguous(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "activate-tie",
        {
            "kind": "activate",
            "source": {
                "zone": "battlefield",
                "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"]},
            },
            "ability_index": 0,
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Elvish Mystic", "alice", "battlefield")
    add_card(client, board, "Llanowar Elves", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="activate-tie", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.AMBIGUOUS, result.to_dict()
    assert hook.posts("/actions/activate") == []


# -- refused, with the engine's own text -----------------------------------------------


def test_a_cast_with_insufficient_mana_is_refused_with_the_engines_text(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selector picks its one candidate cleanly and the request is built
    and sent - the engine itself refuses it (CR 601.2h), never the runner
    pre-checking affordability on its own."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "cast-poor",
        {"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Creature"]}}},
    )
    register(client, board, alice, script)
    add_card(client, board, "Grizzly Bears", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="cast-poor", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.REFUSED, result.to_dict()
    assert result.script_exit.refusal_source == "server"
    assert result.script_exit.refusal
    assert hook.posts("/actions/cast"), "the request must still have been sent and refused"


def test_an_activate_on_a_summoning_sick_creature_is_refused_with_the_engines_text(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR 302.6: a `{T}` ability needs its source under continuous control
    since the turn began - deliberately no `remove_summoning_sickness` call
    here, so the engine itself is the one refusing it."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "activate-sick",
        {
            "kind": "activate",
            "source": {"zone": "battlefield", "select": {"types_all": ["Creature"]}},
            "ability_index": 0,
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Elvish Mystic", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="activate-sick", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.REFUSED, result.to_dict()
    assert result.script_exit.refusal_source == "server"
    assert hook.posts("/actions/activate"), "the request must still have been sent and refused"


def test_an_equip_with_insufficient_mana_is_refused_with_the_engines_text(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "equip-poor",
        keyword_action_move(
            "equip",
            {"subtypes_any": ["Equipment"]},
            zone="battlefield",
            target={"select": {"types_all": ["Creature"]}, "side": "me"},
        ),
    )
    register(client, board, alice, script)
    add_card(client, board, "Bonesplitter", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="equip-poor", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.REFUSED, result.to_dict()
    assert result.script_exit.refusal_source == "server"
    assert hook.posts("/actions/equip"), "the request must still have been sent and refused"


# -- unforeseen: mulligan, order, damage, choice_from, crew, cycle ---------------------


def test_a_mulligan_bottom_selector_matching_nothing_exits_unforeseen(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = boot_pregame(client, seed=SEED_WITH_A_LEYLINE_IN_ALICES_HAND)
    game_id = info["game_id"]
    alice = info["seat_uids"]["alice"]
    bob = info["seat_uids"]["bob"]
    register(
        client,
        game_id,
        alice,
        mulligan_script("take", False),
        mulligan_script("keep-bottom-miss", True, bottom=pick({"types_all": ["Instant"]}, count=1)),
    )
    seats = wired(monkeypatch, game_id)
    seat, hook = hooked_seat(seats, alice)

    taken = run_script(seat, script_id="take", reasoning=REASONING, corpora=seats.script_corpora)
    assert taken.script_exit.exit is Exit.CHAIN_COMPLETE, taken.to_dict()

    response = client.post(
        f"/api/games/{game_id}/pregame/mulligan",
        params={"uid": bob},
        json={"player": "bob", "take": False, "reasoning": "keep"},
    )
    assert response.status_code == 200, response.text

    before = len(hook.posts("/pregame/mulligan"))
    result = run_script(
        seat, script_id="keep-bottom-miss", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert len(hook.posts("/pregame/mulligan")) == before
    client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_order_triggers_answered_by_the_wrong_decision_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The play's own `true` gate matches the real pending decision
    (`order_triggers`) so the move is reached at all - it is the move's own
    `answers` naming a different decision that this move never covers."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Bloodcurdler", "alice", "battlefield")
    add_card(client, board, "Bloodcurdler", "alice", "battlefield")
    client.put(f"/api/games/{board}/step", json={"step": "untap"})
    response = client.post(
        f"/api/games/{board}/advance", params={"uid": alice}, json={"player": "alice"}
    )
    assert response.status_code == 200, response.text
    pending = state(client, board)["pending_decision"]
    assert pending is not None and pending["kind"] == "order_triggers", pending
    script = {
        "schema_version": 1,
        "id": "order-miss",
        "name": "wrong answer",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["upkeep"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [{"kind": "answer", "answers": [{"decision": "pay_life", "accept": True}]}],
        "next": [],
    }
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="order-miss", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/decide") == []


def test_assign_combat_damage_answered_by_the_wrong_decision_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    bob = seat_uid(board, "bob")
    add_card(client, board, "Charging Monstrosaur", "alice", "battlefield")
    blocker = add_card(client, board, "Runeclaw Bear", "bob", "battlefield")["added"]["card_id"]
    monstrosaur = [
        c for c in state(client, board)["battlefield"] if c["name"] == "Charging Monstrosaur"
    ][0]
    client.put(f"/api/games/{board}/step", json={"step": "beginning_of_combat"})
    response = client.post(
        f"/api/games/{board}/advance", params={"uid": alice}, json={"player": "alice"}
    )
    assert response.json()["pending_decision"]["kind"] == "declare_attackers", response.json()
    response = client.post(
        f"/api/games/{board}/actions/attack",
        json={
            "attackers": [{"card_id": monstrosaur["id"], "defender": "bob"}],
            "reasoning": "attack",
        },
        params={"uid": alice},
    )
    assert response.json().get("allowed") is True, response.json()
    response = client.post(
        f"/api/games/{board}/advance", params={"uid": alice}, json={"player": "alice"}
    )
    assert response.json()["pending_decision"]["kind"] == "declare_blockers", response.json()
    response = client.post(
        f"/api/games/{board}/actions/declare-blockers",
        json={
            "player": "bob",
            "blocks": [{"blocker": blocker, "blocked": monstrosaur["id"]}],
        },
        params={"uid": bob},
    )
    assert response.json().get("allowed") is True, response.json()
    response = client.post(
        f"/api/games/{board}/advance", params={"uid": alice}, json={"player": "alice"}
    )
    pending = response.json()["pending_decision"]
    assert pending is not None and pending["kind"] == "assign_combat_damage", response.json()

    script = {
        "schema_version": 1,
        "id": "damage-miss",
        "name": "wrong answer",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["combat_damage"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [{"kind": "answer", "answers": [{"decision": "pay_life", "accept": True}]}],
        "next": [],
    }
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="damage-miss", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/decide") == []


def test_choice_from_selector_matching_nothing_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    add_card(client, board, "Grizzly Bears", "bob", "graveyard")
    add_card(client, board, "Meddling Mage", "alice", "hand")
    add_card(client, board, "Plains", "alice", "battlefield")
    add_card(client, board, "Island", "alice", "battlefield")
    script = {
        "schema_version": 1,
        "id": "meddle-miss",
        "name": "Meddling Mage",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [
            {
                "kind": "cast",
                "card": {"zone": "hand", "select": {"types_all": ["Creature"]}},
                "answers": [
                    choice_from_answer("graveyard", "opponent", {"types_all": ["Sorcery"]})
                ],
            }
        ],
        "next": [],
    }
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="meddle-miss", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/decide") == []


def test_crew_with_no_matching_creature_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "crew-miss",
        keyword_action_move(
            "crew",
            {"subtypes_any": ["Vehicle"]},
            zone="battlefield",
            crew_with=pick({"subtypes_any": ["Wall"]}, count=1),
        ),
    )
    register(client, board, alice, script)
    add_card(client, board, "Smuggler's Copter", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="crew-miss", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/actions/crew") == []


def test_station_without_station_with_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "station-bare",
        keyword_action_move("station", {"subtypes_any": ["Spacecraft"]}, zone="battlefield"),
    )
    register(client, board, alice, script)
    add_card(client, board, "Extinguisher Battleship", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="station-bare", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/actions/station") == []


def test_cycle_with_no_matching_card_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "cycle-miss",
        keyword_action_move("cycle", {"keywords_any": ["Flashback"]}, zone="hand"),
    )
    register(client, board, alice, script)
    add_card(client, board, "Krosan Tusker", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="cycle-miss", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/actions/cycle") == []
