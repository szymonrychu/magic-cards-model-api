"""compact-v1 game log projection: pure functions over `records` rows."""

from __future__ import annotations

from uuid import uuid4

import pytest
import sim_claude_kit as kit
from sim_claude_kit import GREEN, RED

from magic_cards_model_api.sim import gamelog
from magic_cards_model_api.sim.gamelog import page, parse_filter, project, render

NO_NAMES = lambda _id: None  # noqa: E731
DEFAULT = parse_filter(None, None, "plays", True, "changes", 200)


def _lines(rows, names=NO_NAMES, flt=DEFAULT, **kw):
    return project(rows, names, flt, **kw)


def test_cast_line_has_seq_turn_step_seat_card_and_targets():
    cid = str(uuid4())
    names = lambda i: "Emrakul, the Promised End" if i == cid else None  # noqa: E731
    rows = [kit.row(412, "cast", turn=7, card=cid, targets=["b"])]
    text = _lines(rows, names)[-1].text
    assert text.startswith('#412 T7 main1 a cast "Emrakul, the Promised End" -> b')


def test_unresolved_uuid_prints_card_and_plain_name_is_kept():
    rows = [kit.row(1, "cast", card=str(uuid4())), kit.row(2, "cast", card="Shock")]
    texts = [x.text for x in _lines(rows) if not x.header]
    assert ' cast "card"' in texts[0]
    assert ' cast "Shock"' in texts[1]


def test_target_dicts_and_ids_resolve():
    cid = str(uuid4())
    names = lambda i: "Bear" if i == cid else None  # noqa: E731
    rows = [
        kit.row(1, "cast", card="Shock", targets=[{"player": "b"}, {"card_id": cid}, {"id": "x"}])
    ]
    assert "-> b, Bear, x" in _lines(rows, names)[-1].text


def test_why_line_collapses_newlines_and_trims_to_300():
    rows = [kit.row(1, "cast", card="Shock", reasoning="x" * 400 + "\nnext")]
    text = _lines(rows)[-1].text
    first, second = text.split("\n")
    assert second.startswith("   why: ")
    reason = second[len("   why: ") :]
    assert len(reason) == 303 and reason.endswith("...")
    full = project(rows, NO_NAMES, DEFAULT, reason_max=None)[-1].text
    assert full.endswith("x" * 400 + " next")
    assert (
        "why"
        not in project(rows, NO_NAMES, parse_filter(None, None, "plays", False, "changes", 200))[
            -1
        ].text
    )


def test_life_and_hand_deltas_only_when_changed():
    rows = [
        kit.row(1, "pass_priority", snapshot=kit.snap()),
        kit.row(2, "cast", card="Shock", snapshot=kit.snap(life=(20, 14))),
    ]
    text = _lines(rows)[-1].text
    assert "| life a20 b14" in text and "| hand" not in text and "| board" not in text
    none = parse_filter(None, None, "plays", True, "none", 200)
    text = _lines(rows, flt=none)[-1].text
    assert "| life" not in text and "| hand" not in text


def test_baseline_gives_the_first_row_a_delta_and_none_snapshot_is_skipped():
    rows = [
        kit.row(2, "cast", card="A", snapshot=kit.snap(hand=(4, 5))),
        kit.row(3, "cast", card="B", snapshot=None),
    ]
    rows[1]["payload"]["snapshot"] = None
    lines = [x for x in _lines(rows, baseline=kit.snap()) if not x.header]
    assert "| hand a4 b5" in lines[0].text
    assert "|" not in lines[1].text


