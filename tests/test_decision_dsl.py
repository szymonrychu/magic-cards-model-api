"""DR1: `playscripts.decision_dsl` - `compact_board` and `decision_rows`.

Rows are plain dicts shaped like `audit.entries()` (`turn`/`step`/`phase`/
`active_player`/`reasoning`/`detail` top-level, `seq` from the store) - see
`tests/playscript_testkit.py` for the `state_view` builder reused here for
board fixtures.
"""

from __future__ import annotations

import json
from typing import Any

import playscript_testkit as kit

from magic_cards_model_api.playscripts import decision_dsl, schema
from magic_cards_model_api.playscripts.decision_dsl import compact_board, decision_rows

SEAT = "alice"
OPPONENT = "bob"


def row(
    seq: int,
    action: str,
    *,
    player: str = SEAT,
    allowed: bool | None = True,
    turn: int = 3,
    step: str = "precombat_main",
    active_player: str = SEAT,
    reasoning: str | None = "because",
    state_view: dict[str, Any] | None = None,
    **detail: Any,
) -> dict[str, Any]:
    """One ledger entry, shaped like `audit.entries()`: `detail` never
    carries `reasoning` (T10 stamps it top-level, alongside `turn`/`step`/
    `active_player` - see `audit.record`'s own payload) - `decision_dsl`
    reads it from there. `state_view` is DR1's own optional per-row
    attachment (DR2 wires it from `GET /review/frames/{seq}`): a
    `GameStateView`-shaped dict this row's move can be read against."""
    return {
        "seq": seq,
        "action": action,
        "detail": {"player": player, "allowed": allowed, **detail},
        "turn": turn,
        "step": step,
        "active_player": active_player,
        "reasoning": reasoning,
        "state_view": state_view,
    }


def only(records: list[dict[str, Any]]) -> dict[str, Any]:
    assert len(records) == 1, records
    return records[0]


def play_of(record: dict[str, Any]) -> dict[str, Any]:
    assert record["dsl"] is not None, record
    assert record["unexpressible"] is None, record
    errors = schema.play_errors(record["dsl"])
    assert errors == [], errors
    return record["dsl"]


# -- record shape -------------------------------------------------------------


def test_record_has_the_fixed_shape_and_ignores_other_seats_and_rows():
    entries = [
        row(1, "cast", player=OPPONENT, card="Mountain", from_zone="battlefield"),
        row(2, "cast", allowed=False, card="Mountain", from_zone="battlefield"),
        row(3, "game_reflection", text="gg"),
        row(4, "pass_priority"),
    ]
    out = decision_rows(entries, SEAT)
    record = only(out)
    assert set(record) == {"seq", "turn", "step", "action", "dsl", "reasoning", "board"} | {
        "unexpressible"
    }
    assert record["seq"] == 4
    assert record["turn"] == 3
    assert record["step"] == "precombat_main"
    assert record["action"] == "pass"


def test_reasoning_is_verbatim_including_pass_until_threaded_reason():
    entries = [row(1, "pass_priority", reasoning="holding up removal for their combat step")]
    record = only(decision_rows(entries, SEAT))
    assert record["reasoning"] == "holding up removal for their combat step"


def test_board_is_none_without_a_state_view():
    record = only(decision_rows([row(1, "pass_priority")], SEAT))
    assert record["board"] is None


def test_board_is_compact_board_of_the_row_state_view():
    view = kit.state_view(SEAT, opponent=OPPONENT)
    record = only(decision_rows([row(1, "pass_priority", state_view=view)], SEAT))
    assert record["board"] == compact_board(view)
    assert record["board"] is not None


# -- unexpressible: not in the vocabulary -------------------------------------


