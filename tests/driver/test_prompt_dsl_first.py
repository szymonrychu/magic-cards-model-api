"""FD10 (b): the DSL-first rule in the driver's seat prompt and the review
prompt.

`prompt.py`'s seat prompt (`turn_zero`, mirrored into `advisor_turn_zero`)
states that any multi-step or repetitive sequence is written as a
play-script and run with `run_script` - inline (`body=`) or staged first
(`stage_script`) - and names FD9's `repeat` for loops; the no-autofire
sentence (`run_script` only runs when the seat starts it) stays. `review.py`'s
review prompt names a long manual call chain as a gap owed a play or a
`plays no-change`, and lists ad-hoc scripts with the ask to generalize and
admit them.
"""

from __future__ import annotations

from mtg_agent.driver.guides import NONE, Identification, parse_decklist
from mtg_agent.driver.prompt import Assignment, advisor_turn_zero, turn_zero
from mtg_agent.driver.review import (
    ADHOC_SCRIPTS_HEADING,
    MANUAL_CHAINS_HEADING,
    review_prompt,
)

DECKLIST = "4 Psychic Frog\n4 Fatal Push\n"


def assignment(**over) -> Assignment:
    base = dict(
        game_id="g-1",
        uid="secret-uid",
        player="alice",
        opponent="bob",
        api_base="http://game:8000",
        decklist=parse_decklist(DECKLIST),
        identification=Identification(NONE),
    )
    base.update(over)
    return Assignment(**base)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + len(heading) + 1)
    return text[start : end if end != -1 else len(text)]


NO_AUTOFIRE_SENTENCE = (
    "Scripts only run when you start them with `run_script` - arming on a wait never runs "
    "one, only judges: it marks whichever armed play applies first in `scripts.applicable` "
    "with its own `call` ready, and a run you did not start yourself was started by someone "
    "else."
)


# -- prompt.py: the seat prompt's own DSL-first rule -------------------------


def test_seat_prompt_scripts_section_states_the_dsl_first_rule(tmp_path):
    text = turn_zero(assignment(), tmp_path / "current-game.md")
    section = _flat(_section(text, "## Scripts"))
    assert "DSL-first" in section
    assert "multi-step" in section or "repetitive" in section


def test_seat_prompt_names_run_scripts_inline_body_path(tmp_path):
    text = turn_zero(assignment(), tmp_path / "current-game.md")
    section = _flat(_section(text, "## Scripts"))
    assert "`run_script`" in section
    assert "`body`" in section
    assert "inline play document" in section


def test_seat_prompt_names_stage_script(tmp_path):
    text = turn_zero(assignment(), tmp_path / "current-game.md")
    section = _flat(_section(text, "## Scripts"))
    assert "`stage_script`" in section


def test_seat_prompt_names_repeat_for_loops(tmp_path):
    text = turn_zero(assignment(), tmp_path / "current-game.md")
    section = _flat(_section(text, "## Scripts"))
    assert "`repeat`" in section


def test_seat_prompt_keeps_manual_calls_for_one_off_decisions(tmp_path):
    text = turn_zero(assignment(), tmp_path / "current-game.md")
    section = _flat(_section(text, "## Scripts"))
    assert "one-off decision" in section


def test_seat_prompt_still_carries_the_no_autofire_sentence(tmp_path):
    text = turn_zero(assignment(), tmp_path / "current-game.md")
    section = _flat(_section(text, "## Scripts"))
    assert NO_AUTOFIRE_SENTENCE in section


def test_buddy_prompt_also_states_the_dsl_first_rule(tmp_path):
    """Mirrored into the advisor prompt, exactly like every other doctrine
    in this shared "## Scripts" section - an advisor can run scripts too."""
    text = advisor_turn_zero(assignment(), tmp_path / "current-game.md")
    section = _flat(_section(text, "## Scripts"))
    assert "DSL-first" in section
    assert "`repeat`" in section
    assert "`stage_script`" in section


