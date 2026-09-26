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


# -- unexpressible: still not in the vocabulary -------------------------------


def test_declare_attackers_without_board_data_is_unexpressible_attack():
    # A non-empty `attackers` needs every attacker resolved in a state
    # view - with none attached at all, nothing can be resolved.
    record = only(
        decision_rows(
            [row(1, "attack", attackers=[{"card": "00000000-0000-4000-8000-000000000009"}])], SEAT
        )
    )
    assert record["dsl"] is None
    assert record["unexpressible"] == "attack"


def test_declare_blockers_without_board_data_is_unexpressible_block():
    record = only(
        decision_rows(
            [
                row(
                    1,
                    "declare_blockers",
                    blocks=[
                        {
                            "blocker": "00000000-0000-4000-8000-000000000009",
                            "blocked": "00000000-0000-4000-8000-000000000010",
                        }
                    ],
                )
            ],
            SEAT,
        )
    )
    assert record["dsl"] is None
    assert record["unexpressible"] == "block"


def test_mulligan_declared_keep_gives_a_mulligan_move():
    # FD4 (h)/G4: `take=False` (keeping the hand) is the `mulligan` move's
    # own inverse, `keep=True`; `bottom` is left out, the engine's own
    # default - this row names only whether a bottoming happened, never
    # which cards it chose.
    record = only(decision_rows([row(1, "mulligan_declared", take=False)], SEAT))
    play = play_of(record)
    assert play["when"] == {"whose_turn": "either", "steps": ["mulligan"], "stack": "empty"}
    assert play["moves"] == [{"kind": "mulligan", "keep": True}]


def test_mulligan_declared_take_gives_keep_false():
    record = only(decision_rows([row(1, "mulligan_declared", take=True)], SEAT))
    play = play_of(record)
    assert play["moves"] == [{"kind": "mulligan", "keep": False}]


