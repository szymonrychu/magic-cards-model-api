"""`ui/review_model.js` - pure functions over `GET /api/games/{id}/review`'s
own JSON, with no DOM, so this file can ask them the awkward questions
directly the way `tests/test_ui_board_layout.py` asks `ui/board_layout.js`.

What survives here, after R5 retired the old scroll page
(`ui/render/review.js`) and trimmed this module to what the Replay door
still builds on (`ui/replay_model.js`, `render/replay.js`):

- the snapshot parse (`parsePermanent`/`frameState`/`cardNamesIn`), which
  `audit.py` writes as one *string* per permanent ("alice: Grizzly Bears
  (tapped) (sick) [2/2] dmg:1 (+1/+1=2)") rather than as card objects - so a
  replay's fallback board is only as good as the parse of that line;
  - a refused action's engine-verbatim reason, which must survive the parse
    unedited;
- `frameLabel`, the console-style row label the replay's own transport and
  log both read;
- `perspectiveOf`, whose seat's side of the table a replay opens on;
- `adviceOf`, mtg-buddy's own approved/followed/refused derivation, which
  the replay's thinking log reads exactly as the old page did.
"""

from __future__ import annotations

from typing import Any

from ui_node import UI, requires_node, run_module

pytestmark = requires_node

MODULE = "review_model.js"

#: The absolute path of `board_layout.js`, for the couple of tests below that
#: prove `frameState`'s `attached_to`/`exile_pile` are the *live* board's own
#: shape - so `boardLayout` tucks a review frame's Aura or imprinted card
#: under its host with no change to that module at all. `call()` only ever
#: imports `MODULE` as `m`; these load the second module themselves, inside
#: the evaluated expression, which is why they pass this path in as a const.
BOARD_LAYOUT = str(UI / "board_layout.js")


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


def advice_row(
    seq: int,
    player: str = "bob",
    turn: int = 1,
    path: str = "actions/play-land",
    body: dict[str, Any] | None = None,
    summary: str = "play the Forest",
    reasoning: str = "a fourth land keeps the curve on track",
    based_on_seq: int = 0,
    validated: bool = True,
    **over: Any,
) -> dict[str, Any]:
    """One `audit.ADVICE_ACTION` row, in the shape `audit.record` writes it -
    `reasoning` top-level (the same convention every action route's own
    `reasoning` argument already lands under), everything else in `detail`."""
    return entry(
        seq,
        "advice",
        turn=turn,
        reasoning=reasoning,
        detail={
            "player": player,
            "method": "POST",
            "path": f"/api/games/g1/{path}",
            "body": body if body is not None else {},
            "summary": summary,
            "based_on_seq": based_on_seq,
            "validated": validated,
        },
        **over,
    )


def approval_row(seq: int, advice_seq: int, turn: int = 1, **over: Any) -> dict[str, Any]:
    return entry(seq, "advice_approved", turn=turn, detail={"advice_seq": advice_seq}, **over)


def advice_item(seq: int, player: str = "bob", **over: Any) -> dict[str, Any]:
    """One `review.build_review`-computed `advice` list entry, snake_case as
    the server hands it back."""
    row: dict[str, Any] = {
        "seq": seq,
        "ts": f"2026-09-08T20:0{seq}:00+00:00",
        "turn": 1,
        "step": "precombat_main",
        "phase": "precombat_main",
        "player": player,
        "summary": "play the Forest",
        "reasoning": "a fourth land keeps the curve on track",
        "path": "/api/games/g1/actions/play-land",
        "body": {"card_id": "c1"},
        "based_on_seq": 0,
        "validated": True,
        "approved": False,
        "approved_seq": None,
        "did": None,
        "followed": False,
        "refused": [],
    }
    row.update(over)
    return row


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
    assert card["attached_to"] is None


def test_an_attached_permanent_line_carries_its_hosts_index() -> None:
    """`serialize.snapshot`'s `(attached:N)` suffix - `N` the host's own
    index within this same snapshot's battlefield array, which is exactly
    the id `frameState` builds for it (`bf-${index}`) - so this is the whole
    of what lets `board_layout.rootParentId` tuck the card under its host
    the same way it already does for a live board's `attached_to`."""
    card = call('m.parsePermanent("alice: Utopia Sprawl (attached:1)", "bf-2")')
    assert card["name"] == "Utopia Sprawl"
    assert card["attached_to"] == {"kind": "card", "id": "bf-1"}


