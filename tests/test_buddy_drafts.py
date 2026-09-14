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
import threading
import time
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
