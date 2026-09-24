"""FB2 B4 runtime (`playscripts/runtime.py`): the new moves and answers - M1
`pass`, M2 `declare_attackers`, M3 `declare_blockers`, A1 `top_order`/
`position`, A2 choose_targets `targets`, T1 target `prefer`/`ties`/`zone`,
M4 `ability_index`, and the standalone pending-decision answer (D2/item 7).

Unit-level tests call the request/answer builders directly against a
hand-built `state_view`/`pending` dict, the same style `test_playscript_
runtime.py` already uses for `decide_body`/`_move_request`; the happy path of
every new move is also proved end to end against a real engine board through
`run_script` (`register` + `hooked_seat`, imported from that same module).

Part 8 (`run_script` judging through `bindings.judge_bindings`) is not here:
it needs FC1's `playscripts/bindings.py`, which has not landed on this
branch yet (FC1 runs in its own worktree, in parallel) - see the task report.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from playscript_testkit import (
    attack_group,
    block,
    card,
    card_target,
    declare_attackers_move,
    declare_blockers_move,
    pass_move,
    player_target,
    position_answer,
    state_view,
    targets_answer,
    top_order_answer,
)
from test_playscript_runtime import (
    REASONING,
    hooked_seat,
    opponent_passes,
    register,
    seat_uid,
    wired,
)

from magic_cards_model_api.playscripts import conditions, runtime
from magic_cards_model_api.playscripts.runtime import Exit, run_script
from magic_cards_model_api.store import store

# -- shared board setup ---------------------------------------------------------


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


def ready(client: TestClient, game_id: str, added: dict[str, Any]) -> str:
    """`add_card` freshly puts a creature under its controller's control
    this very turn (CR 302.6 summoning sickness, unless it has haste) - most
    of these boards need to attack the same turn a fixture creature was
    added, so this clears that flag the harness way (`PATCH .../cards/{id}`)
    and returns the id."""
    card_id = added["added"]["card_id"]
    response = client.patch(f"/api/games/{game_id}/cards/{card_id}", json={"summoning_sick": False})
    assert response.status_code == 200, response.text
    return card_id


def combat_move_play(play_id: str, move: dict[str, Any], **over: Any) -> dict[str, Any]:
    """A minimal play whose one move is a combat declaration (FB2 M2/M3) -
    `when.steps` names the matching combat step so `_in_when` holds once the
    board is actually paused there."""
    step = move["kind"]  # "declare_attackers"/"declare_blockers"
    doc: dict[str, Any] = {
        "schema_version": 1,
        "id": play_id,
        "name": "Declare",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {
            "whose_turn": "mine" if step == "declare_attackers" else "theirs",
            "steps": [step],
            "stack": "empty",
        },
        "true": [],
        "false": [],
        "moves": [move],
        "next": [],
    }
    doc.update(over)
    return doc


def to_declare_attackers(client: TestClient, game_id: str) -> None:
    """Advances a fresh board to alice's paused `declare_attackers` decision
    (the same "reach past the API into the real engine" pattern `test_
    assign_combat_damage.py`'s own `_boot_double_block` uses)."""
    uids = {name: uid for uid, name in store.seat_uids(game_id).items()}
    client.put(f"/api/games/{game_id}/step", json={"step": "beginning_of_combat"})
    response = client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    assert response.json()["pending_decision"]["kind"] == "declare_attackers", response.json()


def to_declare_blockers(client: TestClient, game_id: str, attacker_id: str) -> None:
    """Alice declares `attacker_id` as attacking bob, pausing on bob's own
    `declare_blockers` decision."""
    uids = {name: uid for uid, name in store.seat_uids(game_id).items()}
    attacked = client.post(
        f"/api/games/{game_id}/actions/attack",
        params={"uid": uids["alice"]},
        json={"attackers": [{"card_id": attacker_id, "defender": "bob"}]},
    )
    assert attacked.json().get("allowed") is True, attacked.json()
    response = client.post(
        f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
    )
    body = response.json()
    assert body["pending_decision"]["kind"] == "declare_blockers", body


# -- M1: pass ---------------------------------------------------------------------


def test_pass_as_the_only_move_releases_priority_and_completes(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    play = {
        "schema_version": 1,
        "id": "just-pass",
        "name": "Just pass",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [pass_move()],
        "next": [],
    }
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="just-pass", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1
    passed = hook.posts("/actions/pass-priority")
    assert len(passed) == 1, passed
    after = state(client, board)
    assert after["priority_holder"] != "alice" or after["active_player"] != "alice"


def test_pass_after_a_cast_releases_priority_once_the_cast_settles(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `cast` move followed by `pass` (M1's own "pass must be last" shape):
    the cast resolves through the ordinary settle loop, then the trailing
    pass releases priority once - `_settle` is skipped for the pass move
    itself, so nothing the opponent then does with the priority this run
    just handed over reads back as `interrupted`."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Mountain", "alice", "battlefield")
    add_card(client, board, "Lightning Bolt", "alice", "hand")
    play = {
        "schema_version": 1,
        "id": "bolt-then-pass",
        "name": "Bolt then pass",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [
            {
                "kind": "cast",
                "card": {"zone": "hand", "select": {}},
                "targets": [{"player": "opponent"}],
            },
            {"kind": "pass"},
        ],
        "next": [],
    }
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="bolt-then-pass", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 2
    assert hook.posts("/actions/pass-priority"), "the trailing pass was never sent"
    after = state(client, board)
    assert next(p for p in after["players"] if p["username"] == "bob")["life"] == 17


class _FakeSeat:
    """The sliver of `Seat` `_send_move` actually calls, for a `pass` move:
    no HTTP, no real game - just enough to prove which `run_action` kwargs
    `_send_move`'s own `pass` branch uses."""

    player = "alice"

    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view

    def fetch_state(self, **_: Any) -> dict[str, Any]:
        return self._view

    def recent_log(self) -> None:
        return None

    def game_id(self) -> str:
        return "g"

    def call_json(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
        return {}


def test_pass_move_sends_hold_priority_false_release_false() -> None:
    """The seat's own bare `pass` tool sends exactly this shape
    (`mcp.pass_once`) - `_send_move`'s own `pass` branch matches it rather
    than the generic `hold_priority=True` every other move sends, and never
    posts a second, redundant pass on top of its own action's release."""
    calls: list[dict[str, Any]] = []

    def spying_run_action(seat: Any, perform: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        perform()  # the pass-priority POST itself, proven separately below
        return {"outcome": {"resolved": "passed"}}

    seat = _FakeSeat(state_view())
    ctx = runtime._Context(
        seat=seat,  # type: ignore[arg-type]
        reasoning=REASONING,
        corpus={},
        inputs={},
        catalog={},
        deadline=0.0,
        poll_seconds=0.0,
        call_started=0.0,
    )
    run = runtime._Run(
        run_id="r",
        script_id="p",
        script_sha="s",
        parent_run_id=None,
        scripts_run=["p"],
        moves_run=0,
    )
    play = {"moves": [{"kind": "pass"}]}
    with patch.object(runtime, "run_action", spying_run_action):
        result = runtime._send_move(ctx, run, play)
    assert result is None, result
    assert calls == [
        {
            "hold_priority": False,
            "release": False,
            "include_actions": False,
            "reasoning": f"{REASONING} | script p@s move 1/1 (pass)",
        }
    ]


# -- M2: declare_attackers ---------------------------------------------------------


def test_declare_attackers_with_a_count_picks_the_named_number(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    ready(client, board, add_card(client, board, "Grizzly Bears", "alice", "battlefield"))
    ready(client, board, add_card(client, board, "Grizzly Bears", "alice", "battlefield"))
    to_declare_attackers(client, board)
    play = combat_move_play(
        "attack-count",
        declare_attackers_move([attack_group({"subtypes_any": ["Bear"]}, count=1)]),
    )
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="attack-count", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1
    [attack] = hook.posts("/actions/attack")
    assert len(attack["attackers"]) == 1
    assert attack["attackers"][0]["defender"] == "bob"


def test_declare_attackers_with_all_attacks_every_matching_creature(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    ready(client, board, add_card(client, board, "Grizzly Bears", "alice", "battlefield"))
    ready(client, board, add_card(client, board, "Grizzly Bears", "alice", "battlefield"))
    ready(client, board, add_card(client, board, "Elvish Mystic", "alice", "battlefield"))
    to_declare_attackers(client, board)
    play = combat_move_play(
        "attack-all",
        declare_attackers_move([attack_group({"subtypes_any": ["Bear"]}, count="all")]),
    )
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="attack-all", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [attack] = hook.posts("/actions/attack")
    assert len(attack["attackers"]) == 2, attack


def test_declare_attackers_may_target_a_planeswalker_defender(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    ready(client, board, add_card(client, board, "Grizzly Bears", "alice", "battlefield"))
    walker = add_card(client, board, "Karn, the Great Creator", "bob", "battlefield")["added"][
        "card_id"
    ]
    to_declare_attackers(client, board)
    play = combat_move_play(
        "attack-walker",
        declare_attackers_move(
            [
                attack_group(
                    {"subtypes_any": ["Bear"]},
                    defender={"select": {"types_any": ["Planeswalker"]}, "side": "opponent"},
                )
            ]
        ),
    )
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="attack-walker", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [attack] = hook.posts("/actions/attack")
    assert attack["attackers"][0]["defender"] == walker


def test_declare_attackers_empty_list_declares_nobody(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    ready(client, board, add_card(client, board, "Grizzly Bears", "alice", "battlefield"))
    to_declare_attackers(client, board)
    play = combat_move_play("attack-none", declare_attackers_move([]))
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="attack-none", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [attack] = hook.posts("/actions/attack")
    assert attack["attackers"] == []


def test_declare_attackers_nothing_matching_is_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    ready(client, board, add_card(client, board, "Grizzly Bears", "alice", "battlefield"))
    to_declare_attackers(client, board)
    play = combat_move_play(
        "attack-nothing", declare_attackers_move([attack_group({"subtypes_any": ["Sliver"]})])
    )
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, _hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="attack-nothing", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()


def test_declare_attackers_a_tie_between_different_names_is_ambiguous(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    ready(client, board, add_card(client, board, "Grizzly Bears", "alice", "battlefield"))
    ready(client, board, add_card(client, board, "Elvish Mystic", "alice", "battlefield"))
    to_declare_attackers(client, board)
    play = combat_move_play(
        "attack-tie", declare_attackers_move([attack_group({"types_all": ["Creature"]}, count=1)])
    )
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, _hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="attack-tie", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.AMBIGUOUS, result.to_dict()
    assert set(result.script_exit.ambiguous_names) == {"Elvish Mystic", "Grizzly Bears"}


def test_declare_attackers_ties_any_takes_the_alphabetically_first(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    ready(client, board, add_card(client, board, "Grizzly Bears", "alice", "battlefield"))
    ready(client, board, add_card(client, board, "Elvish Mystic", "alice", "battlefield"))
    to_declare_attackers(client, board)
    play = combat_move_play(
        "attack-ties-any",
        declare_attackers_move([attack_group({"types_all": ["Creature"]}, count=1, ties="any")]),
    )
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="attack-ties-any", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [attack] = hook.posts("/actions/attack")
    named = state(client, board)["battlefield"]
    by_id = {card["id"]: card["name"] for card in named}
    assert by_id[attack["attackers"][0]["card_id"]] == "Elvish Mystic"


# -- M3: declare_blockers -----------------------------------------------------------


def test_declare_blockers_happy_path_sends_the_blocker_and_attacker(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harbingers = ready(
        client, board, add_card(client, board, "Lupine Harbingers", "alice", "battlefield")
    )
    bear = add_card(client, board, "Grizzly Bears", "bob", "battlefield")["added"]["card_id"]
    to_declare_attackers(client, board)
    to_declare_blockers(client, board, harbingers)
    bob = seat_uid(board, "bob")
    play = combat_move_play(
        "block-happy", declare_blockers_move([block({"subtypes_any": ["Bear"]}, {})])
    )
    register(client, board, bob, play)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, bob)

    result = run_script(
        seat, script_id="block-happy", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1
    [blocks] = hook.posts("/actions/declare-blockers")
    assert blocks["blocks"] == [{"blocker": bear, "blocked": harbingers}]


def test_declare_blockers_empty_list_declares_no_blocks(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harbingers = ready(
        client, board, add_card(client, board, "Lupine Harbingers", "alice", "battlefield")
    )
    add_card(client, board, "Grizzly Bears", "bob", "battlefield")
    to_declare_attackers(client, board)
    to_declare_blockers(client, board, harbingers)
    bob = seat_uid(board, "bob")
    play = combat_move_play("block-none", declare_blockers_move([]))
    register(client, board, bob, play)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, bob)

    result = run_script(
        seat, script_id="block-none", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [blocks] = hook.posts("/actions/declare-blockers")
    assert blocks["blocks"] == []


def test_declare_blockers_nothing_matching_the_blocker_is_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harbingers = ready(
        client, board, add_card(client, board, "Lupine Harbingers", "alice", "battlefield")
    )
    add_card(client, board, "Grizzly Bears", "bob", "battlefield")
    to_declare_attackers(client, board)
    to_declare_blockers(client, board, harbingers)
    bob = seat_uid(board, "bob")
    play = combat_move_play(
        "block-nothing", declare_blockers_move([block({"subtypes_any": ["Sliver"]}, {})])
    )
    register(client, board, bob, play)
    seats = wired(monkeypatch, board)
    seat, _hook = hooked_seat(seats, bob)

    result = run_script(
        seat, script_id="block-nothing", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()


def test_declare_blockers_a_tie_between_different_blockers_is_ambiguous(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    harbingers = ready(
        client, board, add_card(client, board, "Lupine Harbingers", "alice", "battlefield")
    )
    add_card(client, board, "Grizzly Bears", "bob", "battlefield")
    add_card(client, board, "Elvish Mystic", "bob", "battlefield")
    to_declare_attackers(client, board)
    to_declare_blockers(client, board, harbingers)
    bob = seat_uid(board, "bob")
    play = combat_move_play(
        "block-tie", declare_blockers_move([block({"types_all": ["Creature"]}, {})])
    )
    register(client, board, bob, play)
    seats = wired(monkeypatch, board)
    seat, _hook = hooked_seat(seats, bob)

    result = run_script(
        seat, script_id="block-tie", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.AMBIGUOUS, result.to_dict()


def test_declare_blockers_illegal_block_is_refused_with_the_engines_text(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flying attacker blocked by a selector that matches a non-flying,
    non-reach creature is refused by the engine itself, in its own words -
    the runner never pre-validates CR 509.1b, it just builds the request."""
    flier = ready(
        client, board, add_card(client, board, "Birds of Paradise", "alice", "battlefield")
    )
    add_card(client, board, "Grizzly Bears", "bob", "battlefield")
    to_declare_attackers(client, board)
    to_declare_blockers(client, board, flier)
    bob = seat_uid(board, "bob")
    play = combat_move_play(
        "block-illegal", declare_blockers_move([block({"subtypes_any": ["Bear"]}, {})])
    )
    register(client, board, bob, play)
    seats = wired(monkeypatch, board)
    seat, _hook = hooked_seat(seats, bob)

    result = run_script(
        seat, script_id="block-illegal", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.REFUSED, result.to_dict()
    assert result.script_exit.refusal_source == "server"
    assert result.script_exit.refusal


def test_a_combat_move_while_no_such_decision_is_pending_is_not_applicable(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No declare_attackers decision is pending yet (the board is still in
    `precombat_main`), so the play's own `when` position is never reached -
    the ordinary `not_applicable` exit `_wait(wait=False)` already gives,
    unaffected by the FB2 relaxation."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    play = combat_move_play("attack-not-pending", declare_attackers_move([attack_group({})]))
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, _hook = hooked_seat(seats, alice)

    result = run_script(
        seat,
        script_id="attack-not-pending",
        reasoning=REASONING,
        corpora=seats.script_corpora,
        wait=False,
    )

    assert result.script_exit.exit is Exit.NOT_APPLICABLE, result.to_dict()


# -- A1: top_order, position ---------------------------------------------------------


def test_top_order_orders_the_kept_cards_by_tier_then_looked_at_order() -> None:
    forest = card("Forest", type_line="Basic Land — Forest")
    island = card("Island", type_line="Basic Land — Island")
    mountain = card("Mountain", type_line="Basic Land — Mountain")
    pending = {
        "player": "alice",
        "kind": "scry",
        "generation": 1,
        "cards": [mountain["id"], forest["id"], island["id"]],
        "card_details": [mountain, forest, island],
    }
    item = top_order_answer("scry", [{"subtypes_any": ["Forest"]}, {"subtypes_any": ["Island"]}])

    body = runtime.decide_body(item, pending, {}, "alice", REASONING)

    assert body["moved"] == []
    assert body["top_order"] == [forest["id"], island["id"], mountain["id"]]


def test_top_order_orders_only_what_stays_once_moved_is_set_aside() -> None:
    forest = card("Forest", type_line="Basic Land — Forest")
    island = card("Island", type_line="Basic Land — Island")
    pending = {
        "player": "alice",
        "kind": "surveil",
        "generation": 1,
        "cards": [island["id"], forest["id"]],
        "card_details": [island, forest],
    }
    item = {
        "decision": "surveil",
        "to_graveyard": {"subtypes_any": ["Island"]},
        "top_order": [{"subtypes_any": ["Forest"]}],
    }

    body = runtime.decide_body(item, pending, {}, "alice", REASONING)

    assert body["moved"] == [island["id"]]
    assert body["top_order"] == [forest["id"]]


def test_position_top_and_bottom_answer_put_on_top_or_bottom() -> None:
    subtlety_target = card("Some Creature", type_line="Creature")
    pending = {
        "player": "alice",
        "kind": "put_on_top_or_bottom",
        "generation": 1,
        "cards": [subtlety_target["id"]],
        "card_details": [subtlety_target],
    }
    top = runtime.decide_body(position_answer("top"), pending, {}, "alice", REASONING)
    bottom = runtime.decide_body(position_answer("bottom"), pending, {}, "alice", REASONING)
    assert top["top"] is True
    assert bottom["top"] is False


# -- A2/T1: choose_targets targets, and target prefer/ties/zone --------------------


def test_choose_targets_resolves_a_player_target() -> None:
    pending = {
        "player": "alice",
        "kind": "choose_targets",
        "generation": 1,
        "targets": [{"kind": "player", "id": "bob"}],
    }
    item = targets_answer([player_target("opponent")])

    body = runtime.decide_body(item, pending, state_view(), "alice", REASONING)

    assert body["targets"] == ["bob"]


def test_choose_targets_resolves_a_graveyard_card_target_by_zone() -> None:
    grave_card = card("Dead Guy", type_line="Creature", zone="graveyard", controller="alice")
    battlefield_decoy = card(
        "Dead Guy", type_line="Creature", zone="battlefield", controller="alice"
    )
    pending = {
        "player": "alice",
        "kind": "choose_targets",
        "generation": 1,
        "targets": [{"kind": "card", "id": grave_card["id"]}],
    }
    view = state_view(graveyards={"alice": [grave_card]}, battlefield=[battlefield_decoy])
    item = targets_answer([card_target({}, side="me", zone="graveyard")])

    body = runtime.decide_body(item, pending, view, "alice", REASONING)

    assert body["targets"] == [grave_card["id"]]


def test_choose_targets_a_target_not_offered_is_unforeseen() -> None:
    offered = card("Offered", type_line="Creature")
    not_offered = card("Not Offered", type_line="Creature")
    pending = {
        "player": "alice",
        "kind": "choose_targets",
        "generation": 1,
        "targets": [{"kind": "card", "id": offered["id"]}],
    }
    view = state_view(battlefield=[offered, not_offered])
    item = targets_answer([card_target({"ability_text_has": "never printed"}, side="me")])

    body = runtime.decide_body(item, pending, view, "alice", REASONING)

    assert isinstance(body, runtime.ScriptExit)
    assert body.exit is Exit.UNFORESEEN


def test_cast_target_prefer_and_ties_narrow_and_break_ties() -> None:
    """T1: `CardTarget.prefer`/`ties` apply to a cast/activate target too -
    `_targets` passes them straight to `_pick`."""
    bear = card("Grizzly Bears", type_line="Creature — Bear", zone="battlefield", controller="bob")
    mystic = card("Elvish Mystic", type_line="Creature — Elf", zone="battlefield", controller="bob")
    view = state_view(battlefield=[bear, mystic])
    move = {
        "kind": "cast",
        "card": {"zone": "hand", "select": {}},
        "targets": [
            card_target(
                {"types_all": ["Creature"]},
                side="opponent",
                prefer=[{"subtypes_any": ["Bear"]}],
                ties="any",
            )
        ],
    }

    chosen = runtime._targets(move, view, "alice")

    assert chosen == [bear["id"]]


# -- M4: ability_index ---------------------------------------------------------------


def test_ability_index_picks_the_card_s_ability_at_that_position() -> None:
    dual = card(
        "Dual Land",
        type_line="Land",
        abilities=("{T}: Add {W}.", "{T}: Add {U}."),
    )
    view = state_view(battlefield=[dual])
    move = {
        "kind": "activate",
        "source": {"zone": "battlefield", "select": {}},
        "ability_index": 1,
        "targets": [],
    }

    built = runtime._move_request(move, view, "alice", REASONING)

    assert built[0] == "actions/activate"
    assert built[1]["card_id"] == dual["id"]
    assert built[1]["ability_index"] == 1


def test_ability_index_the_card_does_not_have_is_unforeseen() -> None:
    dual = card("Dual Land", type_line="Land", abilities=("{T}: Add {W}.",))
    view = state_view(battlefield=[dual])
    move = {
        "kind": "activate",
        "source": {"zone": "battlefield", "select": {}},
        "ability_index": 3,
        "targets": [],
    }

    built = runtime._move_request(move, view, "alice", REASONING)

    assert isinstance(built, runtime.ScriptExit)
    assert built.exit is Exit.UNFORESEEN
    assert "index 3" in built.stop_reason


def test_ability_index_and_ability_text_pick_the_same_source_card() -> None:
    dual = card("Dual Land", type_line="Land", abilities=("{T}: Add {W}.", "{T}: Add {U}."))
    view = state_view(battlefield=[dual])
    by_index = runtime._move_request(
        {
            "kind": "activate",
            "source": {"zone": "battlefield", "select": {}},
            "ability_index": 0,
            "targets": [],
        },
        view,
        "alice",
        REASONING,
    )
    by_text = runtime._move_request(
        {
            "kind": "activate",
            "source": {"zone": "battlefield", "select": {}},
            "ability_text_has": "Add {W}",
            "targets": [],
        },
        view,
        "alice",
        REASONING,
    )
    assert by_index[1]["card_id"] == by_text[1]["card_id"] == dual["id"]
    assert by_index[1]["ability_index"] == by_text[1]["ability_index"] == 0


# -- D2/item 7: the standalone pending-decision answer ------------------------------


def test_standalone_pending_answer_runs_without_priority(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FC6 owns `conditions.py`'s own `pending_decision` evaluation and has
    not landed on this branch yet (it runs in a sibling worktree, in
    parallel - see the FB2 plan's own ordering note); `conditions.evaluate`
    is stubbed here to simulate what it will report once it does, so this
    proves `runtime.py`'s own half in isolation: the priority gate is never
    consulted for a play whose one move is `answer` and whose `true` names
    the decision genuinely pending for this seat right now."""
    alice = seat_uid(board, "alice")
    # A genuinely off-priority decision (asked of alice while bob holds
    # priority) needs a much larger board build than this file's other
    # boards - the gate under test does not care what kind is pending, only
    # that one is, so `seat.fetch_state` is patched to report one directly
    # (the docstring above explains why `conditions.evaluate` is patched too).
    play = {
        "schema_version": 1,
        "id": "standalone-answer",
        "name": "Answer it",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "either", "steps": ["precombat_main"], "stack": "any"},
        "true": [{"kind": "pending_decision", "decision": "choose_a_mode"}],
        "false": [],
        "moves": [
            {"kind": "answer", "answers": [{"decision": "choose_a_mode", "choice": "Draw a card."}]}
        ],
        "next": [],
    }
    register(client, board, alice, play)
    seats = wired(monkeypatch, board)
    seat, _hook = hooked_seat(seats, alice)

    pending = {
        "player": "alice",
        "kind": "choose_a_mode",
        "prompt": "choose one",
        "generation": 7,
        "options": ["Draw a card.", "Gain 3 life."],
    }
    real_fetch_state = seat.fetch_state
    calls = {"n": 0}

    def fake_fetch_state(**kw: Any) -> dict[str, Any]:
        real = real_fetch_state(**kw)
        calls["n"] += 1
        real["pending_decision"] = pending
        real["priority_holder"] = "bob"  # alice does NOT hold priority
        return real

    with (
        patch.object(seat, "fetch_state", fake_fetch_state),
        patch.object(
            conditions,
            "evaluate",
            lambda *a, **kw: {
                "applies": True,
                "failing_list": None,
                "failing_index": None,
                "seen": None,
                "needs_input": None,
                "invalid_input": None,
            },
        ),
    ):
        result = run_script(
            seat,
            script_id="standalone-answer",
            reasoning=REASONING,
            corpora=seats.script_corpora,
            wait=False,
        )

    assert result.script_exit is not None
    assert result.script_exit.exit in (Exit.CHAIN_COMPLETE, Exit.UNKNOWN_OUTCOME, Exit.REFUSED), (
        result.to_dict()
    )
    # The gate itself is what is under test: it must at least have reached
    # the point of sending the answer rather than exiting `not_applicable`/
    # `unforeseen` for "no priority" - those would mean the relaxation in
    # `_drive` never fired.
    assert result.script_exit.exit not in (Exit.NOT_APPLICABLE,)
    assert calls["n"] >= 1


def test_pending_ready_matches_only_the_first_move_s_own_combat_kind() -> None:
    """`_pending_ready` (FB2 items 2/3/7) - the unit-level proof the
    board-level tests above build on: `True` exactly when a decision
    matching the play's own first move (or standalone `pending_decision`)
    is pending for this seat, and `False` for every other combination."""
    pending = {"player": "alice", "kind": "declare_blockers"}
    state = {"pending_decision": pending, "players": [{"username": "alice"}, {"username": "bob"}]}
    matching = combat_move_play("b", declare_blockers_move([]))
    mismatched = combat_move_play("a", declare_attackers_move([]))
    standalone = {
        "moves": [{"kind": "answer", "answers": []}],
        "true": [{"kind": "pending_decision", "decision": "declare_blockers"}],
    }
    standalone_wrong = {
        "moves": [{"kind": "answer", "answers": []}],
        "true": [{"kind": "pending_decision", "decision": "choose_a_mode"}],
    }

    assert runtime._pending_ready(matching, state, "alice") is True
    assert runtime._pending_ready(mismatched, state, "alice") is False
    assert runtime._pending_ready(standalone, state, "alice") is True
    assert runtime._pending_ready(standalone_wrong, state, "alice") is False
    assert runtime._pending_ready({"moves": [{"kind": "pass"}]}, state, "alice") is False
    assert runtime._pending_ready(matching, {"pending_decision": None}, "alice") is False
