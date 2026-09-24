"""FC1/B1: offers are judged on exactly the bindings the run itself uses.

Before `playscripts/bindings.py` existed, `applicable_hint` (and `arming.
_judge_round`) judged every play against `corpus['_last_asserted_archetype']`,
which fell back to the wildcard `'*'` whenever a seat had never actually
asserted one. A play whose `false` list read the real matchup, or whose
`opposing_archetypes` excluded it, was offered anyway under that stand-in,
and then exited `not_applicable` the moment it actually ran under the seat's
real archetype.

This file proves the fix directly against `playscripts.bindings` and its
callers: `run`, `listing`, `arm` and `applicable_hint` are exercised over a
real board (api practice), and `bindings.py`'s own pure helpers
(`references_archetype`) and `script_tools._first_move_allowed` are exercised
as plain unit tests, no board required."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, force_turn_position
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_playscript_runtime import (
    REASONING,
    Hooked,
    _unlocks_land_script,
    hooked_seat,
    land_script,
    register,
    seat_uid,
    wired,
)

from magic_cards_model_api.playscripts import arming, script_tools
from magic_cards_model_api.playscripts.bindings import (
    Bindings,
    references_archetype,
)
from magic_cards_model_api.playscripts.runtime import Exit
from magic_cards_model_api.playscripts.schema import WILDCARD
from magic_cards_model_api.playscripts.script_tools import _first_move_allowed

ARCHETYPE = "living-end"
OTHER_ARCHETYPE = "dimir-frog"


# -- board plumbing --------------------------------------------------------------


def _new_game(client: TestClient) -> str:
    """A second (or third...) live game beside the `game` fixture's own, for
    a test that needs several independent boards - the `client` fixture's
    teardown sweeps every game left in the store regardless of how many a
    test created."""
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    assert response.status_code == 200, response.text
    return response.json()["game_id"]


def _land_play(play_id: str, **over: Any) -> dict[str, Any]:
    """A minimal move-bearing play: play a land from hand, no condition by
    default (always applies) - `true`/`false`/`opposing_archetypes` are
    replaced wholesale by `over` when a test needs to narrow them."""
    doc: dict[str, Any] = {
        "schema_version": 1,
        "id": play_id,
        "name": "Play a land",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [{"kind": "play_land", "card": {"select": {"types_all": ["Land"]}}}],
        "next": [],
    }
    doc.update(over)
    return doc


def _archetype_excludes(archetype: str) -> list[dict[str, Any]]:
    """A `false` list that excludes exactly `archetype` - the shape the
    root-cause report names: offered under the old `'*'` stand-in, refused
    the moment the run supplied the real matchup."""
    return [{"kind": "input", "name": "opposing_archetype", "op": "eq", "value": archetype}]


def _board(client: TestClient, game_id: str, *, land_in_hand: str = "Island") -> str:
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, "alice")
    add_card(client, game_id, land_in_hand, "alice", "hand")
    return game_id


def _seat(monkeypatch: pytest.MonkeyPatch, game_id: str, player: str = "alice") -> Any:
    seats = wired(monkeypatch, game_id)
    return seats, seats.seat_for(seat_uid(game_id, player))


# -- B1 item 2: the '*' stand-in is gone, however the archetype was asserted ----


@pytest.mark.parametrize("via", ["run", "list", "arm"])
def test_an_archetype_excluding_false_condition_is_never_offered_whichever_call_asserted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, via: str
) -> None:
    game_id = _board(client, _new_game(client))
    alice = seat_uid(game_id, "alice")
    excluded = _land_play("excluded-by-matchup", false=_archetype_excludes(ARCHETYPE))
    register(client, game_id, alice, excluded)
    seats, seat = _seat(monkeypatch, game_id)

    if via == "run":
        # The play's own `false` excludes ARCHETYPE, so running it while
        # asserting ARCHETYPE exits `not_applicable` without ever attempting
        # a move (`releases_slot` frees the once-per-position slot right
        # back) - only the assertion bookkeeping (`_remember_run_inputs`)
        # is what this test is actually after.
        script_tools.run(
            seat,
            seats.script_corpora,
            script="excluded-by-matchup",
            reasoning=REASONING,
            opposing_archetype=ARCHETYPE,
        )
    elif via == "list":
        script_tools.listing(seat, seats.script_corpora, opposing_archetype=ARCHETYPE)
    else:
        arming.arm(
            seat,
            seats.script_corpora,
            arm=["excluded-by-matchup"],
            arm_reasoning="checking whether it applies",
            opposing_archetype=ARCHETYPE,
        )

    state = seat.fetch_state()
    hint = script_tools.applicable_hint(seat, seats.script_corpora, state)

    offered_ids = {
        entry["id"] for entry in (hint or {}).get("applicable", []) + (hint or {}).get("advice", [])
    }
    assert "excluded-by-matchup" not in offered_ids, hint


def test_a_plain_false_condition_that_holds_against_the_board_excludes_the_play(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regression guard, not new behaviour: a non-archetype `false`
    condition already excluded a play before B1 - the fix must not disturb
    that."""
    game_id = _board(client, _new_game(client))
    alice = seat_uid(game_id, "alice")
    excluded = _land_play(
        "excluded-by-board",
        false=[
            {
                "kind": "count",
                "zone": "hand",
                "side": "me",
                "op": ">=",
                "n": 1,
                "select": {"types_all": ["Land"]},
            }
        ],
    )
    register(client, game_id, alice, excluded)
    seats, seat = _seat(monkeypatch, game_id)

    result = script_tools.listing(seat, seats.script_corpora, opposing_archetype=WILDCARD)

    assert isinstance(result, dict), result
    [entry] = result["scripts"]
    assert entry["applies"] is False
    assert entry["failing_condition"]["list"] == "false"


