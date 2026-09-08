"""`ui/review_model.js` - the pure half of the post-game review.

The review page's whole argument is an ordering claim: the agent said what it
expected *before* the rows that show what actually happened. Everything that
decides how those two are grouped and paired lives in this module, with no
DOM, so it can be asked the awkward questions directly the way
`tests/test_ui_board_layout.py` asks `ui/board_layout.js`.

Four of these tests are the cases that break a naive implementation:

- a game with no `plan_declared` row at all - every game played before the
  declaration seam existed, and every game a human played both seats of;
- a declaration with nothing from the opponent after it - the agent moved to
  its next turn's declaration without the opponent ever acting, and an empty
  outcome column must say so rather than silently disappear;
- a refused action, whose engine-verbatim reason is the evidence and must
  survive into the frame unedited;
- the snapshot itself, which `audit.py` writes as one *string* per permanent
  ("alice: Grizzly Bears (tapped) (sick) [2/2] dmg:1 (+1/+1=2)") rather than
  as card objects - so the review's board is only as good as the parse of
  that line.
"""

from __future__ import annotations

from typing import Any

from ui_node import requires_node, run_module

pytestmark = requires_node

MODULE = "review_model.js"


def call(expression: str, **bindings: Any) -> Any:
    return run_module(MODULE, expression, **bindings)


# --------------------------------------------------------------- fixtures --


def snapshot(**over: Any) -> dict[str, Any]:
    """One `serialize.snapshot` frame, in exactly the shape `audit.record`
    writes it - strings for permanents, names for hands and graveyards."""
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
        "ts": f"2026-09-08T20:0{seq}:00+00:00",
        "action": action,
        "turn": 1,
        "step": "precombat_main",
        "phase": "precombat_main",
        "stack_size": 0,
        "detail": {},
        "reasoning": None,
        "snapshot": snapshot(),
    }
    row.update(over)
    return row


PREDICTION = {
    "expecting": "alice takes the three rather than trading her only blocker",
    "because": "she is at 20 and the Bears are her clock, not her defence",
    "confidence": "medium",
}


def plan_row(seq: int, player: str = "bob", turn: int = 1) -> dict[str, Any]:
    return entry(
        seq,
        "plan_declared",
        turn=turn,
        detail={
            "player": player,
            "turns_taken": turn,
            "board_read": "she has one Mountain and no board",
            "my_role": "beatdown - I win before turn six or not at all",
            "plan": "land, Bears, hold the Bolt for her blocker",
            "prediction": PREDICTION,
        },
    )


def declaration(seq: int, player: str = "bob", turn: int = 1, **over: Any) -> dict[str, Any]:
    """One `review.build_review` declaration, as the endpoint assembles it."""
    row: dict[str, Any] = {
        "seq": seq,
        "ts": f"2026-09-08T20:0{seq}:00+00:00",
        "turn": turn,
        "turns_taken": turn,
        "step": "precombat_main",
        "phase": "precombat_main",
        "player": player,
        "board_read": "she has one Mountain and no board",
        "my_role": "beatdown - I win before turn six or not at all",
        "plan": "land, Bears, hold the Bolt for her blocker",
        "prediction": PREDICTION,
        "did": [],
        "opponent_did": [],
    }
    row.update(over)
    return row


def review(entries: list[dict[str, Any]], declarations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "game_id": "g1",
        "players": ["alice", "bob"],
        "created_at": "2026-09-08T20:00:00+00:00",
        "ended_at": None,
        "is_over": True,
        "winner": "bob",
        "drawn": False,
        "ended_seq": entries[-1]["seq"] if entries else None,
        "declarations": declarations,
        "entries": entries,
    }


# ------------------------------------------------ the snapshot's own lines --


def test_a_permanent_line_parses_into_a_card_the_board_renderers_accept() -> None:
    """`serialize.battlefield_line`'s full form, every optional piece
    present. The name is what is left once the suffixes it appends are taken
    off the end - the parse must not eat a name that happens to contain a
    bracket or a colon of its own."""
    line = "alice: Grizzly Bears (tapped) (sick) [4/5] dmg:2 (+1/+1=2,time=3)"
    card = call('m.parsePermanent(line, "bf-0")', line=line)
    assert card["id"] == "bf-0"
    assert card["name"] == "Grizzly Bears"
    assert card["controller"] == "alice"
    assert card["zone"] == "battlefield"
    assert card["tapped"] is True
    assert card["summoning_sick"] is True
    assert card["power"] == "4"
    assert card["toughness"] == "5"
    assert card["damage"] == 2
    assert card["counters"] == {"+1/+1": 2, "time": 3}


