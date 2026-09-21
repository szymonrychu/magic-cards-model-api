"""S2 A2 / API-7: `playscripts/arming.py` - the arming core.

An agent may hand `arm()` an ordered list of script ids instead of running one
by hand: `arm()` judges every armed id against the board with the same
`_judged`/hint logic and reports which of them apply right now - it never
runs anything. Two or more applying at once is a conflict, reported back
rather than picked for the seat.

API-7 removed the whole "fire the sole applier through `script_tools.run`,
re-judge, keep going up to `MAX_ARMED_RUNS`" batch this file used to cover -
the owner dropped autofire entirely, so there is no run, no per-run time
budget, no exit-handling taxonomy left to test here at all. What survives
(and what replaces the removed coverage) is: the up-front refusals, the real
end-to-end judging over a live board (including the contents fetch and the
already-run-this-step exclusion, both still exactly `_judge_round`'s own
unchanged machinery), the conflict case, and the pending-decision gate - each
now asserted as "what `applying` reports", never "what fired".
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import add_card, force_turn_position
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_playscript_runtime import (
    fetch_shock,
    hooked_seat,
    land_script,
    register,
    seat_uid,
    started,
    tap_island_script,
    wired,
)

from magic_cards_model_api.playscripts import arming, script_tools
from magic_cards_model_api.playscripts.runtime import REASONING_PLACEHOLDER

ARM_REASONING = "the human is thinking; arm this so I can act the moment it is legal"


@pytest.fixture()
def board(client: TestClient, game: str) -> str:
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    return game


# -- refusals, before any fetch -------------------------------------------------


def test_an_empty_arm_list_does_nothing(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = arming.arm(seat, seats.script_corpora, arm=[], arm_reasoning="")

    assert result is None
    assert hook.sent == []


def test_a_blank_arm_reasoning_is_refused_before_any_fetch(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = arming.arm(seat, seats.script_corpora, arm=["drop"], arm_reasoning="   ")

    assert isinstance(result, str)
    assert hook.sent == []


def test_the_call_templates_placeholder_reasoning_is_refused_before_any_fetch(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, seat_uid(board, "alice"))

    result = arming.arm(
        seat, seats.script_corpora, arm=["drop"], arm_reasoning=REASONING_PLACEHOLDER
    )

    assert isinstance(result, str)
    assert REASONING_PLACEHOLDER in result
    assert hook.sent == []


# -- real board: judging never runs anything ------------------------------------


def test_a_single_applicable_armed_script_is_reported_applying_and_never_run(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-7: the old test here proved the armed script actually ran
    (`hook.posts("/actions/play-land")`) - that behaviour is gone by owner
    decision. What replaces it: `arm()` reports the id as applying, and
    nothing about the board or the ledger moves at all."""
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop"))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = arming.arm(seat, seats.script_corpora, arm=["drop"], arm_reasoning=ARM_REASONING)

    assert isinstance(result, dict), result
    assert result.get("armed_conflict") is None
    assert result["applying"] == ["drop"]
    assert hook.posts("/actions/play-land") == [], "arm() must never run anything (API-7)"
    assert started(board) == [], "no ledger row - nothing was ever attempted"


def test_a_script_off_its_when_is_reported_not_applying(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, fetch_shock())
    # No fetch land on the board at all: the script's `true` never holds.
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = arming.arm(
        seat, seats.script_corpora, arm=[fetch_shock()["id"]], arm_reasoning=ARM_REASONING
    )

    assert isinstance(result, dict), result
    assert result["applying"] == []
    assert hook.posts("/actions/activate") == []