def test_declare_attackers_is_unexpressible_attack():
    record = only(decision_rows([row(1, "attack", attackers=[])], SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "attack"


def test_declare_blockers_is_unexpressible_block():
    record = only(decision_rows([row(1, "declare_blockers", blocks=[])], SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "block"


def test_mulligan_is_unexpressible_mulligan():
    record = only(decision_rows([row(1, "mulligan_declared", take="keep")], SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "mulligan"


def test_pass_is_unexpressible_pass():
    record = only(decision_rows([row(1, "pass_priority")], SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "pass"


def test_pass_until_rows_are_pass_priority_rows_each_unexpressible():
    # `seatkit.pass_until` threads one reason onto every `pass_priority` row
    # it drives - there is no separate ledger action for it (see the
    # module docstring); two such rows just read as two `pass` records.
    entries = [
        row(1, "pass_priority", reasoning="thread"),
        row(2, "pass_priority", reasoning="thread"),
    ]
    out = decision_rows(entries, SEAT)
    assert [r["unexpressible"] for r in out] == ["pass", "pass"]
    assert [r["reasoning"] for r in out] == ["thread", "thread"]


def test_opening_hand_action_is_unexpressible_pregame():
    # CR 103.6 has no turn/step at all - the real ledger row (`opening.py`)
    # never stamps one, so no `when` block can be built without inventing
    # one - see the module docstring.
    record = only(
        decision_rows(
            [row(1, "opening_hand_action", step="pregame_actions", card="Leyline of the Void")],
            SEAT,
        )
    )
    assert record["dsl"] is None
    assert record["unexpressible"] == "pregame"


def test_every_unexpressible_kind_this_module_can_produce():
    entries = [
        row(1, "attack"),
        row(2, "declare_blockers"),
        row(3, "mulligan_declared"),
        row(4, "pass_priority"),
        row(5, "opening_hand_action", step="pregame_actions"),
    ]
    kinds = {r["unexpressible"] for r in decision_rows(entries, SEAT)}
    assert kinds == {"attack", "block", "mulligan", "pass", "pregame"}


# -- run_script ----------------------------------------------------------------


def test_run_script_records_the_play_id_and_taken_offer_outcome():
    entries = [
        row(
            1,
            "script_run_offered",
            seat=SEAT,
            game_id="g1",
            run_id="r1",
            script_id="fetch-shock",
            priority=50,
            advise=False,
            step="precombat_main",
        ),
        row(
            2,
            "script_run_started",
            seat=SEAT,
            game_id="g1",
            run_id="r1",
            script_id="fetch-shock",
        ),
    ]
    out = decision_rows(entries, SEAT)
    started = [r for r in out if r["action"] == "run_script"]
    record = only(started)
    assert record["unexpressible"] is None
    assert record["dsl"] == {
        "kind": "run_script",
        "play_id": "fetch-shock",
        "offer_outcome": "taken",
    }


def test_run_script_records_unoffered_when_no_matching_offer_precedes_it():
    entries = [
        row(1, "script_run_started", seat=SEAT, game_id="g1", run_id="r1", script_id="fetch-shock")
    ]
    record = only(decision_rows(entries, SEAT))
    assert record["dsl"] == {
        "kind": "run_script",
        "play_id": "fetch-shock",
        "offer_outcome": "unoffered",
    }


# -- cast / play_land: card-name-free selectors --------------------------------


def _view_with_battlefield_card(card: dict[str, Any]) -> dict[str, Any]:
    return kit.state_view(SEAT, opponent=OPPONENT, battlefield=[card])


def test_cast_without_board_data_is_a_bare_empty_selector():
    entries = [row(1, "cast", card="Lightning Bolt", from_zone="hand")]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["kind"] == "cast"
    assert move["card"]["select"] == {}
    assert move["card"]["zone"] == "hand"


def test_cast_with_board_data_builds_a_card_name_free_selector():
    bolt = kit.bolt(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    entries = [
        row(1, "cast", card_id=bolt["id"], card="Lightning Bolt", from_zone="hand", state_view=view)
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    select = play["moves"][0]["card"]["select"]
    dumped = json.dumps(select)
    assert "lightning" not in dumped.lower() and "bolt" not in dumped.lower()
    assert "instant" in select.get("types_all", [])


def test_play_land_with_board_data_builds_a_land_selector():
    fetch = kit.fetchland(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [fetch]})
    entries = [row(1, "play_land", card=fetch["name"], state_view=view)]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["kind"] == "play_land"
    dumped = json.dumps(move["card"]["select"])
    assert "fetch" not in dumped.lower()
    assert "land" in move["card"]["select"].get("types_all", [])


def test_activate_without_board_data_is_unexpressible_missing_ability_text():
    entries = [row(1, "activate", card_id="00000000-0000-4000-8000-000000000001", ability_index=0)]
    record = only(decision_rows(entries, SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "activate:no-ability-text"


def test_activate_with_board_data_uses_the_real_ability_text_as_a_phrase():
    fetch = kit.fetchland(zone="battlefield", controller=SEAT)
    view = _view_with_battlefield_card(fetch)
    entries = [
        row(
            1,
            "activate",
            card_id=fetch["id"],
            ability_index=0,
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["kind"] == "activate"
    assert move["ability_text_has"] == kit.FETCH_TEXT
    assert move["source"]["zone"] == "battlefield"


# -- decide/answer ---------------------------------------------------------------


def _decide(seq: int, kind: str, **extra: Any) -> dict[str, Any]:
    return row(seq, "decide", kind=kind, prompt="?", **extra)


def test_decide_may_is_an_accept_answer():
    record = only(decision_rows([_decide(1, "may", accept=True)], SEAT))
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer == {"decision": "may", "accept": True}


def test_decide_pay_life_amount_is_an_amount_answer():
    record = only(decision_rows([_decide(1, "pay_life_amount", life_amount=2)], SEAT))
    play = play_of(record)
    assert play["moves"][0]["answers"][0] == {"decision": "pay_life_amount", "amount": 2}


def test_decide_choose_a_mode_is_a_choice_answer():
    record = only(
        decision_rows([_decide(1, "choose_a_mode", choice="Destroy target creature")], SEAT)
    )
    play = play_of(record)
    assert play["moves"][0]["answers"][0] == {
        "decision": "choose_a_mode",
        "choice": "Destroy target creature",
    }


def test_decide_choose_pile_is_a_pile_one_answer():
    record = only(decision_rows([_decide(1, "choose_pile", pile_one=False)], SEAT))
    play = play_of(record)
    assert play["moves"][0]["answers"][0] == {"decision": "choose_pile", "pile_one": False}


def test_decide_choose_players_maps_usernames_to_me_and_opponent():
    record = only(decision_rows([_decide(1, "choose_players", usernames=[SEAT, OPPONENT])], SEAT))
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer["decision"] == "choose_players"
    assert set(answer["players"]) == {"me", "opponent"}


def test_decide_surveil_keep_all_is_a_bare_answer():
    record = only(decision_rows([_decide(1, "surveil", moved=[])], SEAT))
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer == {"decision": "surveil"}


def test_decide_surveil_with_cards_moved_is_unexpressible():
    record = only(
        decision_rows([_decide(1, "surveil", moved=["00000000-0000-4000-8000-000000000009"])], SEAT)
    )
    assert record["dsl"] is None
    assert record["unexpressible"] == "decide:surveil"


def test_decide_choose_cards_is_unexpressible_needs_a_selector():
    record = only(
        decision_rows(
            [_decide(1, "choose_cards", card_ids=["00000000-0000-4000-8000-000000000009"])], SEAT
        )
    )
    assert record["dsl"] is None
    assert record["unexpressible"] == "decide:choose_cards"


def test_decide_order_triggers_is_unexpressible():
    record = only(
        decision_rows(
            [_decide(1, "order_triggers", card_ids=["00000000-0000-4000-8000-000000000009"])],
            SEAT,
        )
    )
    assert record["dsl"] is None
    assert record["unexpressible"] == "decide:order_triggers"


def test_decide_put_on_top_or_bottom_is_unexpressible():
    record = only(decision_rows([_decide(1, "put_on_top_or_bottom", top=True)], SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "decide:put_on_top_or_bottom"


def test_decide_declare_attackers_reuses_the_attack_label():
    record = only(decision_rows([_decide(1, "declare_attackers", pairs=[])], SEAT))
    assert record["unexpressible"] == "attack"


def test_decide_mulligan_reuses_the_mulligan_label():
    record = only(decision_rows([_decide(1, "mulligan", accept=True)], SEAT))
    assert record["unexpressible"] == "mulligan"


# -- compact_board ---------------------------------------------------------------


def test_compact_board_is_deterministic_and_sorted():
    view = kit.state_view(SEAT, opponent=OPPONENT)
    first = compact_board(view)
    second = compact_board(view)
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(first)


def test_compact_board_carries_turn_step_active_player_and_life():
    view = kit.state_view(SEAT, opponent=OPPONENT, turn_number=5, active=SEAT, life={SEAT: 14})
    board = compact_board(view)
    assert board["turn"] == 5
    assert board["step"] == "precombat_main"
    assert board["active_player"] == SEAT
    assert board["players"][SEAT]["life"] == 14


def test_compact_board_counts_lands_and_lists_battlefield_permanents():
    tapped_mtn = kit.mountain(controller=SEAT, tapped=True)
    untapped_mtn = kit.mountain(controller=SEAT, tapped=False)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[tapped_mtn, untapped_mtn])
    board = compact_board(view)
    lands = board["players"][SEAT]["lands"]
    assert lands == {"untapped": 1, "total": 2}
    names = {p["name"] for p in board["players"][SEAT]["battlefield"]}
    assert names == {"Mountain"}


def test_compact_board_stays_at_or_under_the_char_cap():
    battlefield = [
        kit.card(
            f"Permanent {i}",
            type_line="Creature",
            controller=SEAT,
            power=i,
            toughness=i,
            counters={"+1/+1": i},
        )
        for i in range(40)
    ]
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=battlefield)
    board = compact_board(view)
    size = len(json.dumps(board, sort_keys=True))
    assert size <= decision_dsl.COMPACT_BOARD_CHARS
    # life is never trimmed away even under heavy pressure
    assert board["players"][SEAT]["life"] == 20


def test_compact_board_trims_graveyard_and_exile_detail_before_life_or_lands():
    battlefield = [
        kit.card(
            f"Permanent {i}",
            type_line="Creature",
            controller=SEAT,
            power=i,
            toughness=i,
            counters={"+1/+1": i},
        )
        for i in range(60)
    ]
    view = kit.state_view(
        SEAT,
        opponent=OPPONENT,
        battlefield=battlefield,
        graveyard_count={SEAT: 12, OPPONENT: 3},
        life={SEAT: 17},
    )
    board = compact_board(view)
    size = len(json.dumps(board, sort_keys=True))
    assert size <= decision_dsl.COMPACT_BOARD_CHARS
    assert board["players"][SEAT]["life"] == 17
    assert board["players"][SEAT]["lands"] == {"untapped": 0, "total": 0}
    # graveyard/exile were the first thing cut
    assert "graveyard" not in board["players"][SEAT]
