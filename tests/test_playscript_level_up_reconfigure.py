"""Play-script vocabulary for level up (CR 702.87a) and reconfigure (CR
702.151a): two `keyword_action` actions over `actions/level-up` and
`actions/reconfigure`.

`level_up` names the leveler and nothing else. `reconfigure` names the
Equipment and, for the attach, the `target` creature; with no `target` it is
the unattach - the same split `ReconfigureRequest.target` makes. The schema,
the runner (a real board through the real routes, the runner's posts
recorded) and the ledger-to-play converter are each proved here.
"""

from __future__ import annotations

from typing import Any

import playscript_testkit as kit
import pytest
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from playscript_testkit import keyword_action_move
from test_decision_dsl import OPPONENT, SEAT, only, play_of, row
from test_playscript_runtime import (
    hooked_seat,
    one_move_script,
    opponent_passes,
    register,
    seat_uid,
    wired,
)
from test_playscript_schema_fb3 import play, play_errors

from magic_cards_model_api import seatkit
from magic_cards_model_api.playscripts.decision_dsl import decision_rows
from magic_cards_model_api.playscripts.runtime import Exit, run_script
from magic_cards_model_api.playscripts.schema import KEYWORD_ACTIONS

REASONING = "level up and reconfigure coverage"


@pytest.fixture(autouse=True)
def _never_available_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("the script runner called available_actions")

    monkeypatch.setattr(seatkit, "available_actions", refuse)


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


# -- schema -------------------------------------------------------------------


def test_both_actions_are_keyword_actions():
    assert "level_up" in KEYWORD_ACTIONS
    assert "reconfigure" in KEYWORD_ACTIONS


def test_level_up_validates_with_just_its_card():
    doc = play(
        moves=[
            {
                "kind": "keyword_action",
                "action": "level_up",
                "card": {"zone": "battlefield", "select": {}},
            }
        ]
    )
    assert play_errors(doc) == []


def test_reconfigure_validates_with_and_without_a_target():
    for extra in ({"target": {"select": {"subtypes_any": ["Bear"]}, "side": "me"}}, {}):
        doc = play(
            moves=[
                {
                    "kind": "keyword_action",
                    "action": "reconfigure",
                    "card": {"zone": "battlefield", "select": {}},
                    **extra,
                }
            ]
        )
        assert play_errors(doc) == [], extra


# -- runtime -------------------------------------------------------------------


def test_level_up_posts_the_leveler(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "level-up",
        keyword_action_move("level_up", {"subtypes_any": ["Minotaur"]}, zone="battlefield"),
    )
    register(client, board, alice, script)
    add_card(client, board, "Lord of Shatterskull Pass", "alice", "battlefield")
    add_card(client, board, "Mountain", "alice", "battlefield")
    add_card(client, board, "Mountain", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    lord = [c for c in state(client, board)["battlefield"] if "Shatterskull" in c["name"]][0]

    with opponent_passes(board):
        result = run_script(
            seat, script_id="level-up", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/level-up")
    assert body["card_id"] == lord["id"]
    assert "target" not in body


def test_reconfigure_with_a_target_attaches_to_it(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "reconfigure",
        keyword_action_move(
            "reconfigure",
            {"subtypes_any": ["Equipment"]},
            zone="battlefield",
            target={"select": {"subtypes_any": ["Bear"]}, "side": "me"},
        ),
    )
    register(client, board, alice, script)
    add_card(client, board, "Simian Sling", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    add_card(client, board, "Mountain", "alice", "battlefield")
    add_card(client, board, "Mountain", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    bear = [c for c in state(client, board)["battlefield"] if c["name"] == "Grizzly Bears"][0]

    with opponent_passes(board):
        result = run_script(
            seat, script_id="reconfigure", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/reconfigure")
    assert body["target"] == bear["id"]


def test_reconfigure_without_a_target_is_the_unattach(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "unattach",
        keyword_action_move("reconfigure", {"subtypes_any": ["Equipment"]}, zone="battlefield"),
    )
    register(client, board, alice, script)
    add_card(client, board, "Simian Sling", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    run_script(seat, script_id="unattach", reasoning=REASONING, corpora=seats.script_corpora)

    # Unattached, the engine refuses it in its own words; the post carried no target.
    [body] = hook.posts("/actions/reconfigure")
    assert "target" not in body


def test_reconfigure_with_no_matching_target_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "reconfigure-miss",
        keyword_action_move(
            "reconfigure",
            {"subtypes_any": ["Equipment"]},
            zone="battlefield",
            target={"select": {"subtypes_any": ["Bear"]}, "side": "me"},
        ),
    )
    register(client, board, alice, script)
    add_card(client, board, "Simian Sling", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="reconfigure-miss", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/actions/reconfigure") == []


# -- ledger to play (decision_dsl) ---------------------------------------------


def test_a_level_up_row_becomes_a_level_up_move():
    leveler = kit.creature("Lord", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[leveler])
    entries = [row(1, "level_up", card_id=str(leveler["id"]), state_view=view)]
    move = play_of(only(decision_rows(entries, SEAT)))["moves"][0]
    assert move["kind"] == "keyword_action"
    assert move["action"] == "level_up"
    assert "target" not in move


def test_a_reconfigure_row_keeps_its_target_and_an_unattach_row_has_none():
    sling = kit.card("Sling", type_line="Artifact Creature — Equipment Monkey", controller=SEAT)
    bear = kit.creature("Bear", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[sling, bear])
    attach = [
        row(1, "reconfigure", card_id=str(sling["id"]), target=str(bear["id"]), state_view=view)
    ]
    move = play_of(only(decision_rows(attach, SEAT)))["moves"][0]
    assert move["action"] == "reconfigure"
    assert move["target"]["side"] == "me"

    unattach = [row(1, "reconfigure", card_id=str(sling["id"]), state_view=view)]
    move = play_of(only(decision_rows(unattach, SEAT)))["moves"][0]
    assert move["action"] == "reconfigure"
    assert "target" not in move


def test_a_reconfigure_row_whose_target_is_gone_is_unresolved_target():
    sling = kit.card("Sling", type_line="Artifact Creature — Equipment Monkey", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[sling])
    entries = [row(1, "reconfigure", card_id=str(sling["id"]), target="nope", state_view=view)]
    assert only(decision_rows(entries, SEAT))["unexpressible"] == "reconfigure:unresolved-target"