def test_a_plain_permanent_line_carries_none_of_the_optional_state() -> None:
    card = call('m.parsePermanent("bob: Mountain", "bf-3")')
    assert card["name"] == "Mountain"
    assert card["controller"] == "bob"
    assert card["tapped"] is False
    assert card["summoning_sick"] is False
    assert card["power"] is None
    assert card["damage"] == 0
    assert card["counters"] == {}


def test_a_frame_state_is_shaped_the_way_board_layout_reads_it() -> None:
    """`frameState` hands `board_layout.js` and `cards.js` exactly what they
    already read off a live state view - so the review draws its boards with
    the game's own renderers rather than a second set."""
    state = call("m.frameState(snap, {})", snap=snapshot())
    assert [c["name"] for c in state["battlefield"]] == ["Mountain", "Grizzly Bears"]
    assert len({c["id"] for c in state["battlefield"]}) == 2
    assert [c["name"] for c in state["hands"]["bob"]] == ["Forest", "Grizzly Bears"]
    assert state["life"] == {"alice": 20, "bob": 17}
    assert state["seats"] == ["alice", "bob"]
    # every id in the frame is unique, since `dom.js`'s keyed patch uses them
    ids = [c["id"] for c in state["battlefield"]]
    ids += [c["id"] for hand in state["hands"].values() for c in hand]
    assert len(ids) == len(set(ids))


def test_a_frame_state_takes_the_printing_from_the_catalogue() -> None:
    """The snapshot carries a name and nothing else about the printing, so
    the type line that decides the land row (`board_layout.isLandRow`) and
    the art the card is drawn with have to be joined in by name."""
    catalogue = {
        "Mountain": {"type_line": "Basic Land - Mountain", "image": "http://img/mountain.png"},
    }
    state = call("m.frameState(snap, catalogue)", snap=snapshot(), catalogue=catalogue)
    mountain = state["battlefield"][0]
    assert mountain["type_line"] == "Basic Land - Mountain"
    assert mountain["image"] == "http://img/mountain.png"
    # An unknown name is still drawn, with no type line rather than a guessed one.
    assert state["battlefield"][1]["type_line"] == ""


def test_the_names_a_review_needs_printings_for_are_every_name_it_shows() -> None:
    rows = [entry(1, "cast", detail={"player": "bob", "card": "Grizzly Bears"})]
    names = call("m.cardNamesIn(rev)", rev=review(rows, []))
    assert names == ["Forest", "Grizzly Bears", "Lightning Bolt", "Mountain"]


def test_an_entry_with_no_snapshot_yields_no_frame_state() -> None:
    """A row recorded for a game the store no longer held carries
    `snapshot: null` (`audit.record`), and the page must skip its board
    rather than throw."""
    assert call("m.frameState(null, {})") is None


# ------------------------------------------------------- turns and pairing --


def test_turns_group_the_ledger_and_lift_each_declaration_out_of_it() -> None:
    rows = [
        entry(1, "pregame_keep", turn=0, step="mulligan", phase=None, detail={"player": "bob"}),
        plan_row(2),
        entry(3, "play_land", detail={"player": "bob", "card": "Forest"}),
        entry(4, "pass_priority", detail={"player": "alice"}),
    ]
    groups = call("m.reviewTurns(rev)", rev=review(rows, [declaration(2)]))
    assert [g["turn"] for g in groups] == [0, 1]
    assert [len(g["declarations"]) for g in groups] == [0, 1]
    # the declaration is the turn's header, never one of its frames
    assert [f["seq"] for f in groups[1]["frames"]] == [3, 4]
    assert groups[1]["declarations"][0]["plan"] == "land, Bears, hold the Bolt for her blocker"


def test_a_game_with_no_declarations_still_reads_back_as_turns_of_frames() -> None:
    """Every game played before the declaration seam existed. The page has
    no prediction to show for these and must still render the ledger."""
    rows = [
        entry(1, "play_land", detail={"player": "alice", "card": "Mountain"}),
        entry(2, "cast", turn=2, detail={"player": "bob", "card": "Grizzly Bears"}),
    ]
    groups = call("m.reviewTurns(rev)", rev=review(rows, []))
    assert [g["turn"] for g in groups] == [1, 2]
    assert all(g["declarations"] == [] for g in groups)
    assert [f["label"] for g in groups for f in g["frames"]] == [
        "play_land Mountain",
        "cast Grizzly Bears",
    ]


