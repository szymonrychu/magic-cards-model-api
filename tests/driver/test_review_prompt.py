from __future__ import annotations

from mtg_agent.driver.guides import NONE, Identification, parse_decklist
from mtg_agent.driver.prompt import Assignment
from mtg_agent.driver.review import MAX_DECLARATIONS, review_prompt

DECKLIST = "4 Psychic Frog\n4 Fatal Push\n"


def _assignment(**overrides) -> Assignment:
    fields = dict(
        game_id="g-1",
        uid="secret-uid",
        player="bob",
        opponent="alice",
        api_base="http://game:8000",
        decklist=parse_decklist(DECKLIST),
        identification=Identification(NONE),
    )
    fields.update(overrides)
    return Assignment(**fields)


def test_review_prompt_names_the_players_and_the_outcome() -> None:
    text = review_prompt(
        _assignment(),
        "the Bolt on turn one was the whole game",
        {
            "players": ["bob", "alice"],
            "winner": "bob",
            "drawn": False,
            "declarations": [],
            "advice": [],
        },
    )
    assert "bob" in text and "alice" in text
    assert "bob won" in text
    assert "bob's own reflection" in text.lower()
    assert "the Bolt on turn one was the whole game" in text


def test_review_prompt_reports_a_draw() -> None:
    text = review_prompt(
        _assignment(),
        "notes",
        {
            "players": ["bob", "alice"],
            "winner": None,
            "drawn": True,
            "declarations": [],
            "advice": [],
        },
    )
    assert "draw" in text


def test_review_prompt_reports_an_unrecorded_outcome() -> None:
    text = review_prompt(
        _assignment(),
        "notes",
        {
            "players": ["bob", "alice"],
            "winner": None,
            "drawn": False,
            "declarations": [],
            "advice": [],
        },
    )
    assert "not recorded" in text


def test_review_prompt_falls_back_when_no_reflection_was_written() -> None:
    text = review_prompt(
        _assignment(),
        "   ",
        {
            "players": ["bob", "alice"],
            "winner": "bob",
            "drawn": False,
            "declarations": [],
            "advice": [],
        },
    )
    assert "left no reflection" in text


def test_review_prompt_summarises_declarations_and_advice() -> None:
    record = {
        "players": ["bob", "alice"],
        "winner": "bob",
        "drawn": False,
        "declarations": [
            {
                "turn": 3,
                "plan": "attack with everything",
                "prediction": "alice takes the damage",
                "did": [{"action": "attack"}],
                "opponent_did": [{"action": "pass_priority"}],
            }
        ],
        "advice": [
            {"summary": "cast Fatal Push on the Frog", "approved": True, "followed": False},
        ],
    }
    text = review_prompt(_assignment(), "notes", record)
    assert "attack with everything" in text
    assert "alice takes the damage" in text
    assert "did: attack" in text
    assert "opponent did: pass_priority" in text
    assert "cast Fatal Push on the Frog" in text
    assert "approved" in text and "not followed" in text


def test_review_prompt_caps_declarations_to_the_most_recent() -> None:
    declarations = [
        {"turn": n, "plan": f"plan {n}", "did": [], "opponent_did": []} for n in range(50)
    ]
    text = review_prompt(
        _assignment(),
        "notes",
        {
            "players": ["bob", "alice"],
            "winner": "bob",
            "drawn": False,
            "declarations": declarations,
            "advice": [],
        },
    )
    assert "plan 0" not in text
    assert f"plan {49}" in text
    assert text.count("- turn ") == MAX_DECLARATIONS


def test_review_prompt_falls_back_to_assignment_players_when_the_record_carries_none() -> None:
    text = review_prompt(_assignment(), "notes", {})
    assert "bob" in text and "alice" in text
    assert "not recorded" in text
