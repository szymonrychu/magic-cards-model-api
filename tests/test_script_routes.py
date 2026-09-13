"""The play-script routes: a seat registers its corpus, reads it back, and
records each run of a script into the ledger.

- `PUT /api/games/{id}/seats/{uid}/scripts` stores one corpus per
  (game, uid), validated script by script with `playscripts.schema`, behind
  the `SCRIPTS_ENABLED` kill switch.
- `GET` of the same path hands it back to that uid, the seat's advisor, and
  the admin key - nobody else.
- `POST /api/games/{id}/script-runs` records `start` (refusing a second start
  of one script at one turn and step with exit `already_ran`) and `finish`
  (one `script_run` row). A `start` names a script registered under the uid
  that runs it - the seat's own corpus for role `seat`, the advisor's own for
  role `advisor` - and is refused with `code: unknown_script` otherwise.
- Neither the corpus nor a run row ever reaches the opponent.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from conftest import force_turn_position, require_postgres_database_url
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from playscript_testkit import FETCH_SHOCK, FETCH_SURVEIL, SLUG

from magic_cards_model_api import audit, game_access, seatkit, visibility
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.store import store

REVISION = "0123abcd"

#: Every field a `finish` writes into its `script_run` row, and nothing else
#: besides `player`, which is what `visibility.redact_log_entry` keys on.
RUN_FIELDS = {
    "run_id",
    "parent_run_id",
    "game_id",
    "seat",
    "role",
    "advise",
    "script_id",
    "script_sha",
    "guides_revision",
    "priority",
    "opposing_archetype_asserted",
    "turn",
    "step",
    "seq_start",
    "seq_end",
    "moves_done",
    "moves_total",
    "exit",
    "stop_reason",
    "refusal_text",
    "resumes",
    "wall_ms",
    "reapplicable_after",
}


def seat_uid(game_id: str, player: str) -> str:
    return next(uid for uid, name in store.seat_uids(game_id).items() if name == player)


def advisor_uid(game_id: str, player: str = "alice", uid: str = "advisor-uid") -> str:
    store.register_advisors(game_id, {uid: Advisor(player=player)})
    return uid


def corpus(*scripts: dict[str, Any]) -> dict[str, Any]:
    chosen = scripts or (FETCH_SHOCK, FETCH_SURVEIL)
    return {
        "deck_slug": SLUG,
        "guides_revision": REVISION,
        "scripts": [copy.deepcopy(script) for script in chosen],
    }


def register(client: TestClient, game: str, path_uid: str, **params: str):
    return client.put(f"/api/games/{game}/seats/{path_uid}/scripts", params=params, json=corpus())


def read(client: TestClient, game: str, path_uid: str, **params: str):
    return client.get(f"/api/games/{game}/seats/{path_uid}/scripts", params=params)


def run(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "phase": "start",
        "run_id": "run-1",
        "seat": "alice",
        "role": "seat",
        "advise": False,
        "script_id": FETCH_SHOCK["id"],
        "turn": 3,
        "step": "precombat_main",
    }
    body.update(over)
    return body


def finish(**over: Any) -> dict[str, Any]:
    body = run(
        phase="finish",
        parent_run_id="run-0",
        script_sha="sha-of-the-script",
        guides_revision=REVISION,
        priority=60,
        opposing_archetype_asserted="dimir-frog",
        seq_start=11,
        seq_end=19,
        moves_done=1,
        moves_total=1,
        exit="completed",
        stop_reason="all moves done",
        refusal_text=None,
        resumes=0,
        wall_ms=842,
        reapplicable_after="next_turn",
    )
    body.update(over)
    return body


def post_run(client: TestClient, game: str, body: dict[str, Any], **params: str):
    return client.post(f"/api/games/{game}/script-runs", params=params, json=body)


def script_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in entries if entry["action"] in audit.SCRIPT_RUN_ACTIONS]


# ---------------------------------------------------------------------------
# register and list, by credential
# ---------------------------------------------------------------------------


def test_a_seat_registers_its_corpus_and_reads_it_back(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")

    put = register(client, game, alice, uid=alice)
    assert put.status_code == 200, put.text
    assert put.json()["registered"] is True
    assert put.json()["count"] == 2

    got = read(client, game, alice, uid=alice)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["deck_slug"] == SLUG
    assert body["guides_revision"] == REVISION
    assert body["scripts"] == [FETCH_SHOCK, FETCH_SURVEIL]


def test_the_seats_advisor_reads_its_corpus(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    advisor = advisor_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200

    got = read(client, game, alice, uid=advisor)

    assert got.status_code == 200, got.text
    assert got.json()["scripts"] == [FETCH_SHOCK, FETCH_SURVEIL]


def test_the_opponent_is_refused_the_corpus(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200

    got = read(client, game, alice, uid=seat_uid(game, "bob"))

    assert got.status_code == 400, got.text
    assert FETCH_SHOCK["id"] not in got.text


def test_the_other_seats_advisor_is_refused_the_corpus(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200

    got = read(client, game, alice, uid=advisor_uid(game, "bob", uid="bobs-advisor"))

    assert got.status_code == 400, got.text


def test_a_read_with_no_uid_is_refused(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200

    got = read(client, game, alice)

    assert got.status_code == 400, got.text
    assert FETCH_SHOCK["id"] not in got.text


def test_the_admin_key_reads_the_corpus(client: TestClient, game: str, admin_key: str) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200

    got = read(client, game, alice, key=admin_key)

    assert got.status_code == 200, got.text
    assert got.json()["scripts"] == [FETCH_SHOCK, FETCH_SURVEIL]


def test_the_admin_key_registers_for_a_seat(client: TestClient, game: str, admin_key: str) -> None:
    alice = seat_uid(game, "alice")

    assert register(client, game, alice, key=admin_key).status_code == 200
    assert read(client, game, alice, uid=alice).status_code == 200


def test_a_corpus_nobody_registered_is_not_found(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")

    assert read(client, game, alice, uid=alice).status_code == 404


def test_the_opponent_cannot_register_into_another_seat(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")

    put = register(client, game, alice, uid=seat_uid(game, "bob"))

    assert put.status_code == 404, put.text
    assert read(client, game, alice, uid=alice).status_code == 404


@pytest.mark.no_harness_credential
def test_a_registration_with_no_credential_is_refused(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")

    put = register(client, game, alice)

    assert put.status_code == 404, put.text
    assert store.scripts(game, alice) is None


def test_a_uid_that_is_not_in_the_game_is_not_found(
    client: TestClient, game: str, admin_key: str
) -> None:
    put = register(client, game, "no-such-uid", key=admin_key)

    assert put.status_code == 404, put.text


def test_an_advisor_registers_and_reads_its_own_corpus(client: TestClient, game: str) -> None:
    """The buddy's seat Job runs under its advisor uid, so that is the
    credential it registers with - a PUT the advisor gate lets through for
    the advisor's own corpus alone."""
    advisor = advisor_uid(game, "alice")

    put = register(client, game, advisor, uid=advisor)

    assert put.status_code == 200, put.text
    assert read(client, game, advisor, uid=advisor).json()["scripts"] == [
        FETCH_SHOCK,
        FETCH_SURVEIL,
    ]


