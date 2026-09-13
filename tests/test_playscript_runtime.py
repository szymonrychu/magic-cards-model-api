"""The play-script runner (`playscripts/runtime.py`): design D1's lifecycle.

Every exit in the closed enum is reached here at least once, most of them on
a real board through the real HTTP routes (the api repo is tested by ordinary
practice, so a few transport outcomes a live engine never produces on demand
- a board-moved refusal twice running, a 5xx, a MissingPlan 422 - are served
by a hook in front of the in-process app instead).

A seat's http client is wrapped by `Hooked`, which records every request the
runner sends and can answer one in place of the app. Nothing here stubs the
runner itself; `pass_until` is replaced only in the two wait tests, where the
point is what the runner asks it for and what it does with the answer.
"""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from playscript_testkit import FETCH_SHOCK, SLUG

from magic_cards_model_api import audit, seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.playscripts import runtime
from magic_cards_model_api.playscripts.runtime import Exit, run_script
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.seatkit import Seats
from magic_cards_model_api.store import store

REASONING = "thin the deck and fix my colours"

# -- plumbing ------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> Any:
        return self._body


class Hooked:
    """A seat's http client, recording every request and letting `answer`
    reply in place of the app (return None to send it for real)."""

    def __init__(
        self,
        inner: Any,
        answer: Callable[[str, str, Any], Any] | None = None,
        after: Callable[[str, str, Any], None] | None = None,
    ) -> None:
        self._inner = inner
        self._answer = answer
        self._after = after
        self.timeout = inner.timeout
        self.sent: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        body = kwargs.get("json")
        self.sent.append((method.upper(), path, copy.deepcopy(body)))
        replaced = self._answer(method.upper(), path, body) if self._answer else None
        if replaced is not None:
            return replaced
        response = self._inner.request(method, path, **kwargs)
        if self._after is not None:
            self._after(method.upper(), path, body)
        return response

    def get(self, path: str, **kwargs: Any) -> Any:
        self.sent.append(("GET", path, None))
        return self._inner.get(path, **kwargs)

    def posts(self, suffix: str) -> list[Any]:
        return [
            body for method, path, body in self.sent if method == "POST" and path.endswith(suffix)
        ]


def seat_uid(game_id: str, player: str) -> str:
    return next(uid for uid, name in store.seat_uids(game_id).items() if name == player)


def wired(monkeypatch: pytest.MonkeyPatch, game_id: str) -> Seats:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id)


def hooked_seat(seats: Seats, uid: str, **hooks: Any) -> tuple[seatkit.Seat, Hooked]:
    seat = seats.seat_for(uid)
    assert seat is not None
    hook = Hooked(seat.http, **hooks)
    seat.http = hook
    return seat, hook


def register(
    client: TestClient, game_id: str, uid: str, *scripts: dict[str, Any]
) -> dict[str, Any]:
    body = {"deck_slug": SLUG, "guides_revision": "rev0", "scripts": list(scripts)}
    response = client.put(
        f"/api/games/{game_id}/seats/{uid}/scripts", params={"uid": uid}, json=body
    )
    assert response.status_code == 200, response.text
    return body


def rows(game_id: str, action: str) -> list[dict[str, Any]]:
    return [entry for entry in audit.entries(game_id) if entry["action"] == action]


def started(game_id: str) -> list[dict[str, Any]]:
    return [row["detail"] for row in rows(game_id, audit.SCRIPT_RUN_STARTED_ACTION)]


def finished(game_id: str) -> list[dict[str, Any]]:
    return [row["detail"] for row in rows(game_id, audit.SCRIPT_RUN_ACTION)]