def test_a_declaration_pairs_with_what_the_opponent_actually_did() -> None:
    rows = [plan_row(2), entry(3, "cast", detail={"player": "alice", "card": "Lightning Bolt"})]
    declared = declaration(
        2,
        opponent_did=[
            {
                "seq": 3,
                "ts": "2026-09-08T20:03:00+00:00",
                "turn": 1,
                "step": "precombat_main",
                "phase": "precombat_main",
                "action": "cast",
                "reasoning": "the Bears have to die before they connect",
                "detail": {"player": "alice", "card": "Lightning Bolt", "allowed": True},
                "player": "alice",
            }
        ],
    )
    groups = call("m.reviewTurns(rev)", rev=review(rows, [declared]))
    pairing = groups[0]["declarations"][0]
    assert pairing["player"] == "bob"
    assert pairing["opponent"] == "alice"
    assert pairing["prediction"]["expecting"].startswith("alice takes the three")
    assert [f["label"] for f in pairing["opponentDid"]] == ["cast Lightning Bolt"]
    assert pairing["opponentDid"][0]["seq"] == 3
    # the ordering claim the whole page rests on, stated as data
    assert pairing["seq"] < pairing["opponentDid"][0]["seq"]


def test_a_declaration_with_no_opponent_actions_says_so_rather_than_vanishing() -> None:
    rows = [plan_row(2), entry(3, "play_land", detail={"player": "bob", "card": "Forest"})]
    declared = declaration(
        2,
        did=[
            {
                "seq": 3,
                "ts": "2026-09-08T20:03:00+00:00",
                "turn": 1,
                "step": "precombat_main",
                "phase": "precombat_main",
                "action": "play_land",
                "reasoning": "I need the fourth land",
                "detail": {"player": "bob", "card": "Forest"},
                "player": "bob",
            }
        ],
    )
    pairing = call("m.reviewTurns(rev)[0].declarations[0]", rev=review(rows, [declared]))
    assert pairing["opponentDid"] == []
    assert [f["label"] for f in pairing["did"]] == ["play_land Forest"]


def test_a_refused_action_keeps_the_engine_s_own_words() -> None:
    rows = [
        entry(
            1,
            "cast",
            reasoning="I want the Bolt now",
            detail={
                "player": "bob",
                "card": "Lightning Bolt",
                "allowed": False,
                "reason": "it is not your priority",
            },
        )
    ]
    frame = call("m.reviewTurns(rev)[0].frames[0]", rev=review(rows, []))
    assert frame["refused"] is True
    assert frame["reason"] == "it is not your priority"
    assert frame["reasoning"] == "I want the Bolt now"
    assert frame["player"] == "bob"


def test_an_allowed_action_is_not_marked_refused() -> None:
    rows = [entry(1, "cast", detail={"player": "bob", "card": "Grizzly Bears", "allowed": True})]
    frame = call("m.reviewTurns(rev)[0].frames[0]", rev=review(rows, []))
    assert frame["refused"] is False
    assert frame["reason"] is None


def test_the_turn_marker_walking_forward_is_marked_routine() -> None:
    """A real game's ledger is mostly `advance`: 219 of 297 rows in the game
    this page was built against. Marking them lets the outcome column lead
    with what the opponent actually *did* instead of burying it under a
    dozen identical step markers - the page still shows them, in order, just
    not at the same weight."""
    rows = [
        entry(1, "advance", detail={"player": "alice", "allowed": True}),
        entry(2, "pass_priority", detail={"player": "alice"}),
        entry(3, "cast", detail={"player": "alice", "card": "Lightning Bolt"}),
        entry(
            4,
            "advance",
            detail={"player": "alice", "allowed": False, "reason": "it is not your priority"},
        ),
    ]
    frames = call("m.reviewTurns(rev)[0].frames", rev=review(rows, []))
    assert [f["routine"] for f in frames] == [True, True, False, False], (
        "a refused row is never routine, whatever its action - the engine's refusal is the evidence"
    )