def test_an_attached_permanent_with_counters_and_damage_still_parses_clean() -> None:
    """The attach marker is the last thing `battlefield_line` appends, so it
    has to come off first - a parse that stripped in the wrong order would
    eat part of the counters block instead."""
    line = "alice: Grizzly Bears (tapped) [4/5] dmg:2 (+1/+1=2) (attached:0)"
    card = call('m.parsePermanent(line, "bf-1")', line=line)
    assert card["name"] == "Grizzly Bears"
    assert card["counters"] == {"+1/+1": 2}
    assert card["damage"] == 2
    assert card["attached_to"] == {"kind": "card", "id": "bf-0"}


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


def _board_layout_of(snap: dict[str, Any]) -> dict[str, Any]:
    """`boardLayout(frameState(snap, {}))`, under Node with `board_layout.js`
    loaded alongside `review_model.js` - see `BOARD_LAYOUT`'s own docstring
    for why this loads it itself rather than going through `call()`."""
    expression = (
        "(async () => {"
        "  const bl = await import(pathToFileURL(BOARD_LAYOUT).href);"
        "  const state = m.frameState(snap, {});"
        "  return bl.boardLayout(state);"
        "})()"
    )
    return run_module(MODULE, expression, snap=snap, BOARD_LAYOUT=BOARD_LAYOUT)


def test_a_frame_state_tucks_an_attached_permanent_under_its_host() -> None:
    """`frameState`'s `attached_to` (built from `parsePermanent`'s
    `(attached:N)`) is `board_layout.js`'s own live-board shape - `{kind:
    "card", id}` - so `boardLayout` tucks a review frame's Aura under its
    host exactly the way it tucks a live one, with no changes to that
    module at all."""
    # No catalogue is loaded here, so `board_layout.isLandRow` (which reads
    # `type_line`) has nothing to classify "Forest" as a land by - it lands
    # in `main`, same as any other untyped card. That is a fact about this
    # test's own setup, not about the tucking under test.
    snap = snapshot(battlefield=["alice: Forest", "alice: Utopia Sprawl (attached:0)"])
    layout = _board_layout_of(snap)
    forest_entry = next(
        e for e in layout["seats"]["alice"]["main"] if e["card"]["name"] == "Forest"
    )
    assert [c["name"] for c in forest_entry["under"]] == ["Utopia Sprawl"]


def test_an_exiled_cards_line_parses_into_a_named_card() -> None:
    state = call("m.frameState(snap, {})", snap=snapshot(exile=["Lightning Bolt"]))
    assert [c["name"] for c in state["exile"]] == ["Lightning Bolt"]
    assert "exile_pile" not in state["exile"][0] or state["exile"][0].get("exile_pile") is None


def test_an_exiled_card_linked_to_a_permanent_names_its_battlefield_id() -> None:
    """`serialize.snapshot`'s exile-line suffix reaches `exile_pile.
    linked_permanent_id` in the same `bf-${index}` shape `attached_to`
    uses, so `board_layout.js` draws the imprinted card under the artifact
    that holds it with no changes of its own."""
    snap = snapshot(battlefield=["alice: Sol Ring"], exile=["Lightning Bolt (attached:0)"])
    state = call("m.frameState(snap, {})", snap=snap)
    exiled = state["exile"][0]
    assert exiled["name"] == "Lightning Bolt"
    assert exiled["exile_pile"] == {"linked_permanent_id": "bf-0"}

    layout = _board_layout_of(snap)
    ring_entry = next(
        e for e in layout["seats"]["alice"]["main"] if e["card"]["name"] == "Sol Ring"
    )
    assert [c["name"] for c in ring_entry["under"]] == ["Lightning Bolt"]
    assert layout["exile"] == []


def test_the_names_a_review_needs_printings_for_ignore_the_attach_suffix() -> None:
    """`cardNamesIn` looks up printings by name - an unparsed `(attached:N)`
    suffix reaching Scryfall as part of the name would simply miss."""
    rows = [
        entry(
            1,
            "cast",
            detail={"player": "bob", "card": "Sol Ring"},
            snapshot=snapshot(
                battlefield=["alice: Sol Ring"], exile=["Lightning Bolt (attached:0)"]
            ),
        )
    ]
    names = call("m.cardNamesIn(rev)", rev=review(rows, []))
    assert "Lightning Bolt" in names
    assert not any("attached" in name for name in names)


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
#
# `reflectionOf`/`aiReviewsOf` kept exported (R5) for `tests/test_review.py`/
# `tests/driver/test_review_filing.py`, which drive them directly against a
# real server-filed review body - `ui/replay_model.js`'s `logMessages` reads
# the same rows itself now, by its own `REFLECTION_ACTIONS`/
# `AI_REVIEW_ACTIONS` scan, rather than calling either.


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


