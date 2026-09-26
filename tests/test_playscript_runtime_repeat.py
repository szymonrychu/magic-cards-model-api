"""FD9's `repeat` move, run by FD3 part 7: `runtime._exec_repeat`.

Fake seat plus a real engine game, `test_playscript_runtime.py`'s own
practice: a real board through the real HTTP routes, a `Hooked` client
recording what the runner sends and, only for the one shape a live engine
will not produce on demand (an iteration that changes nothing), answering in
place of it.

FD9's own proof fixtures (`tests/fixtures/playscripts/repeat_devoted_druid.json`,
`repeat_walking_ballista.json`) are read here, never edited.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_playscript_runtime import (
    FakeResponse,
    hooked_seat,
    one_move_script,
    opponent_passes,
    register,
    seat_uid,
    wired,
)

from magic_cards_model_api import seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.playscripts.runtime import Exit, run_script
from magic_cards_model_api.store import store as game_store

REASONING = "repeat coverage"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "playscripts"


@pytest.fixture(autouse=True)
def _never_available_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("the script runner called available_actions")

    monkeypatch.setattr(seatkit, "available_actions", refuse)


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def remove_summoning_sickness(game_id: str, *names: str) -> None:
    """A creature `add_card` just put down is summoning sick (CR 508.1a) -
    the same "reach past the API into the real engine" pattern
    `force_turn_position` already uses, here for the one flag this harness
    has no HTTP route to clear."""
    game = game_store.get(game_id)
    assert game is not None
    for card_state in game.battlefield:
        if card_state.card.name in names:
            card_state.summoning_sick = False


def give_a_counter(game_id: str, name: str, kind: str, amount: int) -> None:
    """`add_card` deals a permanent no counters - Walking Ballista's own X
    (CR 706... "enters with X +1/+1 counters") needs a real cast to
    announce, so a plain `add_card` leaves it at toughness 0 and CR 704.5f's
    state-based action would bin it before the first activation ever lands.
    The same reach-past-the-API pattern as `remove_summoning_sickness`."""
    from magic_cards_model.models.card_state import Counters

    game = game_store.get(game_id)
    assert game is not None
    for card_state in game.battlefield:
        if card_state.card.name == name:
            card_state.counters = Counters(root={kind: amount})


@contextmanager
def opponent_plays_a_land(game_id: str, player: str = "bob") -> Iterator[None]:
    """Bob takes a real action - playing a land - the moment he is handed
    priority, instead of passing it back (`opponent_passes`'s own shape,
    with a real move in place of the pass) - `_foreign`'s own proof that a
    repeat loop is interrupted by a genuine opponent action, not merely by
    it holding priority for a beat."""
    uid = seat_uid(game_id, player)
    other = TestClient(app)
    stop = threading.Event()
    played = threading.Event()

    def loop() -> None:
        while not stop.is_set() and not played.is_set():
            board_now = other.get(f"/api/games/{game_id}", params={"uid": uid}).json()
            if (
                board_now.get("priority_holder") == player
                and board_now.get("pending_decision") is None
            ):
                response = other.post(
                    f"/api/games/{game_id}/actions/play-land",
                    params={"uid": uid},
                    json={"player": player, "reasoning": "interrupt"},
                )
                if response.json().get("allowed") is True:
                    played.set()
            stop.wait(0.01)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(5)


# -- FD9's own proof fixtures --------------------------------------------------------


def test_the_devoted_druid_loop_reaches_x8_in_one_run_script_call(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding seq 191: about 25 manual activate calls by hand: this fixture
    reaches the same X=8 mana target and casts the X spell in one
    `run_script` call - the seat-visible call count is 1."""
    alice = seat_uid(board, "alice")
    fixture = load_fixture("repeat_devoted_druid.json")
    register(client, board, alice, fixture)
    add_card(client, board, "Devoted Druid", "alice", "battlefield")
    remove_summoning_sickness(board, "Devoted Druid")
    add_card(client, board, "Vizier of Remedies", "alice", "battlefield")
    add_card(client, board, "Animist's Awakening", "alice", "hand")
    add_card(client, board, "Forest", "alice", "battlefield")
    for _ in range(20):
        add_card(client, board, "Forest", "alice", "library")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat,
            script_id="repeat_devoted_druid",
            reasoning=REASONING,
            corpora=seats.script_corpora,
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.iterations == 8
    activations = hook.posts("/actions/activate")
    assert len(activations) == 16, "8 iterations x 2 body moves"
    [cast] = hook.posts("/actions/cast")
    assert cast["x_value"] == 8
    # The seat's own visible surface made exactly one call for the whole
    # loop plus its cast - `run_script` itself, not one per activation.
    run_script_calls = [
        call for call in hook.sent if call[0] == "POST" and call[1].endswith("/script-runs")
    ]
    starts = [call for call in run_script_calls if call[2].get("phase") == "start"]
    assert len(starts) == 1