def test_attack_and_block_pairs():
    rows = [
        kit.row(
            1,
            "attack",
            attackers=[
                {"card": "Thragtusk", "defender": "b"},
                {"card": "Grizzly Bears", "defender": "b"},
            ],
        ),
        kit.row(2, "declare_blockers", blocks=[{"blocker": "Wall", "blocked": "Grizzly Bears"}]),
        kit.row(3, "block", blocks=[{"blocker": "Wall", "blocked": "Grizzly Bears"}]),
    ]
    texts = [x.text for x in _lines(rows) if not x.header]
    assert "attack Thragtusk->b, Grizzly Bears->b" in texts[0]
    assert " block Wall->Grizzly Bears" in texts[1]
    assert " block Wall->Grizzly Bears" in texts[2]


def test_pass_runs_collapse_in_plays_and_stay_in_all():
    rows = [kit.row(i, "pass_priority", player="ab"[(i + 1) % 2]) for i in range(1, 5)]
    plays = [x for x in _lines(rows) if not x.header]
    assert len(plays) == 1
    assert plays[0].text.endswith("pass x4 (a,b)") and plays[0].seq == 1 and plays[0].end_seq == 4
    all_flt = parse_filter(None, None, "all", True, "changes", 200)
    assert len([x for x in _lines(rows, flt=all_flt) if not x.header]) == 4
    single = [x for x in _lines(rows[:1]) if not x.header]
    assert single[0].text.endswith("a pass")


def test_refused_rows_dropped_in_plays_marked_in_all():
    rows = [
        kit.row(1, "cast", card="Shock", allowed=False, reason="not enough mana " * 20),
        kit.row(2, "cast", card="Bolt"),
    ]
    assert [x.seq for x in _lines(rows) if not x.header] == [2]
    all_flt = parse_filter(None, None, "all", True, "changes", 200)
    refused = [x for x in _lines(rows, flt=all_flt) if not x.header][0].text
    assert " REFUSED: not enough mana" in refused
    assert len(refused.split(" REFUSED: ")[1]) <= 120


def test_unrecorded_rows_dropped_in_plays():
    rows = [kit.row(1, "(unrecorded)"), kit.row(2, "cast", card="Bolt")]
    assert [x.seq for x in _lines(rows) if not x.header] == [2]


def test_missing_reasoning_and_pregame_rows():
    rows = [kit.row(1, "mulligan", turn=None, step="mulligan", phase=None, active_player=None)]
    lines = _lines(rows)
    text = [x for x in lines if not x.header][0].text
    assert text.startswith("#1 T? mull a mulligan") and "why" not in text


def test_turn_headers_open_each_turn():
    rows = [
        kit.row(1, "cast", turn=7, card="A", snapshot=kit.snap(board=(6, 4))),
        kit.row(2, "cast", turn=7, card="B"),
        kit.row(3, "cast", turn=8, card="C", active_player="b"),
    ]
    lines = _lines(rows)
    headers = [x for x in lines if x.header]
    assert [h.text for h in headers][0] == "== T7 a (life a20 b20, board a6 b4) =="
    assert len(headers) == 2 and headers[1].turn == 8
    assert lines[0].header and lines[0].seq == 1


def test_filters_turn_range_and_seat():
    rows = [
        kit.row(i, "cast", turn=t, player=p, card="X")
        for i, (t, p) in enumerate([(4, "b"), (5, "a"), (5, "b"), (8, "b"), (9, "b")], start=1)
    ]
    flt = parse_filter("5-8", "b", "plays", True, "changes", 200)
    assert [x.seq for x in _lines(rows, flt=flt) if not x.header] == [3, 4]
    assert any(x.header for x in _lines(rows, flt=flt))
    assert parse_filter(6, None, "plays", True, "changes", 200).turn == (6, 6)
    assert parse_filter("6", None, "plays", True, "changes", 200).turn == (6, 6)
    with pytest.raises(ValueError, match="5-8"):
        parse_filter("8-5", None, "plays", True, "changes", 200)
    with pytest.raises(ValueError, match="a.*b"):
        parse_filter(None, "c", "plays", True, "changes", 200)
    with pytest.raises(ValueError, match="plays.*all"):
        parse_filter(None, None, "x", True, "changes", 200)
    with pytest.raises(ValueError, match="none.*changes.*full"):
        parse_filter(None, None, "plays", True, "weird", 200)
    with pytest.raises(ValueError, match="full"):
        parse_filter(None, None, "plays", True, "full", 11)
    assert parse_filter(None, None, "plays", True, "full", 10).board == "full"