def test_two_armed_scripts_applying_at_once_is_a_conflict(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    register(client, board, alice, land_script("drop-a"), land_script("drop-b", priority=45))
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = arming.arm(
        seat, seats.script_corpora, arm=["drop-a", "drop-b"], arm_reasoning=ARM_REASONING
    )

    assert isinstance(result, dict), result
    assert set(result["armed_conflict"]) == {"drop-a", "drop-b"}
    assert result["applying"] == []
    assert hook.posts("/actions/play-land") == []
    assert started(board) == []


def test_a_contents_needing_armed_script_costs_exactly_one_extra_fetch(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(board, "alice")
    needs_contents = land_script(
        "needs-library-count",
        true=[{"kind": "count", "zone": "library", "side": "me", "op": ">=", "n": 1, "select": {}}],
    )
    register(client, board, alice, needs_contents)
    add_card(client, board, "Island", "alice", "hand")
    add_card(client, board, "Mountain", "alice", "library")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)
    calls = {"n": 0}
    real_fetch_state = seat.__class__.fetch_state

    def counting(self: Any, *a: Any, **kw: Any) -> Any:
        calls["n"] += 1
        return real_fetch_state(self, *a, **kw)

    monkeypatch.setattr(seat.__class__, "fetch_state", counting)

    judged, judged_state = arming._judge_round(
        seat, seats.script_corpora, ["needs-library-count"], {"opposing_archetype": "*"}
    )

    assert judged["needs-library-count"]["applies"] is True, judged
    assert judged_state.get("library_contents"), judged_state
    # One plain board fetch, then one extra `include=contents` fetch, paid
    # once for the whole round - never a second blind re-fetch.
    assert calls["n"] == 2, calls


# -- finding 7b: the judge's own contents fetch never leaks into the answer ----


def test_a_conflicting_round_never_leaks_library_contents_into_the_answer(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 review finding 7: `_judge_round`'s own `include=contents` fetch -
    paid only to judge a condition that reads this seat's own hidden zones -
    must never become the state `arm()` hands back. API-7: with `arm()` now
    never running anything at all, this is not merely the "nothing fired"
    branch any more - it is the ONLY branch, so this must hold for every
    `arm()` answer, not just a conflicting one."""
    alice = seat_uid(board, "alice")
    needs_contents = land_script(
        "needs-library-count",
        priority=60,
        true=[
            {"kind": "count", "zone": "library", "side": "me", "op": ">=", "n": 1, "select": {}},
        ],
    )
    register(client, board, alice, needs_contents, land_script("drop", priority=50))
    add_card(client, board, "Island", "alice", "hand")
    add_card(client, board, "Forest", "alice", "library")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = arming.arm(
        seat,
        seats.script_corpora,
        arm=["needs-library-count", "drop"],
        arm_reasoning=ARM_REASONING,
    )

    assert isinstance(result, dict), result
    assert result.get("armed_conflict"), result
    assert result["applying"] == [], result
    assert "library_contents" not in result["state"], result["state"]
    assert hook.posts("/actions/play-land") == []


# -- pending decision: only a first-move-answer play applies -------------------


def answer_pay_mana_script(script_id: str = "pay-it") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": script_id,
        "name": "Pay the mana",
        "priority": 60,
        "opposing_archetypes": ["*"],
        "when": {"whose_turn": "either", "steps": ["precombat_main"], "stack": "any"},
        "true": [],
        "false": [],
        "moves": [{"kind": "answer", "answers": [{"decision": "pay_mana", "accept": True}]}],
        "next": [],
    }


def test_a_pending_decision_only_lets_a_first_move_answer_play_apply(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 review finding 4 (seat half): while a decision is pending for this
    seat, CR 608.2d freezes every other action until it is answered - a play
    whose first move answers exactly that decision still applies (it is the
    one legal thing to do), but a play that would send any other move does
    not, judged not-applying rather than being fired and refused by the
    engine (`_judge_round`/`_judged` used to ignore `pending_decision`
    entirely)."""
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        answer_pay_mana_script("pay-it"),
        land_script(
            "drop", when={"whose_turn": "either", "steps": ["precombat_main"], "stack": "any"}
        ),
    )
    seats = wired(monkeypatch, board)
    seat, _ = hooked_seat(seats, alice)
    # A real view with the pending decision laid over it: condition
    # evaluation reads the board (untapped mana for `affordable`), so a
    # hand-built state missing `battlefield` is not a view the engine sends.
    pending_state = {
        **seat.fetch_state(),
        "priority_holder": "alice",
        "pending_decision": {"player": "alice", "kind": "pay_mana", "prompt": "pay {3}?"},
    }
    monkeypatch.setattr(seat.__class__, "fetch_state", lambda self, **kw: pending_state)

    judged, _ = arming._judge_round(
        seat, seats.script_corpora, ["pay-it", "drop"], {"opposing_archetype": "*"}
    )

    assert judged["pay-it"]["applies"] is True, judged["pay-it"]
    assert judged["drop"]["applies"] is False, judged["drop"]
    assert judged["drop"]["failing_condition"]["list"] == "pending_decision", judged["drop"]


def test_advisor_arm_is_blocked_by_a_pending_decision_then_reported_once_it_clears(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 review finding 4 (advisor half), narrowed by API-7: an advisor's
    armed script whose first move is a regular action, watching a human with
    a pending `pay_mana` decision, is judged not-applying while the decision
    is pending - the same gate a seat gets, since `_judge_round` never
    distinguishes advisor from seat at all (it only ever judges, never runs,
    so there is no advise-mode branch left to test here). The old test named
    this `test_advisor_armed_run_blocked_by_a_pending_decision_is_not_locked_
    out` and additionally proved a fired run's own advise-mode proposal
    (`exit == "advised"`) - that assertion is gone with the run it covered;
    `arm()` reports `applying` for an advisor exactly as it does for a seat."""
    from test_decision_kinds import resolve_top, stage_mystical_dispute_targeting_bears

    from magic_cards_model_api.repository import Advisor
    from magic_cards_model_api.store import store

    advisor = "pending-decision-advisor-uid"
    store.register_advisors(game, {advisor: Advisor(player="alice")})
    register(
        client,
        game,
        advisor,
        land_script(
            "drop", when={"whose_turn": "either", "steps": ["precombat_main"], "stack": "any"}
        ),
    )
    stage_mystical_dispute_targeting_bears(client, game, {"R": 4})
    add_card(client, game, "Island", "alice", "hand")
    resolved = resolve_top(game)
    assert resolved["paused"] is True, resolved

    seats = wired(monkeypatch, game)
    seat, _ = hooked_seat(seats, advisor)

    blocked = arming.arm(seat, seats.script_corpora, arm=["drop"], arm_reasoning=ARM_REASONING)
    assert isinstance(blocked, dict), blocked
    assert blocked["applying"] == [], blocked
    assert started(game) == [], "nothing was ever attempted while the decision was pending"

    alice = seat_uid(game, "alice")
    paid = client.post(
        f"/api/games/{game}/decide",
        params={"uid": alice},
        json={"player": "alice", "accept": True},
    )
    assert paid.status_code == 200, paid.text
    resolve_top(game)  # Mystical Dispute leaves the stack; Grizzly Bears resolves

    proposed = arming.arm(seat, seats.script_corpora, arm=["drop"], arm_reasoning=ARM_REASONING)
    assert isinstance(proposed, dict), proposed
    assert proposed["applying"] == ["drop"], proposed
    assert started(game) == [], "arm() must never run anything (API-7)"


# -- a script judged needing an unsupplied input is never reported applying ----


def test_a_script_needing_an_unsupplied_input_is_not_reported_applying(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old suite proved `arm()` would still ATTEMPT a script whose
    condition needed an input it was not given, surfacing `needs_input` as a
    fired-but-incomplete run. That attempt is gone with the run: `_judged`
    itself never lets such a script's `applies` become `True` (its own
    `conditions.evaluate` sets `needs_input`/`invalid_input` instead), so
    `arm()` simply never reports it as applying."""
    doc = land_script("need-tempo")
    doc["true"] = [
        *doc["true"],
        {"kind": "input", "name": "prefer_tempo", "op": "eq", "value": True},
    ]
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        doc,
        inputs=[
            {
                "name": "prefer_tempo",
                "type": "bool",
                "description": "Tempo first.",
                "required": True,
            }
        ],
    )
    add_card(client, board, "Island", "alice", "hand")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    result = arming.arm(seat, seats.script_corpora, arm=["need-tempo"], arm_reasoning=ARM_REASONING)

    assert isinstance(result, dict), result
    assert result["applying"] == []
    assert hook.posts("/actions/play-land") == []


# -- conflict excludes ids already run this step --------------------------------


def test_a_script_already_run_this_step_does_not_count_toward_a_conflict(
    client: TestClient, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2 review finding 5: `_judge_round`'s conflict check used to judge
    every armed id fresh against the board, including one this process
    already ran at this same turn and step - so a script that had already
    fired earlier this step, still condition-true (two untapped Islands
    means tapping one still leaves the count condition true), could tie
    with a genuinely new applier and report a conflict that was not really
    there. `_judge_round` skips an id `applicable_hint`'s own already-ran
    cache holds for this position, the same way the hint itself does - this
    machinery is unchanged by API-7 (`script_tools.run` itself still exists
    and is still how a script is actually run, whether by hand or read off
    `scripts.applicable`'s own `call`)."""
    alice = seat_uid(board, "alice")
    register(
        client,
        board,
        alice,
        tap_island_script("float-a", priority=60),
        tap_island_script("float-b", priority=50),
    )
    add_card(client, board, "Island", "alice", "battlefield")
    add_card(client, board, "Island", "alice", "battlefield")
    seats = wired(monkeypatch, board)
    seat, hook = hooked_seat(seats, alice)

    first = script_tools.run(
        seat,
        seats.script_corpora,
        script="float-a",
        reasoning=ARM_REASONING,
        wait=False,
    )
    assert first["script_exit"]["exit"] == "chain_complete", first["script_exit"]

    judged, _ = arming._judge_round(
        seat, seats.script_corpora, ["float-a", "float-b"], {"opposing_archetype": "*"}
    )

    assert judged["float-a"]["applies"] is False, judged["float-a"]
    assert judged["float-a"]["failing_condition"]["list"] == "already_run_this_step", judged[
        "float-a"
    ]
    assert judged["float-b"]["applies"] is True, judged["float-b"]


# -- an unregistered armed id's own computed refusal must reach the caller ------


def test_arm_returns_the_refusal_for_an_unregistered_armed_id(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_judge_round` already computes a `refusal` for an armed id that is
    not registered under this seat's corpus ("no script %r is registered for
    this seat") - API-7's `arm()` surfaces it as `armed_unregistered` rather
    than silently reading the same as "genuinely does not apply yet"."""
    alice = seat_uid(game, "alice")
    seats = wired(monkeypatch, game)
    seat, _ = hooked_seat(seats, alice)
    register(client, game, alice, land_script("drop"))

    result = arming.arm(
        seat,
        seats.script_corpora,
        arm=["not-a-real-script"],
        arm_reasoning=ARM_REASONING,
    )

    assert isinstance(result, dict), result
    assert result["applying"] == []
    [entry] = result["armed_unregistered"]
    assert entry["id"] == "not-a-real-script"
    assert "not-a-real-script" in entry["reason"]
    assert "registered" in entry["reason"]
