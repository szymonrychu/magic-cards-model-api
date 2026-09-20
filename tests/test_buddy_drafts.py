"""S2 A6: buddy drafts.

The advisor's proposal machinery (`test_advise_mode.py`) always did the same
thing on every committing call: dry-run probe, then post `/advice`
immediately. That means a buddy can only ever start thinking about a proposal
once its human's priority has already arrived - nothing prepares ahead of
time, and the human waits on the very first inference.

This file is the other half: `stage_advice` arms the *next* committing call
to be captured as a draft for a future window instead of posted, and
`seatkit.resolve_advice_draft` (wired into `mcp._tool_result` for every
advisor answer, `wait_for_priority`'s own poll loop included) decides on
every later poll whether that draft is still good enough to post - three
independent conditions, all of which have to hold: the window itself, the
opponent's board unchanged, and the action still legal.

Real boards throughout, driven with `conftest.force_turn_position` to reach a
turn position without replaying the whole turn machine to get there - the
same idiom `test_card_split_labels.py`/`test_special_actions.py` already use,
never a substitute for the ordinary API-driven paths this project's own
`CLAUDE.md` (magic-cards-model-api is tested by ordinary practice, not the
engine's own no-mocks rule) allows here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import add_card, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from test_advise_mode import (
    ADVISOR_UID,
    advice_rows,
    advisor_uid,
    buddy,
    mcp_server,
    player_seat,
    seats_for,
    tool_text,
)
from test_decisions import advance_into_cleanup, stuff_hand
from test_playscript_runtime import land_script, register

from magic_cards_model_api import seat_cli, seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seat
from magic_cards_model_api.store import store


def wire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))


@pytest.fixture()
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `seatkit`'s own `httpx.Client` at the in-process app - the same
    trick `test_advise_mode.py`'s own `wired` fixture uses, redefined locally
    rather than imported so this file does not also have to import that
    fixture's own `sent` dependency just to use it."""
    wire(monkeypatch)