# ------------------------------------------------------- the AI review -----


def test_a_review_with_no_ai_review_is_empty() -> None:
    assert call("m.aiReviewsOf(rev)", rev=review([entry(1, "cast")], [])) == []


def test_an_ai_review_row_is_found_and_carries_its_own_seq() -> None:
    rows = [
        entry(1, "cast", detail={"player": "bob", "card": "Grizzly Bears"}),
        entry(2, "game_over", detail={"winner": "bob"}),
        entry(3, "game_reflection", detail={"player": "bob", "text": "notes"}),
        entry(
            4,
            "ai_review",
            detail={
                "player": "bob",
                "text": "the turn 6 attack was a misread of the empty board",
            },
        ),
    ]
    found = call("m.aiReviewsOf(rev)", rev=review(rows, []))
    assert len(found) == 1
    assert found[0]["text"] == "the turn 6 attack was a misread of the empty board"
    assert found[0]["player"] == "bob"
    assert found[0]["seq"] == 4


def test_an_ai_review_is_never_confused_with_a_reflection() -> None:
    """A `game_reflection` row alone must never satisfy `aiReviewsOf`, and an
    `ai_review` row alone must never satisfy `reflectionOf` - the two are
    separate actions, read by separate functions."""
    reflection_only = review(
        [entry(1, "game_reflection", detail={"player": "bob", "text": "notes"})], []
    )
    assert call("m.aiReviewsOf(rev)", rev=reflection_only) == []

    review_only = review(
        [entry(1, "ai_review", detail={"player": "bob", "text": "a critique"})], []
    )
    assert call("m.reflectionOf(rev)", rev=review_only) is None
    assert call("m.aiReviewsOf(rev)", rev=review_only)[0]["text"] == "a critique"


def test_the_newest_review_wins_when_the_same_player_has_more_than_one() -> None:
    rows = [
        entry(1, "ai_review", detail={"player": "bob", "text": "first pass"}),
        entry(2, "ai_review", detail={"player": "bob", "text": "second pass"}),
    ]
    found = call("m.aiReviewsOf(rev)", rev=review(rows, []))
    assert len(found) == 1
    assert found[0]["text"] == "second pass"


def test_an_aivai_game_shows_one_review_per_ai_seat() -> None:
    """Documented, accepted behaviour (`mtg_agent/driver/review.py`'s own
    docstring): an AIvAI game's two drivers each file their own review, of
    their own seat, and both must be shown - never just the last one
    overwriting the other."""
    rows = [
        entry(1, "ai_review", detail={"player": "bob", "text": "bob's own review"}),
        entry(2, "ai_review", detail={"player": "alice", "text": "alice's own review"}),
    ]
    found = call("m.aiReviewsOf(rev)", rev=review(rows, []))
    assert len(found) == 2
    by_player = {r["player"]: r["text"] for r in found}
    assert by_player == {"bob": "bob's own review", "alice": "alice's own review"}


def test_ai_reviews_are_ordered_by_each_players_own_seq() -> None:
    rows = [
        entry(1, "ai_review", detail={"player": "alice", "text": "alice v1"}),
        entry(2, "ai_review", detail={"player": "bob", "text": "bob v1"}),
        entry(3, "ai_review", detail={"player": "alice", "text": "alice v2"}),
    ]
    found = call("m.aiReviewsOf(rev)", rev=review(rows, []))
    # bob's single review (seq 2) sorts ahead of alice's newest (seq 3),
    # even though alice's row appeared first in the ledger.
    assert [r["player"] for r in found] == ["bob", "alice"]
    assert [r["text"] for r in found] == ["bob v1", "alice v2"]


# --------------------------------------------------- mtg-buddy's advice -----


def test_the_advice_action_constants_match_what_audit_py_writes() -> None:
    assert call("m.ADVICE_ACTION") == "advice"
    assert call("m.ADVICE_APPROVED_ACTION") == "advice_approved"