def test_mulligan_declared_without_a_bool_take_is_unexpressible_mulligan():
    record = only(decision_rows([row(1, "mulligan_declared", take=None)], SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "mulligan"


def test_concede_is_unexpressible_concede():
    # `app.action_concede` records `"concede"` with `allowed=True` and the
    # seat's own reasoning (CR 104.3a: concede at any time) - the vocabulary
    # has no move for it, same as mulligan/pregame.
    record = only(decision_rows([row(1, "concede", reasoning="the board is lost")], SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "concede"
    assert record["action"] == "concede"
    assert record["reasoning"] == "the board is lost"


def test_opening_hand_action_gives_a_plain_accept_answer():
    # FD4 (h)/G4: CR 103.6 has no turn/step at all - `opening.py`'s own row
    # never stamps one - so `when` is built straight from the schema-only
    # `pregame_actions` step rather than the row's own (absent) position.
    # This module does not attempt to say *which* card the row acted on.
    record = only(
        decision_rows(
            [row(1, "opening_hand_action", step="pregame_actions", card="Leyline of the Void")],
            SEAT,
        )
    )
    play = play_of(record)
    assert play["when"] == {
        "whose_turn": "either",
        "steps": ["pregame_actions"],
        "stack": "empty",
    }
    assert play["moves"] == [
        {"kind": "answer", "answers": [{"decision": "opening_hand_action", "accept": True}]}
    ]
    assert play["true"] == [{"kind": "pending_decision", "decision": "opening_hand_action"}]


def test_only_concede_stays_unexpressible_by_design():
    # FD4: `concede` is the one decision this module reaches that stays
    # unexpressible on purpose - CR 104.3a lets a player concede at any
    # time, and a script must never end the game. Every other kind this
    # module reaches directly off a row's own action is now expressible.
    entries = [
        row(1, "mulligan_declared", take=False),
        row(2, "concede"),
        row(3, "opening_hand_action", step="pregame_actions"),
    ]
    kinds = {r["unexpressible"] for r in decision_rows(entries, SEAT)}
    assert kinds == {None, "concede"}


# -- pass: FB2's M1 --------------------------------------------------------------


def test_pass_gives_a_pass_move_needing_no_board_data():
    record = only(decision_rows([row(1, "pass_priority")], SEAT))
    play = play_of(record)
    assert play["moves"] == [{"kind": "pass"}]


def test_pass_until_rows_each_give_their_own_pass_move_with_the_row_step():
    # `seatkit.pass_until` threads one reason onto every `pass_priority` row
    # it drives - there is no separate ledger action for it (see the
    # module docstring); two such rows just read as two `pass` records,
    # each carrying its own row's own step in `when`.
    entries = [
        row(1, "pass_priority", reasoning="thread", step="upkeep"),
        row(2, "pass_priority", reasoning="thread", step="end_step"),
    ]
    out = decision_rows(entries, SEAT)
    plays = [play_of(r) for r in out]
    assert [p["when"]["steps"] for p in plays] == [["upkeep"], ["end_step"]]
    assert [p["moves"] for p in plays] == [[{"kind": "pass"}], [{"kind": "pass"}]]
    assert [r["reasoning"] for r in out] == ["thread", "thread"]


# -- declare_attackers / declare_blockers: FB2's M2/M3 --------------------------


def _attacker(controller: str = SEAT, **kw: Any) -> dict[str, Any]:
    return kit.card(
        "Bruiser",
        type_line="Creature — Bear",
        controller=controller,
        power=2,
        toughness=2,
        **kw,
    )


def test_declare_attackers_two_creatures_at_the_player_gives_one_group():
    bear1, bear2 = _attacker(), _attacker()
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[bear1, bear2])
    entries = [
        row(
            1,
            "attack",
            step="declare_attackers",
            attackers=[
                {"card": bear1["id"], "defender": OPPONENT},
                {"card": bear2["id"], "defender": OPPONENT},
            ],
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["kind"] == "declare_attackers"
    assert len(move["attackers"]) == 1
    group = move["attackers"][0]
    assert group["count"] == 2
    assert "defender" not in group  # left out means the opponent player
    dumped = json.dumps(group)
    assert bear1["name"].lower() not in dumped.lower()


def test_declare_attackers_empty_list_is_expressible_with_no_board_data():
    record = only(decision_rows([row(1, "attack", step="declare_attackers", attackers=[])], SEAT))
    play = play_of(record)
    assert play["moves"] == [{"kind": "declare_attackers", "attackers": []}]


def test_declare_attackers_at_a_planeswalker_gives_a_defender_card():
    bear = _attacker()
    walker = kit.card("Walker", type_line="Legendary Planeswalker — Foo", controller=OPPONENT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[bear, walker])
    entries = [
        row(
            1,
            "attack",
            step="declare_attackers",
            attackers=[{"card": bear["id"], "defender": walker["id"]}],
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    group = play["moves"][0]["attackers"][0]
    assert group["defender"]["side"] == "opponent"
    assert "planeswalker" in group["defender"]["select"].get("types_all", [])


def test_declare_blockers_no_block_gives_empty_blocks():
    record = only(
        decision_rows([row(1, "declare_blockers", step="declare_blockers", blocks=[])], SEAT)
    )
    play = play_of(record)
    assert play["moves"] == [{"kind": "declare_blockers", "blocks": []}]


def test_declare_blockers_one_block_groups_by_attacker():
    blocker = kit.card(
        "Guardian", type_line="Creature — Wall", controller=SEAT, power=0, toughness=4
    )
    attacker = _attacker(controller=OPPONENT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[blocker, attacker])
    entries = [
        row(
            1,
            "declare_blockers",
            step="declare_blockers",
            blocks=[{"blocker": blocker["id"], "blocked": attacker["id"]}],
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["kind"] == "declare_blockers"
    assert len(move["blocks"]) == 1
    block = move["blocks"][0]
    assert block["blocker"]["count"] == 1
    dumped = json.dumps(block)
    assert blocker["name"].lower() not in dumped.lower()
    assert attacker["name"].lower() not in dumped.lower()


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
    # FD4 (a)/gap 1: the real ledger's own `detail["card"]` is the card's
    # UUID (`app.action_cast`'s own `card=req.card or str(req.card_id)`),
    # never a separate `card_id` key - a UUID-shaped `card` value, looked up
    # as an id, is exactly what a real row carries.
    bolt = kit.bolt(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    entries = [row(1, "cast", card=str(bolt["id"]), from_zone="hand", state_view=view)]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    select = play["moves"][0]["card"]["select"]
    dumped = json.dumps(select)
    assert "lightning" not in dumped.lower() and "bolt" not in dumped.lower()
    assert "instant" in select.get("types_all", [])


def test_cast_id_lookup_falls_back_to_a_name_when_no_id_matches():
    # A row whose `card` field genuinely names a card (never both alongside
    # a real id on the same row) still resolves - id is only tried first.
    bolt = kit.bolt(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    entries = [row(1, "cast", card=bolt["name"], from_zone="hand", state_view=view)]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    assert "instant" in play["moves"][0]["card"]["select"].get("types_all", [])


def test_play_land_with_board_data_builds_a_land_selector():
    fetch = kit.fetchland(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [fetch]})
    entries = [row(1, "play_land", card=str(fetch["id"]), state_view=view)]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["kind"] == "play_land"
    dumped = json.dumps(move["card"]["select"])
    assert "fetch" not in dumped.lower()
    assert "land" in move["card"]["select"].get("types_all", [])


def test_activate_without_board_data_is_unexpressible_unresolved():
    # FD4 (c): the misnamed `activate:no-ability-text` becomes
    # `activate:unresolved`, used only when the card itself cannot be found
    # at all - with no board data, that is every activate row.
    entries = [row(1, "activate", card_id="00000000-0000-4000-8000-000000000001", ability_index=0)]
    record = only(decision_rows(entries, SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "activate:unresolved"


def test_activate_with_board_data_prefers_ability_index_over_text():
    # FD4 (c)/G7: `ability_index` is used whenever it is a valid index at
    # all - exempt from the lint's `phrase-too-narrow` since FD2, unlike a
    # hand-written oracle-text phrase this module cannot itself prove is
    # shared by three or more cards.
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
    assert move["ability_index"] == 0
    assert "ability_text_has" not in move
    assert move["source"]["zone"] == "battlefield"


def test_activate_falls_back_to_ability_text_when_index_exceeds_the_schema_bound():
    # `MAX_ABILITY_INDEX` (15) comfortably covers every real card's own
    # ability list, so this is a pathological fixture rather than a
    # realistic row - but the fallback still has to hold for one.
    many_abilities = tuple(f"filler {n}" for n in range(16)) + (kit.FETCH_TEXT,)
    exotic = kit.card("Exotic", type_line="Land", controller=SEAT, abilities=many_abilities)
    view = _view_with_battlefield_card(exotic)
    entries = [row(1, "activate", card_id=exotic["id"], ability_index=16, state_view=view)]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["ability_text_has"] == kit.FETCH_TEXT
    assert "ability_index" not in move


def test_activate_with_index_exceeding_bound_and_no_text_is_unresolved():
    exotic = kit.card(
        "Exotic",
        type_line="Land",
        controller=SEAT,
        abilities=tuple(f"filler {n}" for n in range(17)),
    )
    view = _view_with_battlefield_card(exotic)
    entries = [row(1, "activate", card_id=exotic["id"], ability_index=99, state_view=view)]
    record = only(decision_rows(entries, SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "activate:no-ability-reference"


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


def test_decide_put_into_hand_or_graveyard_is_a_to_hand_answer():
    record = only(decision_rows([_decide(1, "put_into_hand_or_graveyard", to_hand=False)], SEAT))
    play = play_of(record)
    assert play["moves"][0]["answers"][0] == {
        "decision": "put_into_hand_or_graveyard",
        "to_hand": False,
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


def test_decide_put_on_top_or_bottom_gives_a_position_answer_needing_no_board_data():
    record = only(decision_rows([_decide(1, "put_on_top_or_bottom", top=True)], SEAT))
    play = play_of(record)
    assert play["moves"][0]["answers"][0] == {"decision": "put_on_top_or_bottom", "position": "top"}


def test_decide_put_on_top_or_bottom_bottom():
    record = only(decision_rows([_decide(1, "put_on_top_or_bottom", top=False)], SEAT))
    play = play_of(record)
    assert play["moves"][0]["answers"][0]["position"] == "bottom"


# -- decide/answer with board data: scry/surveil, choose_cards, choose_targets --


def _library_known(*cards: dict[str, Any]) -> dict[str, Any]:
    return {
        "library": [
            {"index_from_top": i, "source": "scry", "card": card} for i, card in enumerate(cards)
        ]
    }


def test_decide_scry_keep_one_bottom_one_gives_to_bottom_and_top_order():
    on_top = kit.card("Firebolt", type_line="Instant")
    sent_down = kit.card("Swampland", type_line="Land")
    view = kit.state_view(SEAT, opponent=OPPONENT, known=_library_known(on_top, sent_down))
    record = only(
        decision_rows(
            [
                _decide(
                    1, "scry", moved=[sent_down["id"]], top_order=[on_top["id"]], state_view=view
                )
            ],
            SEAT,
        )
    )
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer["decision"] == "scry"
    assert "land" in answer["to_bottom"].get("types_all", [])
    assert answer["top_order"] == [{"types_all": ["instant"]}]
    dumped = json.dumps(answer)
    assert "firebolt" not in dumped.lower() and "swampland" not in dumped.lower()


def test_decide_surveil_with_cards_moved_and_board_data_gives_to_graveyard():
    moved = kit.card("Moved", type_line="Creature — Bear")
    view = kit.state_view(SEAT, opponent=OPPONENT, known=_library_known(moved))
    record = only(
        decision_rows([_decide(1, "surveil", moved=[moved["id"]], state_view=view)], SEAT)
    )
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer["decision"] == "surveil"
    assert "creature" in answer["to_graveyard"].get("types_all", [])


def test_decide_choose_cards_with_board_data_gives_a_pick():
    found = kit.card("Found", type_line="Land — Mountain")
    view = kit.state_view(SEAT, opponent=OPPONENT, known=_library_known(found))
    record = only(
        decision_rows([_decide(1, "choose_cards", card_ids=[found["id"]], state_view=view)], SEAT)
    )
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer["decision"] == "choose_cards"
    assert answer["pick"]["count"] == 1
    assert "land" in answer["pick"]["select"].get("types_all", [])


def test_decide_choose_targets_player_target_gives_targets():
    view = kit.state_view(SEAT, opponent=OPPONENT)
    record = only(
        decision_rows([_decide(1, "choose_targets", targets=[OPPONENT], state_view=view)], SEAT)
    )
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer == {"decision": "choose_targets", "targets": [{"player": "opponent"}]}


def test_decide_choose_targets_card_target_resolves_side_and_zone():
    theirs = kit.card("Theirs", type_line="Creature — Bear", controller=OPPONENT, zone="graveyard")
    view = kit.state_view(SEAT, opponent=OPPONENT, graveyards={OPPONENT: [theirs]})
    record = only(
        decision_rows([_decide(1, "choose_targets", targets=[theirs["id"]], state_view=view)], SEAT)
    )
    play = play_of(record)
    target = play["moves"][0]["answers"][0]["targets"][0]
    assert target["side"] == "opponent"
    assert target["zone"] == "graveyard"


# -- every decide row names its own pending decision (FD4 (f)) ------------------


def test_decide_answered_off_priority_gives_a_pending_decision_condition():
    view = kit.state_view(SEAT, opponent=OPPONENT)
    view["priority_holder"] = OPPONENT
    record = only(decision_rows([_decide(1, "may", accept=True, state_view=view)], SEAT))
    play = play_of(record)
    assert play["true"] == [{"kind": "pending_decision", "decision": "may"}]
    assert play["moves"][0]["answers"][0] == {"decision": "may", "accept": True}


def test_decide_answered_with_priority_also_carries_the_pending_decision_condition():
    # FD4 (f): unconditional now - a standalone answer play only ever runs
    # as a direct answer to a decision already on the table, regardless of
    # whether the seat happened to hold priority when it answered.
    view = kit.state_view(SEAT, opponent=OPPONENT)
    view["priority_holder"] = SEAT
    record = only(decision_rows([_decide(1, "may", accept=True, state_view=view)], SEAT))
    play = play_of(record)
    assert play["true"] == [{"kind": "pending_decision", "decision": "may"}]


# -- activate: FB2's M4 ability_index fallback ------------------------------------


def test_activate_with_blank_ability_text_uses_ability_index():
    blank = kit.card(
        "Blank",
        type_line="Artifact",
        controller=SEAT,
        abilities=(("", False),),
    )
    view = _view_with_battlefield_card(blank)
    entries = [row(1, "activate", card_id=blank["id"], ability_index=0, state_view=view)]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["ability_index"] == 0
    assert "ability_text_has" not in move


def test_decide_declare_attackers_empty_pairs_gives_a_no_attackers_move():
    # FD4 (g): `/decide`'s own `pairs` route becomes the same
    # `declare_attackers` move the dedicated `actions/attack` route gives -
    # an empty list needs no board data, same as the dedicated route's own.
    record = only(decision_rows([_decide(1, "declare_attackers", pairs=[])], SEAT))
    play = play_of(record)
    assert play["moves"] == [{"kind": "declare_attackers", "attackers": []}]
    assert play["true"] == [{"kind": "pending_decision", "decision": "declare_attackers"}]


def test_decide_declare_attackers_pairs_resolves_the_same_way_the_dedicated_route_does():
    bear = _attacker()
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[bear])
    record = only(
        decision_rows(
            [
                _decide(
                    1,
                    "declare_attackers",
                    pairs=[{"creature": bear["id"], "defender": OPPONENT}],
                    state_view=view,
                )
            ],
            SEAT,
        )
    )
    play = play_of(record)
    move = play["moves"][0]
    assert move["kind"] == "declare_attackers"
    assert move["attackers"][0]["count"] == 1
    assert "defender" not in move["attackers"][0]


def test_decide_declare_attackers_unresolved_pairs_is_the_attack_fallback():
    record = only(
        decision_rows(
            [_decide(1, "declare_attackers", pairs=[{"creature": "nope", "defender": OPPONENT}])],
            SEAT,
        )
    )
    assert record["dsl"] is None
    assert record["unexpressible"] == "attack"


def test_decide_declare_blockers_pairs_becomes_a_declare_blockers_move():
    bear = _attacker(controller=OPPONENT)
    blocker = _attacker()
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[bear, blocker])
    record = only(
        decision_rows(
            [
                _decide(
                    1,
                    "declare_blockers",
                    pairs=[{"creature": blocker["id"], "defender": bear["id"]}],
                    state_view=view,
                )
            ],
            SEAT,
        )
    )
    play = play_of(record)
    assert play["moves"] == [
        {
            "kind": "declare_blockers",
            "blocks": [
                {
                    "blocker": {
                        "select": play["moves"][0]["blocks"][0]["blocker"]["select"],
                        "count": 1,
                    },
                    "attacker": {"select": play["moves"][0]["blocks"][0]["attacker"]["select"]},
                }
            ],
        }
    ]


def test_decide_mulligan_gives_a_plain_accept_answer():
    # FD4 (h)/G4: the engine's own pregame `decide` flow for mulligan (as
    # opposed to `mulligan_declared`'s own dedicated action row) answers
    # with a plain `accept` bool, the same footing `may`/`pay_life` stand
    # on - built from the schema-only `mulligan` step since this row, like
    # every pregame decision, carries no `turn`/`step` of its own.
    record = only(decision_rows([_decide(1, "mulligan", accept=True)], SEAT))
    play = play_of(record)
    assert play["when"] == {"whose_turn": "either", "steps": ["mulligan"], "stack": "empty"}
    assert play["moves"] == [
        {"kind": "answer", "answers": [{"decision": "mulligan", "accept": True}]}
    ]


def test_decide_mulligan_without_a_bool_accept_is_unexpressible():
    record = only(decision_rows([_decide(1, "mulligan", accept=None)], SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "decide:mulligan"


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


def test_compact_board_stays_at_or_under_the_char_cap_with_a_long_stack():
    # The battlefield is empty - the loop used to stop as soon as every
    # battlefield list was empty, even though the stack alone still pushed
    # the board over the cap.
    stack = [kit.card(f"Stack Spell {i}", type_line="Instant", zone="stack") for i in range(20)]
    view = kit.state_view(SEAT, opponent=OPPONENT, stack=stack)
    board = compact_board(view)
    size = len(json.dumps(board, sort_keys=True))
    assert size <= decision_dsl.COMPACT_BOARD_CHARS
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


# -- FD4 (b)/G1/G16: the pre-action frame ----------------------------------------


def test_cast_reads_the_pre_action_frame_not_the_rows_own_post_action_one():
    # The card is in hand in the *earlier* row's own frame, and already on
    # the stack in the cast row's own (post-action) frame - the zone must
    # come from the earlier one.
    bolt = kit.bolt(zone="hand", controller=SEAT)
    pre_view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    on_stack = dict(bolt, zone="stack")
    post_view = kit.state_view(SEAT, opponent=OPPONENT, stack=[on_stack])
    entries = [
        row(1, "pass_priority", state_view=pre_view),
        row(2, "cast", card=str(bolt["id"]), from_zone="hand", state_view=post_view),
    ]
    out = decision_rows(entries, SEAT)
    cast_record = out[1]
    play = play_of(cast_record)
    assert play["moves"][0]["card"]["zone"] == "hand"


def test_activate_off_zone_reads_the_pre_action_zone():
    # A channel-style activation from hand: the card has already left hand
    # by the time the row's own (post-action) frame is taken, but the
    # source's zone must read as it stood before the activation.
    channel_card = kit.card("Channeler", type_line="Creature", controller=SEAT, abilities=("cost",))
    pre_view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [channel_card]})
    post_view = kit.state_view(SEAT, opponent=OPPONENT, exile=[dict(channel_card, zone="exile")])
    entries = [
        row(1, "pass_priority", state_view=pre_view),
        row(2, "activate", card_id=channel_card["id"], ability_index=0, state_view=post_view),
    ]
    out = decision_rows(entries, SEAT)
    play = play_of(out[1])
    assert play["moves"][0]["source"]["zone"] == "hand"


def test_pass_whose_turn_reads_the_pre_action_active_player():
    # FD4 (i)/G16: a pass that ends the turn must not read back as having
    # been made on the turn it produced.
    entries = [
        row(1, "cast", card="irrelevant", active_player=SEAT),
        row(2, "pass_priority", active_player=OPPONENT),
    ]
    out = decision_rows(entries, SEAT)
    play = play_of(out[1])
    assert play["when"]["whose_turn"] == "mine"


# -- FD4 (d): keyword-action routes -----------------------------------------------


def test_suspend_gives_a_keyword_action_move():
    spell = kit.card("Spell", type_line="Instant", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [spell]})
    record = only(decision_rows([row(1, "suspend", card=str(spell["id"]), state_view=view)], SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move == {
        "kind": "keyword_action",
        "action": "suspend",
        "card": {"zone": "hand", "select": move["card"]["select"]},
    }
    assert "instant" in move["card"]["select"].get("types_all", [])


def test_suspend_unresolved_card_is_unresolved():
    record = only(decision_rows([row(1, "suspend", card="nope")], SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "suspend:unresolved"


def test_equip_gives_a_target_creature():
    equipment = kit.card("Sword", type_line="Artifact — Equipment", controller=SEAT)
    creature = kit.creature("Bear", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[equipment, creature])
    entries = [
        row(1, "equip", card_id=str(equipment["id"]), target=str(creature["id"]), state_view=view)
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["action"] == "equip"
    assert move["target"]["side"] == "me"


def test_equip_unresolved_target_is_unresolved_target():
    equipment = kit.card("Sword", type_line="Artifact — Equipment", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[equipment])
    entries = [row(1, "equip", card_id=str(equipment["id"]), target="nope", state_view=view)]
    record = only(decision_rows(entries, SEAT))
    assert record["unexpressible"] == "equip:unresolved-target"


def test_crew_gives_a_crew_with_pick():
    vehicle = kit.card("Ship", type_line="Artifact — Vehicle", controller=SEAT)
    crewer = kit.creature("Bear", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[vehicle, crewer])
    entries = [
        row(
            1,
            "crew",
            card_id=str(vehicle["id"]),
            crewing=[str(crewer["id"])],
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["action"] == "crew"
    assert move["crew_with"]["count"] == 1


# -- FD4 (h)/G10/G11: order_triggers and assign_combat_damage --------------------


def test_decide_order_triggers_gives_an_order():
    one = kit.creature("Trigger One", controller=SEAT)
    two = kit.creature("Trigger Two", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[one, two])
    record = only(
        decision_rows(
            [_decide(1, "order_triggers", order=[one["id"], two["id"]], state_view=view)], SEAT
        )
    )
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer["decision"] == "order_triggers"
    assert len(answer["order"]) == 2


def test_decide_order_triggers_unresolved_is_unexpressible():
    record = only(decision_rows([_decide(1, "order_triggers", order=["nope"])], SEAT))
    assert record["dsl"] is None
    assert record["unexpressible"] == "decide:order_triggers"


def test_decide_assign_combat_damage_gives_a_damage_policy_with_rest_to_player():
    blocker = kit.creature("Blocker", controller=OPPONENT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[blocker])
    damage = [{"target": blocker["id"], "amount": 2}, {"target": OPPONENT, "amount": 1}]
    record = only(
        decision_rows([_decide(1, "assign_combat_damage", damage=damage, state_view=view)], SEAT)
    )
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer["decision"] == "assign_combat_damage"
    assert len(answer["damage"]["tiers"]) == 1
    assert answer["damage"]["rest_to"] == "player"


def test_decide_assign_combat_damage_without_trample_has_no_rest_to():
    blocker = kit.creature("Blocker", controller=OPPONENT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[blocker])
    damage = [{"target": blocker["id"], "amount": 2}]
    record = only(
        decision_rows([_decide(1, "assign_combat_damage", damage=damage, state_view=view)], SEAT)
    )
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert "rest_to" not in answer["damage"]


def test_decide_assign_combat_damage_unresolved_target_is_unexpressible():
    record = only(
        decision_rows(
            [_decide(1, "assign_combat_damage", damage=[{"target": "nope", "amount": 1}])], SEAT
        )
    )
    assert record["dsl"] is None
    assert record["unexpressible"] == "decide:assign_combat_damage"


# -- FD4 (h)/G14: choose-zero answers ---------------------------------------------


def test_decide_choose_cards_empty_is_a_choose_zero_pick():
    record = only(decision_rows([_decide(1, "choose_cards", card_ids=[])], SEAT))
    play = play_of(record)
    assert play["moves"][0]["answers"][0] == {
        "decision": "choose_cards",
        "pick": {"select": {}, "count": 0},
    }


def test_decide_choose_targets_empty_is_a_choose_zero_targets():
    record = only(decision_rows([_decide(1, "choose_targets", targets=[])], SEAT))
    play = play_of(record)
    assert play["moves"][0]["answers"][0] == {"decision": "choose_targets", "targets": []}


def test_decide_choose_players_empty_is_a_choose_zero_players():
    record = only(decision_rows([_decide(1, "choose_players", usernames=[])], SEAT))
    play = play_of(record)
    assert play["moves"][0]["answers"][0] == {"decision": "choose_players", "players": []}


# -- FD4 (h)/G15: choice_from naming a card ---------------------------------------


def test_decide_choose_a_quality_naming_a_card_gives_choice_from():
    named = kit.creature("Meddling Target", controller=OPPONENT, zone="hand")
    view = kit.state_view(SEAT, opponent=OPPONENT, known={"hand": []}, hands={OPPONENT: [named]})
    record = only(
        decision_rows([_decide(1, "choose_a_quality", choice=named["name"], state_view=view)], SEAT)
    )
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer["decision"] == "choose_a_quality"
    assert "choice_from" in answer
    assert "choice" not in answer
    assert answer["choice_from"]["side"] == "opponent"


def test_decide_choose_a_quality_not_naming_a_card_keeps_plain_choice():
    record = only(decision_rows([_decide(1, "choose_a_quality", choice="Merfolk")], SEAT))
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer == {"decision": "choose_a_quality", "choice": "Merfolk"}


def test_decide_choose_a_mode_never_gets_choice_from():
    named = kit.creature("Some Card", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[named])
    record = only(
        decision_rows([_decide(1, "choose_a_mode", choice=named["name"], state_view=view)], SEAT)
    )
    play = play_of(record)
    answer = play["moves"][0]["answers"][0]
    assert answer == {"decision": "choose_a_mode", "choice": named["name"]}


# -- FD4 (c): cast/activate rich fields (read from FD5's future logging) --------


def test_cast_reads_x_and_targets_when_the_row_carries_them():
    bolt = kit.bolt(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    entries = [
        row(
            1,
            "cast",
            card=str(bolt["id"]),
            from_zone="hand",
            x_value=3,
            targets=[OPPONENT],
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["x"] == 3
    assert move["targets"] == [{"player": "opponent"}]


def test_cast_reads_keyword_cost_and_optional_costs():
    # `optional_costs` is logged as `OptionalCostChoice` dicts
    # (`{"keyword": "Kicker", ...}`), never bare strings - `modes` is a list
    # of mode *indices* with no board-view text to resolve them against, so
    # it is never emitted at all (see `_add_cast_fields`'s own docstring).
    bolt = kit.bolt(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    entries = [
        row(
            1,
            "cast",
            card=str(bolt["id"]),
            from_zone="hand",
            keyword="Flashback",
            modes=[0, 1],
            optional_costs=[{"keyword": "Kicker", "times": 1}],
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["cost"] == "flashback"
    assert "modes" not in move
    assert move["optional_costs"] == [{"text_has": "Kicker"}]


def test_cast_alternative_cost_gives_the_printed_cost():
    bolt = kit.bolt(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    entries = [
        row(
            1,
            "cast",
            card=str(bolt["id"]),
            from_zone="hand",
            alternative_cost=0,
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    assert play["moves"][0]["cost"] == "printed"


def test_cast_reads_convoke_pick_from_convoke_choices():
    # `CastRequest.convoke` is a list of `{"card_id": ..., "color": ...}`
    # dicts, one per creature tapped - never a bare id list.
    bolt = kit.bolt(zone="hand", controller=SEAT)
    helper = kit.creature("Helper", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]}, battlefield=[helper])
    entries = [
        row(
            1,
            "cast",
            card=str(bolt["id"]),
            from_zone="hand",
            convoke=[{"card_id": helper["id"], "color": "G"}],
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    assert play["moves"][0]["convoke"]["count"] == 1


def test_cast_reads_sacrificing_discarding_exiling_tapping_as_cost_object_picks():
    bolt = kit.bolt(zone="hand", controller=SEAT)
    fodder = kit.creature("Fodder", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]}, battlefield=[fodder])
    entries = [
        row(
            1,
            "cast",
            card=str(bolt["id"]),
            from_zone="hand",
            sacrificing=[fodder["id"]],
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    assert play["moves"][0]["sacrifice"]["count"] == 1


def test_activate_reads_sacrificing_pick_and_color():
    # `ActivateRequest` logs `sacrificing`, not `sacrifice` - the ledger's
    # own field name, not the DSL's `ActivateMove.sacrifice` field name.
    fetch = kit.fetchland(zone="battlefield", controller=SEAT)
    other = kit.card("Other Land", type_line="Land", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, battlefield=[fetch, other])
    entries = [
        row(
            1,
            "activate",
            card_id=fetch["id"],
            ability_index=0,
            sacrificing=[other["id"]],
            color="R",
            state_view=view,
        )
    ]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    move = play["moves"][0]
    assert move["sacrifice"]["count"] == 1
    assert move["color"] == "R"


# -- FD4 (e): a paused row still converts, committing like any other ------------


def test_paused_cast_still_converts_like_an_ordinary_committing_row():
    bolt = kit.bolt(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    entries = [row(1, "cast", card=str(bolt["id"]), from_zone="hand", paused=True, state_view=view)]
    record = only(decision_rows(entries, SEAT))
    play = play_of(record)
    assert play["moves"][0]["kind"] == "cast"


# -- FD4 (f)/G2: folding a decide into the preceding move's own object ----------


def test_decide_resolving_a_paused_casts_own_targets_folds_into_it():
    bolt = kit.bolt(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    entries = [
        row(1, "cast", card=str(bolt["id"]), from_zone="hand", paused=True, state_view=view),
        row(2, "decide", kind="choose_targets", targets=[OPPONENT], state_view=view),
    ]
    out = decision_rows(entries, SEAT)
    cast_record, decide_record = out
    cast_play = play_of(cast_record)
    decide_play = play_of(decide_record)
    assert cast_play["moves"][0]["kind"] == "cast"
    assert cast_play["moves"][0]["answers"] == [
        {"decision": "choose_targets", "targets": [{"player": "opponent"}]}
    ]
    # Both rows now show the same, enriched play - a dedup-by-DSL reader
    # (report.py) treats them as the one decision they always were.
    assert decide_play == cast_play


def test_fold_stops_when_the_decide_cannot_be_resolved():
    bolt = kit.bolt(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    entries = [
        row(1, "cast", card=str(bolt["id"]), from_zone="hand", paused=True, state_view=view),
        # Names a card id that resolves in neither this row's own frame nor
        # (via the fold) the cast's earlier one - genuinely unresolvable,
        # unlike the choose-zero case.
        row(2, "decide", kind="choose_targets", targets=["00000000-0000-4000-8000-000000009999"]),
    ]
    out = decision_rows(entries, SEAT)
    cast_record, decide_record = out
    assert cast_record["dsl"]["moves"][0].get("answers", []) == []
    assert decide_record["dsl"] is None
    assert decide_record["unexpressible"] == "decide:choose_targets"


def test_a_move_that_did_not_pause_never_opens_a_fold():
    bolt = kit.bolt(zone="hand", controller=SEAT)
    view = kit.state_view(SEAT, opponent=OPPONENT, hands={SEAT: [bolt]})
    entries = [
        row(1, "cast", card=str(bolt["id"]), from_zone="hand", state_view=view),
        row(2, "decide", kind="choose_targets", targets=[OPPONENT], state_view=view),
    ]
    out = decision_rows(entries, SEAT)
    cast_record, decide_record = out
    assert cast_record["dsl"]["moves"][0].get("answers", []) == []
    assert decide_record["dsl"]["moves"][0]["answers"][0]["decision"] == "choose_targets"
    # The decide got its own standalone play, distinct from the cast's.
    assert decide_record["dsl"] != cast_record["dsl"]


# -- FD4 (j)/G17: a script-driven row is not counted twice -----------------------


def test_a_row_inside_a_run_scripts_span_is_skipped():
    from magic_cards_model_api.audit import SCRIPT_RUN_ACTION, SCRIPT_RUN_STARTED_ACTION

    entries = [
        row(1, SCRIPT_RUN_STARTED_ACTION, run_id="r1", script_id="p1", seat=SEAT),
        row(2, "pass_priority"),  # sent by the script, inside its own span
        row(
            3,
            SCRIPT_RUN_ACTION,
            run_id="r1",
            seq_start=1,
            seq_end=3,
            exit="chain_complete",
            seat=SEAT,
        ),
    ]
    out = decision_rows(entries, SEAT)
    actions = [r["action"] for r in out]
    # The finish row (`script_run`) is not itself one of `_ACTION_KIND`'s
    # recognized kinds - only the started row ever becomes its own
    # `run_script` record; the manual `pass` inside its span is skipped.
    assert actions == ["run_script"]


def test_a_row_with_the_runner_reasoning_suffix_is_skipped_even_outside_a_span():
    entries = [
        row(1, "pass_priority", reasoning="the plan | script p1@abc123 move 1/1 (pass)"),
    ]
    out = decision_rows(entries, SEAT)
    assert out == []