@pytest.fixture(autouse=True)
def _isolated_cli_drafts_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every `seat_cli stage-advice` command in this file persists its draft
    to disk (S2 A6 review fix 2) - point it at a throwaway directory rather
    than the real default (a dev checkout's repo-relative `.cli-drafts`, or a
    live seat pod's `$WORKSPACE/cli-drafts`), so no test run leaves a file
    behind. Autouse: every test in this file gets this for free, whether or
    not it happens to touch the CLI."""
    monkeypatch.setenv(seat_cli.CLI_DRAFTS_DIR_ENV, str(tmp_path / "cli-drafts"))


def stock_a_bolt(client: TestClient, game_id: str, *, player: str = "alice") -> tuple[str, str]:
    add_card(client, game_id, "Mountain", player, "battlefield")
    add_card(client, game_id, "Lightning Bolt", player, "hand")
    board = state(client, game_id)
    mountain = find(board["battlefield"], "Mountain")
    bolt = board["hands"][player][0]
    return bolt["id"], mountain["id"]


def capture_a_pass_draft(seat: Seat, game_id: str, *, reasoning: str = "nothing to do yet") -> Any:
    """Arm nothing - just the committing call that fills in whatever window a
    prior `stage_advice` armed, with the cheapest possible shape (`pass`, one
    of the 5 unvalidated action shapes, needing no board setup at all)."""
    return seat.call_json(
        "POST",
        f"/api/games/{game_id}/actions/pass-priority",
        {"player": seat.player, "reasoning": reasoning},
    )


# ---------------------------------------------------------------------------
# 1. stage_advice: arming, next-occurrence semantics, refusals
# ---------------------------------------------------------------------------


def test_stage_advice_is_refused_for_a_seat(client: TestClient, game: str, wired: None) -> None:
    seat = player_seat(game)

    result = seat.stage_advice("end_step", "mine")

    assert result == {"staged": False, "reason": seatkit.STAGE_ADVICE_ADVISOR_ONLY}


def test_stage_advice_refuses_an_unknown_step_the_same_way_pass_until_does(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)

    result = seat.stage_advice("cleanup", "mine")

    assert result["staged"] is False
    assert "cleanup" in result["reason"]


def test_stage_advice_arms_a_window_stamped_from_the_current_board(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)

    result = seat.stage_advice("end_step", "mine")

    assert result == {"staged": True, "window": {"step": "end_step", "whose_turn": "mine"}}
    # `buddy()` mints a bare `Seat` (not routed through a `Seats`), so the
    # drafts dict this stamped is that instance's own private one.
    assert seat._advice_drafts[ADVISOR_UID]["action"] is None
    assert seat._advice_drafts[ADVISOR_UID]["window"]["step"] == "end_step"
    assert seat._advice_drafts[ADVISOR_UID]["window"]["started_step"] == "precombat_main"


def test_staging_again_before_capture_replaces_the_armed_window(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)
    seat.stage_advice("end_step", "mine")

    seat.stage_advice("upkeep", "opponent")

    armed = seat._advice_drafts[ADVISOR_UID]
    assert armed["action"] is None
    assert armed["window"]["step"] == "upkeep"
    assert armed["window"]["whose_turn"] == "opponent"


# ---------------------------------------------------------------------------
# 1b. draft_window_status: next-occurrence semantics (2026-09-14 review, 1)
#
# The pre-fix "after" check only asked whether the current step's rank was
# later than the target's - wrong the moment the target step's own rank comes
# BEFORE the step the draft was staged in, since naming that step's next
# occurrence means next turn, not later this one. Every scenario below stages
# in a step that ranks at-or-after the target, then proves moving later in
# the SAME turn stays "before", and only the real next occurrence (a later
# `turns_taken`) reads "reached".
# ---------------------------------------------------------------------------


def _staged_window(
    staged_state: dict[str, Any], target_step: str, whose_turn: str
) -> dict[str, Any]:
    """A window exactly as `Seat.stage_advice` stamps one - `draft_window_
    status` is a pure function of the `(window, state)` pair, so these
    scenarios build both by hand rather than through a live game."""
    window = seatkit.start_autopass(staged_state, target_step, whose_turn)
    window["stamped_step"] = staged_state.get("step")
    window["stamped_turns_taken"] = staged_state.get("turns_taken")
    return window


def _board(*, step: str, active_player: str, turn_number: int, turns_taken: int) -> dict[str, Any]:
    return {
        "step": step,
        "active_player": active_player,
        "turn_number": turn_number,
        "turns_taken": turns_taken,
    }


def test_staged_at_the_target_step_itself_waits_a_full_turn(client, game, wired) -> None:
    """Staged in the human's own precombat_main for `precombat_main mine`:
    the next occurrence is NEXT turn's, so moving to beginning_of_combat
    later the SAME turn must stay "before", not read "after" the moment the
    step rank alone looks later."""
    staged = _board(step="precombat_main", active_player="alice", turn_number=1, turns_taken=1)
    window = _staged_window(staged, "precombat_main", "mine")

    still_this_turn = _board(
        step="beginning_of_combat", active_player="alice", turn_number=1, turns_taken=1
    )
    assert seatkit.draft_window_status(window, still_this_turn, "alice") == "before"

    next_turns_precombat = _board(
        step="precombat_main", active_player="alice", turn_number=2, turns_taken=3
    )
    assert seatkit.draft_window_status(window, next_turns_precombat, "alice") == "reached"


def test_staged_after_the_target_step_waits_for_next_turn(client, game, wired) -> None:
    """Staged in the human's main for `upkeep mine`: upkeep already passed
    this turn, so the next occurrence is next turn's, and moving further
    through THIS turn must never read "after"."""
    staged = _board(step="precombat_main", active_player="alice", turn_number=1, turns_taken=1)
    window = _staged_window(staged, "upkeep", "mine")

    later_this_turn = _board(
        step="postcombat_main", active_player="alice", turn_number=1, turns_taken=1
    )
    assert seatkit.draft_window_status(window, later_this_turn, "alice") == "before"

    next_turns_upkeep = _board(step="upkeep", active_player="alice", turn_number=2, turns_taken=3)
    assert seatkit.draft_window_status(window, next_turns_upkeep, "alice") == "reached"


def test_staged_at_the_opponents_end_step_for_upkeep_any(client, game, wired) -> None:
    """Staged at the opponent's end step for `upkeep any`: the next
    occurrence is whichever side's untap/upkeep comes next, so cleanup - this
    same turn, right after end_step - must not read "after"."""
    staged = _board(step="end_step", active_player="bob", turn_number=1, turns_taken=2)
    window = _staged_window(staged, "upkeep", "any")

    cleanup = _board(step="cleanup", active_player="bob", turn_number=1, turns_taken=2)
    assert seatkit.draft_window_status(window, cleanup, "alice") == "before"

    next_upkeep = _board(step="upkeep", active_player="alice", turn_number=2, turns_taken=3)
    assert seatkit.draft_window_status(window, next_upkeep, "alice") == "reached"


def test_staged_in_the_opponents_combat_for_upkeep_opponent(client, game, wired) -> None:
    """Staged in the opponent's combat for `upkeep opponent`: upkeep already
    passed this opponent turn, so the very next step must not read "after"."""
    staged = _board(step="declare_attackers", active_player="bob", turn_number=1, turns_taken=2)
    window = _staged_window(staged, "upkeep", "opponent")

    next_step = _board(step="declare_blockers", active_player="bob", turn_number=1, turns_taken=2)
    assert seatkit.draft_window_status(window, next_step, "alice") == "before"

    opponents_next_upkeep = _board(step="upkeep", active_player="bob", turn_number=2, turns_taken=4)
    assert seatkit.draft_window_status(window, opponents_next_upkeep, "alice") == "reached"


def test_a_window_many_turns_stale_reads_after_not_reached(client, game, wired) -> None:
    """The step/whose_turn match alone is not enough to call a window
    "reached": that same step recurs every turn, so with no bound on how far
    `turns_taken` has drifted past the stamp, a window nobody ever polled
    during its real next occurrence would read "reached" again at ANY later
    matching step - staged at `turns_taken=2` and first read again at 12
    (ten turns later, nobody having polled in between) is not "the moment
    has arrived", it is a window whose one real occurrence came and went
    long ago."""
    staged = _board(step="declare_attackers", active_player="bob", turn_number=1, turns_taken=2)
    window = _staged_window(staged, "upkeep", "opponent")

    long_stale_upkeep = _board(step="upkeep", active_player="bob", turn_number=6, turns_taken=12)
    assert seatkit.draft_window_status(window, long_stale_upkeep, "alice") == "after"


def test_an_extra_turn_does_not_falsely_mask_reached_as_before(client, game, wired) -> None:
    """CR 500.7: an extra turn bumps `turns_taken` but leaves `turn_number`
    alone (`seatkit.py`'s own `turns_taken` note). Staged in the human's
    precombat_main for `precombat_main mine`, an immediately following extra
    turn for the same player shares that same `turn_number` - a same-
    occurrence check keyed on `turn_number` alone would misread the extra
    turn's own precombat_main as still the occurrence staged in and mask a
    real "reached" as "before" (`same_occurrence_started_in` now keys on
    `turns_taken` instead, exactly to tell the two apart)."""
    staged = _board(step="precombat_main", active_player="alice", turn_number=1, turns_taken=1)
    window = _staged_window(staged, "precombat_main", "mine")

    # The extra turn under way, not yet at its own precombat_main - still
    # before the real next occurrence, whatever its shared turn_number.
    extra_turns_upkeep = _board(step="upkeep", active_player="alice", turn_number=1, turns_taken=2)
    assert seatkit.draft_window_status(window, extra_turns_upkeep, "alice") == "before"

    # The extra turn's OWN precombat_main - a genuinely new occurrence, even
    # though `turn_number` reads identical to the one staged in.
    extra_turns_precombat = _board(
        step="precombat_main", active_player="alice", turn_number=1, turns_taken=2
    )
    assert seatkit.draft_window_status(window, extra_turns_precombat, "alice") == "reached"


def test_end_to_end_a_window_staged_at_its_own_target_step_survives_the_rest_of_the_turn(
    client: TestClient, game: str, wired: None
) -> None:
    """The same scenario as `test_staged_at_the_target_step_itself_waits_a_
    full_turn`, but through a real game and `resolve_advice_draft`, per the
    review's own request for one end-to-end case alongside the pure ones."""
    seat = buddy(game)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    baseline_turns_taken = seat.fetch_state()["turns_taken"]
    seat.stage_advice("precombat_main", "mine")
    capture_a_pass_draft(seat, game)

    force_turn_position(game, Step.END_STEP, "alice")
    held = seatkit.resolve_advice_draft(seat, seat.fetch_state())
    assert held == {"status": "held"}
    assert ADVISOR_UID in seat._advice_drafts

    engine_game = store.get(game)
    assert engine_game is not None and engine_game.turn is not None
    engine_game.turn.turns_taken = baseline_turns_taken + 2
    engine_game.turn.turn_number += 1
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    posted = seatkit.resolve_advice_draft(seat, seat.fetch_state())

    assert posted["status"] == "posted"
    assert len(advice_rows(game)) == 1


# ---------------------------------------------------------------------------
# 2. capturing the next committing call as a draft, instead of posting
# ---------------------------------------------------------------------------


def test_the_next_committing_call_is_captured_not_posted(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")

    answer = capture_a_pass_draft(seat, game)

    assert answer["drafted"] is True
    assert answer["window"] == {"step": "declare_attackers", "whose_turn": "mine"}
    assert advice_rows(game) == [], "a captured draft must not post to /advice yet"
    entry = seat._advice_drafts[ADVISOR_UID]
    assert entry["action"]["path"].endswith("/actions/pass-priority")
    assert entry["action"]["validated"] is False
    assert isinstance(entry["drafted_at_seq"], int)
    assert entry["opponent_signature"] is not None


def test_a_call_with_nothing_armed_still_posts_immediately(
    client: TestClient, game: str, wired: None
) -> None:
    """No regression: an advisor who never called `stage_advice` still gets
    the ordinary immediate-propose behaviour `test_advise_mode.py` pins."""
    seat = buddy(game)

    answer = capture_a_pass_draft(seat, game)

    assert answer["proposed"] is True
    assert len(advice_rows(game)) == 1


def test_a_refused_probe_leaves_the_arm_untouched(
    client: TestClient, game: str, wired: None
) -> None:
    """Guard 4's own dry-run refusal still returns verbatim, unrecorded - and
    must not consume the armed slot, so the advisor can try something else."""
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt_id = state(client, game)["hands"]["alice"][0]["id"]

    answer = seat.call_json(
        "POST",
        f"/api/games/{game}/actions/cast",
        {"player": "alice", "card_id": bolt_id, "targets": ["bob"], "reasoning": "no mana"},
    )

    assert answer.get("drafted") is not True
    assert answer["allowed"] is False
    assert seat._advice_drafts[ADVISOR_UID]["action"] is None, "the arm must still be empty"


# ---------------------------------------------------------------------------
# 3. resolve_advice_draft: held / reached+posted / each stale condition
# ---------------------------------------------------------------------------


def test_held_before_the_window(client: TestClient, game: str, wired: None) -> None:
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")
    capture_a_pass_draft(seat, game)

    result = seatkit.resolve_advice_draft(seat, seat.fetch_state())

    assert result == {"status": "held"}
    assert seat._advice_drafts[ADVISOR_UID]["action"] is not None, "held must not drop the draft"


def test_posted_once_the_window_is_reached_and_priority_is_theirs(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")
    capture_a_pass_draft(seat, game)

    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")
    result = seatkit.resolve_advice_draft(seat, seat.fetch_state())

    assert result["status"] == "posted"
    assert isinstance(result["log_seq"], int)
    rows = advice_rows(game)
    assert len(rows) == 1
    assert isinstance(rows[0]["detail"]["drafted_at_seq"], int)
    assert rows[0]["detail"]["drafted_at_seq"] < rows[0]["seq"], (
        "drafted before it was posted, so its seq must be strictly earlier"
    )
    assert ADVISOR_UID not in seat._advice_drafts, "a posted draft leaves nothing staged behind"


def test_condition_a2_the_ledger_drifting_far_past_staging_drops_the_draft(
    client: TestClient, game: str, wired: None
) -> None:
    """A second, independent bound on "reached": `draft_window_status`'s own
    `MAX_DRAFT_WINDOW_TURNS_TAKEN_DRIFT` catches a window stale by whole
    turns, but says nothing about how much has happened WITHIN the turns
    since staging - many priority passes, casts and responses can pile up
    `last_seq` well past `window["stamped_seq"]` without ever moving
    `turns_taken` at all. Reached on the letter (still turn 1, the exact
    step and whose_turn staged for) but not the spirit: this many ledger
    rows since staging means the board this draft was drafted against is
    not the board it would post against."""
    from magic_cards_model_api import audit

    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")
    capture_a_pass_draft(seat, game)

    for _ in range(seatkit.MAX_DRAFT_WINDOW_SEQ_DRIFT + 1):
        audit.record(game, "test_filler", player="alice")

    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")
    result = seatkit.resolve_advice_draft(seat, seat.fetch_state())

    assert result == {"status": "stale", "failed": "window"}
    assert advice_rows(game) == []
    assert ADVISOR_UID not in seat._advice_drafts


def test_condition_a_the_window_passing_drops_the_draft(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")
    capture_a_pass_draft(seat, game)

    # Jump straight past declare_attackers to postcombat_main, in the same
    # turn, alice still active - the window's one chance at (turn 1, "mine")
    # is now behind the board, not ahead of it.
    force_turn_position(game, Step.POSTCOMBAT_MAIN, "alice")
    result = seatkit.resolve_advice_draft(seat, seat.fetch_state())

    assert result == {"status": "stale", "failed": "window"}
    assert advice_rows(game) == []
    assert ADVISOR_UID not in seat._advice_drafts


def test_condition_b_an_opponent_board_change_drops_the_draft(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")
    capture_a_pass_draft(seat, game)

    add_card(client, game, "Mountain", "bob", "battlefield")  # the opponent's board moved
    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")
    result = seatkit.resolve_advice_draft(seat, seat.fetch_state())

    assert result == {"status": "stale", "failed": "board"}
    assert advice_rows(game) == []
    assert ADVISOR_UID not in seat._advice_drafts


def test_condition_c_a_probe_that_now_fails_drops_the_draft(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)
    bolt_id, mountain_id = stock_a_bolt(client, game)
    seat.stage_advice("declare_attackers", "mine")

    drafted = seat.call_json(
        "POST",
        f"/api/games/{game}/actions/cast",
        {
            "player": "alice",
            "card_id": bolt_id,
            "targets": ["bob"],
            "mana_sources": [mountain_id],
            "reasoning": "three to the face while we still can",
        },
    )
    assert drafted["drafted"] is True
    assert drafted["validated"] is True

    # Alice's own Mountain is gone before the window arrives - the debug tap
    # endpoint, not `activate` (which would leave floating {R} behind that the
    # re-probe would just spend instead, proving nothing). Alice's own mana
    # going away, never the opponent's board - condition (b) already covers
    # that on its own.
    client.post(f"/api/games/{game}/cards/{mountain_id}/tap", json={"tapped": True})
    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")
    result = seatkit.resolve_advice_draft(seat, seat.fetch_state())

    assert result == {"status": "stale", "failed": "legality"}
    assert advice_rows(game) == []


def test_an_unvalidated_draft_still_posts_once_reached(
    client: TestClient, game: str, wired: None
) -> None:
    """The 5 shapes with no `dry_run` (`_unvalidated_action_still_legal`) can
    still be drafted and posted - `pass-priority` is legal exactly when this
    seat holds priority, which is also the very moment the window requires."""
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")
    capture_a_pass_draft(seat, game)

    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")
    result = seatkit.resolve_advice_draft(seat, seat.fetch_state())

    assert result["status"] == "posted"
    assert advice_rows(game)[0]["detail"]["validated"] is False


# ---------------------------------------------------------------------------
# 4. drafted_at_seq
# ---------------------------------------------------------------------------


def test_drafted_at_seq_is_the_ledger_position_at_staging_time(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")
    before_capture = seat.recent_log()["last_seq"]

    capture_a_pass_draft(seat, game)

    assert seat._advice_drafts[ADVISOR_UID]["drafted_at_seq"] == before_capture

    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")
    seatkit.resolve_advice_draft(seat, seat.fetch_state())

    row = advice_rows(game)[0]
    assert row["detail"]["drafted_at_seq"] == before_capture


def test_an_ordinary_proposal_carries_no_drafted_at_seq(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)

    capture_a_pass_draft(seat, game)

    assert advice_rows(game)[0]["detail"].get("drafted_at_seq") is None


# ---------------------------------------------------------------------------
# 5. wired into mcp._tool_result: posted within one poll, wake_to_draft
# ---------------------------------------------------------------------------


def test_a_staged_draft_posts_within_one_poll_of_the_human_holding_priority(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A buddy advising bob (not the active player at game start, so its
    first wait never trivially wakes) stages a draft, then the human's
    priority arrives *while `wait_for_priority` is still open* - the same
    threaded-mutation idiom `test_action_flow.py`'s own poll test uses."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    wire(monkeypatch)
    uid = advisor_uid(game, player="bob")
    # One `Seats` for both the direct staging below and the MCP server - two
    # separate `Seats()` instances would not share `advice_drafts` at all
    # (`mcp_server(game)` mints its own), and the draft would never be visible
    # to the very call this test is exercising.
    seats = seats_for(game)
    server = build_server(seats)
    seat = seats.seat_for(uid)
    assert seat is not None
    seat.stage_advice("declare_attackers", "mine")
    capture_a_pass_draft(seat, game, reasoning="pass until my attack step")

    def hand_bob_priority() -> None:
        time.sleep(0.2)
        force_turn_position(game, Step.DECLARE_ATTACKERS, "bob")

    threading.Thread(target=hand_bob_priority, daemon=True).start()

    started = time.monotonic()
    raw = asyncio.run(
        server.call_tool("wait_for_priority", {"player_uid": uid, "max_seconds": 2.0})
    )
    elapsed = time.monotonic() - started
    answer = json.loads(raw.content[0].text)

    assert answer["draft"]["status"] == "posted", answer.get("draft")
    assert elapsed < 1.0, f"took {elapsed:.2f}s - should post within one poll, not the whole bound"
    rows = advice_rows(game)
    assert len(rows) == 1
    assert rows[0]["detail"]["player"] == "bob"


def test_wake_to_draft_is_refused_for_a_seat(client: TestClient, game: str, wired: None) -> None:
    uid = next(iter(store.seat_uids(game)))
    server = mcp_server(game)

    answer = tool_text(server, "wait_for_priority", {"player_uid": uid, "wake_to_draft": True})

    assert answer == seatkit.WAKE_TO_DRAFT_ADVISOR_ONLY


def test_wake_to_draft_does_not_wake_while_staged_but_wakes_once_stale(
    client: TestClient, game: str, wired: None
) -> None:
    uid = advisor_uid(game, player="bob")
    seats = seats_for(game)
    server = build_server(seats)

    # `force_turn_position` grants priority to whichever player it names
    # active (CR 505.5: the active player receives priority first in a step),
    # so keeping alice active for this whole test - never bob - is what keeps
    # `priority_holder != bob` true throughout: this is a buddy watching its
    # human's *opponent's* turn ("opponent" from bob's own side), never
    # reaching a moment that could itself post.
    #
    # Step A: alice already active at game start; this seeds nothing and
    # answers NOTHING_NEW_YET on its own - the trivial "before is None reads
    # as changed" case does not apply, because the ordinary priority branch
    # requires priority_holder == player (bob) in the first place, which is
    # false throughout this whole test.
    idle = json.loads(tool_text(server, "wait_for_priority", {"player_uid": uid}))
    assert idle["wake_reason"] == seatkit.NOTHING_NEW_YET

    seat = seats.seat_for(uid)
    assert seat is not None
    seat.stage_advice("declare_blockers", "opponent")
    capture_a_pass_draft(seat, game, reasoning="watching for now")

    # Step C: still well before the window (beginning_of_combat < declare_blockers
    # in turn order), alice still active - the draft is staged, so wake_to_draft
    # must not fire even though the board plainly changed underneath it.
    force_turn_position(game, Step.BEGINNING_OF_COMBAT, "alice")
    held = json.loads(
        tool_text(server, "wait_for_priority", {"player_uid": uid, "wake_to_draft": True})
    )
    assert held["wake_reason"] == seatkit.NOTHING_NEW_YET, held["wake_reason"]
    assert held["draft"]["status"] == "held"

    # Step D: jump straight past declare_blockers - the window is now behind
    # the board. wake_to_draft is still gated off on THIS call (the draft was
    # still staged at the top of it), but the poll's own draft check catches
    # the staleness and drops it.
    force_turn_position(game, Step.POSTCOMBAT_MAIN, "alice")
    passing = json.loads(
        tool_text(server, "wait_for_priority", {"player_uid": uid, "wake_to_draft": True})
    )
    assert passing["wake_reason"] == seatkit.NOTHING_NEW_YET
    assert passing["draft"] == {"status": "stale", "failed": "window"}

    # Step E: nothing has staged since - wake_to_draft fires now that the
    # draft is gone, against the same still-unchanged-from-cache board.
    woken = json.loads(
        tool_text(server, "wait_for_priority", {"player_uid": uid, "wake_to_draft": True})
    )
    assert woken["wake_reason"] == seatkit.DRAFT_TIME_WAKE_REASON
    assert "draft" not in woken


# ---------------------------------------------------------------------------
# 5b. arming and a staged draft in the same answer silently superseding
# (2026-09-14 review, finding 4)
# ---------------------------------------------------------------------------


def test_an_armed_scripts_proposal_supersedes_a_reached_staged_draft(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A captured draft whose window is reached in the very same poll that
    also fires an armed script would otherwise double-post: `resolve_advice_
    draft` sees the window reached, the board unchanged and the action still
    legal, so it posts the draft right alongside the script's own `/advice`
    row - two proposals in one answer, the second of which
    `ui/advice_model.js`'s `currentAdvice` (newest-wins) then hides without a
    trace. The fix drops the draft instead and says so."""
    wire(monkeypatch)
    uid = advisor_uid(game, player="alice")
    register(client, game, uid, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")

    seats = seats_for(game)
    server = build_server(seats)
    seat = seats.seat_for(uid)
    assert seat is not None

    # Stage a window earlier this same turn, for a step that ranks after
    # where it is staged - "reached" the moment the board gets there, no
    # extra turn or occurrence bookkeeping needed for this test.
    force_turn_position(game, Step.UPKEEP, "alice")
    seat.stage_advice("precombat_main", "mine")
    capture_a_pass_draft(seat, game, reasoning="nothing to do until my main phase")

    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    answer = json.loads(
        tool_text(
            server,
            "wait_for_priority",
            {"player_uid": uid, "arm": "drop", "arm_reasoning": "make the land drop"},
        )
    )

    assert answer.get("armed") and answer["armed"][0]["exit"] == "advised"
    assert answer["draft"] == {"status": "superseded_by_script", "script": "drop"}
    rows = advice_rows(game)
    assert len(rows) == 1, rows
    assert rows[0]["detail"]["path"].endswith("/actions/play-land"), rows[0]["detail"]["path"]
    assert uid not in seat._advice_drafts, "a superseded draft must be dropped, not left staged"


# ---------------------------------------------------------------------------
# 5c. decide drafts check the pending decision's own kind and id, not merely
# whether ANY decision is pending (2026-09-14 review, finding 6)
# ---------------------------------------------------------------------------


def test_unvalidated_decide_legality_requires_kind_and_generation_to_match() -> None:
    """Pure unit test of `_unvalidated_action_still_legal`'s `decide` branch -
    the exact place the pre-fix code only asked `pending is not None and
    pending.player == player`, legal against ANY decision addressed to the
    player, not the one the draft actually answers."""
    action = {"action_key": "decide", "decision_kind": "choose_cards", "decision_generation": 3}

    matching = {"pending_decision": {"player": "alice", "kind": "choose_cards", "generation": 3}}
    assert seatkit._unvalidated_action_still_legal(action, matching, "alice") is True

    wrong_generation = {
        "pending_decision": {"player": "alice", "kind": "choose_cards", "generation": 4}
    }
    assert seatkit._unvalidated_action_still_legal(action, wrong_generation, "alice") is False

    wrong_kind = {"pending_decision": {"player": "alice", "kind": "may", "generation": 3}}
    assert seatkit._unvalidated_action_still_legal(action, wrong_kind, "alice") is False

    nothing_pending = {"pending_decision": None}
    assert seatkit._unvalidated_action_still_legal(action, nothing_pending, "alice") is False


def test_capturing_a_decide_draft_stores_the_real_pending_decisions_kind_and_generation(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)
    stuff_hand(client, game, "alice", 8)  # 7 max + 1 to discard
    paused = advance_into_cleanup(game)
    assert paused is not None, "the discard should have asked, not defaulted silently"
    alice_uid = next(uid for uid, name in store.seat_uids(game).items() if name == "alice")
    pending = state(client, game, alice_uid)["pending_decision"]
    assert pending["kind"] == "choose_cards"

    seat.stage_advice("end_step", "mine")
    drafted = seat.call_json(
        "POST",
        f"/api/games/{game}/decide",
        {
            "player": "alice",
            "card_ids": [pending["cards"][0]],
            "reasoning": "discard the excess",
        },
    )

    assert drafted["drafted"] is True
    action = seat._advice_drafts[ADVISOR_UID]["action"]
    assert action["decision_kind"] == "choose_cards"
    assert action["decision_generation"] == pending["generation"]


def _decide_window_and_state(
    game_id: str, *, decision_generation: int, pending_generation: int | None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """A hand-built `(window, action, state)` triple for a decide draft
    already reached, with a fully controlled `pending_decision` - isolates
    the legality check `resolve_advice_draft` makes for it without needing a
    second real decision replayed through the engine to go stale."""
    staged = {
        "step": "precombat_main",
        "active_player": "alice",
        "turn_number": 1,
        "turns_taken": 1,
    }
    window = seatkit.start_autopass(staged, "declare_attackers", "mine")
    window["stamped_step"] = staged["step"]
    window["stamped_turns_taken"] = staged["turns_taken"]
    now_state: dict[str, Any] = {
        "step": "declare_attackers",
        "active_player": "alice",
        "priority_holder": "alice",
        "turn_number": 1,
        "turns_taken": 1,
        "battlefield": [],
        "stack": [],
        "players": [{"username": "alice", "life": 20}, {"username": "bob", "life": 20}],
        "pending_decision": (
            None
            if pending_generation is None
            else {"player": "alice", "kind": "choose_cards", "generation": pending_generation}
        ),
    }
    action = {
        "method": "POST",
        "path": f"/api/games/{game_id}/decide",
        "body": {"player": "alice", "card_ids": []},
        "reasoning": "discard the excess",
        "validated": False,
        "action_key": "decide",
        "outcome": {"allowed": None},
        "decision_kind": "choose_cards",
        "decision_generation": decision_generation,
    }
    return window, action, now_state


def test_a_decide_draft_goes_stale_when_the_pending_decision_has_moved_on(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)
    window, action, now_state = _decide_window_and_state(
        game, decision_generation=3, pending_generation=4
    )
    seat._advice_drafts[ADVISOR_UID] = {
        "window": window,
        "action": action,
        "opponent_signature": seatkit.opponent_board_signature(now_state, "alice"),
        "drafted_at_seq": 0,
    }

    result = seatkit.resolve_advice_draft(seat, now_state)

    assert result == {"status": "stale", "failed": "legality"}
    assert advice_rows(game) == []
    assert ADVISOR_UID not in seat._advice_drafts


def test_a_decide_draft_still_posts_while_the_same_question_stands(
    client: TestClient, game: str, wired: None
) -> None:
    seat = buddy(game)
    window, action, now_state = _decide_window_and_state(
        game, decision_generation=3, pending_generation=3
    )
    seat._advice_drafts[ADVISOR_UID] = {
        "window": window,
        "action": action,
        "opponent_signature": seatkit.opponent_board_signature(now_state, "alice"),
        "drafted_at_seq": 0,
    }

    result = seatkit.resolve_advice_draft(seat, now_state)

    assert result["status"] == "posted"
    assert len(advice_rows(game)) == 1


# ---------------------------------------------------------------------------
# 5d. a per-uid lock: no double post, a restage during posting survives
# (2026-09-14 review, finding 5)
# ---------------------------------------------------------------------------


def _slow_advice_post(delay: float) -> Any:
    """Patches `Seat._request_json` so the `POST .../advice` call specifically
    takes `delay` seconds - long enough that a second, concurrent `resolve_
    advice_draft` call is guaranteed to find the lock still held. Every other
    request (`fetch_state`, the dry-run probe, ...) is untouched."""
    original = seatkit.Seat._request_json

    def slow(self: Seat, method: str, path: str, body: Any) -> Any:
        if method == "POST" and path.endswith("/advice"):
            time.sleep(delay)
        return original(self, method, path, body)

    return slow


def test_two_concurrent_resolves_produce_exactly_one_post(
    client: TestClient, game: str, wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")
    capture_a_pass_draft(seat, game)
    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")
    state_now = seat.fetch_state()
    monkeypatch.setattr(seatkit.Seat, "_request_json", _slow_advice_post(0.15))

    results: list[Any] = []
    results_lock = threading.Lock()

    def resolve() -> None:
        result = seatkit.resolve_advice_draft(seat, state_now)
        with results_lock:
            results.append(result)

    first = threading.Thread(target=resolve)
    second = threading.Thread(target=resolve)
    first.start()
    time.sleep(0.03)  # let `first` claim the lock and reach the slow POST
    second.start()
    first.join(5)
    second.join(5)

    posted = [r for r in results if r.get("status") == "posted"]
    held = [r for r in results if r.get("status") == "held"]
    assert len(posted) == 1, results
    assert len(held) == 1, results
    assert len(advice_rows(game)) == 1


def test_a_restage_during_posting_survives(
    client: TestClient, game: str, wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unconditional pop the old code ran right after its own POST would
    remove a NEW draft staged in the meantime just as readily as its own -
    `resolve_advice_draft` must only ever drop the entry it itself claimed."""
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")
    capture_a_pass_draft(seat, game)
    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")
    state_now = seat.fetch_state()
    monkeypatch.setattr(seatkit.Seat, "_request_json", _slow_advice_post(0.2))

    outcome: dict[str, Any] = {}

    def resolve() -> None:
        outcome["result"] = seatkit.resolve_advice_draft(seat, state_now)

    thread = threading.Thread(target=resolve)
    thread.start()
    time.sleep(0.05)  # `resolve` is now inside the slow POST, lock held
    seat.stage_advice("end_step", "mine")
    capture_a_pass_draft(seat, game, reasoning="restaged while the first draft posts")
    thread.join(5)

    assert outcome["result"]["status"] == "posted"
    assert len(advice_rows(game)) == 1
    survived = seat._advice_drafts.get(ADVISOR_UID)
    assert survived is not None, "the restage must survive the first draft's own cleanup"
    assert survived["window"]["step"] == "end_step"


# ---------------------------------------------------------------------------
# 6. exceptions never break the underlying tool answer
# ---------------------------------------------------------------------------


def test_an_exception_resolving_a_draft_never_breaks_the_answer(
    client: TestClient,
    game: str,
    wired: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    uid = advisor_uid(game)
    server = mcp_server(game)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(seatkit, "resolve_advice_draft", boom)

    with caplog.at_level(logging.WARNING, logger="mcm.mcp"):
        raw = asyncio.run(server.call_tool("wait_for_priority", {"player_uid": uid}))
    answer = json.loads(raw.content[0].text)

    assert "draft" not in answer
    assert answer.get("players"), "the underlying answer must still be whole"
    assert sum("staged advice draft raised" in r.message for r in caplog.records) == 1


# ---------------------------------------------------------------------------
# 7. CLI parity
# ---------------------------------------------------------------------------


def test_the_cli_stage_advice_command_matches_the_mcp_tool(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    wire(monkeypatch)
    uid = advisor_uid(game)

    assert (
        seat_cli.run(
            ["--uid", uid, "--game", game, "stage-advice", "end_step", "--whose-turn", "mine"]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)

    assert printed == {"staged": True, "window": {"step": "end_step", "whose_turn": "mine"}}


def test_the_cli_stage_advice_command_is_refused_for_a_seat(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    wire(monkeypatch)
    uid = next(iter(store.seat_uids(game)))

    assert seat_cli.run(["--uid", uid, "--game", game, "stage-advice", "end_step"]) == 0
    printed = json.loads(capsys.readouterr().out)

    assert printed == {"staged": False, "reason": seatkit.STAGE_ADVICE_ADVISOR_ONLY}


def test_the_cli_persists_a_captured_draft_across_processes_holds_then_posts_it(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """S2 A6 review fix 2: each `seat_cli.run()` call is a fresh process with
    a fresh `Seats()`, so a window `stage-advice` armed in one process used
    to vanish the instant that process exited - the next CLI committing call
    posted immediately instead of holding, and CLI `wait` never resolved a
    draft at all. Four separate `seat_cli.run()` calls below, each its own
    `Seats()` instance - a real subprocess boundary in production, exercised
    in-process here the same way `test_the_cli_stage_advice_command_matches_
    the_mcp_tool` already does."""
    wire(monkeypatch)
    uid = advisor_uid(game)

    # Process 1: stage the window.
    assert seat_cli.run(["--uid", uid, "--game", game, "stage-advice", "declare_attackers"]) == 0
    staged = json.loads(capsys.readouterr().out)
    assert staged["staged"] is True

    # Process 2 (a fresh Seats()): the next committing call is CAPTURED, not
    # posted - proof the staged window survived the first process exiting.
    assert seat_cli.run(["--uid", uid, "--game", game, "--reasoning", "nothing yet", "pass"]) == 0
    drafted = json.loads(capsys.readouterr().out)
    assert drafted["outcome"].get("drafted") is True
    assert advice_rows(game) == []

    # Process 3: still well before the window - held, still staged on disk.
    assert seat_cli.run(["--uid", uid, "--game", game, "wait"]) == 0
    held = json.loads(capsys.readouterr().out)
    assert held["draft"] == {"status": "held"}
    assert advice_rows(game) == []

    # Process 4: the board is at the window now - `wait` resolves it and posts.
    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")
    assert seat_cli.run(["--uid", uid, "--game", game, "wait"]) == 0
    posted = json.loads(capsys.readouterr().out)
    assert posted["draft"]["status"] == "posted"
    rows = advice_rows(game)
    assert len(rows) == 1
    assert rows[0]["detail"]["path"].endswith("/actions/pass-priority")


# ---------------------------------------------------------------------------
# S2 A8: `wait --wake-to-draft` - CLI parity for the MCP `wake_to_draft` cue,
# so a buddy following the CLI brief can wait ahead of time the same way an
# MCP buddy does (`test_wake_to_draft_does_not_wake_while_staged_but_wakes_
# once_stale` is the MCP-side version of this).
# ---------------------------------------------------------------------------


def test_the_cli_wait_command_supports_wake_to_draft(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    wire(monkeypatch)
    uid = advisor_uid(game, player="bob")

    # alice stays active throughout, so priority_holder is never bob's own -
    # an ordinary bare `wait` sees nothing new even once the board moves.
    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    assert seat_cli.run(["--uid", uid, "--game", game, "wait"]) == 0
    idle = json.loads(capsys.readouterr().out)
    assert idle["wake_reason"] == seatkit.NOTHING_NEW_YET

    force_turn_position(game, Step.BEGINNING_OF_COMBAT, "alice")
    assert seat_cli.run(["--uid", uid, "--game", game, "wait"]) == 0
    still_idle = json.loads(capsys.readouterr().out)
    assert still_idle["wake_reason"] == seatkit.NOTHING_NEW_YET, (
        "a bare `wait` must not wake to draft on its own"
    )

    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")
    assert seat_cli.run(["--uid", uid, "--game", game, "wait", "--wake-to-draft"]) == 0
    woken = json.loads(capsys.readouterr().out)
    assert woken["wake_reason"] == seatkit.DRAFT_TIME_WAKE_REASON


def test_the_cli_wait_wake_to_draft_is_refused_for_a_seat(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    wire(monkeypatch)
    uid = next(iter(store.seat_uids(game)))

    assert seat_cli.run(["--uid", uid, "--game", game, "wait", "--wake-to-draft"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == seatkit.WAKE_TO_DRAFT_ADVISOR_ONLY


# ---------------------------------------------------------------------------
# 7. re-review, 2026-09-14, finding 1: an OS file lock and a token-guarded
# delete, so overlapping CLI processes never lose or double-post a draft
# ---------------------------------------------------------------------------


def test_save_cli_draft_never_deletes_a_draft_it_never_saw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit-level: the mechanism review fix 1 adds to `_save_cli_draft` - a
    `None` write (nothing staged from THIS call's own point of view) only
    ever deletes the file when it still carries the token this call itself
    read. A call that loaded nothing at all (`expected_token=None`, exactly
    what a background `wait` starting from an empty drafts directory sees)
    must never delete a draft a different call wrote in the meantime."""
    monkeypatch.setenv(seat_cli.CLI_DRAFTS_DIR_ENV, str(tmp_path))
    game_id, uid = "game-1", "uid-1"

    # Nothing staged - a background `wait`'s own load sees exactly this.
    loaded, note = seat_cli._load_cli_draft(game_id, uid)
    assert loaded is None and note is None

    # A foreground `stage-advice`, in a completely separate call, stages a
    # window in the meantime.
    trouble = seat_cli._save_cli_draft(
        game_id, uid, {"window": {"step": "end_step"}, "action": None}
    )
    assert trouble is None

    # The background `wait` finishes and persists "nothing changed" from ITS
    # own point of view - `expected_token=None`, exactly what its own load
    # above saw.
    trouble = seat_cli._save_cli_draft(game_id, uid, None, expected_token=None)
    assert trouble is None

    survived, _note = seat_cli._load_cli_draft(game_id, uid)
    assert survived is not None, "a call that loaded nothing must never delete a draft staged since"
    assert survived["window"]["step"] == "end_step"


def test_a_background_wait_racing_a_foreground_stage_advice_never_loses_the_stage(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end, two real `seat_cli.run()` calls (each its own `Seats()` -
    the same simulated-process boundary every other CLI test in this file
    uses) racing on the file lock. Whichever gets there first, the staged
    window must survive both - review fix 1's own cross-process claim."""
    wire(monkeypatch)
    uid = advisor_uid(game)

    entered = threading.Event()
    release = threading.Event()
    original_load = seat_cli._load_cli_draft

    def slow_load(game_id: str, seat_uid: str) -> Any:
        result = original_load(game_id, seat_uid)
        if seat_uid == uid:
            entered.set()
            release.wait(5)
        return result

    monkeypatch.setattr(seat_cli, "_load_cli_draft", slow_load)

    results: dict[str, int] = {}

    def run_wait() -> None:
        results["wait"] = seat_cli.run(["--uid", uid, "--game", game, "wait", "--max-seconds", "0"])

    wait_thread = threading.Thread(target=run_wait)
    wait_thread.start()
    assert entered.wait(5), "the background wait never reached its own load"

    # The background `wait` is now stuck holding the file lock, inside its
    # own load. A foreground `stage-advice` racing it must simply wait its
    # turn on the same lock rather than run concurrently against it.
    def run_stage() -> None:
        results["stage"] = seat_cli.run(
            ["--uid", uid, "--game", game, "stage-advice", "declare_attackers"]
        )

    stage_thread = threading.Thread(target=run_stage)
    stage_thread.start()
    time.sleep(0.05)
    release.set()
    wait_thread.join(5)
    stage_thread.join(5)

    assert results["wait"] == 0
    assert results["stage"] == 0
    persisted, note = seat_cli._load_cli_draft(game, uid)
    assert persisted is not None, "the foreground stage must survive the background wait"
    assert persisted["window"]["step"] == "declare_attackers"


def test_two_cli_processes_racing_to_resolve_the_same_ready_draft_post_exactly_once(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-process version of `test_two_concurrent_resolves_produce_exactly_
    one_post`: two separate `seat_cli.run()` calls (their own `Seats()` each)
    both finding the SAME ready, staged draft on disk. Without review fix 1's
    file lock, both used to probe it legal and both `POST .../advice` - two
    ledger rows for the one proposal a human approves once."""
    wire(monkeypatch)
    uid = advisor_uid(game)

    assert seat_cli.run(["--uid", uid, "--game", game, "stage-advice", "declare_attackers"]) == 0
    assert seat_cli.run(["--uid", uid, "--game", game, "--reasoning", "nothing yet", "pass"]) == 0
    force_turn_position(game, Step.DECLARE_ATTACKERS, "alice")

    results: list[int] = []
    results_lock = threading.Lock()

    def run_wait() -> None:
        code = seat_cli.run(["--uid", uid, "--game", game, "wait", "--max-seconds", "0"])
        with results_lock:
            results.append(code)

    threads = [threading.Thread(target=run_wait) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert results == [0, 0]
    assert len(advice_rows(game)) == 1


# ---------------------------------------------------------------------------
# 8. re-review, 2026-09-14, finding 3: a broken drafts directory never hides
# a command's own answer
# ---------------------------------------------------------------------------


def test_an_unwritable_cli_drafts_dir_still_prints_the_state(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """`CLI_DRAFTS_DIR_ENV` pointed at a plain file (not a directory at all)
    used to crash `_cli_drafts_dir`'s own `mkdir` on the very first advisor
    command - before the command's own action ever ran, on every single
    advisor CLI command. `state` alone still exercises the load-at-`_
    dispatch`-start and the lock-acquisition-at-`run` codepaths."""
    wire(monkeypatch)
    uid = advisor_uid(game)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("nope")
    monkeypatch.setenv(seat_cli.CLI_DRAFTS_DIR_ENV, str(blocked))

    assert seat_cli.run(["--uid", uid, "--game", game, "state"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "players" in out


def test_an_unwritable_cli_drafts_dir_reports_a_draft_note_in_the_answer(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    """A `_save_cli_draft` failure (here, a directory made read-only after a
    window was staged in it) is caught, never raised, and the command still
    prints its own action result - `emit()`'s old persist-before-print
    ordering used to let this exception escape uncaught, losing the action's
    own answer with it. The failure is also folded into the printed JSON's
    own `draft.note`, never merely swallowed without a trace. A real WRITE
    has to be attempted for this - a directory with nothing staged in it has
    nothing to write and so nothing to fail on - so this stages a window
    while the directory is still writable, then makes it read-only before
    the committing call that has to persist the capture."""
    wire(monkeypatch)
    uid = advisor_uid(game)
    drafts_dir = tmp_path / "drafts"
    monkeypatch.setenv(seat_cli.CLI_DRAFTS_DIR_ENV, str(drafts_dir))
    assert seat_cli.run(["--uid", uid, "--game", game, "stage-advice", "end_step"]) == 0
    capsys.readouterr()
    os.chmod(drafts_dir, 0o500)
    try:
        assert (
            seat_cli.run(["--uid", uid, "--game", game, "--reasoning", "nothing to do", "pass"])
            == 0
        )
    finally:
        os.chmod(drafts_dir, 0o700)
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out.get("draft"), dict)
    assert isinstance(out["draft"].get("note"), str) and out["draft"]["note"]


# ---------------------------------------------------------------------------
# 9. re-review, 2026-09-14, finding 2: a script only supersedes a draft whose
# own window is reached in this very answer
# ---------------------------------------------------------------------------


def test_a_draft_for_another_window_stays_held_when_a_script_advises_elsewhere(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The original review fix 4 superseded ANY captured draft the instant a
    script advised anywhere in the same answer, even one staged for a window
    nowhere near reached yet. A draft for a LATER window must simply stay
    held while an armed script fires for its own, current moment."""
    wire(monkeypatch)
    uid = advisor_uid(game, player="alice")
    register(client, game, uid, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")

    seats = seats_for(game)
    server = build_server(seats)
    seat = seats.seat_for(uid)
    assert seat is not None

    # Stage a window well after the script's own precombat_main moment.
    force_turn_position(game, Step.UPKEEP, "alice")
    seat.stage_advice("declare_attackers", "mine")
    capture_a_pass_draft(seat, game, reasoning="nothing to do until attackers")

    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    answer = json.loads(
        tool_text(
            server,
            "wait_for_priority",
            {"player_uid": uid, "arm": "drop", "arm_reasoning": "make the land drop"},
        )
    )

    assert answer.get("armed") and answer["armed"][0]["exit"] == "advised"
    assert answer["draft"] == {"status": "held"}
    rows = advice_rows(game)
    assert len(rows) == 1, rows  # the script's own proposal, never the draft
    assert rows[0]["detail"]["path"].endswith("/actions/play-land")
    assert uid in seat._advice_drafts, "a draft for an unreached window must survive"


def test_a_manual_run_script_proposal_supersedes_a_reached_staged_draft(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of review fix 2's re-review: the original fix only
    ever checked `state["armed"]` - an ARMED script firing inside `wait_for_
    priority`'s own poll. A MANUAL `run_script` call (MCP tool, or CLI
    `script run`) that proposes in the very same answer a staged draft has
    just reached collides exactly the same way, and used to double-post."""
    wire(monkeypatch)
    uid = advisor_uid(game, player="alice")
    register(client, game, uid, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")

    seats = seats_for(game)
    server = build_server(seats)
    seat = seats.seat_for(uid)
    assert seat is not None

    force_turn_position(game, Step.UPKEEP, "alice")
    seat.stage_advice("precombat_main", "mine")
    capture_a_pass_draft(seat, game, reasoning="nothing to do until my main phase")

    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    answer = json.loads(
        tool_text(
            server,
            "run_script",
            {"player_uid": uid, "script": "drop", "reasoning": "make the land drop"},
        )
    )

    assert answer["script_exit"]["exit"] == "advised"
    assert answer["draft"] == {"status": "superseded_by_script", "script": "drop"}
    rows = advice_rows(game)
    assert len(rows) == 1, rows
    assert rows[0]["detail"]["path"].endswith("/actions/play-land")
    assert uid not in seat._advice_drafts


def test_the_cli_script_run_supersedes_a_reached_staged_draft(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """CLI parity for the manual-run supersede fix: `seat_cli`'s own `script
    run` command threads its own `script_exit` through to `draft_status_for_
    answer` exactly as the MCP tool does."""
    wire(monkeypatch)
    uid = advisor_uid(game, player="alice")
    register(client, game, uid, land_script("drop"))
    add_card(client, game, "Island", "alice", "hand")

    force_turn_position(game, Step.UPKEEP, "alice")
    assert (
        seat_cli.run(
            [
                "--uid",
                uid,
                "--game",
                game,
                "stage-advice",
                "precombat_main",
                "--whose-turn",
                "mine",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert seat_cli.run(["--uid", uid, "--game", game, "--reasoning", "nothing yet", "pass"]) == 0
    drafted = json.loads(capsys.readouterr().out)
    assert drafted["outcome"].get("drafted") is True

    force_turn_position(game, Step.PRECOMBAT_MAIN, "alice")
    assert (
        seat_cli.run(
            [
                "--uid",
                uid,
                "--game",
                game,
                "--reasoning",
                "make the land drop",
                "script",
                "run",
                "drop",
            ]
        )
        == 0
    )
    ran = json.loads(capsys.readouterr().out)

    assert ran["script_exit"]["exit"] == "advised"
    assert ran["draft"] == {"status": "superseded_by_script", "script": "drop"}
    rows = advice_rows(game)
    assert len(rows) == 1, rows
    assert rows[0]["detail"]["path"].endswith("/actions/play-land")


# ---------------------------------------------------------------------------
# 10. re-review, 2026-09-14, finding 4: a window staged before the game has
# a turn must refuse, and a `None` stamp must never be immortal either way
# ---------------------------------------------------------------------------


def test_stage_advice_refuses_before_the_game_has_a_turn(
    client: TestClient, game: str, wired: None
) -> None:
    """`state["turns_taken"]` is `None` whenever `game.turn` itself is `None`
    (`serialize.py`'s own fallback) - genuinely reachable before CR 103.8's
    first turn exists at all, earlier even than CR 103's own mulligan/
    opening-hand phase (which already has a real `Turn` with `turns_taken ==
    0`, not the bug on its own). A window staged then could never go stale
    on its own - see `STAGE_ADVICE_PREGAME`'s own docstring - so this is
    refused outright instead."""
    seat = buddy(game)
    live = store.get(game)
    assert live is not None
    live.turn = None

    result = seat.stage_advice("end_step", "mine")

    assert result == {"staged": False, "reason": seatkit.STAGE_ADVICE_PREGAME}
    assert ADVISOR_UID not in seat._advice_drafts


def test_a_none_turns_taken_stamp_can_still_go_stale() -> None:
    """The defensive half: even if a window's own stamp somehow still
    carries a `None` `turns_taken` (belt and braces - `stage_advice` itself
    now refuses to ever produce one), `draft_window_status` must never treat
    that as "nothing to compare" and hold the draft forever. `None` is
    normalised to `0`, the same as a genuine turn 0."""
    staged = {"step": "upkeep", "active_player": "alice", "turn_number": None, "turns_taken": None}
    window = seatkit.start_autopass(staged, "end_step", "mine")
    window["stamped_step"] = staged["step"]
    window["stamped_turns_taken"] = None  # what the old code would have stamped

    now_state = {"step": "cleanup", "active_player": "alice", "turns_taken": 2}

    assert seatkit.draft_window_status(window, now_state, "alice") == "after"


# ---------------------------------------------------------------------------
# 11. re-review, 2026-09-14, finding 5: a draft capture installs atomically,
# under the per-uid lock, so a concurrent reader never sees it half-built
# ---------------------------------------------------------------------------


def test_a_concurrent_read_during_capture_never_sees_a_draft_without_drafted_at_seq(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old code set `armed["action"]` and only afterward made the
    `recent_log` HTTP call that fills in `drafted_at_seq`/`opponent_
    signature` - a window, however short, where a concurrent reader could
    see a draft that LOOKS captured (`action` is not `None`) but is still
    missing the fields `resolve_advice_draft` needs to post it. The fix
    builds the whole thing first and installs it atomically under the
    per-uid lock, so no reader can ever observe that half-state."""
    wire(monkeypatch)
    seat = buddy(game)
    seat.stage_advice("declare_attackers", "mine")

    release = threading.Event()
    original_recent_log = seatkit.Seat.recent_log

    def slow_recent_log(self: Seat, *a: Any, **k: Any) -> Any:
        release.wait(2)
        return original_recent_log(self, *a, **k)

    monkeypatch.setattr(seatkit.Seat, "recent_log", slow_recent_log)

    observed: list[dict[str, Any]] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            entry = seat._advice_drafts.get(ADVISOR_UID)
            if entry is not None and entry.get("action") is not None:
                observed.append(dict(entry))
            time.sleep(0.001)

    poller = threading.Thread(target=poll)
    poller.start()

    capturer = threading.Thread(target=lambda: capture_a_pass_draft(seat, game))
    capturer.start()
    time.sleep(0.1)  # let the capturer reach the slowed `recent_log` call
    release.set()
    capturer.join(5)
    stop.set()
    poller.join(5)

    assert all(entry.get("drafted_at_seq") is not None for entry in observed), observed
    final = seat._advice_drafts[ADVISOR_UID]
    assert final["action"] is not None
    assert final["drafted_at_seq"] is not None


# ---------------------------------------------------------------------------
# 12. re-review round 2, 2026-09-14, finding 3: the draft lock scopes to the
# read-modify-write sections, never a `wait`'s own poll
# ---------------------------------------------------------------------------


def test_a_background_wait_does_not_block_a_foreground_stage_advice(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old `_cli_draft_lock` was held around the whole `_dispatch()` call
    in `run()`, `wait` included - and `wait --max-seconds N` genuinely sleeps
    inside `block_until_wake`'s own poll loop for up to N seconds when
    nothing on the board changes. A background buddy's `mcp-seat wait
    --max-seconds 25` therefore held the same human's foreground `stage-
    advice` (or any other action command) for up to 25 seconds too, since
    both compete for the one lock file.

    Nothing on this board changes here, so the background `wait` really does
    poll for its whole `--max-seconds` window (no early wake) - and the
    foreground `stage-advice`, started well inside that window, must still
    return almost at once. Fails on the un-fixed code, where it takes close
    to the wait's own bound.

    `force_turn_position` puts bob, not alice, in precombat main holding
    priority: `compute_wake_reason` wakes an advisor at once whenever its own
    player already holds priority and `before` is `None` (a fresh CLI
    process's own `wait`, every time), which alice's own priority would do
    here regardless of whether the lock is scoped correctly - the whole
    point of this setup is a `wait` with nothing to wake it early, so it
    genuinely sits in `block_until_wake`'s poll for the full bound."""
    wire(monkeypatch)
    uid = advisor_uid(game)
    force_turn_position(game, Step.PRECOMBAT_MAIN, "bob")

    wait_entered = threading.Event()

    def run_wait() -> None:
        wait_entered.set()
        seat_cli.run(["--uid", uid, "--game", game, "wait", "--max-seconds", "3"])

    wait_thread = threading.Thread(target=run_wait, daemon=True)
    wait_thread.start()
    assert wait_entered.wait(5), "the background wait never started"
    time.sleep(0.3)  # let it reach its own first poll, well inside the 3s bound

    start = time.monotonic()
    assert seat_cli.run(["--uid", uid, "--game", game, "stage-advice", "declare_attackers"]) == 0
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, (
        f"a foreground stage-advice took {elapsed:.2f}s while a background wait was "
        "still polling - the CLI draft lock is still held across the wait"
    )
    wait_thread.join(5)
