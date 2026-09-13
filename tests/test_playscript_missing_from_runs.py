"""`missing_from` on real boards: one play fetches the Tron piece you lack.

Battle 4's B4-10 needs the Tron land-search plays to fire exactly once
whenever a missing piece is fetchable, within six active plays. A selector's
`missing_from` makes that one play: its count condition holds while the
library holds a piece whose subtype is on neither the battlefield nor in hand,
and its search pick takes that piece. Real printings (Urza's Tower, Mine and
Power-Plant, Expedition Map) played through the real engine: first through
`runtime.run_script` for the pick rule, then through the MCP `run_script`
tool for the three boards the requirement names.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_mcp_run_script import run, server_for
from test_playscript_runtime import (
    REASONING,
    cards_named,
    hooked_seat,
    opponent_passes,
    register,
    seat_uid,
    wired,
)

from magic_cards_model_api import seatkit
from magic_cards_model_api.playscripts.runtime import Exit, run_script
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.store import store

TRON = ["Tower", "Mine", "Power-Plant"]
MAP_SEARCH = "Search your library for"
PIECES = {"Tower": "Urza's Tower", "Mine": "Urza's Mine", "Power-Plant": "Urza's Power Plant"}


@pytest.fixture(autouse=True)
def _never_available_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("the script runner called available_actions")

    monkeypatch.setattr(seatkit, "available_actions", refuse)


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


def piece_selector() -> dict[str, Any]:
    return {
        "types_all": ["Land"],
        "subtypes_any": list(TRON),
        "missing_from": {"zones": ["battlefield", "hand"], "side": "me", "by": "subtype"},
    }


def fetch_missing_piece(prefer: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    pick: dict[str, Any] = {"select": piece_selector(), "count": 1}
    if prefer:
        pick["prefer"] = prefer
    return {
        "schema_version": 1,
        "id": "fetch-missing-tron-piece",
        "name": "Map for the Tron piece I lack",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [
            {
                "kind": "count",
                "zone": "library",
                "side": "me",
                "op": ">=",
                "n": 1,
                "select": piece_selector(),
            }
        ],
        "false": [],
        "moves": [
            {
                "kind": "activate",
                "source": {
                    "zone": "battlefield",
                    "select": {"types_all": ["Artifact"], "ability_text_has": MAP_SEARCH},
                },
                "ability_text_has": MAP_SEARCH,
                "sacrifice": "source",
                "answers": [{"decision": "choose_cards", "pick": pick}],
            }
        ],
        "next": [],
    }


def set_up(
    client: TestClient,
    game: str,
    *,
    battlefield: tuple[str, ...] = (),
    hand: tuple[str, ...] = (),
    graveyard: tuple[str, ...] = (),
    library: tuple[str, ...] = tuple(TRON),
) -> None:
    add_card(client, game, "Expedition Map", "alice", "battlefield")
    for _ in range(2):
        add_card(client, game, "Wastes", "alice", "battlefield")
    for zone, pieces in (
        ("battlefield", battlefield),
        ("hand", hand),
        ("graveyard", graveyard),
        ("library", library),
    ):
        for piece in pieces:
            add_card(client, game, PIECES[piece], "alice", zone)


def hand_names(client: TestClient, game: str) -> list[str]:
    return sorted(card["name"] for card in state(client, game)["hands"]["alice"])


# -- the pick rule, through the runner -----------------------------------------


@pytest.mark.parametrize("library", [("Mine", "Power-Plant"), ("Power-Plant", "Mine")])
def test_prefer_picks_one_missing_piece_whatever_the_library_order(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, library: tuple[str, ...]
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_missing_piece(prefer=[{"subtypes_any": ["Mine"]}])
    register(client, board, alice, script)
    set_up(client, board, battlefield=("Tower",), library=library)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert hand_names(client, board) == ["Urza's Mine"]
    mine = cards_named(state(client, board)["hands"]["alice"], "Urza's Mine")[0]
    searches = [body["card_ids"] for body in hook.posts("/decide") if body.get("card_ids")]
    assert searches and all(ids == [mine["id"]] for ids in searches), searches


def test_two_different_missing_pieces_tied_without_prefer_exit_ambiguous(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_missing_piece()
    register(client, board, alice, script)
    set_up(client, board, battlefield=("Tower",), library=("Tower", "Mine", "Power-Plant"))
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.AMBIGUOUS, result.to_dict()
    assert result.script_exit.ambiguous_names == ["Urza's Mine", "Urza's Power Plant"]


def test_copies_of_one_missing_piece_are_interchangeable_not_ambiguous(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_missing_piece()
    register(client, board, alice, script)
    set_up(
        client,
        board,
        battlefield=("Tower",),
        hand=("Mine",),
        library=("Tower", "Mine", "Power-Plant", "Power-Plant"),
    )
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert hand_names(client, board) == ["Urza's Mine", "Urza's Power Plant"]


# -- the three boards of B4-10, through the MCP run_script tool ------------------


def test_tower_in_play_and_mine_in_hand_fetches_the_power_plant(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_missing_piece())
    set_up(client, board, battlefield=("Tower",), hand=("Mine",), library=("Power-Plant",))
    _, server = server_for(client, monkeypatch, board)

    with opponent_passes(board):
        out = run(server, alice, "fetch-missing-tron-piece")["script_exit"]

    assert out["exit"] == "chain_complete", out
    assert hand_names(client, board) == ["Urza's Mine", "Urza's Power Plant"]


def test_tower_in_the_graveyard_and_the_others_held_fetches_the_tower(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_missing_piece())
    set_up(
        client,
        board,
        graveyard=("Tower",),
        hand=("Mine", "Power-Plant"),
        library=("Tower", "Mine", "Power-Plant"),
    )
    _, server = server_for(client, monkeypatch, board)

    with opponent_passes(board):
        out = run(server, alice, "fetch-missing-tron-piece")["script_exit"]

    assert out["exit"] == "chain_complete", out
    assert hand_names(client, board) == ["Urza's Mine", "Urza's Power Plant", "Urza's Tower"]


def test_nothing_missing_is_not_applicable(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_missing_piece())
    set_up(
        client,
        board,
        battlefield=("Tower",),
        hand=("Mine", "Power-Plant"),
        library=("Tower", "Mine", "Power-Plant"),
    )
    _, server = server_for(client, monkeypatch, board)

    out = run(server, alice, "fetch-missing-tron-piece")["script_exit"]

    assert out["exit"] == "not_applicable", out
    assert out["failing_condition"] == {"list": "true", "index": 0, "seen": 0}
    assert cards_named(state(client, board)["battlefield"], "Expedition Map"), "never activated"


# -- advise mode ----------------------------------------------------------------


def test_advise_mode_builds_move_one_from_my_library_contents_when_it_reads_them(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advise mode builds move 1 itself, so a move whose source selector
    reads my own library through `missing_from` needs `include=contents`."""
    advisor = "advisor-uid"
    store.register_advisors(board, {advisor: Advisor(player="alice")})
    script = fetch_missing_piece()
    script["moves"][0]["source"]["select"]["missing_from"] = {
        "zones": ["library"],
        "side": "me",
        "by": "name",
    }
    register(client, board, advisor, script)
    set_up(client, board, battlefield=("Tower",), library=("Mine",))
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, advisor)

    result = run_script(
        seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    out = result.script_exit
    assert out.exit is Exit.ADVISED, result.to_dict()
    assert out.proposal is not None and out.proposal.get("proposed") is True