def cards_named(cards: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [card for card in cards if card["name"] == name]


@contextmanager
def opponent_passes(game_id: str, player: str = "bob") -> Iterator[None]:
    """The opponent passes whenever it holds priority over a non-empty
    stack - it never ends a step on the runner's behalf."""
    uid = seat_uid(game_id, player)
    other = TestClient(app)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            board = other.get(f"/api/games/{game_id}", params={"uid": uid}).json()
            if (
                board.get("priority_holder") == player
                and board.get("pending_decision") is None
                and board.get("stack")
            ):
                other.post(
                    f"/api/games/{game_id}/actions/pass-priority",
                    params={"uid": uid},
                    json={"player": player},
                )
            stop.wait(0.02)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(5)


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


# -- scripts -------------------------------------------------------------------

FETCH = "Search your library for"


def fetch_shock(**over: Any) -> dict[str, Any]:
    """The worked fetch-shock example, minus its library-size guard (these
    boards carry no library beyond what a test adds)."""
    doc = copy.deepcopy(FETCH_SHOCK)
    doc["false"] = [{"kind": "stack_top", "controller": "opponent", "select": {}}]
    doc.update(over)
    return doc


def land_script(script_id: str, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "id": script_id,
        "name": "Make the land drop",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [{"kind": "land_drop_available", "value": True}],
        "false": [],
        "moves": [{"kind": "play_land", "card": {"select": {"types_all": ["Land"]}}}],
        "next": [],
    }
    doc.update(over)
    return doc


def tap_island_script(script_id: str, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "id": script_id,
        "name": "Float blue",
        "priority": 40,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [
            {
                "kind": "count",
                "zone": "battlefield",
                "side": "me",
                "op": ">=",
                "n": 1,
                "select": {"subtypes_any": ["Island"], "tapped": False},
            }
        ],
        "false": [],
        "moves": [
            {
                "kind": "activate",
                "source": {"zone": "battlefield", "select": {"subtypes_any": ["Island"]}},
                "ability_text_has": "Add {U}",
            }
        ],
        "next": [],
    }
    doc.update(over)
    return doc


def strand_board(client: TestClient, game_id: str, *library: str) -> None:
    add_card(client, game_id, "Flooded Strand", "alice", "battlefield")
    for name in library or ("Steam Vents", "Island"):
        add_card(client, game_id, name, "alice", "library")


# -- chain_complete, reasoning, priority, corpus cache -------------------------


def test_fetch_shock_runs_to_chain_complete_on_a_real_board(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_shock()
    register(client, board, alice, script)
    strand_board(client, board)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.finished is True, result.to_dict()
    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.moves_done == 1
    after = state(client, board)
    vents = cards_named(after["battlefield"], "Steam Vents")
    assert vents and vents[0]["tapped"] is False, "pay_life accepted: the shock entered untapped"
    assert next(p for p in after["players"] if p["username"] == "alice")["life"] == 17
    assert [card.name for card in store.get(board).player("alice").library] == ["Island"]
    # Priority never passed beyond the resolution of the last move.
    assert after["priority_holder"] == "alice" and after["step"] == "precombat_main"

    sha8 = runtime.script_sha(script)[:8]
    suffix = f" | script {script['id']}@{sha8} move 1/1 (activate)"
    activation = hook.posts("/actions/activate")
    assert len(activation) == 1 and activation[0]["reasoning"] == REASONING + suffix
    for body in hook.posts("/decide"):
        assert body["reasoning"] == REASONING + suffix
        assert isinstance(body["generation"], int)
    assert hook.posts("/plan") == [], "the runner never declares a plan"

    [start] = started(board)
    [finish] = finished(board)
    assert start["run_id"] == finish["run_id"] == result.script_exit.run_id
    assert finish["exit"] == "chain_complete"
    assert finish["script_sha"] == runtime.script_sha(script)
    assert finish["moves_done"] == 1 and finish["moves_total"] == 1
    assert finish["resumes"] == 0 and isinstance(finish["wall_ms"], int)
    assert finish["reapplicable_after"] == "false", "the strand is gone, so it no longer applies"
    assert finish["parent_run_id"] is None

    # The corpus is cached on `Seats`, never on the `Seat`.
    assert (board, alice) in seats.script_corpora
    assert not any("script" in name.lower() for name in vars(seat))


def test_the_corpus_is_read_once_per_seats(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    seats = wired(monkeypatch, board)
    add_card(client, board, "Island", "alice", "hand")

    first, first_hook = hooked_seat(seats, alice)
    run_script(first, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)
    second, second_hook = hooked_seat(seats, alice)
    run_script(second, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    reads = [
        path
        for hook in (first_hook, second_hook)
        for method, path, _ in hook.sent
        if method == "GET" and path.endswith("/scripts")
    ]
    assert len(reads) == 1, reads


def test_moves_re_arm_expected_seq_and_hold_priority_between_them(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = land_script(
        "drop-and-float",
        moves=[
            {"kind": "play_land", "card": {"select": {"subtypes_any": ["Island"]}}},
            {
                "kind": "activate",
                "source": {"zone": "battlefield", "select": {"subtypes_any": ["Island"]}},
                "ability_text_has": "Add {U}",
            },
        ],
    )
    register(client, board, alice, script)
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [land] = hook.posts("/actions/play-land")
    [tap] = hook.posts("/actions/activate")
    assert isinstance(land["expected_seq"], int) and isinstance(tap["expected_seq"], int)
    assert tap["expected_seq"] > land["expected_seq"], "one watermark per move, never one per chain"
    assert land["reasoning"].endswith("move 1/2 (play_land)")
    assert tap["reasoning"].endswith("move 2/2 (activate)")
    assert hook.posts("/actions/pass-priority") == [], "priority is held between chained moves"


def test_the_runs_own_trigger_is_passed_through_and_answered(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = land_script(
        "sewers",
        moves=[
            {
                "kind": "play_land",
                "card": {"select": {"types_all": ["Land"], "subtypes_any": ["Swamp"]}},
                "answers": [{"decision": "surveil", "to_graveyard": {"types_any": ["Land"]}}],
            }
        ],
    )
    register(client, board, alice, script)
    add_card(client, board, "Undercity Sewers", "alice", "hand")
    add_card(client, board, "Mountain", "alice", "library")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert hook.posts("/actions/pass-priority"), "the runner passed on its own trigger"
    assert cards_named(state(client, board)["graveyards"]["alice"], "Mountain")


# -- conditions, archetypes, inputs -------------------------------------------


def test_a_failing_condition_exits_not_applicable_with_index_and_seen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_shock()
    script["true"][1] = {"kind": "life", "side": "me", "op": ">=", "n": 25}
    register(client, board, alice, script)
    strand_board(client, board)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    out = result.script_exit
    assert out.exit is Exit.NOT_APPLICABLE
    assert out.failing_condition == {"list": "true", "index": 1, "seen": 20}
    assert out.moves_done == 0 and hook.posts("/actions/activate") == []
    assert [row["exit"] for row in finished(board)] == ["not_applicable"]
    assert len(started(board)) == 1


def test_an_archetype_the_script_does_not_list_is_not_applicable(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = land_script("frog-only", opposing_archetypes=["dimir-frog"])
    register(client, board, alice, script)
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    unknown = run_script(
        seat, script_id="frog-only", reasoning=REASONING, corpora=seats.script_corpora
    )
    assert unknown.script_exit.exit is Exit.NOT_APPLICABLE
    assert unknown.script_exit.failing_condition["list"] == "opposing_archetypes"
    assert hook.posts("/actions/play-land") == []


def test_opposing_archetype_is_taken_from_the_agents_inputs(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = land_script(
        "frog-only",
        opposing_archetypes=["dimir-frog"],
        true=[
            {"kind": "land_drop_available", "value": True},
            {"kind": "input", "name": "opposing_archetype", "op": "eq", "value": "dimir-frog"},
        ],
    )
    register(client, board, alice, script)
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)

    result = run_script(
        seat,
        script_id="frog-only",
        reasoning=REASONING,
        corpora=seats.script_corpora,
        inputs={"opposing_archetype": "dimir-frog"},
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert finished(board)[0]["opposing_archetype_asserted"] == "dimir-frog"


PREFERENCE = {"name": "prefer_tempo", "type": "bool", "description": "Tempo first."}


def input_script(polarity: str = "true") -> dict[str, Any]:
    condition = {"kind": "input", "name": "prefer_tempo", "op": "eq", "value": True}
    doc = land_script("tempo-drop")
    doc[polarity] = [*doc[polarity], condition]
    return doc


def test_a_missing_required_input_exits_needs_input_naming_it(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, input_script())
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat,
        script_id="tempo-drop",
        reasoning=REASONING,
        corpora=seats.script_corpora,
        declared_inputs=[{**PREFERENCE, "required": True}],
    )

    assert result.script_exit.exit is Exit.NEEDS_INPUT
    assert result.script_exit.needs_input == "prefer_tempo"
    assert hook.posts("/actions/play-land") == []


def test_a_missing_optional_input_makes_its_condition_not_hold(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, input_script())
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)

    result = run_script(
        seat,
        script_id="tempo-drop",
        reasoning=REASONING,
        corpora=seats.script_corpora,
        declared_inputs=[{**PREFERENCE, "required": False}],
    )

    assert result.script_exit.exit is Exit.NOT_APPLICABLE
    assert result.script_exit.needs_input is None
    assert result.script_exit.failing_condition == {"list": "true", "index": 1, "seen": None}


def test_a_present_optional_input_takes_the_conditions_not_hold_path(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, input_script())
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat,
        script_id="tempo-drop",
        reasoning=REASONING,
        corpora=seats.script_corpora,
        inputs={"prefer_tempo": False},
        declared_inputs=[{**PREFERENCE, "required": False}],
    )

    assert result.script_exit.exit is Exit.NOT_APPLICABLE
    assert result.script_exit.failing_condition == {"list": "true", "index": 1, "seen": False}
    assert hook.posts("/actions/play-land") == []


# -- next: ties, chaining, limits ---------------------------------------------


def test_two_applicable_next_plays_exit_ambiguous_naming_both(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    first = land_script("drop", next=["float-a", "float-b"])
    register(
        client, board, alice, first, tap_island_script("float-a"), tap_island_script("float-b")
    )
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.AMBIGUOUS, result.to_dict()
    assert result.script_exit.applicable_next == ["float-a", "float-b"]
    assert hook.posts("/actions/activate") == [], "neither tied play was taken"


def test_one_applicable_next_play_chains_with_the_parent_run_id(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    blocked = tap_island_script(
        "float-never", true=[{"kind": "life", "side": "me", "op": ">=", "n": 99}]
    )
    register(
        client,
        board,
        alice,
        land_script("drop", next=["float-never", "float"]),
        blocked,
        tap_island_script("float"),
    )
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert result.script_exit.script_id == "float"
    assert result.script_exit.scripts_run == ["drop", "float"]
    parent, child = finished(board)
    assert parent["script_id"] == "drop" and child["script_id"] == "float"
    assert child["parent_run_id"] == parent["run_id"]
    assert [row["script_id"] for row in started(board)] == ["drop", "float"]
    assert len(hook.posts("/actions/activate")) == 1


def test_chain_limits() -> None:
    one_move = land_script("x")
    assert runtime.chain_limit_reason(["a"], 1, one_move) is None
    assert runtime.chain_limit_reason(["a", "x"], 1, one_move) is not None, "an id at most once"
    assert runtime.chain_limit_reason(["a", "b", "c"], 3, one_move) is not None, "3 scripts"
    four = land_script("y", moves=[one_move["moves"][0]] * 4)
    assert runtime.chain_limit_reason(["a"], 2, four) is None
    assert runtime.chain_limit_reason(["a"], 3, four) is not None, "6 moves"
    assert runtime.MAX_SCRIPTS_PER_RUN == 3 and runtime.MAX_MOVES_PER_RUN == 6


def test_a_next_play_already_run_in_this_run_is_not_run_again(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    loop = tap_island_script("float", next=["float"])
    register(client, board, alice, loop)
    add_card(client, board, "Island", "alice", "battlefield")
    add_card(client, board, "Island", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(seat, script_id="float", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE
    assert result.script_exit.stop_reason
    assert len(hook.posts("/actions/activate")) == 1


# -- selection ------------------------------------------------------------------


def test_two_different_sources_exit_ambiguous_naming_both(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_shock()
    register(client, board, alice, script)
    strand_board(client, board)
    add_card(client, board, "Polluted Delta", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.AMBIGUOUS
    assert sorted(result.script_exit.ambiguous_names) == ["Flooded Strand", "Polluted Delta"]
    assert hook.posts("/actions/activate") == []


def test_prefer_breaks_a_tie_between_different_candidates(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_shock()
    register(client, board, alice, script)
    strand_board(client, board, "Hallowed Fountain", "Steam Vents")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    after = state(client, board)
    assert cards_named(after["battlefield"], "Steam Vents"), "prefer Mountain chose the Vents"
    assert not cards_named(after["battlefield"], "Hallowed Fountain")
    vents = cards_named(after["battlefield"], "Steam Vents")[0]
    searches = [body["card_ids"] for body in hook.posts("/decide") if body.get("card_ids")]
    assert searches and all(ids == [vents["id"]] for ids in searches), searches


def test_different_candidates_tied_after_prefer_exit_ambiguous(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_shock()
    del script["moves"][0]["answers"][0]["pick"]["prefer"]
    register(client, board, alice, script)
    strand_board(client, board, "Steam Vents", "Hallowed Fountain")
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.AMBIGUOUS, result.to_dict()
    assert sorted(result.script_exit.ambiguous_names) == ["Hallowed Fountain", "Steam Vents"]


def test_an_empty_pick_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_shock()
    register(client, board, alice, script)
    strand_board(client, board, "Island")
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert result.script_exit.stop_reason


def test_a_decision_of_another_kind_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = fetch_shock()
    script["moves"][0]["answers"] = [{"decision": "pay_life", "accept": True}]
    register(client, board, alice, script)
    strand_board(client, board)
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert "choose_cards" in result.script_exit.stop_reason


# -- refusals and transport outcomes -------------------------------------------


def test_an_empty_reasoning_is_refused_before_any_call(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = run_script(seat, script_id="anything", reasoning="  ", corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.REFUSED
    assert result.script_exit.refusal_source == "runner"
    assert hook.sent == []


def test_an_engine_refusal_is_passed_through(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = land_script(
        "bolt-the-bear",
        true=[],
        moves=[
            {
                "kind": "cast",
                "card": {"zone": "hand", "select": {"types_all": ["Instant"]}},
                "targets": [{"select": {"types_all": ["Creature"]}, "side": "opponent"}],
            }
        ],
    )
    register(client, board, alice, script)
    add_card(client, board, "Lightning Bolt", "alice", "hand")
    add_card(client, board, "Grizzly Bears", "bob", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    out = result.script_exit
    assert out.exit is Exit.REFUSED, result.to_dict()
    assert out.refusal_source == "server"
    [cast_row] = [row for row in rows(board, "cast") if row["detail"].get("allowed") is False]
    assert out.refusal == cast_row["detail"]["reason"]
    assert finished(board)[0]["refusal_text"] == out.refusal
    [body] = hook.posts("/actions/cast")
    grizzly = cards_named(state(client, board)["battlefield"], "Grizzly Bears")[0]
    assert body["targets"] == [grizzly["id"]] and body["from_zone"] == "hand"


def refuse_activate(status: int, payload: Any) -> Callable[[str, str, Any], Any]:
    def answer(method: str, path: str, body: Any) -> Any:
        if method == "POST" and path.endswith("/actions/activate"):
            return FakeResponse(status, payload)
        return None

    return answer


def test_a_board_moved_refusal_twice_running_exits_stale(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_shock())
    strand_board(client, board)
    seats = wired(monkeypatch, board)
    stale = {"allowed": False, "stale": True, "reason": "the board moved", "moved_from": 1}
    seat, hook = hooked_seat(seats, alice, answer=refuse_activate(409, stale))

    result = run_script(
        seat, script_id=FETCH_SHOCK["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.STALE
    assert len(hook.posts("/actions/activate")) == 2, "run_action's one inherited retry"


def test_a_5xx_exits_unknown_outcome(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_shock())
    strand_board(client, board)
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice, answer=refuse_activate(500, {"detail": "boom"}))

    result = run_script(
        seat, script_id=FETCH_SHOCK["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNKNOWN_OUTCOME
    assert finished(board)[0]["exit"] == "unknown_outcome"


def test_a_missing_plan_exits_needs_plan_with_the_board_unchanged(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_shock())
    strand_board(client, board)
    before = state(client, board)
    missing = {
        "detail": [
            {"loc": ["body", "plan"], "msg": "alice has declared no plan", "type": "missing"}
        ]
    }
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice, answer=refuse_activate(422, missing))

    result = run_script(
        seat, script_id=FETCH_SHOCK["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.NEEDS_PLAN
    assert result.script_exit.moves_done == 0
    after = state(client, board)
    assert after["battlefield"] == before["battlefield"]
    assert after["players"] == before["players"]
    assert hook.posts("/plan") == []


def test_already_ran_from_the_start_route_is_surfaced(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    now = state(client, board)
    earlier = client.post(
        f"/api/games/{board}/script-runs",
        params={"uid": alice},
        json={
            "phase": "start",
            "run_id": "earlier",
            "seat": "alice",
            "role": "seat",
            "script_id": "drop",
            "turn": now["turn_number"],
            "step": now["step"],
        },
    )
    assert earlier.status_code == 200, earlier.text
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.ALREADY_RAN
    assert result.script_exit.prior_run_id == "earlier"
    assert hook.posts("/actions/play-land") == []


# -- waiting --------------------------------------------------------------------


def test_a_wait_that_stops_short_exits_unforeseen_with_every_stop_forced_on(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_turn_position(game, Step.UPKEEP, "alice")
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))
    asked: list[dict[str, Any]] = []

    def fake_pass_until(seat: Any, **kwargs: Any) -> dict[str, Any]:
        asked.append(kwargs)
        return {
            "finished": True,
            "stopped_because": "something new went on the stack: Lightning Bolt",
            "passes": 0,
            "state": seat.fetch_state(),
        }

    monkeypatch.setattr(runtime, "pass_until", fake_pass_until)
    seats = wired(monkeypatch, game)
    seat, _ = hooked_seat(seats, alice)

    result = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.UNFORESEEN
    assert result.script_exit.stop_reason == "something new went on the stack: Lightning Bolt"
    [kwargs] = asked
    assert kwargs["until_step"] == "precombat_main" and kwargs["whose_turn"] == "mine"
    assert kwargs["stop_on_stack"] is True
    assert kwargs["stop_when_attacked"] is True
    assert kwargs["stop_when_you_can_attack"] is True


def test_a_wait_that_reaches_the_step_runs_the_script(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_turn_position(game, Step.UPKEEP, "alice")
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")

    def fake_pass_until(seat: Any, **kwargs: Any) -> dict[str, Any]:
        force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
        return {
            "finished": True,
            "stopped_because": "reached your precombat main",
            "state": seat.fetch_state(),
        }

    monkeypatch.setattr(runtime, "pass_until", fake_pass_until)
    seats = wired(monkeypatch, game)
    seat, _ = hooked_seat(seats, alice)

    result = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert started(game)[0]["step"] == "precombat_main"


def test_without_wait_a_script_off_its_step_is_not_applicable(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_turn_position(game, Step.UPKEEP, "alice")
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("wait=False never passes priority")

    monkeypatch.setattr(runtime, "pass_until", refuse)
    seats = wired(monkeypatch, game)
    seat, _ = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora, wait=False
    )

    assert result.script_exit.exit is Exit.NOT_APPLICABLE
    assert result.script_exit.failing_condition["list"] == "when"


# -- resume -----------------------------------------------------------------------


def capped_fetch(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, script: dict[str, Any]
) -> tuple[Seats, dict[str, Any]]:
    """The fetch activated with nobody passing for bob, so the run hits its
    time cap waiting for the ability to resolve."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, script)
    strand_board(client, board)
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)
    result = run_script(
        seat,
        script_id=script["id"],
        reasoning=REASONING,
        corpora=seats.script_corpora,
        max_seconds=0.5,
    )
    assert result.finished is False, result.to_dict()
    return seats, result.resume


def test_the_cap_returns_a_resume_token_and_resuming_completes(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = fetch_shock()
    seats, token = capped_fetch(client, board, monkeypatch, script)

    assert set(runtime.RESUME_FIELDS) <= set(token)
    assert token["script_id"] == script["id"]
    assert token["script_sha"] == runtime.script_sha(script)
    assert token["move_index"] == 1 and token["answer_index"] == 0
    assert token["turn"] == state(client, board)["turn_number"]
    assert token["step"] == "precombat_main" and token["parent_run_id"] is None
    assert token["known_stack_ids"], "the fetch's own ability is tracked"

    seat, _ = hooked_seat(seats, seat_uid(board, "alice"))
    with opponent_passes(board):
        result = run_script(
            seat,
            script_id=script["id"],
            reasoning=REASONING,
            corpora=seats.script_corpora,
            resume=token,
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert len(started(board)) == 1
    [finish] = finished(board)
    assert finish["resumes"] == 1 and finish["run_id"] == token["run_id"]
    assert cards_named(state(client, board)["battlefield"], "Steam Vents")


def test_resume_is_refused_when_the_script_sha_no_longer_matches(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = fetch_shock()
    seats, token = capped_fetch(client, board, monkeypatch, script)
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_shock(priority=61))
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat,
        script_id=script["id"],
        reasoning=REASONING,
        corpora=seats.script_corpora,
        resume=token,
    )

    assert result.script_exit.exit is Exit.REFUSED
    assert result.script_exit.refusal_source == "runner"
    assert hook.posts("/decide") == [] and hook.posts("/actions/pass-priority") == []


def test_resume_is_refused_when_the_step_moved(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = fetch_shock()
    seats, token = capped_fetch(client, board, monkeypatch, script)
    force_turn_position(board, Step.POSTCOMBAT_MAIN, "alice")
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = run_script(
        seat,
        script_id=script["id"],
        reasoning=REASONING,
        corpora=seats.script_corpora,
        resume=token,
    )

    assert result.script_exit.exit is Exit.REFUSED
    assert result.script_exit.refusal_source == "runner"
    assert hook.posts("/actions/pass-priority") == []


def test_resume_after_the_start_record_is_refused_when_the_turn_moved(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap trips after the start record posted but before move 1 is
    sent (`move_index` 0). The start route's once-per-turn-and-step check
    has already run for that turn, so a resume in a later turn must be
    refused, never run its moves under the old start record."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)

    def slow_start(method: str, path: str, body: Any) -> None:
        if method == "POST" and path.endswith("/script-runs") and body.get("phase") == "start":
            time.sleep(0.4)

    seat, _ = hooked_seat(seats, alice, after=slow_start)
    capped = run_script(
        seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora, max_seconds=0.3
    )
    assert capped.finished is False, capped.to_dict()
    token = capped.resume
    assert token["move_index"] == 0 and token["chain"]["started"] is True
    [start] = started(board)
    assert (token["turn"], token["step"]) == (start["turn"], start["step"])

    store.get(board).turn.turn_number += 1
    seat, hook = hooked_seat(seats, alice)
    result = run_script(
        seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora, resume=token
    )

    assert result.script_exit.exit is Exit.REFUSED, result.to_dict()
    assert result.script_exit.refusal_source == "runner"
    assert hook.posts("/actions/play-land") == []
    assert len(started(board)) == 1
    [finish] = finished(board)
    assert (finish["turn"], finish["step"]) == (start["turn"], start["step"])


def test_resume_re_checks_that_nothing_foreign_happened(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = fetch_shock()
    seats, token = capped_fetch(client, board, monkeypatch, script)
    bob = seat_uid(board, "bob")
    add_card(client, board, "Mountain", "bob", "battlefield")
    add_card(client, board, "Lightning Bolt", "bob", "hand")
    bob_board = state(client, board, uid=bob)
    bolt = cards_named(bob_board["hands"]["bob"], "Lightning Bolt")[0]
    mountain = cards_named(bob_board["battlefield"], "Mountain")[0]
    cast = client.post(
        f"/api/games/{board}/actions/cast",
        params={"uid": bob},
        json={
            "player": "bob",
            "card_id": bolt["id"],
            "targets": ["alice"],
            "mana_sources": [mountain["id"]],
        },
    )
    assert cast.status_code == 200 and cast.json()["allowed"] is True, cast.text
    seat, _ = hooked_seat(seats, seat_uid(board, "alice"))

    result = run_script(
        seat,
        script_id=script["id"],
        reasoning=REASONING,
        corpora=seats.script_corpora,
        resume=token,
    )

    assert result.script_exit.exit is Exit.INTERRUPTED, result.to_dict()


def test_another_players_non_pass_action_interrupts_with_nothing_on_the_stack(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log check on its own: bob taps a Mountain for mana while the
    run's fetch waits on the stack. A mana ability never uses the stack, so
    only the log since the run's watermark shows it. The Mountain is added
    before the run, so its `add_card` row is under the watermark."""
    script = fetch_shock()
    add_card(client, board, "Mountain", "bob", "battlefield")
    seats, token = capped_fetch(client, board, monkeypatch, script)
    bob = seat_uid(board, "bob")
    mountain = cards_named(state(client, board, uid=bob)["battlefield"], "Mountain")[0]
    tapped = client.post(
        f"/api/games/{board}/actions/activate",
        params={"uid": bob},
        json={"player": "bob", "card_id": mountain["id"]},
    )
    assert tapped.status_code == 200 and tapped.json()["allowed"] is True, tapped.text
    assert tapped.json()["mana_ability"] is True
    assert [obj["controller"] for obj in state(client, board)["stack"]] == ["alice"]
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = run_script(
        seat,
        script_id=script["id"],
        reasoning=REASONING,
        corpora=seats.script_corpora,
        resume=token,
    )

    assert result.script_exit.exit is Exit.INTERRUPTED, result.to_dict()
    assert "bob acted (activate" in result.script_exit.stop_reason
    assert hook.posts("/decide") == [] and hook.posts("/actions/pass-priority") == []


def test_a_foreign_object_on_the_stack_interrupts_when_the_log_shows_nothing_new(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stack check on its own: bob's Bolt is on the stack, but the token's
    watermark is already past the cast, so the log since it holds nothing."""
    script = fetch_shock()
    seats, token = capped_fetch(client, board, monkeypatch, script)
    alice = seat_uid(board, "alice")
    bob = seat_uid(board, "bob")
    add_card(client, board, "Mountain", "bob", "battlefield")
    add_card(client, board, "Lightning Bolt", "bob", "hand")
    bob_board = state(client, board, uid=bob)
    cast = client.post(
        f"/api/games/{board}/actions/cast",
        params={"uid": bob},
        json={
            "player": "bob",
            "card_id": cards_named(bob_board["hands"]["bob"], "Lightning Bolt")[0]["id"],
            "targets": ["alice"],
            "mana_sources": [cards_named(bob_board["battlefield"], "Mountain")[0]["id"]],
        },
    )
    assert cast.status_code == 200 and cast.json()["allowed"] is True, cast.text
    log = client.get(f"/api/games/{board}/log", params={"uid": alice}).json()["log"]
    token = {**token, "last_seq": max(int(entry["seq"]) for entry in log)}
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat,
        script_id=script["id"],
        reasoning=REASONING,
        corpora=seats.script_corpora,
        resume=token,
    )

    assert result.script_exit.exit is Exit.INTERRUPTED, result.to_dict()
    assert result.script_exit.stop_reason == "bob put Lightning Bolt on the stack"
    assert hook.posts("/decide") == [] and hook.posts("/actions/pass-priority") == []


# -- the pause-slot race --------------------------------------------------------


def test_the_pause_slot_clobber_race_surfaces_as_stale_or_interrupted(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tests/test_pause_slot_clobber.py`'s live defect, replayed against the
    runner: the opponent's autopass re-posts `pass-priority` in the window
    between the runner's answer to the search and its answer to the
    shockland. The runner must never carry on as if nothing happened."""
    alice = seat_uid(board, "alice")
    bob = seat_uid(board, "bob")
    register(client, board, alice, fetch_shock())
    strand_board(client, board)
    other = TestClient(app)
    fired: list[bool] = []

    def clobber(method: str, path: str, body: Any) -> None:
        if method == "POST" and path.endswith("/decide") and not fired:
            fired.append(True)
            other.post(
                f"/api/games/{board}/actions/pass-priority",
                params={"uid": bob},
                json={"player": "bob"},
            )

    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice, after=clobber)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=FETCH_SHOCK["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert fired, "the race window was reached"
    assert result.script_exit.exit in (Exit.STALE, Exit.INTERRUPTED), result.to_dict()


# -- advise mode ------------------------------------------------------------------


def test_advise_mode_proposes_move_one_and_returns_the_rest_unvalidated(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    advisor = "advisor-uid"
    store.register_advisors(board, {advisor: Advisor(player="alice")})
    script = fetch_shock()
    script["moves"].append({"kind": "play_land", "card": {"select": {"types_all": ["Land"]}}})
    register(client, board, advisor, script)
    strand_board(client, board)
    before = state(client, board)

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("advise mode never calls pass_until")

    monkeypatch.setattr(runtime, "pass_until", refuse)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, advisor)
    assert seat.advise is True

    result = run_script(
        seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    out = result.script_exit
    assert out.exit is Exit.ADVISED, result.to_dict()
    assert out.proposal is not None and out.proposal.get("proposed") is True
    assert [entry["kind"] for entry in out.unvalidated] == ["play_land"]
    assert out.moves_done == 0
    after = state(client, board)
    assert after["battlefield"] == before["battlefield"] and after["players"] == before["players"]
    assert len(rows(board, "advice")) == 1
    [start] = started(board)
    [finish] = finished(board)
    assert start["role"] == finish["role"] == "advisor"
    assert start["advise"] is True and finish["exit"] == "advised"
    assert hook.posts("/actions/pass-priority") == []


def test_the_exit_vocabulary_is_closed() -> None:
    assert [exit.value for exit in Exit] == [
        "chain_complete",
        "not_applicable",
        "unforeseen",
        "interrupted",
        "ambiguous",
        "refused",
        "stale",
        "unknown_outcome",
        "needs_plan",
        "already_ran",
        "advised",
        "needs_input",
    ]
