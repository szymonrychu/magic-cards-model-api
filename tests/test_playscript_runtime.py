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
from playscript_testkit import FETCH_SHOCK, FETCH_SURVEIL, SLUG, state_view

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


def test_a_bare_surveil_answer_keeps_everything_on_top(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B7-07: a bare `{"decision": "surveil"}` answer (no `to_graveyard`) is
    schema-valid and means "keep everything on top" - the shape
    `fetch-surveil-land-keep` uses. It must send `moved: []`, not leave the
    surveil unanswered."""
    alice = seat_uid(board, "alice")
    script = land_script(
        "sewers-keep",
        moves=[
            {
                "kind": "play_land",
                "card": {"select": {"types_all": ["Land"], "subtypes_any": ["Swamp"]}},
                "answers": [{"decision": "surveil"}],
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
    body = hook.posts("/decide")[-1]
    assert body["moved"] == [] and body["top_order"] is None
    assert cards_named(state(client, board)["graveyards"]["alice"], "Mountain") == []


def test_a_bare_scry_answer_keeps_everything_on_top(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B7-07's scry twin: a bare `{"decision": "scry"}` answer keeps every
    scried card on top instead of leaving the decision unanswered."""
    alice = seat_uid(board, "alice")
    script = land_script(
        "peek",
        true=[],
        moves=[
            {
                "kind": "cast",
                "card": {"zone": "hand", "select": {"types_all": ["Sorcery"]}},
                "answers": [{"decision": "scry"}],
            }
        ],
    )
    register(client, board, alice, script)
    add_card(client, board, "Preordain", "alice", "hand")
    add_card(client, board, "Island", "alice", "battlefield")
    add_card(client, board, "Mountain", "alice", "library")
    add_card(client, board, "Forest", "alice", "library")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    body = hook.posts("/decide")[-1]
    assert body["moved"] == [] and body["top_order"] is None


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
    # B7-14: the fetch itself was already accepted before the search answer
    # turned up ambiguous.
    assert result.script_exit.moves_done == 1, result.to_dict()


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
    # B7-14: the fetch itself was already accepted before the search came up
    # with nothing to answer it with.
    assert result.script_exit.moves_done == 1, result.to_dict()


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
    # B7-14: the fetch itself was already accepted before the mismatched
    # answer kind surfaced.
    assert result.script_exit.moves_done == 1, result.to_dict()


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


def test_a_run_after_game_over_is_refused_with_no_ledger_row(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B8-06: a finished game (CR 104.1) refuses `script-runs` the same way
    every other action route is refused, and the run reports that refusal -
    no `script_run_started`/`script_run` row for a game that is already
    over."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    conceded = client.post(f"/api/games/{board}/actions/concede", json={"player": "bob"})
    assert conceded.status_code == 200 and conceded.json()["winner"] == "alice", conceded.text
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.REFUSED, result.to_dict()
    assert hook.posts("/actions/play-land") == []
    assert started(board) == [] and finished(board) == []


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


def test_an_advisors_advise_run_does_not_consume_the_seats_own_slot(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B7-08: `already_ran` is scoped per uid role. An advisor's advise run
    of a script proposes a move and takes nothing; the seat's own run of the
    same script, at the same turn and step, must still proceed."""
    alice = seat_uid(board, "alice")
    advisor = "b708-advisor-uid"
    store.register_advisors(board, {advisor: Advisor(player="alice")})
    script = land_script("drop")
    register(client, board, alice, script)
    register(client, board, advisor, script)
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)

    advisor_seat, _ = hooked_seat(seats, advisor)
    advised = run_script(
        advisor_seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora
    )
    assert advised.script_exit.exit is Exit.ADVISED, advised.to_dict()

    seat, hook = hooked_seat(seats, alice)
    result = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert hook.posts("/actions/play-land"), "the seat's own run must actually play the land"


def test_two_advise_runs_by_the_same_advisor_still_exit_already_ran(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B7-08's other half: the role scope narrows `already_ran` to the uid
    kind, not away entirely - two advise runs of the same script by the same
    advisor, in the same turn and step, still collide."""
    advisor = "b708-advisor-uid-2"
    store.register_advisors(board, {advisor: Advisor(player="alice")})
    script = land_script("drop")
    register(client, board, advisor, script)
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, advisor)

    first = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)
    assert first.script_exit.exit is Exit.ADVISED, first.to_dict()

    second = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)
    assert second.script_exit.exit is Exit.ALREADY_RAN, second.to_dict()


