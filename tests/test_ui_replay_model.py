"""`ui/replay_model.js` - the pure replay model behind the "Replay" door that
succeeds the old "Show thinking" scroll page.

No DOM, no timers, no `fetch`: everything here is a function over `GET
/api/games/{id}/review`'s own JSON (`review_model.js`'s seam,
`tests/test_ui_review_model.py`'s pattern) plus, for the transport, an
injected clock rather than a real one.

Four things this module has to get right, and each has its own section
below:

1. `replaySteps` - which ledger rows are "a decision point" (a step) and
   which are not. A step is a real seat action (or a refusal, whatever its
   action) with a snapshot; a routine `advance`/`pass_priority` row folds
   into the *next* step rather than becoming its own; `plan_declared`,
   advice, reflection, and the rest are never steps at all.
2. `logMessages`/`messagesAt` - the thinking log: one message per row worth
   showing, aligned to steps by `seq`, monotonic as the transport advances,
   and - because the reflection and AI reviews are written *after*
   `game_over` - fully shown once the last step is reached rather than
   trailing off just past the board's final frame.
3. `snapshotView`/`omniscient` - a ledger row's snapshot turned into the
   exact shape `board_layout.js` and `render/board.js`/`render/panels.js`
   already read, so the replay draws with the live board's own renderers.
4. The transport state machine - pure reducers over `{index, playing,
   lastAt}`, driven by an explicit `now` rather than a timer, so the
   5-second autoplay cadence is asserted with arithmetic instead of a sleep.

One test (`test_a_real_played_game_...`) builds its fixture from the real
HTTP route rather than a hand-shaped ledger row, per the MEMORY lesson dated
2026-09-13: a hand-shaped ledger row has hidden the `reasoning`-is-top-level-
not-`detail`-level shape bug before (`test_ui_advice.py`'s own regression
test for exactly that). Every other test here hand-builds its fixture, the
same way `test_ui_review_model.py` does - this repo's `magic-cards-model-api`
is tested by ordinary practice, fakes included; the "no mocks" rule is
`magic-cards-model`'s alone.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from test_review import _agent_game, _declare, _review
from ui_node import UI, requires_node, run_module

pytestmark = requires_node

MODULE = "replay_model.js"
REVIEW_MODEL = str(UI / "review_model.js")
BOARD_LAYOUT = str(UI / "board_layout.js")


def call(expression: str, **bindings: Any) -> Any:
    return run_module(MODULE, expression, **bindings)


# --------------------------------------------------------------- fixtures --


def snapshot(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "life": {"alice": 20, "bob": 17},
        "pools": {"alice": {}, "bob": {}},
        "battlefield": ["alice: Mountain (tapped)", "bob: Grizzly Bears [2/2]"],
        "hands": {"alice": ["Lightning Bolt"], "bob": ["Forest", "Grizzly Bears"]},
        "stack": [],
        "graveyards": {"alice": [], "bob": []},
        "exile": [],
        "combat": [],
    }
    base.update(over)
    return base


def entry(seq: int, action: str, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "seq": seq,
        "ts": f"2026-09-08T20:{seq:02d}:00+00:00",
        "action": action,
        "turn": 1,
        "step": "precombat_main",
        "phase": "precombat_main",
        "stack_size": 0,
        "detail": {},
        "reasoning": None,
        "snapshot": snapshot(),
        "active_player": "bob",
        "priority_holder": "bob",
    }
    row.update(over)
    return row


def meta_entry(seq: int, action: str, **over: Any) -> dict[str, Any]:
    """A loud/meta row, as `audit.record` actually writes one: no `snapshot`
    at all is the common case (`plan_declared`, `advice`, ...), so this
    defaults it away rather than reusing `entry`'s."""
    row = entry(seq, action, **over)
    row["snapshot"] = None
    return row


def review(entries: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "game_id": "g1",
        "players": ["alice", "bob"],
        "created_at": "2026-09-08T20:00:00+00:00",
        "ended_at": None,
        "is_over": True,
        "winner": "bob",
        "drawn": False,
        "ended_seq": entries[-1]["seq"] if entries else None,
        "declarations": [],
        "advice": [],
        "entries": entries,
    }
    base.update(over)
    return base