def test_board_full_appends_sorted_json():
    rows = [kit.row(1, "cast", card="A", snapshot=kit.snap())]
    flt = parse_filter(None, None, "plays", True, "full", 10)
    text = _lines(rows, flt=flt)[-1].text
    assert (
        "\n   board: {" in text and '"battlefield"' in text and ", " not in text.split("board: ")[1]
    )


def test_page_respects_limit_and_char_cap():
    rows = kit.ledger(30, 10)
    lines = _lines(rows)
    kept, more = page(lines, 200, 40_000)
    assert len([x for x in kept if not x.header]) == 200 and more is True
    kept, more = page(lines, 200, 2_000)
    assert len(kept) > 0 and len(render(kept)) <= 2_000 and more is True
    kept, more = page(_lines(rows[:3]), 200, 40_000)
    assert more is False
    huge = [kit.row(1, "cast", card="A", reasoning="y " * 50)]
    one = project(huge, NO_NAMES, DEFAULT, reason_max=None)
    kept, more = page(one, 10, 50)
    assert len([x for x in kept if not x.header]) == 1
    assert kept[-1].text.endswith(" [cut]") and len(render(kept)) <= 50


def test_head_tail_keeps_first_two_turns_and_newest_lines():
    rows = kit.ledger(10, 60)
    lines = _lines(rows, flt=parse_filter(None, None, "plays", False, "none", 200))
    kept, truncated = gamelog.head_tail(lines, max_chars=8_000)
    assert truncated is True and len(render(kept)) <= 8_000
    marker = [x for x in kept if "lines omitted" in x.text]
    assert len(marker) == 1 and "sim_match_log(since_seq=" in marker[0].text
    at = kept.index(marker[0])
    assert {x.turn for x in kept[:at] if x.turn is not None} == {1, 2}
    assert [x.seq for x in kept[:at] if not x.header] == list(range(1, 121))
    assert f"since_seq={kept[at - 1].seq})" in marker[0].text
    assert kept[at + 1].turn > 2
    assert kept[-1].seq == rows[-1]["seq"]
    small = _lines(rows[:5])
    assert gamelog.head_tail(small) == (small, False)


def test_output_is_ascii_only():
    name = "Lim-D" + chr(0xFB) + "l"
    rows = [kit.row(1, "cast", card=name, reasoning="a " + chr(0x2014) + " " + chr(0x201C) + "b")]
    out = render(_lines(rows))
    out.encode("ascii")
    assert "Lim-Dul" in out and "?" in out
    assert gamelog.ascii_text("caf" + chr(0xE9) + chr(0x2603)) == "cafe?"


def test_scrub_replaces_only_the_substring():
    rows = [kit.row(1, "cast", card="Shock", reasoning="uid deadbeef here")]
    lines = _lines(rows, scrub=lambda s: s.replace("deadbeef", "<redacted>"))
    action = [x for x in lines if not x.header][0].text
    assert action.split("\n")[1] == "   why: uid <redacted> here"
    assert action.startswith("#1 T3 main1 a cast")


def test_projects_a_real_record_ledger():
    from magic_cards_model_api import audit
    from magic_cards_model_api.decks_api import boot_decklist_game
    from magic_cards_model_api.store import store

    game_id, _, _ = boot_decklist_game(
        [("a", RED, True), ("b", GREEN, True)], seed=1, owner_user_id=None
    )
    try:
        audit.record(game_id, "note", player="b", reasoning="nothing to do")
        audit.record(game_id, "pass_priority", player="a", reasoning="still nothing")
        game = store.get(game_id)
        from uuid import UUID

        def names(card_id: str) -> str | None:
            found = game.find_by_id(UUID(card_id))
            return getattr(found, "name", None)

        lines = project(store.records(game_id), names, DEFAULT)
        text = render(lines)
        text.encode("ascii")
        assert lines[-1].text.split("\n")[0].count(" a pass") == 1
    finally:
        store.delete(game_id)