def test_a_script_dropped_from_the_corpus_after_the_read_is_refused_at_its_start(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner resolves a script from the corpus it read once per `Seats`,
    so the start route's `unknown_script` refusal is reachable only when the
    seat registers a corpus without that script after that read. The run
    exits refused in the server's words, naming the script, makes no move,
    and posts no finish for a run the ledger never started."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    seats.script_corpora[(board, alice)] = client.get(
        f"/api/games/{board}/seats/{alice}/scripts", params={"uid": alice}
    ).json()
    register(client, board, alice, land_script("other"))

    result = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    out = result.script_exit
    assert out.exit is Exit.REFUSED, result.to_dict()
    assert out.refusal_source == "server"
    assert "'drop'" in (out.refusal or "")
    assert out.script_id == "drop"
    assert hook.posts("/actions/play-land") == []
    assert [body["phase"] for body in hook.posts("/script-runs")] == ["start"]
    assert started(board) == [] and finished(board) == []


def assert_refused_unregistered(result: Any, hook: Hooked, game_id: str, script_id: str) -> None:
    out = result.script_exit
    assert out.exit is Exit.REFUSED, result.to_dict()
    assert out.refusal_source == "server"
    assert f"'{script_id}'" in (out.refusal or "")
    assert out.script_id == script_id
    assert [body["phase"] for body in hook.posts("/script-runs")] == ["start"]
    assert started(game_id) == [] and finished(game_id) == []


@pytest.mark.parametrize(
    "stale",
    [{"priority": 0}, {"opposing_archetypes": ["dimir-frog"]}],
    ids=["priority-0", "archetype-not-listed"],
)
def test_an_exit_before_the_start_from_a_stale_corpus_records_no_finish(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, stale: dict[str, Any]
) -> None:
    """A run that exits before its start (priority 0, an unlisted archetype)
    posts its start on the way out. From a stale corpus that start is refused
    `unknown_script` too, and the run reports that refusal with no finish."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop", **stale))
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    seats.script_corpora[(board, alice)] = client.get(
        f"/api/games/{board}/seats/{alice}/scripts", params={"uid": alice}
    ).json()
    register(client, board, alice, land_script("other"))

    result = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    assert_refused_unregistered(result, hook, board, "drop")


def test_a_wait_exit_from_a_stale_corpus_records_no_finish(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_turn_position(game, Step.UPKEEP, "alice")
    alice = seat_uid(game, "alice")
    register(client, game, alice, land_script("drop"))

    def stops_short(seat: Any, **_: Any) -> dict[str, Any]:
        return {
            "finished": True,
            "stopped_because": "something new went on the stack: Lightning Bolt",
            "passes": 0,
            "state": seat.fetch_state(),
        }

    monkeypatch.setattr(runtime, "pass_until", stops_short)
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, alice)
    seats.script_corpora[(game, alice)] = client.get(
        f"/api/games/{game}/seats/{alice}/scripts", params={"uid": alice}
    ).json()
    register(client, game, alice, land_script("other"))

    result = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)

    assert_refused_unregistered(result, hook, game, "drop")


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


def theirs_float_script(script_id: str, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "id": script_id,
        "name": "Float blue on the opponent's end step",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "theirs", "steps": ["end_step"], "stack": "empty"},
        "true": [],
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


def test_a_theirs_script_waits_for_this_seats_own_priority_before_its_first_move(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B8-04: `when` names a board position (step, turn, stack), not a
    priority window. bob's `theirs` script is already sitting at alice's
    end_step on an empty stack, but alice (the active player) holds priority
    there first - the runner waits for bob's own priority in this same
    occurrence rather than sending a move the engine can only refuse ("bob
    does not have priority, alice does")."""
    force_turn_position(game, Step.END_STEP, "alice")
    bob = seat_uid(game, "bob")
    script = theirs_float_script("float-theirs")
    register(client, game, bob, script)
    add_card(client, game, "Island", "bob", "battlefield")
    assert state(client, game)["priority_holder"] == "alice"
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, bob)

    def alice_passes_soon() -> None:
        time.sleep(0.05)
        TestClient(app).post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})

    thread = threading.Thread(target=alice_passes_soon, daemon=True)
    thread.start()
    try:
        result = run_script(
            seat, script_id="float-theirs", reasoning=REASONING, corpora=seats.script_corpora
        )
    finally:
        thread.join(5)

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    assert hook.posts("/actions/activate")
    assert hook.posts("/actions/activate")[0].get("mana_sources") in (None, [])