def test_runs_of_turn_marker_rows_collapse_without_being_reordered() -> None:
    """The outcome column has to lead with what the opponent actually did,
    and in this ledger that is one row in twelve. Consecutive routine rows
    collapse into a single run *in place* - nothing is reordered, nothing is
    dropped, and the run still names the `seq` span it stands for, so a
    reader can count them and open them."""
    rows = [
        entry(1, "advance", detail={"player": "alice"}),
        entry(2, "advance", detail={"player": "alice"}),
        entry(3, "advance", detail={"player": "alice"}),
        entry(4, "cast", detail={"player": "alice", "card": "Lightning Bolt"}),
        entry(5, "advance", detail={"player": "alice"}),
    ]
    frames = call("m.reviewTurns(rev)[0].frames", rev=review(rows, []))
    items = call("m.collapseRoutine(frames)", frames=frames)
    assert [item["kind"] for item in items] == ["run", "frame", "frame"]
    assert items[0]["from"] == 1
    assert items[0]["to"] == 3
    assert items[0]["count"] == 3
    assert [f["seq"] for f in items[0]["frames"]] == [1, 2, 3]
    assert items[1]["frame"]["label"] == "cast Lightning Bolt"
    assert items[2]["frame"]["seq"] == 5, "a lone routine row is left as itself"


def test_a_frame_label_names_the_card_the_target_and_a_zone_move() -> None:
    labels = call(
        "rows.map((r) => m.frameLabel(r))",
        rows=[
            {"action": "cast", "detail": {"card": "Lightning Bolt", "target": "bob"}},
            {"action": "pass_priority", "detail": {}},
            {"action": "move", "detail": {"from": "hand", "to": "graveyard"}},
        ],
    )
    assert labels == [
        "cast Lightning Bolt -> bob",
        "pass_priority",
        "move hand -> graveyard",
    ]


def test_a_frame_label_never_prints_a_card_id_at_a_reader() -> None:
    """`detail["card"]` is whatever the acting client sent - `POST
    .../actions/cast` takes `card_id` *or* `card`, and a client that sent an
    id leaves a uuid in the ledger where a card name would be (the server's
    own console line has the same hole). A uuid on this page is noise a
    sceptic reads as sloppiness, so it is dropped rather than shown: the
    frame's own board still has the spell on the stack."""
    labels = call(
        "rows.map((r) => m.frameLabel(r))",
        rows=[
            {
                "action": "cast",
                "detail": {"player": "bob", "card": "51302feb-db27-4283-8bc3-e39d21252371"},
            },
            {
                "action": "resolve_top",
                "detail": {"resolved": "Grizzly Bears", "zone": "battlefield"},
            },
            {
                "action": "decide",
                "detail": {
                    "kind": "choose_cards",
                    "prompt": "discard 1 card(s) to maximum hand size",
                },
            },
            {"action": "game_over", "detail": {"winner": "bob", "note": "bob wins"}},
        ],
    )
    assert labels == [
        "cast",
        "resolve_top Grizzly Bears",
        "decide: discard 1 card(s) to maximum hand size",
        "game_over bob wins",
    ]


def test_the_page_is_read_from_the_declaring_seat_s_side_of_the_table() -> None:
    """The board in each frame is drawn the way the game was drawn for the
    seat whose reasoning the page is showing - the agent's, which is the seat
    that declared."""
    rows = [plan_row(2), plan_row(4, turn=2)]
    who = call("m.perspectiveOf(rev)", rev=review(rows, [declaration(2), declaration(4, turn=2)]))
    assert who == "bob"


def test_with_no_declarations_the_perspective_falls_back_to_the_first_seat() -> None:
    assert call("m.perspectiveOf(rev)", rev=review([entry(1, "cast")], [])) == "alice"


# ----------------------------------------------------------- the reflection --


def test_a_review_with_no_reflection_has_none() -> None:
    """Today's `GET .../review` carries no reflection at all, so this is the
    ordinary case and the section simply does not render."""
    assert call("m.reflectionOf(rev)", rev=review([entry(1, "cast")], [])) is None


def test_a_reflection_written_after_the_result_is_found_and_marked_as_such() -> None:
    rows = [
        entry(1, "cast", detail={"player": "bob", "card": "Grizzly Bears"}),
        entry(2, "game_over", detail={"winner": "bob"}),
        entry(
            3,
            "game_reflection",
            detail={"player": "bob", "text": "her removal was all sorcery speed"},
        ),
    ]
    found = call("m.reflectionOf(rev)", rev=review(rows, []))
    assert found["text"] == "her removal was all sorcery speed"
    assert found["player"] == "bob"
    assert found["afterTheResult"] is True


def test_a_reflection_the_endpoint_carries_itself_is_used_when_present() -> None:
    rev = review([entry(1, "cast")], [])
    rev["reflection"] = {
        "text": "I was the beatdown and played like the control deck",
        "player": "bob",
    }
    found = call("m.reflectionOf(rev)", rev=rev)
    assert found["text"] == "I was the beatdown and played like the control deck"
    assert found["afterTheResult"] is True