def test_an_advisor_cannot_register_into_its_humans_corpus(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    advisor = advisor_uid(game, "alice")

    put = register(client, game, alice, uid=advisor)

    assert put.status_code == 404, put.text
    assert store.scripts(game, alice) is None


def test_registering_again_replaces_the_corpus(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200

    put = client.put(
        f"/api/games/{game}/seats/{alice}/scripts",
        params={"uid": alice},
        json=corpus(FETCH_SURVEIL),
    )

    assert put.status_code == 200, put.text
    assert read(client, game, alice, uid=alice).json()["scripts"] == [FETCH_SURVEIL]


def test_one_seats_corpus_does_not_touch_the_others(client: TestClient, game: str) -> None:
    alice, bob = seat_uid(game, "alice"), seat_uid(game, "bob")
    assert register(client, game, alice, uid=alice).status_code == 200
    assert (
        client.put(
            f"/api/games/{game}/seats/{bob}/scripts",
            params={"uid": bob},
            json=corpus(FETCH_SURVEIL),
        ).status_code
        == 200
    )

    assert read(client, game, alice, uid=alice).json()["scripts"] == [FETCH_SHOCK, FETCH_SURVEIL]
    assert read(client, game, bob, uid=bob).json()["scripts"] == [FETCH_SURVEIL]


def test_the_corpus_is_in_no_read_the_opponent_makes(client: TestClient, game: str) -> None:
    alice, bob = seat_uid(game, "alice"), seat_uid(game, "bob")
    assert register(client, game, alice, uid=alice).status_code == 200

    for response in (
        client.get(f"/api/games/{game}", params={"uid": bob}),
        client.get(f"/api/games/{game}/log", params={"uid": bob}),
    ):
        assert response.status_code == 200, response.text
        assert FETCH_SHOCK["id"] not in response.text
        assert FETCH_SURVEIL["id"] not in response.text


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_an_invalid_script_is_rejected_with_its_code(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    broken = copy.deepcopy(FETCH_SHOCK)
    broken["iterate"] = True

    put = client.put(
        f"/api/games/{game}/seats/{alice}/scripts",
        params={"uid": alice},
        json=corpus(FETCH_SURVEIL, broken),
    )

    assert put.status_code == 422, put.text
    body = put.json()
    assert body["code"] == "invalid_scripts"
    assert [(error["code"], error["path"]) for error in body["errors"]] == [
        ("unknown-field", "scripts[1]")
    ]
    assert store.scripts(game, alice) is None


def test_every_invalid_script_is_reported(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    no_moves = copy.deepcopy(FETCH_SHOCK)
    no_moves["moves"] = []
    fights = copy.deepcopy(FETCH_SURVEIL)
    fights["moves"] = [{"kind": "attack"}]

    put = client.put(
        f"/api/games/{game}/seats/{alice}/scripts",
        params={"uid": alice},
        json=corpus(no_moves, fights),
    )

    assert put.status_code == 422, put.text
    codes = {(error["code"], error["path"]) for error in put.json()["errors"]}
    assert ("bounds-exceeded", "scripts[0]") in codes
    assert ("forbidden-kind", "scripts[1]") in codes


def test_two_scripts_with_one_id_are_rejected(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")

    put = client.put(
        f"/api/games/{game}/seats/{alice}/scripts",
        params={"uid": alice},
        json=corpus(FETCH_SHOCK, FETCH_SHOCK),
    )

    assert put.status_code == 422, put.text
    assert [(error["code"], error["path"]) for error in put.json()["errors"]] == [
        ("malformed-play", "scripts[1]")
    ]


# ---------------------------------------------------------------------------
# declared inputs: decks/<slug>/plays/inputs.json, registered beside the plays
# ---------------------------------------------------------------------------

DECLARED = [
    {
        "name": "plan",
        "type": "enum",
        "values": ["race", "control"],
        "description": "Which half of the plan this game is on.",
        "required": True,
    },
    {
        "name": "turns_left",
        "type": "int",
        "min": 0,
        "max": 20,
        "description": "d",
        "required": False,
    },
]


def test_declared_inputs_are_registered_and_read_back(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")

    put = client.put(
        f"/api/games/{game}/seats/{alice}/scripts",
        params={"uid": alice},
        json={**corpus(), "inputs": DECLARED},
    )

    assert put.status_code == 200, put.text
    assert read(client, game, alice, uid=alice).json()["inputs"] == DECLARED


def test_a_corpus_registered_without_inputs_declares_none(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200

    assert read(client, game, alice, uid=alice).json()["inputs"] == []


def test_malformed_declared_inputs_refuse_the_corpus(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    redeclared = [
        {"name": "opposing_archetype", "type": "bool", "description": "d", "required": False}
    ]

    put = client.put(
        f"/api/games/{game}/seats/{alice}/scripts",
        params={"uid": alice},
        json={**corpus(), "inputs": redeclared},
    )

    assert put.status_code == 422, put.text
    body = put.json()
    assert body["code"] == "invalid_scripts"
    assert {(error["code"], error["path"]) for error in body["errors"]} == {
        ("malformed-inputs", "inputs")
    }
    assert store.scripts(game, alice) is None


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"x": 1}, "inputs must be a list"),
        ("nope", "inputs must be a list"),
        (None, "inputs must be a list"),
        ([1], "inputs[0]: must be an object"),
    ],
)
def test_inputs_of_the_wrong_shape_refuse_as_malformed_inputs(
    client: TestClient, game: str, inputs: Any, message: str
) -> None:
    """B6-01: an `inputs` that is not a list of objects is judged by
    `playscripts.inputs` like any other malformed declaration, not refused by
    pydantic's generic `loc: [body, inputs]` 422 before the route sees it."""
    alice = seat_uid(game, "alice")

    put = client.put(
        f"/api/games/{game}/seats/{alice}/scripts",
        params={"uid": alice},
        json={**corpus(), "inputs": inputs},
    )

    assert put.status_code == 422, put.text
    body = put.json()
    assert body.get("code") == "invalid_scripts", body
    assert body["errors"] == [{"code": "malformed-inputs", "path": "inputs", "message": message}]
    assert store.scripts(game, alice) is None


# ---------------------------------------------------------------------------
# the kill switch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
def test_the_kill_switch_refuses_a_registration(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("SCRIPTS_ENABLED", value)
    alice = seat_uid(game, "alice")

    put = register(client, game, alice, uid=alice)

    assert put.status_code == 409, put.text
    body = put.json()
    assert body["code"] == "scripts_disabled"
    assert body["allowed"] is False
    assert "SCRIPTS_ENABLED" in body["reason"]
    assert store.scripts(game, alice) is None


def test_scripts_are_enabled_when_the_switch_is_unset(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCRIPTS_ENABLED", raising=False)
    alice = seat_uid(game, "alice")

    assert register(client, game, alice, uid=alice).status_code == 200


def test_the_kill_switch_refuses_a_start_but_still_records_a_finish(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run already under way when the switch flips still gets its row:
    losing the record of what it did would be worse than letting it land."""
    monkeypatch.setenv("SCRIPTS_ENABLED", "false")
    alice = seat_uid(game, "alice")

    started = post_run(client, game, run(), uid=alice)
    assert started.status_code == 409, started.text
    assert started.json()["code"] == "scripts_disabled"

    finished = post_run(client, game, finish(), uid=alice)
    assert finished.status_code == 200, finished.text
    assert [row["action"] for row in script_rows(audit.entries(game))] == [audit.SCRIPT_RUN_ACTION]


# ---------------------------------------------------------------------------
# script runs
# ---------------------------------------------------------------------------


def test_a_start_is_recorded(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200

    started = post_run(client, game, run(), uid=alice)

    assert started.status_code == 200, started.text
    body = started.json()
    assert body["recorded"] is True
    assert body["phase"] == "start"
    assert body["run_id"] == "run-1"
    rows = script_rows(audit.entries(game))
    assert [row["action"] for row in rows] == [audit.SCRIPT_RUN_STARTED_ACTION]
    assert rows[0]["seq"] == body["log_seq"]


def test_a_second_start_of_the_same_script_at_the_same_turn_and_step_already_ran(
    client: TestClient, game: str
) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200
    assert post_run(client, game, run(), uid=alice).status_code == 200

    again = post_run(client, game, run(run_id="run-2"), uid=alice)

    assert again.status_code == 409, again.text
    body = again.json()
    assert body["exit"] == "already_ran"
    assert body["allowed"] is False
    assert len(script_rows(audit.entries(game))) == 1


@pytest.mark.parametrize(
    "over",
    [
        {"turn": 4},
        {"step": "end"},
        {"script_id": FETCH_SURVEIL["id"]},
        {"seat": "bob", "run_id": "run-bob"},
    ],
    ids=["another-turn", "another-step", "another-script", "another-seat"],
)
def test_a_start_that_differs_in_any_part_of_the_key_is_not_already_ran(
    client: TestClient, game: str, admin_key: str, over: dict[str, Any]
) -> None:
    for player in ("alice", "bob"):
        assert register(client, game, seat_uid(game, player), key=admin_key).status_code == 200
    assert post_run(client, game, run(), key=admin_key).status_code == 200

    other = post_run(client, game, run(**{"run_id": "run-2", **over}), key=admin_key)

    assert other.status_code == 200, other.text


def test_a_finish_that_ran_without_a_start_still_counts_as_already_ran(
    client: TestClient, game: str
) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200
    assert post_run(client, game, finish(), uid=alice).status_code == 200

    again = post_run(client, game, run(run_id="run-2"), uid=alice)

    assert again.status_code == 409, again.text
    assert again.json()["exit"] == "already_ran"


def test_a_zero_move_finish_does_not_burn_the_already_ran_slot(
    client: TestClient, game: str
) -> None:
    """B8-04: a finish that made zero moves (refused before its first move,
    say) touched nothing, so a retry of the same script at the same turn and
    step is not `already_ran` - consistent with that finish's own
    `reapplicable_after: true`."""
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200
    zero_moves = finish(moves_done=0, moves_total=1, exit="refused", reapplicable_after="true")
    assert post_run(client, game, zero_moves, uid=alice).status_code == 200

    again = post_run(client, game, run(run_id="run-2"), uid=alice)

    assert again.status_code == 200, again.text


def test_a_started_run_that_finished_with_zero_moves_does_not_burn_the_slot(
    client: TestClient, game: str
) -> None:
    """The runner always posts a start before its finish, and that start row
    carries the same key: releasing the slot must cover the run's own start
    too, or a zero-move run still blocks its retry."""
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200
    assert post_run(client, game, run(), uid=alice).status_code == 200
    zero_moves = finish(moves_done=0, moves_total=1, exit="refused", reapplicable_after="true")
    assert post_run(client, game, zero_moves, uid=alice).status_code == 200

    again = post_run(client, game, run(run_id="run-2"), uid=alice)

    assert again.status_code == 200, again.text


def test_a_zero_move_finish_with_an_unknown_outcome_still_burns_the_slot(
    client: TestClient, game: str
) -> None:
    """`unknown_outcome` means the engine may have taken the move even though
    `moves_done` never counted it, so a retry could make the move twice."""
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200
    assert post_run(client, game, run(), uid=alice).status_code == 200
    unknown = finish(moves_done=0, moves_total=1, exit="unknown_outcome")
    assert post_run(client, game, unknown, uid=alice).status_code == 200

    again = post_run(client, game, run(run_id="run-2"), uid=alice)

    assert again.status_code == 409, again.text
    assert again.json()["exit"] == "already_ran"


def test_a_finish_that_did_move_still_burns_the_slot(client: TestClient, game: str) -> None:
    """The other half of B8-04's fix: a finish naming at least one move done
    still blocks a retry, exactly as before."""
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200
    assert post_run(client, game, finish(moves_done=1), uid=alice).status_code == 200

    again = post_run(client, game, run(run_id="run-2"), uid=alice)

    assert again.status_code == 409, again.text
    assert again.json()["exit"] == "already_ran"


def test_a_start_or_finish_after_game_over_is_refused_with_no_record(
    client: TestClient, game: str
) -> None:
    """B8-06: a finished game (CR 104.1) refuses `script-runs` the same way
    every action route is refused - both phases, no ledger row for either."""
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200
    conceded = client.post(f"/api/games/{game}/actions/concede", json={"player": "bob"})
    assert conceded.status_code == 200 and conceded.json()["winner"] == "alice", conceded.text

    started = post_run(client, game, run(), uid=alice)
    assert started.status_code == 409, started.text
    assert started.json().get("type") == "game_over", started.json()

    finished = post_run(client, game, finish(), uid=alice)
    assert finished.status_code == 409, finished.text
    assert finished.json().get("type") == "game_over", finished.json()

    assert script_rows(audit.entries(game)) == []


def test_a_finish_writes_one_script_run_row_with_every_field(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    body = finish()

    finished = post_run(client, game, body, uid=alice)

    assert finished.status_code == 200, finished.text
    assert finished.json()["phase"] == "finish"
    rows = [row for row in audit.entries(game) if row["action"] == audit.SCRIPT_RUN_ACTION]
    assert len(rows) == 1
    detail = rows[0]["detail"]
    assert set(detail) == RUN_FIELDS | {"player"}
    assert detail["player"] == "alice"
    assert detail["game_id"] == game
    for field in RUN_FIELDS - {"game_id"}:
        assert detail[field] == body[field], field
    assert rows[0]["seq"] == finished.json()["log_seq"]


def test_a_finish_needs_an_exit(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    body = finish()
    del body["exit"]

    assert post_run(client, game, body, uid=alice).status_code == 422


def test_an_unknown_phase_is_refused(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")

    assert post_run(client, game, run(phase="resume"), uid=alice).status_code == 422


def test_the_opponent_cannot_record_a_run_in_another_seats_name(
    client: TestClient, game: str
) -> None:
    started = post_run(client, game, run(), uid=seat_uid(game, "bob"))

    assert started.status_code == 404, started.text
    assert script_rows(audit.entries(game)) == []


def test_an_advisor_records_an_advise_run_for_its_human(client: TestClient, game: str) -> None:
    advisor = advisor_uid(game, "alice")
    assert register(client, game, advisor, uid=advisor).status_code == 200

    started = post_run(client, game, run(role="advisor", advise=True), uid=advisor)

    assert started.status_code == 200, started.text


def test_an_advisor_cannot_claim_the_seat_role(client: TestClient, game: str) -> None:
    advisor = advisor_uid(game, "alice")

    started = post_run(client, game, run(role="seat"), uid=advisor)

    assert started.status_code == 400, started.text
    assert script_rows(audit.entries(game)) == []


def test_a_seat_cannot_claim_the_advisor_role(client: TestClient, game: str) -> None:
    started = post_run(client, game, run(role="advisor"), uid=seat_uid(game, "alice"))

    assert started.status_code == 400, started.text


def test_script_runs_is_on_the_advisor_allowlist() -> None:
    assert "/script-runs" in game_access.ADVISOR_POST_SUFFIXES


def test_a_run_owes_no_reasoning_no_plan_and_no_board_position(
    client: TestClient, game: str
) -> None:
    """An agent seat on its own turn with no plan declared is refused every
    action (`records.require_plan`) and any action with no `reasoning`
    (`records.require_reasoning`). A script run is not an action - its moves
    go through the action routes, which gate them - so neither applies, and
    the body has no `expected_seq` for `require_current_board` to read."""
    store.register_ai_players(game, ["bob"])
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")
    bob = seat_uid(game, "bob")
    assert register(client, game, bob, uid=bob).status_code == 200
    refused = client.post(
        f"/api/games/{game}/actions/pass-priority", params={"uid": bob}, json={"player": "bob"}
    )
    assert refused.status_code == 422, "the gates this route is exempt from must be live here"

    started = post_run(client, game, run(seat="bob"), uid=bob)
    finished = post_run(client, game, finish(seat="bob"), uid=bob)

    assert started.status_code == 200, started.text
    assert finished.status_code == 200, finished.text


def test_a_run_body_with_expected_seq_is_refused_rather_than_ignored(
    client: TestClient, game: str
) -> None:
    alice = seat_uid(game, "alice")

    assert post_run(client, game, run(expected_seq=1), uid=alice).status_code == 422


# ---------------------------------------------------------------------------
# a start names a script registered under the uid that runs it
# ---------------------------------------------------------------------------


def assert_unknown_script(response: Any, script_id: str) -> None:
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == "unknown_script"
    assert body["allowed"] is False
    assert body["script_id"] == script_id
    assert repr(script_id) in body["reason"]


def test_a_start_of_a_script_the_seat_did_not_register_is_refused(
    client: TestClient, game: str
) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200

    started = post_run(client, game, run(script_id="no-such-play"), uid=alice)

    assert_unknown_script(started, "no-such-play")
    assert script_rows(audit.entries(game)) == []


def test_a_start_with_no_corpus_registered_is_refused(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")

    started = post_run(client, game, run(), uid=alice)

    assert_unknown_script(started, FETCH_SHOCK["id"])
    assert script_rows(audit.entries(game)) == []


def test_a_refused_start_leaves_nothing_for_already_ran_to_find(
    client: TestClient, game: str
) -> None:
    alice = seat_uid(game, "alice")
    assert (
        client.put(
            f"/api/games/{game}/seats/{alice}/scripts",
            params={"uid": alice},
            json=corpus(FETCH_SURVEIL),
        ).status_code
        == 200
    )
    assert_unknown_script(post_run(client, game, run(), uid=alice), FETCH_SHOCK["id"])
    assert register(client, game, alice, uid=alice).status_code == 200

    started = post_run(client, game, run(run_id="run-2"), uid=alice)

    assert started.status_code == 200, started.text


def test_the_opponents_corpus_registers_nothing_for_this_seat(
    client: TestClient, game: str
) -> None:
    bob = seat_uid(game, "bob")
    assert register(client, game, bob, uid=bob).status_code == 200

    started = post_run(client, game, run(), uid=seat_uid(game, "alice"))

    assert_unknown_script(started, FETCH_SHOCK["id"])


def test_an_advisor_start_is_checked_against_the_advisors_own_corpus(
    client: TestClient, game: str
) -> None:
    alice = seat_uid(game, "alice")
    advisor = advisor_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200
    advise = {"role": "advisor", "advise": True}

    assert_unknown_script(post_run(client, game, run(**advise), uid=advisor), FETCH_SHOCK["id"])
    assert (
        client.put(
            f"/api/games/{game}/seats/{advisor}/scripts",
            params={"uid": advisor},
            json=corpus(FETCH_SURVEIL),
        ).status_code
        == 200
    )
    assert_unknown_script(post_run(client, game, run(**advise), uid=advisor), FETCH_SHOCK["id"])

    started = post_run(client, game, run(script_id=FETCH_SURVEIL["id"], **advise), uid=advisor)

    assert started.status_code == 200, started.text


def test_a_seat_start_is_not_satisfied_by_its_advisors_corpus(
    client: TestClient, game: str
) -> None:
    advisor = advisor_uid(game, "alice")
    assert register(client, game, advisor, uid=advisor).status_code == 200

    started = post_run(client, game, run(), uid=seat_uid(game, "alice"))

    assert_unknown_script(started, FETCH_SHOCK["id"])


def test_the_admin_keys_seat_start_is_checked_against_that_seats_corpus(
    client: TestClient, game: str, admin_key: str
) -> None:
    alice = seat_uid(game, "alice")
    advisor = advisor_uid(game, "alice")
    assert register(client, game, advisor, uid=advisor).status_code == 200
    assert (
        client.put(
            f"/api/games/{game}/seats/{alice}/scripts",
            params={"key": admin_key},
            json=corpus(FETCH_SURVEIL),
        ).status_code
        == 200
    )

    assert_unknown_script(post_run(client, game, run(), key=admin_key), FETCH_SHOCK["id"])
    started = post_run(client, game, run(script_id=FETCH_SURVEIL["id"]), key=admin_key)

    assert started.status_code == 200, started.text


def test_the_admin_keys_advisor_start_is_checked_against_that_seats_advisors(
    client: TestClient, game: str, admin_key: str
) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200
    bobs = advisor_uid(game, "bob", uid="bobs-advisor")
    assert register(client, game, bobs, uid=bobs).status_code == 200
    advise = {"role": "advisor", "advise": True}

    assert_unknown_script(post_run(client, game, run(**advise), key=admin_key), FETCH_SHOCK["id"])
    advisor = advisor_uid(game, "alice")
    assert register(client, game, advisor, uid=advisor).status_code == 200

    started = post_run(client, game, run(**advise), key=admin_key)

    assert started.status_code == 200, started.text


# ---------------------------------------------------------------------------
# the opponent never sees a run
# ---------------------------------------------------------------------------


def _played_runs(client: TestClient, game: str) -> None:
    alice = seat_uid(game, "alice")
    assert register(client, game, alice, uid=alice).status_code == 200
    assert post_run(client, game, run(), uid=alice).status_code == 200
    assert post_run(client, game, finish(), uid=alice).status_code == 200


def test_the_opponents_log_read_omits_script_run_rows(client: TestClient, game: str) -> None:
    _played_runs(client, game)
    bob = seat_uid(game, "bob")

    log = client.get(f"/api/games/{game}/log", params={"uid": bob})

    assert log.status_code == 200, log.text
    assert script_rows(log.json()["log"]) == []
    assert FETCH_SHOCK["id"] not in log.text
    assert log.json()["last_seq"] == audit.last_seq(game), (
        "omitting a row must not move the watermark"
    )


def test_a_uid_less_log_read_omits_script_run_rows(client: TestClient, game: str) -> None:
    _played_runs(client, game)

    log = client.get(f"/api/games/{game}/log")

    assert script_rows(log.json()["log"]) == []


def test_the_seat_its_advisor_and_the_admin_read_its_script_run_rows(
    client: TestClient, game: str, admin_key: str
) -> None:
    _played_runs(client, game)
    alice = seat_uid(game, "alice")
    advisor = advisor_uid(game, "alice")

    for params in ({"uid": alice}, {"uid": advisor}, {"uid": admin_key}):
        log = client.get(f"/api/games/{game}/log", params=params)
        assert [row["action"] for row in script_rows(log.json()["log"])] == [
            audit.SCRIPT_RUN_STARTED_ACTION,
            audit.SCRIPT_RUN_ACTION,
        ], params


def test_redact_log_entry_drops_a_script_run_row_for_anyone_but_its_seat(
    client: TestClient, game: str
) -> None:
    _played_runs(client, game)

    for entry in script_rows(audit.entries(game)):
        assert visibility.redact_log_entry(entry, "bob") is None
        assert visibility.redact_log_entry(entry, None) is None
        assert visibility.redact_log_entry(entry, visibility.ADMIN) is not None
        kept = visibility.redact_log_entry(entry, "alice")
        assert kept is not None
        assert kept["detail"] == entry["detail"]


# ---------------------------------------------------------------------------
# contract surfaces
# ---------------------------------------------------------------------------


def test_script_runs_is_not_a_mutating_game_action() -> None:
    assert "script-runs" not in seatkit.ACTION_REQUEST_SHAPES


def test_the_seatkit_19_paragraph_names_the_script_routes() -> None:
    source = Path(seatkit.__file__).read_text()
    paragraph = source.split("PROTOCOL_VERSION = ", 1)[1].split("`seatkit-18`", 1)[0]
    for term in (
        "/seats/{uid}/scripts",
        "/script-runs",
        "already_ran",
        "scripts_disabled",
        "unknown_script",
    ):
        assert term in paragraph, f"the seatkit-19 paragraph does not name {term}"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_a_registered_corpus_survives_an_api_restart_under_postgres() -> None:
    """Two `PostgresRepository` instances on separate pools stand in for the
    process before and after a restart: nothing the first held in memory is
    visible to the second, so the corpus coming back is the database's."""
    from test_decks_api import LEGAL_DECK, LEGAL_DECK_B

    from magic_cards_model_api.db import make_engine
    from magic_cards_model_api.decks import load_deck_into_game
    from magic_cards_model_api.repository_postgres import PostgresRepository

    url = require_postgres_database_url("MCM_TEST_DATABASE_URL")
    before = PostgresRepository(url, engine=make_engine(url))
    game = load_deck_into_game([("you", LEGAL_DECK), ("bot", LEGAL_DECK_B)]).games[-1]
    game_id = before.create(game)
    before.register_seats(game_id, {"uid-you": "you", "uid-bot": "bot"})
    registered = {**corpus(), "registered_at": "2026-09-13T00:00:00+00:00"}
    before.register_scripts(game_id, "uid-you", registered)

    after = PostgresRepository(url, engine=make_engine(url))

    assert after.scripts(game_id, "uid-you") == registered
    assert after.scripts(game_id, "uid-bot") is None