# =========================================================== replaySteps ==


def test_a_real_seat_action_with_a_snapshot_is_a_step() -> None:
    r = review([entry(1, "play_land", detail={"player": "bob", "card": "Forest"})])
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    assert [s["seq"] for s in steps] == [1]
    assert steps[0]["actor"] == "bob"
    assert steps[0]["refused"] is False
    assert steps[0]["folded"] == []


def test_meta_rows_never_become_steps() -> None:
    entries = [
        meta_entry(1, "plan_declared", detail={"player": "bob", "plan": "attack"}),
        meta_entry(2, "advice", detail={"player": "alice", "summary": "play a land"}),
        meta_entry(3, "advice_approved", detail={"advice_seq": 2}),
        meta_entry(4, "game_reflection", detail={"player": "bob", "text": "it went fine"}),
        meta_entry(5, "ai_review", detail={"player": "bob", "text": "solid game"}),
        meta_entry(6, "ai_review_failed", detail={"player": "bob", "reason": "claude timed out"}),
        meta_entry(7, "script_run_started", detail={"seat": "bob", "script_id": "s1"}),
        meta_entry(8, "script_run", detail={"seat": "bob", "script_id": "s1"}),
        meta_entry(
            9, "report_problem", detail={"player": "alice", "text": "the board looks wrong"}
        ),
        meta_entry(
            10, "pregame_overruled", detail={"player": "bob", "text": "the backstop already moved"}
        ),
        entry(11, "create_game", snapshot=None, detail={}),
        entry(12, "play_land", detail={"player": "bob", "card": "Forest"}),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    assert [s["seq"] for s in steps] == [12], steps


def test_a_refused_row_is_always_a_step_whatever_its_action() -> None:
    entries = [
        entry(1, "advance"),
        entry(
            2,
            "play_land",
            detail={
                "player": "bob",
                "card": "Mountain",
                "allowed": False,
                "reason": "already played a land",
            },
        ),
        entry(3, "advance"),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    # the refusal is a step even though nothing legal happened, and the
    # `advance` before it folds into it; the trailing `advance` after it has
    # no later step to fold into, so it becomes its own final step.
    assert [s["seq"] for s in steps] == [2, 3]
    assert steps[0]["refused"] is True
    assert steps[0]["folded"] == [1]
    assert steps[1]["refused"] is False
    assert steps[1]["folded"] == []


def test_consecutive_routine_rows_fold_into_the_next_step() -> None:
    entries = [
        entry(1, "advance"),
        entry(2, "pass_priority", detail={"player": "alice"}),
        entry(3, "advance"),
        entry(4, "cast", detail={"player": "bob", "card": "Lightning Bolt"}),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    assert [s["seq"] for s in steps] == [4]
    assert steps[0]["folded"] == [1, 2, 3]


def test_trailing_routine_rows_become_one_final_step_at_the_last_of_them() -> None:
    entries = [
        entry(1, "cast", detail={"player": "bob", "card": "Lightning Bolt"}),
        entry(2, "advance"),
        entry(3, "advance"),
        entry(4, "advance"),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    assert [s["seq"] for s in steps] == [1, 4]
    assert steps[1]["folded"] == [2, 3]
    assert steps[1]["refused"] is False


def test_game_over_is_a_step_the_final_board() -> None:
    entries = [
        entry(1, "cast", detail={"player": "bob", "card": "Lightning Bolt"}),
        entry(2, "advance"),
        entry(3, "game_over", detail={"winner": "bob", "drawn": False, "note": "bob wins"}),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    assert [s["seq"] for s in steps] == [1, 3]
    assert steps[1]["folded"] == [2]


def test_a_lone_routine_row_between_two_steps_folds_into_the_second() -> None:
    entries = [
        entry(1, "cast", detail={"player": "bob", "card": "Lightning Bolt"}),
        entry(2, "advance"),
        entry(3, "play_land", detail={"player": "bob", "card": "Forest"}),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    assert [s["seq"] for s in steps] == [1, 3]
    assert steps[1]["folded"] == [2]


# `run_module`'s own generated script already does
# `import { pathToFileURL } from "node:url"` before `eval`-ing the
# expression, and the expression is a *direct* eval (so it inherits that
# module scope) - `pathToFileURL` is already in scope here with no preamble
# of this file's own, the same way `test_ui_review_model.py`'s
# `_board_layout_of` cross-checks `board_layout.js`.


def call_with_review_model(expression: str, **bindings: Any) -> Any:
    return run_module(MODULE, expression, **bindings)


def test_label_reuses_review_models_frame_label() -> None:
    e = entry(1, "cast", detail={"player": "bob", "card": "Lightning Bolt", "target": "alice"})
    r = review([e])
    result = call_with_review_model(
        """
        (async () => {
          const rm = await import(pathToFileURL(REVIEW_MODEL).href);
          const steps = m.replaySteps(REVIEW);
          return { got: steps[0].label, want: rm.frameLabel(REVIEW.entries[0]) };
        })()
        """,
        REVIEW=r,
        REVIEW_MODEL=REVIEW_MODEL,
    )
    assert result["got"] == result["want"]
    assert result["got"], "the fixture's own label should not be empty"


# ============================================================ logMessages ==


def test_each_step_becomes_an_action_message_carrying_its_top_level_reasoning() -> None:
    e = entry(
        1,
        "play_land",
        detail={"player": "bob", "card": "Forest"},
        reasoning="a fourth land keeps the curve on track",
    )
    r = review([e])
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    assert len(messages) == 1
    assert messages[0]["kind"] == "action"
    assert messages[0]["seq"] == 1
    assert messages[0]["actor"] == "bob"
    assert "a fourth land keeps the curve on track" in messages[0]["text"]
    assert messages[0]["stepIndex"] == 0


def test_a_refused_steps_message_carries_the_engines_refusal_text_verbatim() -> None:
    e = entry(
        1,
        "play_land",
        detail={
            "player": "bob",
            "card": "Mountain",
            "allowed": False,
            "reason": "you have already played a land this turn",
        },
        reasoning="taking the advice anyway",
    )
    r = review([e])
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    assert len(messages) == 1
    assert messages[0]["kind"] == "refused"
    assert "you have already played a land this turn" in messages[0]["text"]
    assert "taking the advice anyway" in messages[0]["text"]


def test_a_routine_row_with_reasoning_becomes_a_compact_pass_message() -> None:
    entries = [
        entry(
            1,
            "pass_priority",
            detail={"player": "alice"},
            reasoning="nothing to do, holding up mana",
        ),
        entry(2, "play_land", detail={"player": "bob", "card": "Forest"}),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    passes = [m for m in messages if m["kind"] == "pass"]
    assert len(passes) == 1
    assert passes[0]["seq"] == 1
    assert "nothing to do, holding up mana" in passes[0]["text"]


def test_a_routine_row_with_no_reasoning_produces_no_message_at_all() -> None:
    entries = [
        entry(1, "advance", reasoning=None),
        entry(2, "play_land", detail={"player": "bob", "card": "Forest"}),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    assert [m["seq"] for m in messages] == [2]


def test_plan_declared_becomes_a_message_carrying_the_four_fields() -> None:
    e = meta_entry(
        1,
        "plan_declared",
        detail={
            "player": "bob",
            "board_read": "she has one Mountain and no board",
            "my_role": "beatdown",
            "plan": "land, Bears, hold the Bolt",
            "prediction": {
                "expecting": "she takes it",
                "because": "no blockers",
                "confidence": "medium",
            },
        },
    )
    entries = [e, entry(2, "play_land", detail={"player": "bob", "card": "Forest"})]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    plans = [m for m in messages if m["kind"] == "plan"]
    assert len(plans) == 1
    text = plans[0]["text"]
    for expected in (
        "she has one Mountain and no board",
        "beatdown",
        "land, Bears, hold the Bolt",
        "she takes it",
    ):
        assert expected in text, text
    assert plans[0]["actor"] == "bob"


def test_advice_and_advice_approved_become_messages_read_the_way_advice_of_does() -> None:
    advice_row = meta_entry(
        1,
        "advice",
        detail={
            "player": "alice",
            "summary": "play the Forest",
            "based_on_seq": 0,
            "validated": True,
        },
        reasoning="a fourth land keeps the curve on track",
    )
    approved_row = meta_entry(2, "advice_approved", detail={"advice_seq": 1})
    played = entry(3, "play_land", detail={"player": "alice", "card": "Forest"})
    r = review([advice_row, approved_row, played])
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    advice_msgs = [m for m in messages if m["kind"] == "advice"]
    approved_msgs = [m for m in messages if m["kind"] == "advice_approved"]
    assert len(advice_msgs) == 1
    assert "play the Forest" in advice_msgs[0]["text"]
    assert "a fourth land keeps the curve on track" in advice_msgs[0]["text"]
    assert len(approved_msgs) == 1
    assert approved_msgs[0]["seq"] == 2


def test_reflection_ai_review_and_related_rows_all_become_messages() -> None:
    entries = [
        entry(1, "play_land", detail={"player": "bob", "card": "Forest"}),
        entry(2, "game_over", detail={"winner": "bob", "drawn": False, "note": "bob wins"}),
        meta_entry(3, "game_reflection", detail={"player": "bob", "text": "that felt close"}),
        meta_entry(
            4, "ai_review", detail={"player": "bob", "text": "strong sequencing throughout"}
        ),
        meta_entry(
            5,
            "ai_review_failed",
            detail={"player": "alice", "stage": "generate", "reason": "claude cli was unreachable"},
        ),
        meta_entry(
            6,
            "report_problem",
            detail={"player": "alice", "text": "the board looked wrong at turn 3"},
        ),
        meta_entry(
            7,
            "pregame_overruled",
            detail={"player": "bob", "text": "the backstop already kept the hand for bob"},
        ),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    by_kind = {m["kind"]: m for m in messages}
    assert "bob wins" in by_kind["game_over"]["text"]
    assert "that felt close" in by_kind["reflection"]["text"]
    assert "strong sequencing throughout" in by_kind["ai_review"]["text"]
    assert "claude cli was unreachable" in by_kind["ai_review_failed"]["text"]
    assert "the board looked wrong at turn 3" in by_kind["report_problem"]["text"]
    assert "the backstop already kept the hand for bob" in by_kind["pregame_overruled"]["text"]


def test_script_run_rows_become_messages_when_present() -> None:
    entries = [
        entry(1, "play_land", detail={"player": "bob", "card": "Forest"}),
        meta_entry(
            2, "script_run", detail={"seat": "bob", "role": "seat", "script_id": "opening-1"}
        ),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    script_msgs = [m for m in messages if m["kind"] == "script_run"]
    assert len(script_msgs) == 1
    assert "opening-1" in script_msgs[0]["text"]


def test_messages_are_returned_in_seq_order() -> None:
    entries = [
        meta_entry(1, "plan_declared", detail={"player": "bob", "plan": "go wide"}),
        entry(2, "advance"),
        entry(3, "play_land", detail={"player": "bob", "card": "Forest"}, reasoning="land first"),
    ]
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    seqs = [m["seq"] for m in messages]
    assert seqs == sorted(seqs)
    assert seqs[0] == 1


# ============================================================= messagesAt ==


def _built(entries: list[dict[str, Any]]) -> tuple[dict[str, Any], list[Any], list[Any]]:
    r = review(entries)
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    return r, steps, messages


def test_messages_at_is_exact_at_the_first_boundary() -> None:
    entries = [
        meta_entry(1, "plan_declared", detail={"player": "bob", "plan": "go wide"}),
        entry(2, "play_land", detail={"player": "bob", "card": "Forest"}, reasoning="land first"),
        entry(
            3, "cast", detail={"player": "bob", "card": "Lightning Bolt"}, reasoning="burn the face"
        ),
    ]
    _, steps, messages = _built(entries)
    at0 = call("m.messagesAt(MESSAGES, STEPS, 0)", MESSAGES=messages, STEPS=steps)
    assert [m["seq"] for m in at0] == [1, 2]


def test_messages_at_is_monotonic_in_i() -> None:
    entries = [
        meta_entry(1, "plan_declared", detail={"player": "bob", "plan": "go wide"}),
        entry(2, "play_land", detail={"player": "bob", "card": "Forest"}, reasoning="land first"),
        entry(3, "advance"),
        entry(
            4, "cast", detail={"player": "bob", "card": "Lightning Bolt"}, reasoning="burn the face"
        ),
    ]
    _, steps, messages = _built(entries)
    counts = [
        len(call("m.messagesAt(MESSAGES, STEPS, I)", MESSAGES=messages, STEPS=steps, I=i))
        for i in range(len(steps))
    ]
    assert counts == sorted(counts)
    assert counts[0] >= 1


def test_messages_at_the_last_step_shows_everything_including_after_game_over() -> None:
    entries = [
        entry(1, "play_land", detail={"player": "bob", "card": "Forest"}, reasoning="land first"),
        entry(2, "game_over", detail={"winner": "bob", "drawn": False, "note": "bob wins"}),
        meta_entry(3, "game_reflection", detail={"player": "bob", "text": "that felt close"}),
        meta_entry(4, "ai_review", detail={"player": "bob", "text": "strong sequencing"}),
    ]
    _, steps, messages = _built(entries)
    last = call("m.messagesAt(MESSAGES, STEPS, STEPS.length - 1)", MESSAGES=messages, STEPS=steps)
    assert len(last) == len(messages)
    assert {m["kind"] for m in last} >= {"action", "game_over", "reflection", "ai_review"}


def test_messages_at_is_empty_for_an_empty_game() -> None:
    r = review([])
    steps = call("m.replaySteps(REVIEW)", REVIEW=r)
    messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=r, STEPS=steps)
    assert steps == []
    assert messages == []
    at0 = call("m.messagesAt(MESSAGES, STEPS, 0)", MESSAGES=messages, STEPS=steps)
    assert at0 == []


# ============================================================= perspective ==


def test_perspective_prefers_the_reviews_own_viewer() -> None:
    r = review([entry(1, "advance")], viewer="alice", declarations=[])
    assert call("m.perspective(REVIEW)", REVIEW=r) == "alice"


def test_perspective_falls_back_to_review_models_perspective_of() -> None:
    e = entry(1, "advance")
    r = review(
        [e],
        declarations=[
            {
                "seq": 1,
                "player": "bob",
                "turn": 1,
                "turns_taken": 1,
                "board_read": "x",
                "my_role": "y",
                "plan": "z",
                "prediction": {"expecting": "a", "because": "b", "confidence": "low"},
                "did": [],
                "opponent_did": [],
            }
        ],
    )
    result = call_with_review_model(
        """
        (async () => {
          const rm = await import(pathToFileURL(REVIEW_MODEL).href);
          return { got: m.perspective(REVIEW), want: rm.perspectiveOf(REVIEW) };
        })()
        """,
        REVIEW=r,
        REVIEW_MODEL=REVIEW_MODEL,
    )
    assert result["got"] == result["want"] == "bob"


# ============================================================= snapshotView ==

CATALOGUE = {
    "Mountain": {
        "type_line": "Basic Land - Mountain",
        "mana_cost": "",
        "image": None,
        "small_image": None,
    },
    "Grizzly Bears": {
        "type_line": "Creature - Bear",
        "mana_cost": "{1}{G}",
        "image": None,
        "small_image": None,
    },
    "Lightning Bolt": {
        "type_line": "Instant",
        "mana_cost": "{R}",
        "image": None,
        "small_image": None,
    },
    "Forest": {
        "type_line": "Basic Land - Forest",
        "mana_cost": "",
        "image": None,
        "small_image": None,
    },
}


def test_snapshot_view_is_null_without_a_snapshot() -> None:
    e = entry(1, "advance", snapshot=None)
    assert call("m.snapshotView(ENTRY, CAT, YOU)", ENTRY=e, CAT=CATALOGUE, YOU="alice") is None


def test_snapshot_view_carries_every_field_boardlayout_and_panels_read() -> None:
    e = entry(
        1,
        "advance",
        turn=3,
        step="declare_attackers",
        active_player="bob",
        priority_holder="alice",
    )
    view = call("m.snapshotView(ENTRY, CAT, YOU)", ENTRY=e, CAT=CATALOGUE, YOU="alice")
    assert view["you"] == "alice"
    assert view["turn_number"] == 3
    assert view["step"] == "declare_attackers"
    assert view["active_player"] == "bob"
    assert view["priority_holder"] == "alice"
    assert view["play_permissions"] == {}
    assert set(view["known_cards"].keys()) == {"alice"}
    assert view["known_cards"]["alice"] == {"hand": [], "library": [], "sideboard": []}
    usernames = sorted(p["username"] for p in view["players"])
    assert usernames == ["alice", "bob"]
    alice = next(p for p in view["players"] if p["username"] == "alice")
    assert alice["life"] == 20
    assert alice["hand_count"] == 1
    bob = next(p for p in view["players"] if p["username"] == "bob")
    assert bob["hand_count"] == 2


def test_snapshot_view_feeds_board_layout_correctly() -> None:
    e = entry(1, "advance")
    result = call_with_review_model(
        """
        (async () => {
          const bl = await import(pathToFileURL(BOARD_LAYOUT).href);
          const view = m.snapshotView(ENTRY, CAT, YOU);
          const layout = bl.boardLayout(view);
          const mine = bl.seatRows(layout, "alice");
          const theirs = bl.seatRows(layout, "bob");
          const ribbon = bl.zoneRibbon(view, layout);
          return {
            mineLands: mine.lands.map((e) => e.card.name),
            theirsMain: theirs.main.map((e) => e.card.name),
            ribbonKeys: ribbon.map((r) => r.key),
          };
        })()
        """,
        ENTRY=e,
        CAT=CATALOGUE,
        YOU="alice",
        BOARD_LAYOUT=BOARD_LAYOUT,
    )
    assert result["mineLands"] == ["Mountain"]
    assert result["theirsMain"] == ["Grizzly Bears"]
    assert set(result["ribbonKeys"]) == {
        "opp-command",
        "opp-graveyard",
        "opp-exile",
        "you-exile",
        "you-graveyard",
        "you-command",
    }


def test_snapshot_view_hides_the_opponents_hand_by_default() -> None:
    """The fallback view - before `omniscient` is applied - is what a live
    (redacted) view would look like: `known_cards[you]` carries nothing, so
    `opponentHandItems` (unchanged) draws only card backs."""
    e = entry(1, "advance")
    view = call("m.snapshotView(ENTRY, CAT, YOU)", ENTRY=e, CAT=CATALOGUE, YOU="alice")
    assert view["known_cards"]["alice"]["hand"] == []
    # the opponent's actual hand is still on the view (a review is
    # full-information at the top level) - it is only `known_cards` that is
    # empty, which is the field `opponentHandItems` reads.
    assert [c["name"] for c in view["hands"]["bob"]] == ["Forest", "Grizzly Bears"]


# ============================================================== omniscient ==


def test_omniscient_reveals_the_opponents_hand_face_up() -> None:
    e = entry(1, "advance")
    view = call("m.snapshotView(ENTRY, CAT, YOU)", ENTRY=e, CAT=CATALOGUE, YOU="alice")
    revealed = call("m.omniscient(VIEW, YOU)", VIEW=view, YOU="alice")
    hand = revealed["known_cards"]["alice"]["hand"]
    assert sorted(c["name"] for c in hand) == ["Forest", "Grizzly Bears"]
    assert all(c["owner"] == "bob" for c in hand)


def test_omniscient_forces_you_to_the_given_perspective() -> None:
    e = entry(1, "advance")
    view = call("m.snapshotView(ENTRY, CAT, YOU)", ENTRY=e, CAT=CATALOGUE, YOU="alice")
    flipped = call("m.omniscient(VIEW, YOU)", VIEW=view, YOU="bob")
    assert flipped["you"] == "bob"
    hand = flipped["known_cards"]["bob"]["hand"]
    assert sorted(c["name"] for c in hand) == ["Lightning Bolt"]
    assert all(c["owner"] == "alice" for c in hand)


def test_omniscient_leaves_opponent_hand_items_drawing_them_face_up() -> None:
    """`render/board.js`'s `opponentHandItems` is module-private (not
    exported) and untouched by this task, so it cannot be imported here -
    this mirrors its own filter line for line (`(knownByYou(state).hand ||
    []).filter((card) => card.owner === opp.username)`) over the exact view
    `omniscient` produces, proving the shape lines up with what it reads
    rather than re-testing `render/board.js` itself."""
    e = entry(1, "advance")
    view = call("m.snapshotView(ENTRY, CAT, YOU)", ENTRY=e, CAT=CATALOGUE, YOU="alice")
    revealed = call("m.omniscient(VIEW, YOU)", VIEW=view, YOU="alice")
    result = call(
        """
        (() => {
          const known = (VIEW.known_cards[VIEW.you] || { hand: [] }).hand;
          return known.filter((card) => card.owner === "bob").map((card) => card.name);
        })()
        """,
        VIEW=revealed,
    )
    assert sorted(result) == ["Forest", "Grizzly Bears"]


# ================================================================ transport ==


def test_create_transport_starts_at_zero_and_playing() -> None:
    t = call("m.createTransport({count: COUNT, now: NOW})", COUNT=5, NOW=1000)
    assert t == {"index": 0, "playing": True, "lastAt": 1000, "count": 5, "intervalMs": 5000}


def test_create_transport_with_zero_count_is_not_playing() -> None:
    t = call("m.createTransport({count: COUNT, now: NOW})", COUNT=0, NOW=1000)
    assert t["index"] == 0
    assert t["playing"] is False


def test_tick_does_not_advance_before_the_interval_elapses() -> None:
    t = call("m.createTransport({count: COUNT, now: NOW})", COUNT=3, NOW=0)
    for now in (100, 999, 4998, 4999):
        t = call("m.tick(T, NOW)", T=t, NOW=now)
    assert t["index"] == 0
    assert t["playing"] is True


def test_tick_advances_exactly_at_the_interval() -> None:
    t = call("m.createTransport({count: COUNT, now: NOW})", COUNT=3, NOW=0)
    t = call("m.tick(T, NOW)", T=t, NOW=5000)
    assert t["index"] == 1
    assert t["lastAt"] == 5000


def test_tick_stops_playing_at_the_last_index() -> None:
    t = call("m.createTransport({count: COUNT, now: NOW})", COUNT=2, NOW=0)
    t = call("m.tick(T, NOW)", T=t, NOW=5000)
    assert t["index"] == 1
    assert t["playing"] is True
    t = call("m.tick(T, NOW)", T=t, NOW=10000)
    assert t["index"] == 1
    assert t["playing"] is False


def test_pause_holds_the_index_across_ticks() -> None:
    t = call("m.createTransport({count: COUNT, now: NOW})", COUNT=5, NOW=0)
    t = call("m.toggle(T, NOW)", T=t, NOW=100)
    assert t["playing"] is False
    for now in (5100, 10100, 20100):
        t = call("m.tick(T, NOW)", T=t, NOW=now)
    assert t["index"] == 0
    assert t["playing"] is False


def test_forward_back_and_seek_clamp_to_bounds() -> None:
    t = call("m.createTransport({count: COUNT, now: NOW})", COUNT=3, NOW=0)
    t = call("m.seek(T, I, NOW)", T=t, I=2, NOW=1)
    assert t["index"] == 2
    t = call("m.forward(T, NOW)", T=t, NOW=2)
    assert t["index"] == 2, "clamped to count - 1"
    t = call("m.back(T, NOW)", T=t, NOW=3)
    assert t["index"] == 1
    t = call("m.seek(T, I, NOW)", T=t, I=-5, NOW=4)
    assert t["index"] == 0
    t = call("m.seek(T, I, NOW)", T=t, I=99, NOW=5)
    assert t["index"] == 2


def test_forward_back_seek_reset_last_at_and_keep_playing() -> None:
    t = call("m.createTransport({count: COUNT, now: NOW})", COUNT=3, NOW=0)
    t = call("m.toggle(T, NOW)", T=t, NOW=1)  # paused
    assert t["playing"] is False
    t = call("m.forward(T, NOW)", T=t, NOW=500)
    assert t["playing"] is False
    assert t["lastAt"] == 500


def test_toggle_resumes_from_zero_when_at_the_last_index() -> None:
    t = call("m.createTransport({count: COUNT, now: NOW})", COUNT=2, NOW=0)
    t = call("m.tick(T, NOW)", T=t, NOW=5000)  # now at index 1 (last)
    t = call("m.tick(T, NOW)", T=t, NOW=10000)  # stops playing at the end
    assert t["index"] == 1
    assert t["playing"] is False
    t = call("m.toggle(T, NOW)", T=t, NOW=10001)
    assert t["playing"] is True
    assert t["index"] == 0


# ================================================== the real-route fixture ==


def test_a_real_played_game_produces_steps_messages_and_a_real_refusal(
    client: TestClient,
) -> None:
    """Play a short game through the real HTTP routes - declare, play a
    land with reasoning, attempt an illegal second land drop (refused, with
    the engine's own verbatim reason), concede - then feed the real
    `GET .../review` JSON through `replay_model.js`. Nothing here is a
    hand-shaped ledger row."""
    info = _agent_game(client, active="bob")
    game_id = info["game_id"]
    try:
        assert _declare(client, info).status_code == 200

        client.post(
            f"/api/games/{game_id}/cards", json={"card": "Forest", "player": "bob", "zone": "hand"}
        )
        first = client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={"player": "bob", "card": "Forest", "reasoning": "land first, then the attack"},
        )
        assert first.status_code == 200 and first.json()["allowed"] is True, first.text

        client.post(
            f"/api/games/{game_id}/cards",
            json={"card": "Mountain", "player": "bob", "zone": "hand"},
        )
        second = client.post(
            f"/api/games/{game_id}/actions/play-land",
            json={
                "player": "bob",
                "card": "Mountain",
                "reasoning": "trying for a second land anyway",
            },
        )
        assert second.status_code == 200, second.text
        assert second.json()["allowed"] is False, second.text
        refusal_reason = second.json()["reason"]
        assert refusal_reason

        conceded = client.post(
            f"/api/games/{game_id}/actions/concede",
            json={"player": "alice", "reasoning": "the race is lost"},
        )
        assert conceded.status_code == 200, conceded.text

        body = _review(client, game_id, key=info["admin_key"]).json()

        steps = call("m.replaySteps(REVIEW)", REVIEW=body)
        messages = call("m.logMessages(REVIEW, STEPS)", REVIEW=body, STEPS=steps)

        assert len(steps) > 0

        all_text = " ".join(m["text"] for m in messages if m.get("text"))
        assert "land first, then the attack" in all_text

        refused_steps = [s for s in steps if s["refused"]]
        assert len(refused_steps) == 1
        assert refused_steps[0]["entry"]["action"] == "play_land"

        refused_messages = [m for m in messages if m["kind"] == "refused"]
        assert len(refused_messages) == 1
        assert refusal_reason in refused_messages[0]["text"]
        assert "trying for a second land anyway" in refused_messages[0]["text"]

        # the plan is a message, never a step.
        step_actions = {s["entry"]["action"] for s in steps}
        assert "plan_declared" not in step_actions
        plan_messages = [m for m in messages if m["kind"] == "plan"]
        assert len(plan_messages) == 1

        # perspective and snapshotView keep working end to end on the real
        # payload.
        who = call("m.perspective(REVIEW)", REVIEW=body)
        assert who in ("alice", "bob")
        view = call(
            "m.snapshotView(ENTRY, CAT, YOU)",
            ENTRY=body["entries"][-1],
            CAT={},
            YOU=who,
        )
        assert view is not None
        assert view["you"] == who
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