def test_buddy_prompt_still_carries_the_no_autofire_sentence(tmp_path):
    text = advisor_turn_zero(assignment(), tmp_path / "current-game.md")
    section = _flat(_section(text, "## Scripts"))
    assert NO_AUTOFIRE_SENTENCE in section


# -- review.py: manual chains and ad-hoc scripts are gaps, never dropped ----


def review_record(**over) -> dict:
    base = {
        "players": ["alice", "bob"],
        "winner": "alice",
        "drawn": False,
        "declarations": [],
        "advice": [],
        "decisions": {},
        "play_stats": {},
        "adhoc_scripts": {},
        "entries": [],
    }
    base.update(over)
    return base


def test_the_dsl_submission_ask_names_manual_chains_and_ad_hoc_scripts_as_gaps():
    text = review_prompt(assignment(), "no reflection", review_record())
    section = _flat(text)
    assert "manual chain" in section.lower()
    assert "ad-hoc script" in section.lower()
    assert "plays no-change" in section


def test_a_manual_chain_is_listed_and_named_a_gap():
    record = review_record(
        play_stats={
            "alice": {
                "manual_chains": [
                    {
                        "seqs": [10, 11, 12, 13],
                        "kinds": ["activate"] * 4,
                        "turn": 5,
                        "step": "precombat_main",
                    }
                ]
            }
        }
    )
    text = review_prompt(assignment(), "no reflection", record)
    assert MANUAL_CHAINS_HEADING in text
    assert "#10-#13" in text
    assert "decision #10-#13" in text or "decisions #10-#13" in text


def test_an_ad_hoc_script_that_ran_is_listed_with_the_ask_to_generalize_it():
    record = review_record(
        adhoc_scripts={
            "alice": [
                {
                    "id": "adhoc-1-alice",
                    "turn": 5,
                    "step": "precombat_main",
                    "play": {"id": "adhoc-1-alice"},
                    "reasoning": "looping the mana rocks to reach X=8",
                    "warnings": [],
                    "runs": [{"run_id": "run-x", "exit": "chain_complete", "moves_done": 25}],
                }
            ]
        }
    )
    text = review_prompt(assignment(), "no reflection", record)
    assert ADHOC_SCRIPTS_HEADING in text
    assert "adhoc-1-alice" in text
    assert "looping the mana rocks" in text


def _section_rendered(text: str, heading: str) -> bool:
    """Whether `heading` opens an actual listing section (its own line,
    followed by a blank line and a bullet) - never merely mentioned inline
    inside the DSL submission ask's own prose, which quotes both headings by
    name regardless of whether either section was ever rendered."""
    return f"\n{heading}\n\n-" in text


def test_an_ad_hoc_script_that_never_ran_is_not_listed():
    record = review_record(
        adhoc_scripts={
            "alice": [
                {
                    "id": "adhoc-1-alice",
                    "turn": 5,
                    "step": "precombat_main",
                    "play": {},
                    "reasoning": "r",
                    "warnings": [],
                    "runs": [],
                }
            ]
        }
    )
    text = review_prompt(assignment(), "no reflection", record)
    assert not _section_rendered(text, ADHOC_SCRIPTS_HEADING)
    assert "adhoc-1-alice" not in text


def test_no_manual_chains_or_ad_hoc_scripts_means_neither_section_is_rendered():
    text = review_prompt(assignment(), "no reflection", review_record())
    assert not _section_rendered(text, MANUAL_CHAINS_HEADING)
    assert not _section_rendered(text, ADHOC_SCRIPTS_HEADING)


def test_another_seats_manual_chains_are_not_shown_under_this_seats_review():
    record = review_record(
        play_stats={
            "bob": {
                "manual_chains": [
                    {"seqs": [1, 2, 3, 4], "kinds": ["activate"] * 4, "turn": 1, "step": "main"}
                ]
            }
        }
    )
    text = review_prompt(assignment(), "no reflection", record)
    assert not _section_rendered(text, MANUAL_CHAINS_HEADING)