def test_head_tail_marker_counts_every_omitted_line_when_the_head_cap_pops_lines():
    lines = _lines(kit.ledger(2, 40), flt=parse_filter(None, None, "plays", False, "none", 200))
    kept, truncated = gamelog.head_tail(lines, max_chars=1_500)
    assert truncated is True
    marker = [x for x in kept if "lines omitted" in x.text][0]
    shown = len([x for x in kept if not x.header and x is not marker])
    total = len([x for x in lines if not x.header])
    omitted = int(marker.text.split()[1])
    assert shown + omitted == total
    assert shown < 80


# -- export renderers --


def _header() -> dict:
    return {
        "match_id": "claude-x",
        "status": "done",
        "decks": {"a": "Red", "b": "Green"},
        "seed": 7,
        "seats": {
            "a": {"model": "claude-opus-5-5", "effort": "low", "state": "stopped"},
            "b": {"model": "claude-opus-5-5", "effort": "low", "state": "stopped"},
        },
        "elapsed_s": 12.5,
        "result": {"winner": "a", "drawn": False, "turns": 2},
        "error": None,
    }


def _export_rows(reason: str = "r") -> list:
    return [
        kit.row(1, "play_land", turn=1, card="Forest", reasoning=reason),
        kit.row(2, "cast", turn=1, card="Bolt", reasoning="x"),
        kit.row(3, "play_land", turn=2, player="b", active_player="b", card="Mountain"),
    ]


def test_markdown_export_has_header_turn_sections_and_final_board():
    reason = "w" * 400
    text = gamelog.render_markdown(_header(), _export_rows(reason), lambda v: None, lambda s: s)
    assert text.startswith("# Claude match claude-x")
    assert "## Turn 1 (active a)" in text
    assert "## Turn 2 (active b)" in text
    assert "## Final board" in text
    assert reason in text
    assert "== T1" not in text
    text.encode("ascii")


def test_markdown_export_of_an_empty_log_has_header_and_a_note():
    text = gamelog.render_markdown(_header(), [], lambda v: None, lambda s: s)
    assert text.startswith("# Claude match claude-x")
    assert "## Turn" not in text and "No actions were recorded" in text


def test_markdown_export_scrubs_by_substring():
    rows = [kit.row(1, "play_land", turn=1, card="Forest", reasoning="uid SECRET1 here")]
    text = gamelog.render_markdown(
        _header(), rows, lambda v: None, lambda s: s.replace("SECRET1", "<redacted>")
    )
    assert "SECRET1" not in text and "uid <redacted> here" in text


def test_jsonl_export_one_object_per_record():
    import json

    rows = _export_rows()
    for r in rows:
        r["payload"]["snapshot"] = kit.snap(life=(20, 14))
    out = gamelog.render_jsonl(rows, lambda s: s)
    lines = out.splitlines()
    assert len(lines) == 3 and out.endswith("\n")
    keys = {"seq", "ts", "turn", "step", "phase", "actor", "action", "detail", "life", "reasoning"}
    for line in lines:
        obj = json.loads(line)
        assert set(obj) == keys
        assert obj["life"] == {"a": 20, "b": 14}
    assert json.loads(lines[2])["actor"] == "b"
    assert gamelog.render_jsonl([], lambda s: s) == ""


def test_jsonl_export_scrubs_every_string_value():
    rows = [kit.row(1, "cast", card="SECRET1", reasoning="SECRET1 x")]
    out = gamelog.render_jsonl(rows, lambda s: s.replace("SECRET1", "<redacted>"))
    assert "SECRET1" not in out and "<redacted>" in out
