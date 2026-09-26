"""FD9's lint half: every per-move rule (card-name ban, selector rules,
`phrase-too-narrow` with the `ability_index` exemption, the answer-shape
rules) applies to a `repeat`'s own body moves; the new `repeat-until-static`
warning; and the move-selector conflict dimension reading a `repeat`'s first
body move as the play's first move (FD2 item 7b, extended by FD9 item 2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from playscript_testkit import make_root, play, put

from magic_cards_model_api.playscripts.lint import check_report, exclusivity

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "playscripts"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return make_root(tmp_path)


def error_codes(root: Path) -> list[str]:
    return sorted(e.code for e in check_report(root)[0])


def warning_codes(root: Path) -> list[str]:
    return sorted(w.code for w in check_report(root)[1])


def one_activate_body() -> list[dict[str, Any]]:
    return [
        {"kind": "activate", "source": {"zone": "battlefield", "select": {}}, "ability_index": 0}
    ]


def repeat_move(body: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    move: dict[str, Any] = {
        "kind": "repeat",
        "moves": body,
        "until": [{"kind": "mana_pool", "op": ">=", "n": 4}],
        "max_iterations": 5,
    }
    move.update(over)
    return move


# -- the card-name ban applies to body moves ----------------------------------


def test_card_name_in_a_repeat_body_selector_is_refused(root):
    """Arid Mesa is a real cached card (`playscript_testkit.CARDS`); naming
    it inside a repeat's own body must be caught the same way naming it
    anywhere else in the file already is."""
    body = [
        {
            "kind": "activate",
            "source": {"zone": "battlefield", "select": {"ability_text_has": "Arid Mesa"}},
            "ability_index": 0,
        }
    ]
    doc = play(moves=[repeat_move(body)])
    put(root, doc)
    assert "card-name-in-field" in error_codes(root)


# -- phrase-too-narrow applies to body moves, with the ability_index exemption -


def test_phrase_too_narrow_applies_inside_a_repeat_body(root):
    body = [
        {
            "kind": "activate",
            "source": {
                "zone": "battlefield",
                "select": {"ability_text_has": "This exact narrow phrase names one card"},
            },
            "ability_text_has": "This exact narrow phrase names one card",
        }
    ]
    doc = play(moves=[repeat_move(body)])
    put(root, doc)
    assert "phrase-too-narrow" in error_codes(root)


def test_ability_index_exempts_a_repeat_body_move_from_phrase_too_narrow(root):
    """FD9 item 2: the FD2 `ability_index` exemption (item 7a) applies at
    whatever nesting the `activate` move sits at - here inside a repeat's own
    body, `moves[0].moves[0]`."""
    body = [
        {
            "kind": "activate",
            "source": {
                "zone": "battlefield",
                "select": {"ability_text_has": "This exact narrow phrase names one card"},
            },
            "ability_index": 0,
        }
    ]
    doc = play(moves=[repeat_move(body)])
    put(root, doc)
    assert check_report(root) == ([], [])


def test_ability_index_exemption_does_not_leak_to_a_sibling_body_move(root):
    """The exemption is scoped to the move that actually names `ability_index`
    - a second body move in the same repeat that still names its ability by
    `ability_text_has` keeps the 3-card rule."""
    body = [
        {
            "kind": "activate",
            "source": {"zone": "battlefield", "select": {}},
            "ability_index": 0,
        },
        {
            "kind": "activate",
            "source": {
                "zone": "battlefield",
                "select": {"ability_text_has": "This exact narrow phrase names one card"},
            },
            "ability_text_has": "This exact narrow phrase names one card",
        },
    ]
    doc = play(moves=[repeat_move(body)])
    put(root, doc)
    assert "phrase-too-narrow" in error_codes(root)


# -- the answer-shape rules apply to a repeat body move's own answers --------


def test_answer_shape_mismatch_inside_a_repeat_body_is_refused(root):
    body = [
        {
            "kind": "activate",
            "source": {"zone": "battlefield", "select": {}},
            "ability_index": 0,
            "answers": [{"decision": "pay_life_amount", "accept": True}],
        }
    ]
    doc = play(moves=[repeat_move(body)])
    put(root, doc)
    assert "answer-shape-mismatch" in error_codes(root)


# -- repeat-until-static -------------------------------------------------------


def test_repeat_until_static_when_until_reads_only_turn_and_input(root):
    body = one_activate_body()
    move = repeat_move(body, until=[{"kind": "turn", "op": ">=", "n": 3}])
    doc = play(moves=[move])
    put(root, doc)
    errors, warnings = check_report(root)
    assert errors == []
    assert "repeat-until-static" in [w.code for w in warnings]


def test_repeat_until_static_fires_when_every_until_condition_is_turn_or_input(root):
    body = one_activate_body()
    move = repeat_move(
        body,
        until=[
            {"kind": "turn", "op": ">=", "n": 1},
            {"kind": "input", "name": "opponent_clock", "op": "<", "value": 3},
        ],
    )
    doc = play(moves=[move])
    put(root, doc)
    assert "repeat-until-static" in warning_codes(root)


def test_repeat_until_static_does_not_fire_when_a_board_condition_is_present(root):
    """`mana_pool` reads the board, which the body's own activations change -
    no warning."""
    body = one_activate_body()
    move = repeat_move(
        body,
        until=[
            {"kind": "turn", "op": ">=", "n": 1},
            {"kind": "mana_pool", "op": ">=", "n": 4},
        ],
    )
    doc = play(moves=[move])
    put(root, doc)
    assert "repeat-until-static" not in warning_codes(root)


def test_repeat_until_static_never_fails_the_gate(root):
    """A warning, never an error - the gate still exits 0."""
    from magic_cards_model_api import seat_cli

    body = one_activate_body()
    move = repeat_move(body, until=[{"kind": "turn", "op": ">=", "n": 1}])
    doc = play(moves=[move])
    put(root, doc)
    assert seat_cli.run(["plays", "check", "--root", str(root)]) == 0


# -- move-selector conflict dimension: a repeat's first body move -------------


def test_repeat_first_body_move_and_a_plain_play_are_exclusive_on_disjoint_selectors(root):
    """FD9 item 2: the move-selector dimension reads a repeat's own first
    body move as the play's first move, so an Instant-only repeat body and a
    Creature-only plain play are exclusive the same way two plain plays with
    those selectors already are (FD2 item 7b)."""
    a_fragment = {
        "moves": [
            repeat_move(
                [
                    {
                        "kind": "cast",
                        "card": {"zone": "hand", "select": {"types_all": ["Instant"]}},
                    }
                ]
            )
        ]
    }
    b_fragment = {
        "moves": [
            {"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Creature"]}}}
        ]
    }
    assert exclusivity(a_fragment, b_fragment, {}) == ["move_selector"]
    assert exclusivity(b_fragment, a_fragment, {}) == ["move_selector"]


def test_repeat_first_body_move_and_a_plain_play_conflict_on_overlapping_selectors(root):
    a_fragment = {
        "moves": [
            repeat_move(
                [
                    {
                        "kind": "activate",
                        "source": {"zone": "battlefield", "select": {"types_all": ["Creature"]}},
                        "ability_index": 0,
                    }
                ]
            )
        ]
    }
    b_fragment = {
        "moves": [
            {
                "kind": "activate",
                "source": {"zone": "battlefield", "select": {"types_all": ["Creature"]}},
                "ability_index": 1,
            }
        ]
    }
    assert exclusivity(a_fragment, b_fragment, {}) == []


def test_two_repeat_plays_with_disjoint_first_body_moves_are_exclusive(root):
    a_fragment = {
        "moves": [
            repeat_move(
                [{"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Instant"]}}}]
            )
        ]
    }
    b_fragment = {
        "moves": [
            repeat_move(
                [{"kind": "cast", "card": {"zone": "hand", "select": {"types_all": ["Creature"]}}}]
            )
        ]
    }
    assert exclusivity(a_fragment, b_fragment, {}) == ["move_selector"]


# -- FD9's two proof fixtures: lint clean --------------------------------------


@pytest.mark.parametrize("name", ["repeat_devoted_druid.json", "repeat_walking_ballista.json"])
def test_proof_fixture_lints_clean(root, name: str):
    doc = json.loads((FIXTURES / name).read_text())
    put(root, doc)
    assert check_report(root) == ([], [])


def test_a_repeat_play_check_report_with_a_real_plain_play_conflict(root):
    put(
        root,
        play(
            id="repeat-play",
            priority=10,
            moves=[
                repeat_move(
                    [
                        {
                            "kind": "activate",
                            "source": {
                                "zone": "battlefield",
                                "select": {"types_all": ["Creature"]},
                            },
                            "ability_index": 0,
                        }
                    ]
                )
            ],
        ),
    )
    put(
        root,
        play(
            id="plain-play",
            priority=10,
            moves=[
                {
                    "kind": "activate",
                    "source": {"zone": "battlefield", "select": {"types_all": ["Creature"]}},
                    "ability_index": 1,
                }
            ],
        ),
    )
    errors, warnings = check_report(root)
    assert errors == []
    assert any(w.code == "play-conflict" for w in warnings)