def test_a_theirs_script_without_wait_is_not_applicable_while_the_other_seat_holds_priority(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-wait` never blocks on the other seat's priority; the same
    position is simply not applicable yet."""
    force_turn_position(game, Step.END_STEP, "alice")
    bob = seat_uid(game, "bob")
    script = theirs_float_script("float-theirs")
    register(client, game, bob, script)
    add_card(client, game, "Island", "bob", "battlefield")
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, bob)

    result = run_script(
        seat,
        script_id="float-theirs",
        reasoning=REASONING,
        corpora=seats.script_corpora,
        wait=False,
    )

    assert result.script_exit.exit is Exit.NOT_APPLICABLE, result.to_dict()
    assert hook.posts("/actions/activate") == []


def test_a_game_that_ends_while_waiting_for_own_priority_ends_the_run(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished game never hands this seat priority again, so the wait
    ends the run rather than polling to the time cap and handing back a
    resume token that would only poll again."""
    force_turn_position(game, Step.END_STEP, "alice")
    bob = seat_uid(game, "bob")
    script = theirs_float_script("float-theirs")
    register(client, game, bob, script)
    add_card(client, game, "Island", "bob", "battlefield")
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, bob)
    fetch = seat.fetch_state
    calls = {"n": 0}

    def ends_after_first_read(**kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        read = fetch(**kwargs)
        return read if calls["n"] == 1 else {**read, "winner": "alice"}

    monkeypatch.setattr(seat, "fetch_state", ends_after_first_read)

    result = run_script(
        seat,
        script_id="float-theirs",
        reasoning=REASONING,
        corpora=seats.script_corpora,
        max_seconds=1.0,
    )

    assert result.finished is True, result.to_dict()
    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/actions/activate") == []


def test_a_decision_for_this_seat_while_waiting_for_own_priority_ends_the_run(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question put to this seat is only ever answered by this seat, so
    waiting for priority behind it could never end before the time cap."""
    force_turn_position(game, Step.END_STEP, "alice")
    bob = seat_uid(game, "bob")
    script = theirs_float_script("float-theirs")
    register(client, game, bob, script)
    add_card(client, game, "Island", "bob", "battlefield")
    seats = wired(monkeypatch, game)
    seat, hook = hooked_seat(seats, bob)
    fetch = seat.fetch_state
    calls = {"n": 0}
    asked = {"player": "bob", "kind": "surveil", "prompt": "Surveil 1"}

    def asks_after_first_read(**kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        read = fetch(**kwargs)
        return read if calls["n"] == 1 else {**read, "pending_decision": asked}

    monkeypatch.setattr(seat, "fetch_state", asks_after_first_read)

    result = run_script(
        seat,
        script_id="float-theirs",
        reasoning=REASONING,
        corpora=seats.script_corpora,
        max_seconds=1.0,
    )

    assert result.finished is True, result.to_dict()
    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert hook.posts("/actions/activate") == []


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
    # B7-14: the engine already accepted this move (activated, land
    # sacrificed, life paid) before the cap cut the run off mid-resolution,
    # so the token's own `moves_done` must already say so.
    assert token["chain"]["moves_done"] == 1, token

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
    # B7-14: the fetch was already accepted before the cast interrupted it.
    assert result.script_exit.moves_done == 1, result.to_dict()


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
    # B7-14: the fetch was already accepted before the interruption.
    assert result.script_exit.moves_done == 1, result.to_dict()


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


def test_the_pause_slot_clobber_race_never_reports_interrupted(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tests/test_pause_slot_clobber.py`'s live defect, replayed against the
    runner: the opponent's autopass re-posts `pass-priority` in the window
    between the runner's answer to the search and its answer to the
    shockland.

    B7-09: that re-posted pass itself pauses on *this run's own* pending
    decision (`paused_on.player == alice`) - the engine's own CR 608.2g fix
    (`test_pause_slot_clobber.py`) already keeps it from touching the search
    that was already answered, so it is not a board change: never
    `interrupted`, read off the entry's typed `paused`/`paused_on` fields
    rather than guessed at."""
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
    assert result.script_exit.exit is not Exit.INTERRUPTED, result.to_dict()
    assert result.script_exit.exit in (Exit.STALE, Exit.CHAIN_COMPLETE), result.to_dict()


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
        "invalid_input",
    ]


# -- cast.cost ------------------------------------------------------------------


def one_move_script(script_id: str, move: dict[str, Any]) -> dict[str, Any]:
    return land_script(script_id, true=[], moves=[move])


def test_a_cast_cost_is_announced_as_the_alternative_cost_and_the_engine_casts_for_it(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overlord of the Mistmoors costs {5}{W}{W}; four Plains only pay its
    impending {2}{W}{W} (CR 702.176a), so a cast that lands paid for impending."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "impend",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Creature"]}},
            "cost": "impending",
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Overlord of the Mistmoors", "alice", "hand")
    for _ in range(4):
        add_card(client, board, "Plains", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="impend", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["alternative_cost"] == "impending"
    [overlord] = cards_named(state(client, board)["battlefield"], "Overlord of the Mistmoors")
    assert overlord["counters"] == {"time": 4}, "entered with impending's time counters"
    assert finished(board)[0]["exit"] == "chain_complete"


def test_a_cast_cost_the_card_does_not_offer_is_refused_with_the_engines_text(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "bolt-flashback",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Instant"]}},
            "cost": "flashback",
            "targets": [{"player": "opponent"}],
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Lightning Bolt", "alice", "hand")
    add_card(client, board, "Mountain", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="bolt-flashback", reasoning=REASONING, corpora=seats.script_corpora
    )

    out = result.script_exit
    assert out.exit is Exit.REFUSED, result.to_dict()
    assert out.refusal_source == "server"
    assert "alternative cost for Lightning Bolt" in out.refusal
    [body] = hook.posts("/actions/cast")
    assert body["alternative_cost"] == "flashback"
    assert finished(board)[0]["refusal_text"] == out.refusal


def test_a_printed_cost_is_announced_by_the_cards_own_name(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`printed` is the card's own CR 118.9 cost, which the engine names by the
    spell's name; Force of Negation's pitch is refused on its caster's own turn
    in the engine's words."""
    alice = seat_uid(board, "alice")
    script = one_move_script(
        "pitch",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Instant"]}},
            "cost": "printed",
        },
    )
    register(client, board, alice, script)
    add_card(client, board, "Force of Negation", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(seat, script_id="pitch", reasoning=REASONING, corpora=seats.script_corpora)

    assert result.script_exit.exit is Exit.REFUSED, result.to_dict()
    assert result.script_exit.refusal_source == "server"
    [body] = hook.posts("/actions/cast")
    assert body["alternative_cost"] == "Force of Negation"


# -- targets --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("player", "hit", "cost"), [("opponent", "bob", "normal"), ("me", "alice", None)]
)
def test_a_player_target_is_sent_as_that_players_name(
    client: TestClient,
    board: str,
    monkeypatch: pytest.MonkeyPatch,
    player: str,
    hit: str,
    cost: str | None,
) -> None:
    alice = seat_uid(board, "alice")
    move: dict[str, Any] = {
        "kind": "cast",
        "card": {"zone": "hand", "select": {"types_all": ["Instant"]}},
        "targets": [{"player": player}],
    }
    if cost is not None:
        move["cost"] = cost
    register(client, board, alice, one_move_script("bolt", move))
    add_card(client, board, "Lightning Bolt", "alice", "hand")
    add_card(client, board, "Mountain", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="bolt", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert body["targets"] == [hit]
    assert "alternative_cost" not in body, "normal (or no cost) casts for the mana cost"
    after = state(client, board)
    assert next(p for p in after["players"] if p["username"] == hit)["life"] == 17


def bolt_a_creature(client: TestClient, board: str) -> dict[str, Any]:
    script = one_move_script(
        "bolt-creature",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Instant"]}},
            "targets": [{"select": {"types_all": ["Creature"]}, "side": "opponent"}],
        },
    )
    register(client, board, seat_uid(board, "alice"), script)
    add_card(client, board, "Lightning Bolt", "alice", "hand")
    add_card(client, board, "Mountain", "alice", "battlefield")
    return script