def test_opposing_archetypes_field_excluding_the_asserted_archetype_is_never_offered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_id = _board(client, _new_game(client))
    alice = seat_uid(game_id, "alice")
    excluded = _land_play("frog-only", opposing_archetypes=[OTHER_ARCHETYPE])
    register(client, game_id, alice, excluded)
    seats, seat = _seat(monkeypatch, game_id)

    script_tools.listing(seat, seats.script_corpora, opposing_archetype=ARCHETYPE)
    state = seat.fetch_state()
    hint = script_tools.applicable_hint(seat, seats.script_corpora, state)

    offered_ids = {
        entry["id"] for entry in (hint or {}).get("applicable", []) + (hint or {}).get("advice", [])
    }
    assert "frog-only" not in offered_ids, hint


# -- B1 item 2: before any assertion, nothing is judged against '*' ------------


def test_before_any_assertion_an_archetype_free_play_is_offered_and_a_dependent_one_is_not(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_id = _board(client, _new_game(client))
    alice = seat_uid(game_id, "alice")
    dependent = _land_play("dependent", false=_archetype_excludes(ARCHETYPE))
    free = _land_play("free")
    register(client, game_id, alice, dependent, free)
    seats, seat = _seat(monkeypatch, game_id)

    # No `run`, `list_scripts` or `arm` has ever been called for this seat -
    # `_LAST_ASSERTED_KEY` is genuinely absent from its corpus.
    state = seat.fetch_state()
    hint = script_tools.applicable_hint(seat, seats.script_corpora, state)

    assert hint is not None
    applicable_ids = {entry["id"] for entry in hint.get("applicable", [])}
    assert "free" in applicable_ids, hint
    assert "dependent" not in applicable_ids, hint
    assert "dependent" not in {entry["id"] for entry in hint.get("advice", [])}


def test_judged_marks_an_unasserted_archetype_dependent_play_needs_input() -> None:
    """The direct unit: `_judged`, judged through `bindings.asserted is
    None`, marks a play `references_archetype` says True for not applicable
    with `needs_input` naming the built-in input - never silently matched
    against a stand-in (B1 item 2)."""
    play = {
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [],
        "false": [{"kind": "input", "name": "opposing_archetype", "op": "eq", "value": ARCHETYPE}],
        "moves": [{"kind": "pass"}],
    }
    doc = {"id": "dependent", "name": "Dependent"}
    bindings = Bindings(inputs={}, catalog={}, asserted=None, _cache=object())  # type: ignore[arg-type]
    # `_cache` is never read: no condition here needs `conditions.
    # needs_hypothetical`, so `_judged` never calls `bindings.facts`.

    entry = script_tools._judged(doc, play, {}, "alice", bindings, [])

    assert entry["applies"] is False
    assert entry["needs_input"] == "opposing_archetype"
    assert entry["failing_condition"]["list"] == "opposing_archetypes"


def test_an_archetype_free_play_is_still_judged_true_before_any_assertion() -> None:
    play = {
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
        "true": [],
        "false": [],
        "moves": [{"kind": "pass"}],
    }
    doc = {"id": "free", "name": "Free"}
    bindings = Bindings(inputs={}, catalog={}, asserted=None, _cache=object())  # type: ignore[arg-type]
    state = {
        "pending_decision": None,
        "priority_holder": "alice",
        "turn_number": 1,
        "step": "precombat_main",
        "phase": "precombat_main",
        "active_player": "alice",
        "players": [],
        "battlefield": [],
        "stack": [],
    }

    entry = script_tools._judged(doc, play, state, "alice", bindings, [])

    assert entry["applies"] is True, entry


# -- bindings.references_archetype (pure) ---------------------------------------


@pytest.mark.parametrize(
    ("play", "expected"),
    [
        pytest.param(
            {"opposing_archetypes": ["dimir-frog"], "true": [], "false": []}, True, id="field"
        ),
        pytest.param(
            {"opposing_archetypes": ["*"], "true": [], "false": []}, False, id="wildcard-only"
        ),
        pytest.param(
            {
                "opposing_archetypes": ["*"],
                "true": [{"kind": "input", "name": "opposing_archetype", "op": "eq", "value": "x"}],
                "false": [],
            },
            True,
            id="true-input",
        ),
        pytest.param(
            {
                "opposing_archetypes": ["*"],
                "true": [],
                "false": [
                    {"kind": "input", "name": "opposing_archetype", "op": "eq", "value": "x"}
                ],
            },
            True,
            id="false-input",
        ),
        pytest.param(
            {
                "opposing_archetypes": ["*"],
                "true": [],
                "false": [
                    {
                        "kind": "all_of",
                        "conditions": [
                            {
                                "kind": "input",
                                "name": "opposing_archetype",
                                "op": "eq",
                                "value": "x",
                            },
                            {"kind": "life", "side": "me", "op": ">=", "n": 1},
                        ],
                    }
                ],
            },
            True,
            id="nested-in-all-of",
        ),
        pytest.param(
            {
                "opposing_archetypes": ["*"],
                "true": [],
                "false": [
                    {
                        "kind": "all_of",
                        "conditions": [
                            {"kind": "life", "side": "me", "op": ">=", "n": 1},
                            {"kind": "life", "side": "opponent", "op": "<=", "n": 20},
                        ],
                    }
                ],
            },
            False,
            id="all-of-without-archetype",
        ),
        pytest.param(
            {
                "opposing_archetypes": ["*"],
                "true": [{"kind": "input", "name": "combo", "op": "eq", "value": "x"}],
                "false": [],
            },
            False,
            id="unrelated-input",
        ),
    ],
)
def test_references_archetype(play: dict[str, Any], expected: bool) -> None:
    assert references_archetype(play) is expected


# -- the offer row carries the asserted archetype (B1 item 3) -------------------


def test_the_offer_row_carries_the_asserted_archetype(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_id = _board(client, _new_game(client))
    alice = seat_uid(game_id, "alice")
    register(client, game_id, alice, _land_play("free"))
    seats = wired(monkeypatch, game_id)
    seat, hook = hooked_seat(seats, seat_uid(game_id, "alice"))

    script_tools.listing(seat, seats.script_corpora, opposing_archetype=ARCHETYPE)
    state = seat.fetch_state()
    hint = script_tools.applicable_hint(seat, seats.script_corpora, state)

    assert hint is not None
    offers = [body for body in hook.posts("/script-runs") if body.get("phase") == "offer"]
    assert offers, hint
    assert all(body["opposing_archetype_asserted"] == ARCHETYPE for body in offers), offers


# -- parity: offered iff running it would not exit not_applicable --------------

_PARITY_ROWS = [
    pytest.param(
        {"true": [], "false": []},
        True,
        id="plain-no-conditions",
    ),
    pytest.param(
        {
            "true": [
                {
                    "kind": "count",
                    "zone": "battlefield",
                    "side": "me",
                    "op": ">=",
                    "n": 1,
                    "select": {"types_all": ["Land"]},
                }
            ]
        },
        True,
        id="true-count-holds",
    ),
    pytest.param(
        {
            "false": [
                {
                    "kind": "count",
                    "zone": "battlefield",
                    "side": "me",
                    "op": ">=",
                    "n": 1,
                    "select": {"types_all": ["Land"]},
                }
            ]
        },
        False,
        id="false-count-excludes",
    ),
    pytest.param(
        {"true": [{"kind": "input", "name": "opposing_archetype", "op": "eq", "value": ARCHETYPE}]},
        True,
        id="input-matches",
    ),
    pytest.param(
        {
            "false": [
                {"kind": "input", "name": "opposing_archetype", "op": "eq", "value": ARCHETYPE}
            ]
        },
        False,
        id="input-excludes",
    ),
    pytest.param(
        {"opposing_archetypes": [OTHER_ARCHETYPE]},
        False,
        id="archetype-field-excludes",
    ),
]


@pytest.mark.parametrize(("over", "expect_applies"), _PARITY_ROWS)
def test_parity_offer_agrees_with_the_run_it_invites(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, over: dict[str, Any], expect_applies: bool
) -> None:
    """B1: the offer side (`listing`, through `judge_bindings`) and the run
    side (`runtime.run_script`, through `script_tools.run`) must agree on
    whether this play applies for the same asserted archetype - never one
    saying "offer it" and the other "not_applicable"."""
    game_id = _board(client, _new_game(client))
    alice = seat_uid(game_id, "alice")
    add_card(client, game_id, "Mountain", "alice", "battlefield")
    play = _land_play("row", **over)
    register(client, game_id, alice, play)
    seats, seat = _seat(monkeypatch, game_id)

    listed = script_tools.listing(seat, seats.script_corpora, opposing_archetype=ARCHETYPE)
    assert isinstance(listed, dict), listed
    [entry] = listed["scripts"]
    assert entry["applies"] is expect_applies, entry

    run_result = script_tools.run(
        seat, seats.script_corpora, script="row", reasoning=REASONING, opposing_archetype=ARCHETYPE
    )
    assert isinstance(run_result, dict), run_result
    exit_ = run_result["script_exit"]["exit"]
    assert (exit_ != Exit.NOT_APPLICABLE.value) is expect_applies, run_result


def test_parity_holds_for_a_play_that_needs_the_next_turn_horizon(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `unlocks_next`/`payable_any` dimension: whatever the engine's real
    answer is, the offer side and the run side must land on the same side of
    `applies`, because both now read that fact through the same
    horizon-aware cache (`Bindings.facts`/`runtime._HypotheticalCache`)."""
    game_id = _board(client, _new_game(client))
    alice = seat_uid(game_id, "alice")
    add_card(client, game_id, "Preordain", "alice", "hand")
    play = _unlocks_land_script("row")
    play["true"] = [
        {
            "kind": "count",
            "zone": "hand",
            "side": "me",
            "op": ">=",
            "n": 1,
            "select": {"types_all": ["Land"], "unlocks_next": True},
        }
    ]
    register(client, game_id, alice, play)
    seats, seat = _seat(monkeypatch, game_id)

    listed = script_tools.listing(seat, seats.script_corpora, opposing_archetype=ARCHETYPE)
    assert isinstance(listed, dict), listed
    [entry] = listed["scripts"]

    run_result = script_tools.run(
        seat, seats.script_corpora, script="row", reasoning=REASONING, opposing_archetype=ARCHETYPE
    )
    assert isinstance(run_result, dict), run_result
    exit_ = run_result["script_exit"]["exit"]
    assert (exit_ != Exit.NOT_APPLICABLE.value) == entry["applies"], (entry, run_result)


# -- hypothetical facts: horizon-aware, at most once per horizon per hint -------


def _horizons_asked(hook: Hooked) -> list[Any]:
    return [
        params.get("horizon") if params else None
        for method, path, params in hook.sent
        if method == "GET" and path.endswith("/playscripts/hypothetical-facts")
    ]


def _unlocks_next_land_script(script_id: str, **over: Any) -> dict[str, Any]:
    doc = land_script(script_id)
    doc["true"] = [
        {
            "kind": "count",
            "zone": "hand",
            "side": "me",
            "op": ">=",
            "n": 1,
            "select": {"types_all": ["Land"], "unlocks_next": True},
        }
    ]
    doc.update(over)
    return doc


def test_a_play_needing_the_plain_fetch_asks_with_no_horizon(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_id = _board(client, _new_game(client))
    add_card(client, game_id, "Preordain", "alice", "hand")
    alice = seat_uid(game_id, "alice")
    register(client, game_id, alice, _unlocks_land_script("plain-hypo", priority=50))
    seats = wired(monkeypatch, game_id)
    seat, hook = hooked_seat(seats, alice)

    result = script_tools.listing(seat, seats.script_corpora)

    assert isinstance(result, dict), result
    horizons = _horizons_asked(hook)
    assert horizons, "the corpus does read unlocks/enters_any, so at least one fetch must happen"
    assert all(horizon is None for horizon in horizons), horizons


def test_a_play_needing_the_next_turn_pass_asks_with_that_horizon(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    game_id = _board(client, _new_game(client))
    add_card(client, game_id, "Preordain", "alice", "hand")
    alice = seat_uid(game_id, "alice")
    register(client, game_id, alice, _unlocks_next_land_script("next-turn-hypo", priority=50))
    seats = wired(monkeypatch, game_id)
    seat, hook = hooked_seat(seats, alice)

    result = script_tools.listing(seat, seats.script_corpora)

    assert isinstance(result, dict), result
    horizons = _horizons_asked(hook)
    assert "next_turn" in horizons, horizons


def test_two_horizons_in_one_corpus_each_fetch_at_most_once_per_hint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corpus mixing a plain-hypothetical play and a next-turn-horizon
    play still pays at most one fetch per horizon for the whole judging
    round, never one per play (mirrors `test_listing_fetches_hypothetical_
    facts_once_for_every_script_at_one_position`, extended to two horizons -
    B1's own `_FactsCache`, two independent single slots)."""
    game_id = _board(client, _new_game(client))
    add_card(client, game_id, "Preordain", "alice", "hand")
    alice = seat_uid(game_id, "alice")
    register(
        client,
        game_id,
        alice,
        _unlocks_land_script("plain-hypo-a", priority=50),
        _unlocks_land_script("plain-hypo-b", priority=49),
        _unlocks_next_land_script("next-turn-a", priority=48),
        _unlocks_next_land_script("next-turn-b", priority=47),
    )
    seats = wired(monkeypatch, game_id)
    seat, hook = hooked_seat(seats, alice)

    result = script_tools.listing(seat, seats.script_corpora)

    assert isinstance(result, dict), result
    horizons = _horizons_asked(hook)
    assert sorted(horizons, key=str) == sorted([None, "next_turn"], key=str), horizons


# -- _first_move_allowed (FC1 part 5, after FC4) ---------------------------------

_ME = "alice"


def _state(
    *, pending: dict[str, Any] | None = None, priority_holder: str | None = None
) -> dict[str, Any]:
    return {"pending_decision": pending, "priority_holder": priority_holder, "players": []}


def _play(move: dict[str, Any], *, true: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"moves": [move], "true": true or []}


def test_declare_attackers_first_move_allowed_when_that_decision_is_pending_and_mine() -> None:
    play = _play({"kind": "declare_attackers", "attackers": []})
    state = _state(pending={"kind": "declare_attackers", "player": _ME})
    assert _first_move_allowed(play, state, _ME) is True


def test_declare_attackers_first_move_refused_when_nothing_is_pending() -> None:
    play = _play({"kind": "declare_attackers", "attackers": []})
    state = _state(pending=None)
    assert _first_move_allowed(play, state, _ME) is False


def test_declare_attackers_first_move_refused_when_a_different_decision_is_pending() -> None:
    play = _play({"kind": "declare_attackers", "attackers": []})
    state = _state(pending={"kind": "declare_blockers", "player": _ME})
    assert _first_move_allowed(play, state, _ME) is False


def test_declare_blockers_first_move_allowed_when_that_decision_is_pending_and_mine() -> None:
    play = _play({"kind": "declare_blockers", "blocks": []})
    state = _state(pending={"kind": "declare_blockers", "player": _ME})
    assert _first_move_allowed(play, state, _ME) is True


def test_declare_blockers_first_move_refused_when_pending_for_someone_else() -> None:
    play = _play({"kind": "declare_blockers", "blocks": []})
    state = _state(pending={"kind": "declare_blockers", "player": "bob"})
    assert _first_move_allowed(play, state, _ME) is False


def test_pass_first_move_allowed_when_this_seat_holds_priority() -> None:
    play = _play({"kind": "pass"})
    state = _state(priority_holder=_ME)
    assert _first_move_allowed(play, state, _ME) is True


def test_pass_first_move_refused_when_this_seat_does_not_hold_priority() -> None:
    play = _play({"kind": "pass"})
    state = _state(priority_holder="bob")
    assert _first_move_allowed(play, state, _ME) is False


def test_pending_decision_standalone_answer_allowed_when_that_decision_is_pending_and_mine() -> (
    None
):
    play = _play(
        {"kind": "answer", "answers": [{"decision": "pay_life", "accept": True}]},
        true=[{"kind": "pending_decision", "decision": "pay_life"}],
    )
    state = _state(pending={"kind": "pay_life", "player": _ME})
    assert _first_move_allowed(play, state, _ME) is True


def test_pending_decision_standalone_answer_refused_for_a_different_pending_decision() -> None:
    play = _play(
        {"kind": "answer", "answers": [{"decision": "pay_life", "accept": True}]},
        true=[{"kind": "pending_decision", "decision": "pay_life"}],
    )
    state = _state(pending={"kind": "scry", "player": _ME})
    assert _first_move_allowed(play, state, _ME) is False


def test_pending_decision_standalone_answer_refused_when_pending_for_someone_else() -> None:
    play = _play(
        {"kind": "answer", "answers": [{"decision": "pay_life", "accept": True}]},
        true=[{"kind": "pending_decision", "decision": "pay_life"}],
    )
    state = _state(pending={"kind": "pay_life", "player": "bob"})
    assert _first_move_allowed(play, state, _ME) is False


def test_pending_decision_standalone_answer_refused_when_nothing_is_pending() -> None:
    play = _play(
        {"kind": "answer", "answers": [{"decision": "pay_life", "accept": True}]},
        true=[{"kind": "pending_decision", "decision": "pay_life"}],
    )
    state = _state(pending=None)
    assert _first_move_allowed(play, state, _ME) is False