def test_the_walking_ballista_loop_pumps_then_pings_down_toughness_one(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    fixture = load_fixture("repeat_walking_ballista.json")
    register(client, board, alice, fixture)
    add_card(client, board, "Walking Ballista", "alice", "battlefield")
    give_a_counter(board, "Walking Ballista", "+1/+1", 2)
    for _ in range(16):
        add_card(client, board, "Forest", "alice", "battlefield")
    # A single opposing toughness-1 creature: the fixture's own ping target
    # (`select: {..., toughness: {op: "==", n: 1}}, side: opponent`) names no
    # `prefer`/`ties` of its own, so two same-toughness, differently-named
    # creatures at once would tie for the pick - genuinely `ambiguous`, the
    # same rule every other target selector in this DSL is held to, and not
    # this fixture's own claim to prove.
    add_card(client, board, "Llanowar Elves", "bob", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat,
            script_id="repeat_walking_ballista",
            reasoning=REASONING,
            corpora=seats.script_corpora,
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    after = state(client, board)
    opposing_toughness_one = [
        c
        for c in after["battlefield"]
        if c.get("controller") == "bob" and (c.get("toughness") or 0) == 1
    ]
    assert opposing_toughness_one == []


# -- until already true, zero iterations ---------------------------------------------


def test_until_already_true_runs_zero_iterations(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    add_card(client, board, "Devoted Druid", "alice", "battlefield")
    remove_summoning_sickness(board, "Devoted Druid")
    script = one_move_script(
        "already-there",
        {
            "kind": "repeat",
            "moves": [
                {
                    "kind": "activate",
                    "source": {
                        "zone": "battlefield",
                        "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"]},
                    },
                    "ability_index": 0,
                }
            ],
            "until": [{"kind": "untapped_mana", "op": ">=", "n": 0}],
            "max_iterations": 5,
        },
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="already-there", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.iterations == 0
    assert hook.posts("/actions/activate") == []


# -- max_iterations hit ---------------------------------------------------------------


def test_max_iterations_hit_exits_unforeseen_repeat_bound(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    add_card(client, board, "Devoted Druid", "alice", "battlefield")
    remove_summoning_sickness(board, "Devoted Druid")
    add_card(client, board, "Vizier of Remedies", "alice", "battlefield")
    script = one_move_script(
        "bound",
        {
            "kind": "repeat",
            "moves": [
                {
                    "kind": "activate",
                    "source": {
                        "zone": "battlefield",
                        "select": {
                            "types_all": ["Creature"],
                            "subtypes_any": ["Elf"],
                            "tapped": False,
                        },
                    },
                    "ability_index": 0,
                },
                {
                    "kind": "activate",
                    "source": {
                        "zone": "battlefield",
                        "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"]},
                    },
                    "ability_index": 1,
                },
            ],
            "until": [{"kind": "mana_pool", "color": "G", "op": ">=", "n": 1000}],
            "max_iterations": 3,
        },
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="bound", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert result.script_exit.stop_reason == "repeat_bound"
    assert result.script_exit.iterations == 3
    assert len(hook.posts("/actions/activate")) == 6


# -- no-progress guard ------------------------------------------------------------------


def test_an_iteration_that_changes_nothing_exits_unforeseen_repeat_no_progress(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live engine has no accepted move that changes nothing on demand -
    the module's own established practice for exactly this shape (`test_
    playscript_runtime.py`'s docstring): hook `/actions/activate` to answer
    `allowed: true` without the real engine ever running, so the board
    genuinely never moves between iterations."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Devoted Druid", "alice", "battlefield")
    remove_summoning_sickness(board, "Devoted Druid")
    script = one_move_script(
        "stuck",
        {
            "kind": "repeat",
            "moves": [
                {
                    "kind": "activate",
                    "source": {
                        "zone": "battlefield",
                        "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"]},
                    },
                    "ability_index": 0,
                }
            ],
            "until": [{"kind": "mana_pool", "color": "G", "op": ">=", "n": 1000}],
            "max_iterations": 5,
        },
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)

    def answer(method: str, path: str, body: Any) -> Any:
        if method == "POST" and path.endswith("/actions/activate"):
            return FakeResponse(200, {"allowed": True})
        return None

    seat, hook = hooked_seat(seats, alice, answer=answer)

    result = run_script(seat, script_id="stuck", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert result.script_exit.stop_reason == "repeat_no_progress"
    assert result.script_exit.iterations == 0


# -- stop_when_refused ------------------------------------------------------------------


def already_tapped_body_script(script_id: str, *, stop_when_refused: bool) -> dict[str, Any]:
    return one_move_script(
        script_id,
        {
            "kind": "repeat",
            "moves": [
                {
                    "kind": "activate",
                    "source": {
                        "zone": "battlefield",
                        "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"]},
                    },
                    "ability_index": 0,
                }
            ],
            "until": [{"kind": "mana_pool", "color": "G", "op": ">=", "n": 1000}],
            "max_iterations": 5,
            "stop_when_refused": stop_when_refused,
        },
    )


def test_stop_when_refused_false_exits_refused_with_the_engines_text(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Devoted Druid's own mana ability, with no untap move in the body: the
    first iteration taps it, the second tries to tap an already-tapped
    permanent - the engine's own refusal, since there is no `tapped: false`
    filter to make the runner refuse it first."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Devoted Druid", "alice", "battlefield")
    remove_summoning_sickness(board, "Devoted Druid")
    register(
        client, board, alice, already_tapped_body_script("refuse-stop", stop_when_refused=False)
    )
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="refuse-stop", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.REFUSED, result.to_dict()
    assert result.script_exit.refusal_source == "server"
    assert len(hook.posts("/actions/activate")) == 2


def test_stop_when_refused_true_ends_the_loop_normally(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    add_card(client, board, "Devoted Druid", "alice", "battlefield")
    remove_summoning_sickness(board, "Devoted Druid")
    register(
        client, board, alice, already_tapped_body_script("refuse-continue", stop_when_refused=True)
    )
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="refuse-continue", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert len(hook.posts("/actions/activate")) == 2


# -- a pending decision mid-loop --------------------------------------------------------


def test_a_pending_decision_mid_loop_is_answered_from_the_bodys_own_answers(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    add_card(client, board, "Krosan Tusker", "alice", "hand")
    for _ in range(3):
        add_card(client, board, "Forest", "alice", "battlefield")
    script = one_move_script(
        "cycle-loop",
        {
            "kind": "repeat",
            "moves": [
                {
                    "kind": "keyword_action",
                    "action": "cycle",
                    "card": {"zone": "hand", "select": {"keywords_any": ["Cycling"]}},
                    "answers": [{"decision": "may", "accept": False}],
                }
            ],
            "until": [{"kind": "zone_size", "zone": "hand", "side": "me", "op": "<=", "n": 0}],
            "max_iterations": 2,
        },
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="cycle-loop", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert hook.posts("/actions/cycle")


def test_a_pending_decision_mid_loop_not_covered_is_handed_back(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same loop, minus the `may` answer: the run stops the moment the
    engine asks a decision this move's `answers` do not cover - exactly the
    way any other move already does (`_answer`'s own "none for it" branch) -
    and the board is left paused exactly there, so a follow-up call that DOES
    answer it (a plain `/decide`, standing in for a resumed script) carries
    the game on from that same spot rather than losing the pause."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Krosan Tusker", "alice", "hand")
    for _ in range(3):
        add_card(client, board, "Forest", "alice", "battlefield")
    script = one_move_script(
        "cycle-uncovered",
        {
            "kind": "repeat",
            "moves": [
                {
                    "kind": "keyword_action",
                    "action": "cycle",
                    "card": {"zone": "hand", "select": {"keywords_any": ["Cycling"]}},
                }
            ],
            "until": [{"kind": "zone_size", "zone": "hand", "side": "me", "op": "<=", "n": 0}],
            "max_iterations": 2,
        },
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="cycle-uncovered", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    pending = state(client, board)["pending_decision"]
    assert pending is not None and pending["kind"] == "may", pending
    response = client.post(
        f"/api/games/{board}/decide",
        params={"uid": alice},
        json={"player": "alice", "accept": False, "reasoning": "resume by hand"},
    )
    assert response.json().get("accepted") is True, response.json()


# -- an opponent response mid-loop is interrupted ----------------------------------------


def test_an_opponent_action_mid_loop_is_interrupted(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    add_card(client, board, "Devoted Druid", "alice", "battlefield")
    remove_summoning_sickness(board, "Devoted Druid")
    add_card(client, board, "Vizier of Remedies", "alice", "battlefield")
    add_card(client, board, "Forest", "bob", "hand")
    script = one_move_script(
        "watched",
        {
            "kind": "repeat",
            "moves": [
                {
                    "kind": "activate",
                    "source": {
                        "zone": "battlefield",
                        "select": {
                            "types_all": ["Creature"],
                            "subtypes_any": ["Elf"],
                            "tapped": False,
                        },
                    },
                    "ability_index": 0,
                },
                {
                    "kind": "activate",
                    "source": {
                        "zone": "battlefield",
                        "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"]},
                    },
                    "ability_index": 1,
                },
            ],
            "until": [{"kind": "mana_pool", "color": "G", "op": ">=", "n": 1000}],
            "max_iterations": 20,
        },
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_plays_a_land(board):
        result = run_script(
            seat,
            script_id="watched",
            reasoning=REASONING,
            corpora=seats.script_corpora,
            max_seconds=5,
        )

    assert result.finished, result.to_dict()
    assert result.script_exit.exit is Exit.INTERRUPTED, result.to_dict()


# -- the time cap mid-loop, then resume --------------------------------------------------


def test_the_time_cap_mid_loop_then_resume(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    add_card(client, board, "Devoted Druid", "alice", "battlefield")
    remove_summoning_sickness(board, "Devoted Druid")
    add_card(client, board, "Vizier of Remedies", "alice", "battlefield")
    script = one_move_script(
        "slow-loop",
        {
            "kind": "repeat",
            "moves": [
                {
                    "kind": "activate",
                    "source": {
                        "zone": "battlefield",
                        "select": {
                            "types_all": ["Creature"],
                            "subtypes_any": ["Elf"],
                            "tapped": False,
                        },
                    },
                    "ability_index": 0,
                },
                {
                    "kind": "activate",
                    "source": {
                        "zone": "battlefield",
                        "select": {"types_all": ["Creature"], "subtypes_any": ["Elf"]},
                    },
                    "ability_index": 1,
                },
            ],
            "until": [{"kind": "mana_pool", "color": "G", "op": ">=", "n": 4}],
            "max_iterations": 10,
        },
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    seat._poll_seconds_override = None

    real_sleep = time.sleep

    def slow_sleep(_seconds: float) -> None:
        real_sleep(0.05)

    with opponent_passes(board):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(time, "sleep", slow_sleep)
            first = run_script(
                seat,
                script_id="slow-loop",
                reasoning=REASONING,
                corpora=seats.script_corpora,
                max_seconds=0.001,
            )

        assert not first.finished, first.to_dict()
        assert first.resume is not None

        second = run_script(
            seat,
            script_id="slow-loop",
            reasoning=REASONING,
            corpora=seats.script_corpora,
            resume=first.resume,
        )

    assert second.script_exit.exit is Exit.CHAIN_COMPLETE, second.to_dict()
    assert len(hook.posts("/actions/activate")) >= 8