def test_advice_of_prefers_the_servers_own_computed_list() -> None:
    rows = [advice_row(2)]
    rev = review(rows, [])
    rev["advice"] = [
        advice_item(
            2,
            approved=True,
            approved_seq=3,
            followed=True,
            refused=[
                {
                    "seq": 4,
                    "ts": "2026-09-08T20:04:00+00:00",
                    "turn": 1,
                    "step": "precombat_main",
                    "phase": "precombat_main",
                    "action": "play_land",
                    "reasoning": None,
                    "detail": {
                        "player": "bob",
                        "card_id": "c1",
                        "allowed": False,
                        "reason": "no land drop left",
                    },
                    "player": "bob",
                }
            ],
        )
    ]
    items = call("m.adviceOf(rev)", rev=rev)
    assert len(items) == 1
    assert items[0]["approved"] is True
    assert items[0]["approvedSeq"] == 3
    assert items[0]["followed"] is True
    assert len(items[0]["refused"]) == 1
    assert items[0]["refused"][0]["reason"] == "no land drop left"


def test_advice_of_falls_back_to_deriving_from_raw_entries() -> None:
    """No `review.advice` at all - an older server, or a payload assembled by
    hand - is exactly `test_ui_review_model.py`'s own `review()` fixture:
    every existing test above this section already exercises this path."""
    rows = [
        advice_row(2, body={"card_id": "c1"}),
        approval_row(3, advice_seq=2),
        entry(4, "play_land", detail={"player": "bob", "card_id": "c1", "allowed": True}),
    ]
    items = call("m.adviceOf(rev)", rev=review(rows, []))
    assert len(items) == 1
    item = items[0]
    assert item["seq"] == 2
    assert item["player"] == "bob"
    assert item["approved"] is True
    assert item["approvedSeq"] == 3
    assert item["did"]["seq"] == 4
    assert item["followed"] is True
    assert item["refused"] == []


def test_advice_of_derives_approved_and_followed() -> None:
    rows = [
        advice_row(2, body={"card_id": "c1"}),
        approval_row(3, advice_seq=2),
        entry(4, "play_land", detail={"player": "bob", "card_id": "c1", "allowed": True}),
    ]
    item = call("m.adviceOf(rev)[0]", rev=review(rows, []))
    assert item["approved"] is True
    assert item["did"]["action"] == "play_land"
    assert item["followed"] is True
    assert item["refused"] == []


def test_advice_of_derives_approved_but_refused_distinct_from_ignored() -> None:
    """A refusal is never `did` - `did` is `None` here, and only the
    `approved` flag tells this apart from a piece of advice nobody ever
    approved at all. The refusal is not dropped, though - it is carried in
    `refused`, reason verbatim, since it is the single most instructive line
    a retrospective can contain."""
    rows = [
        advice_row(2, body={"card_id": "c1"}),
        approval_row(3, advice_seq=2),
        entry(
            4,
            "play_land",
            detail={
                "player": "bob",
                "card_id": "c1",
                "allowed": False,
                "reason": "no land drop left",
            },
        ),
    ]
    item = call("m.adviceOf(rev)[0]", rev=review(rows, []))
    assert item["approved"] is True
    assert item["did"] is None
    assert item["followed"] is False
    assert len(item["refused"]) == 1
    assert item["refused"][0]["seq"] == 4
    assert item["refused"][0]["action"] == "play_land"
    assert item["refused"][0]["reason"] == "no land drop left"
    assert item["refused"][0]["refused"] is True


def test_advice_of_derives_more_than_one_refusal_in_the_same_window() -> None:
    rows = [
        advice_row(2, body={"card_id": "c1"}),
        entry(
            3,
            "play_land",
            detail={"player": "bob", "card_id": "c1", "allowed": False, "reason": "first no"},
        ),
        entry(
            4,
            "play_land",
            detail={"player": "bob", "card_id": "c1", "allowed": False, "reason": "second no"},
        ),
    ]
    item = call("m.adviceOf(rev)[0]", rev=review(rows, []))
    assert item["did"] is None
    assert [r["seq"] for r in item["refused"]] == [3, 4]
    assert [r["reason"] for r in item["refused"]] == ["first no", "second no"]


def test_advice_of_derives_ignored_did_something_else() -> None:
    rows = [
        advice_row(2, body={"card_id": "c1"}),
        entry(3, "pass_priority", detail={"player": "bob", "allowed": True}),
    ]
    item = call("m.adviceOf(rev)[0]", rev=review(rows, []))
    assert item["approved"] is False
    assert item["approvedSeq"] is None
    assert item["did"]["action"] == "pass_priority"
    assert item["followed"] is False


def test_advice_of_derives_ignored_did_nothing() -> None:
    rows = [advice_row(2, body={"card_id": "c1"}), entry(3, "cast", detail={"player": "alice"})]
    item = call("m.adviceOf(rev)[0]", rev=review(rows, []))
    assert item["approved"] is False
    assert item["did"] is None
    assert item["followed"] is False