def test_a_card_target_matching_nothing_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bolt_a_creature(client, board)
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = run_script(
        seat, script_id="bolt-creature", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert "target 1" in result.script_exit.stop_reason
    assert hook.posts("/actions/cast") == []


def test_different_card_targets_tied_exit_ambiguous(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bolt_a_creature(client, board)
    add_card(client, board, "Grizzly Bears", "bob", "battlefield")
    add_card(client, board, "Savannah Lions", "bob", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = run_script(
        seat, script_id="bolt-creature", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.AMBIGUOUS, result.to_dict()
    assert result.script_exit.ambiguous_names == ["Grizzly Bears", "Savannah Lions"]
    assert hook.posts("/actions/cast") == []


# -- activate: targets and sacrifice ---------------------------------------------


def bombard(target: dict[str, Any], sacrifice: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "kind": "activate",
        "source": {"zone": "battlefield", "select": {"types_all": ["Enchantment"]}},
        "ability_text_has": "Sacrifice a creature",
        "targets": [target],
    }
    if sacrifice is not None:
        move["sacrifice"] = sacrifice
    return one_move_script("bombard", move)


def test_an_activation_sacrifices_by_selector_and_targets_a_player(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        bombard({"player": "opponent"}, {"select": {"types_all": ["Creature"]}}),
    )
    add_card(client, board, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    bears = cards_named(state(client, board)["battlefield"], "Grizzly Bears")[0]

    with opponent_passes(board):
        result = run_script(
            seat, script_id="bombard", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/activate")
    assert body["sacrificing"] == [bears["id"]] and body["targets"] == ["bob"]
    after = state(client, board)
    assert cards_named(after["graveyards"]["alice"], "Grizzly Bears")
    assert next(p for p in after["players"] if p["username"] == "bob")["life"] == 19


def test_an_activation_targets_a_card_by_selector_and_side(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        bombard(
            {"select": {"types_all": ["Creature"]}, "side": "opponent"},
            {"select": {"types_all": ["Creature"]}},
        ),
    )
    add_card(client, board, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    add_card(client, board, "Savannah Lions", "bob", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    lions = cards_named(state(client, board)["battlefield"], "Savannah Lions")[0]

    with opponent_passes(board):
        result = run_script(
            seat, script_id="bombard", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/activate")
    assert body["targets"] == [lions["id"]]
    assert cards_named(state(client, board)["graveyards"]["bob"], "Savannah Lions")


def test_sacrifice_none_names_nothing_and_the_engine_refuses_the_cost(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, bombard({"player": "opponent"}, "none"))
    add_card(client, board, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="bombard", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.REFUSED, result.to_dict()
    assert "sacrifice needs 1 card(s) chosen, 0 given" in result.script_exit.refusal
    [body] = hook.posts("/actions/activate")
    assert "sacrificing" not in body


def test_a_sacrifice_choice_matching_nothing_exits_unforeseen(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        bombard({"player": "opponent"}, {"select": {"types_all": ["Creature"]}}),
    )
    add_card(client, board, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "bob", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat, script_id="bombard", reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.UNFORESEEN, result.to_dict()
    assert "sacrifice" in result.script_exit.stop_reason
    assert hook.posts("/actions/activate") == []


def test_a_sacrifice_choice_tied_between_different_cards_exits_ambiguous_unless_preferred(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    choice: dict[str, Any] = {"select": {"types_all": ["Creature"]}}
    register(client, board, alice, bombard({"player": "opponent"}, choice))
    add_card(client, board, "Goblin Bombardment", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "alice", "battlefield")
    add_card(client, board, "Savannah Lions", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    tied = run_script(seat, script_id="bombard", reasoning=REASONING, corpora=seats.script_corpora)

    assert tied.script_exit.exit is Exit.AMBIGUOUS, tied.to_dict()
    assert tied.script_exit.ambiguous_names == ["Grizzly Bears", "Savannah Lions"]
    assert hook.posts("/actions/activate") == []

    store.get(board).turn.turn_number += 1
    preferred = {**choice, "prefer": [{"toughness": {"op": "<=", "n": 1}}]}
    register(client, board, alice, bombard({"player": "opponent"}, preferred))
    seat, hook = hooked_seat(seats, alice)
    with opponent_passes(board):
        chosen = run_script(seat, script_id="bombard", reasoning=REASONING, corpora={})
    assert chosen.script_exit.exit is Exit.CHAIN_COMPLETE, chosen.to_dict()
    lions = cards_named(state(client, board)["graveyards"]["alice"], "Savannah Lions")
    assert lions and hook.posts("/actions/activate")[0]["sacrificing"] == [lions[0]["id"]]


def test_cavern_of_souls_played_with_eldrazi_answers_choose_a_quality_with_choice(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O6: `Answer.choice`, "for choose_a_quality and choose_a_mode only",
    proved against the real "As this land enters, choose a creature type"
    replacement (CR 614.12a) on a real Cavern of Souls, played from hand
    through play_land."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Cavern of Souls", "alice", "hand")
    script = land_script(
        "cavern",
        moves=[
            {
                "kind": "play_land",
                "card": {"select": {}},
                "answers": [{"decision": "choose_a_quality", "choice": "Eldrazi"}],
            }
        ],
    )
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(seat, script_id="cavern", reasoning=REASONING, corpora=seats.script_corpora)
    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    (decided,) = hook.posts("/decide")
    assert decided["choice"] == "Eldrazi"

    cavern = cards_named(state(client, board)["battlefield"], "Cavern of Souls")[0]
    assert cavern["choice"] == {"kind": "creature_type", "value": "Eldrazi"}


def test_play_land_ties_any_takes_the_first_by_sorted_name_instead_of_ambiguous(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O2: `prefer` (up to 4) and `ties: "any"` on `play_land.card`. Two
    differently-named lands tied under a plain `{types_all: [Land]}` selector
    are `ambiguous` without `ties`; with `ties: "any"` the runner takes the
    first by sorted name instead."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Sacred Foundry", "alice", "hand")
    add_card(client, board, "Steam Vents", "alice", "hand")
    register(client, board, alice, land_script("drop"))
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    tied = run_script(seat, script_id="drop", reasoning=REASONING, corpora=seats.script_corpora)
    assert tied.script_exit.exit is Exit.AMBIGUOUS, tied.to_dict()
    assert tied.script_exit.ambiguous_names == ["Sacred Foundry", "Steam Vents"]
    assert hook.posts("/actions/play-land") == []

    any_land_move = {
        "kind": "play_land",
        "card": {"select": {}, "ties": "any"},
        "answers": [{"decision": "pay_life", "accept": False}],
    }
    register(
        client,
        board,
        alice,
        land_script("drop", moves=[any_land_move]),
        land_script("drop-any", moves=[any_land_move]),
    )
    seat, hook = hooked_seat(seats, alice)
    chosen = run_script(seat, script_id="drop-any", reasoning=REASONING, corpora={})
    assert chosen.script_exit.exit is Exit.CHAIN_COMPLETE, chosen.to_dict()
    foundry = cards_named(state(client, board)["battlefield"], "Sacred Foundry")
    assert foundry, "the alphabetically-first tied land was played"
    assert hook.posts("/actions/play-land")[0]["card_id"] == foundry[0]["id"]


def test_a_choice_not_among_the_offered_options_exits_unforeseen() -> None:
    """O6: choose_a_mode is always fully enumerated (`pending.options`), so
    a `choice` naming something else can never be sent to the engine - it
    is `unforeseen`, the same as `_pick` finding nothing to match."""
    pending = {
        "player": "alice",
        "kind": "choose_a_mode",
        "prompt": "choose one",
        "generation": 1,
        "options": ["Create a 3/3 green Beast creature token.", "You gain 3 life.", "Draw a card."],
    }
    item = {"decision": "choose_a_mode", "choice": "Destroy target creature."}
    built = runtime.decide_body(item, pending, {}, "alice", "reasoning")
    assert isinstance(built, runtime.ScriptExit)
    assert built.exit is Exit.UNFORESEEN
    assert "Destroy target creature." in built.stop_reason


def test_play_land_plays_an_mdfcs_land_face_by_the_cards_own_id() -> None:
    """O5: "play_land plays that face by card_id; confirm the route does
    this." Confirmed by reading, not assumed: `_move_request`'s play_land
    branch (below) sends only `card_id`, no separate face id, for every
    land pick alike; the engine's own `Game.play_land` (magic_cards_model/
    models/game.py) already resolves an MDFC's back face from that one id
    via `Card.land_face` - "There is no separate 'playing an MDFC's land
    face' special action in the rules - it is the same CR 305.1 land drop."
    No live MDFC-land card is cached in tests/fixtures/scryfall (checked),
    so this proves the runner's half directly against a hand-built view in
    the same shape `serialize.state_view` emits, the way most of
    `test_playscript_selectors.py`'s double-faced-card tests already do."""
    witch = {
        "id": "11111111-1111-4111-8111-111111111111",
        "name": "Witch Enchanter",
        "card_name": "Witch Enchanter // Witch-Blessed Meadow",
        "zone": "hand",
        "controller": "alice",
        "type_line": "Creature — Human Warlock",
        "oracle_text": None,
        "faces": [
            {
                "name": "Witch Enchanter",
                "mana_cost": "{3}{W}",
                "type_line": "Creature — Human Warlock",
                "oracle_text": "When this creature enters, destroy target artifact or enchantment.",
            },
            {
                "name": "Witch-Blessed Meadow",
                "mana_cost": "",
                "type_line": "Land",
                "oracle_text": "As this land enters, you may pay 3 life.",
            },
        ],
    }
    state = state_view(hands={"alice": [witch]})
    move = {"kind": "play_land", "card": {"select": {"land_face": True}}}
    built = runtime._move_request(move, state, "alice", "reasoning")
    assert built == (
        "actions/play-land",
        {"player": "alice", "card_id": witch["id"], "reasoning": "reasoning"},
    )


def test_an_off_zone_ability_activates_from_the_hand(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O3: "hand views carry that ability" was unverified - proved here
    against the live serializer with a real card. `list_abilities` reads a
    zone other than the battlefield through `off_zone_abilities`
    (`machines/keywords.py`), which synthesizes a Cycling/Landcycling card's
    activated ability whether or not it is printed - Angelsong's plain
    Cycling {2} exercises the same code path a landcycling card's
    `searches_for_any` selector would (O3's own selector-matching tests
    cover the search-template parsing with fabricated fetch/landcycling
    text; the live cache carries no landcycling card)."""
    alice = seat_uid(board, "alice")
    add_card(client, board, "Angelsong", "alice", "hand")
    add_card(client, board, "Island", "alice", "battlefield")
    add_card(client, board, "Island", "alice", "battlefield")
    before = state(client, board)
    angelsong = cards_named(before["hands"]["alice"], "Angelsong")[0]
    cycling = next(a for a in angelsong["abilities"] if "Discard this card" in a["text"])
    assert cycling["text"] == "{2}, Discard this card: Draw a card."

    script = {
        "schema_version": 1,
        "id": "cycle",
        "name": "Cycle Angelsong",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [
            {
                "kind": "activate",
                "source": {"zone": "hand", "select": {}},
                "ability_text_has": "Discard this card",
            }
        ],
        "next": [],
    }
    register(client, board, alice, script)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id="cycle", reasoning=REASONING, corpora=seats.script_corpora
        )
    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    (activated,) = hook.posts("/actions/activate")
    assert activated["player"] == "alice"
    assert activated["card_id"] == angelsong["id"]
    assert activated["ability_index"] == cycling["index"]
    sha8 = runtime.script_sha(script)[:8]
    assert activated["reasoning"] == f"{REASONING} | script cycle@{sha8} move 1/1 (activate)"


def test_the_fetch_sacrifices_its_own_source(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_shock())
    strand_board(client, board)
    strand = cards_named(state(client, board)["battlefield"], "Flooded Strand")[0]
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=FETCH_SHOCK["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/activate")
    assert body["sacrificing"] == [strand["id"]]


def test_an_activation_names_no_mana_sources_unless_the_play_names_them(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B7-10 (B5-05 variant): the fetch's activation names no cost in mana,
    so the request omits `mana_sources` and lets the engine auto-pay
    (CR 601.2g), even with an untapped Mountain sitting right there to name."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_shock())
    strand_board(client, board)
    add_card(client, board, "Mountain", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=FETCH_SHOCK["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/activate")
    assert "mana_sources" not in body, body
    assert cards_named(state(client, board)["battlefield"], "Mountain")[0]["tapped"] is False


def test_a_cast_names_no_mana_sources_and_the_engine_auto_pays(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B7-10's cast twin: the request never names sources, so an audit row
    shows only the permanent that actually paid, never every untapped one."""
    alice = seat_uid(board, "alice")
    script = land_script(
        "bolt-a-mountain",
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
    add_card(client, board, "Mountain", "alice", "battlefield")
    add_card(client, board, "Grizzly Bears", "bob", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    [body] = hook.posts("/actions/cast")
    assert "mana_sources" not in body, body
    assert cards_named(state(client, board)["battlefield"], "Mountain")[0]["tapped"] is True


@pytest.mark.parametrize(
    ("cost", "count"),
    [
        ("Sacrifice a creature", 1),
        ("{T}, Sacrifice another creature", 1),
        ("{2}, Sacrifice two artifacts", 2),
        ("Sacrifice three lands", 3),
        ("{1}, {T}", 1),
    ],
)
def test_a_sacrifice_count_left_out_is_the_costs_own_number(cost: str, count: int) -> None:
    assert runtime.sacrifice_count({"cost": cost}) == count


# -- pay-amount answers ----------------------------------------------------------


def discharge_script(amount: Any) -> dict[str, Any]:
    return one_move_script(
        "discharge",
        {
            "kind": "cast",
            "card": {"zone": "hand", "select": {"types_all": ["Instant"]}},
            "targets": [{"select": {"types_all": ["Creature"]}, "side": "opponent"}],
            "answers": [{"decision": "pay_energy_amount", "amount": amount}],
        },
    )


def discharge_board(client: TestClient, board: str, amount: Any) -> None:
    register(client, board, seat_uid(board, "alice"), discharge_script(amount))
    add_card(client, board, "Mountain", "alice", "battlefield")
    add_card(client, board, "Galvanic Discharge", "alice", "hand")
    add_card(client, board, "Grizzly Bears", "bob", "battlefield")


@pytest.mark.parametrize(("amount", "paid"), [(1, 1), ("max", 3)])
def test_an_amount_answer_is_posted_on_the_decisions_answer_field(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch, amount: Any, paid: int
) -> None:
    discharge_board(client, board, amount)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    with opponent_passes(board):
        result = run_script(
            seat, script_id="discharge", reasoning=REASONING, corpora=seats.script_corpora
        )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    # The opponent's pass can move the board between the runner's read and its
    # post, so a stale refusal and `run_action`'s one retry may add a post.
    answers = hook.posts("/decide")
    assert answers and all(body["energy_amount"] == paid for body in answers), answers
    assert all(isinstance(body["generation"], int) for body in answers)
    [accepted] = [row["detail"] for row in rows(board, "decide") if row["detail"]["allowed"]]
    assert accepted["kind"] == "pay_energy_amount" and accepted["energy_amount"] == paid
    after = state(client, board)
    alice = next(p for p in after["players"] if p["username"] == "alice")
    assert alice["counters"].get("energy", 0) == 3 - paid, "a spent-out counter is left out"
    bears = cards_named(after["battlefield"], "Grizzly Bears")
    if paid >= 2:
        assert not bears, "three damage killed the Bears"
    else:
        assert bears[0]["damage"] == paid


def test_an_amount_the_engine_refuses_exits_refused(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    discharge_board(client, board, 9)
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    with opponent_passes(board):
        result = run_script(
            seat, script_id="discharge", reasoning=REASONING, corpora=seats.script_corpora
        )

    out = result.script_exit
    assert out.exit is Exit.REFUSED, result.to_dict()
    assert out.refusal_source == "server" and out.refusal
    assert {body["energy_amount"] for body in hook.posts("/decide")} == {9}
    refusals = [
        row["detail"]
        for row in rows(board, "decide")
        if row["detail"]["allowed"] is False and not row["detail"].get("stale")
    ]
    assert [refusal["reason"] for refusal in refusals] == [out.refusal]
    assert finished(board)[0]["refusal_text"] == out.refusal
    client.post(
        f"/api/games/{board}/decide",
        params={"uid": seat_uid(board, "alice")},
        json={"player": "alice", "energy_amount": 0},
    )


# -- invalid inputs --------------------------------------------------------------


@pytest.mark.parametrize(
    ("inputs", "declared", "name", "value"),
    [
        ({"prefer_tmepo": True}, [PREFERENCE], "prefer_tmepo", True),
        (
            {"threat": "burn"},
            [{"name": "threat", "type": "enum", "values": ["tempo", "combo"], "description": "x"}],
            "threat",
            "burn",
        ),
        (
            {"turns_left": 9},
            [{"name": "turns_left", "type": "int", "min": 0, "max": 5, "description": "x"}],
            "turns_left",
            9,
        ),
    ],
)
def test_an_invalid_input_exits_invalid_input_naming_it(
    client: TestClient,
    board: str,
    monkeypatch: pytest.MonkeyPatch,
    inputs: dict[str, Any],
    declared: list[dict[str, Any]],
    name: str,
    value: Any,
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = run_script(
        seat,
        script_id="drop",
        reasoning=REASONING,
        corpora=seats.script_corpora,
        inputs={"opposing_archetype": "dimir-frog", **inputs},
        declared_inputs=declared,
    )

    out = result.script_exit
    assert out.exit is Exit.INVALID_INPUT, result.to_dict()
    assert out.invalid_input["name"] == name and out.invalid_input["value"] == value
    assert out.invalid_input["reason"]
    assert out.failing_condition is None
    assert hook.posts("/actions/play-land") == []
    [finish] = finished(board)
    assert finish["exit"] == "invalid_input"
    assert name in finish["stop_reason"] and repr(value) in finish["stop_reason"]


def test_an_invalid_input_met_by_a_next_play_after_a_resume_exits_invalid_input(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed run skips its own conditions (its moves already started), so
    the first evaluation of the new inputs is the `next` play's."""
    script = fetch_shock(next=["drop"])
    alice = seat_uid(board, "alice")
    seats, token = capped_fetch(client, board, monkeypatch, script)
    register(client, board, alice, script, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seat, _ = hooked_seat(seats, alice)

    with opponent_passes(board):
        result = run_script(
            seat,
            script_id=script["id"],
            reasoning=REASONING,
            corpora=seats.script_corpora,
            inputs={"bogus": 1},
            resume=token,
        )

    assert result.script_exit.exit is Exit.INVALID_INPUT, result.to_dict()
    assert result.script_exit.invalid_input["name"] == "bogus"
    assert finished(board)[-1]["exit"] == "invalid_input"


# -- answers that name no split, and the active opponent's own pass -------------


@pytest.mark.parametrize("kind", ["surveil", "scry"])
def test_an_answer_naming_no_split_keeps_every_card_on_top(kind: str) -> None:
    """`{"decision": "surveil"}` with no `to_graveyard` (or a scry with no
    `to_bottom`) moves nothing: every card looked at stays on top, in the
    order it was looked at."""
    pending = {
        "kind": kind,
        "player": "alice",
        "generation": 3,
        "amount": 1,
        "cards": ["00000000-0000-4000-8000-000000000001"],
        "card_labels": ["Island"],
    }

    body = runtime.decide_body({"decision": kind}, pending, {}, "alice", REASONING)

    assert body == {
        "player": "alice",
        "reasoning": REASONING,
        "generation": 3,
        "moved": [],
        "top_order": None,
    }


def test_the_active_opponent_passing_after_the_runs_resolution_does_not_interrupt(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the opponent's end step the opponent is the active player, so the
    moment the fetch finishes resolving it holds priority over the fetched
    land's own trigger, and passes. That pass comes after the resolution the
    run answered, not inside it: bob passes here from inside alice's own
    requests, before the runner has looked at the board again."""
    force_turn_position(game, Step("end_step"), "bob")
    alice = seat_uid(game, "alice")
    bob = seat_uid(game, "bob")
    script = copy.deepcopy(FETCH_SURVEIL)
    register(client, game, alice, script)
    add_card(client, game, "Polluted Delta", "alice", "battlefield")
    add_card(client, game, "Undercity Sewers", "alice", "library")
    add_card(client, game, "Island", "alice", "library")
    other = TestClient(app)

    def bob_passes() -> None:
        passed = other.post(
            f"/api/games/{game}/actions/pass-priority", params={"uid": bob}, json={"player": "bob"}
        )
        assert passed.status_code == 200, passed.text

    bob_passes()
    assert state(client, game)["priority_holder"] == "alice"

    passed_after: list[str] = []

    def after(method: str, path: str, body: Any) -> None:
        if method != "POST" or not path.endswith(("/actions/pass-priority", "/decide")):
            return
        board = other.get(f"/api/games/{game}", params={"uid": bob}).json()
        # A standing surveil question is part of the resolution: nobody holds
        # priority until it is answered, whatever the paused board names.
        if (
            board["pending_decision"] is None
            and board["priority_holder"] == "bob"
            and board["stack"]
        ):
            bob_passes()
            passed_after.append(path)

    seats = wired(monkeypatch, game)
    seat, _ = hooked_seat(seats, alice, after=after)

    result = run_script(
        seat, script_id=script["id"], reasoning=REASONING, corpora=seats.script_corpora
    )

    assert result.script_exit.exit is Exit.CHAIN_COMPLETE, result.to_dict()
    after_state = state(client, game)
    assert cards_named(after_state["battlefield"], "Undercity Sewers")
    assert cards_named(after_state["graveyards"]["alice"], "Island"), "the surveil was answered"
    assert passed_after, "bob never held priority after the resolution, so nothing was tested"
