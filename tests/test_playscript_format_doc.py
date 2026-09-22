"""The play-script format doc (design D2) names the schema's closed sets.

`docs/superpowers/specs/2026-09-13-play-script-format-design.md` is the prose a
script author reads. Its `cast.cost` and `answers[].decision` tables must name
exactly what `playscripts.schema` accepts, so a cast option or decision kind
added to the schema fails here until the doc says so too.
"""

from __future__ import annotations

import re
from pathlib import Path

from magic_cards_model_api.playscripts.lint import MAX_ADVICE, MAX_PLAYS
from magic_cards_model_api.playscripts.runtime import Exit
from magic_cards_model_api.playscripts.schema import (
    AMOUNT_DECISION_KINDS,
    BASIC_LAND_TYPES,
    CAST_COSTS,
    CONDITION_KINDS,
    DECISION_KINDS,
    FORBIDDEN_DECISION_KINDS,
    MAX_ANSWERS,
    MAX_NOTE_LENGTH,
    MAX_PREFER,
    MAX_TARGETS,
    MISSING_FROM_BY,
    MISSING_FROM_SIDES,
    MISSING_FROM_ZONES,
    PAYABLE_STATES,
)
from magic_cards_model_api.playscripts.selectors import COLOR_LETTERS

DOC = (
    Path(__file__).resolve().parents[1]
    / "docs/superpowers/specs/2026-09-13-play-script-format-design.md"
)


def _listed(marker: str) -> list[str]:
    """The backticked words on the doc line starting with `marker`."""
    for line in DOC.read_text().splitlines():
        if line.startswith(marker):
            return re.findall(r"`([a-z_]+)`", line[len(marker) :])
    raise AssertionError(f"no line starting {marker!r} in {DOC.name}")


def test_doc_names_every_cast_cost_in_schema_order() -> None:
    assert _listed("- Cast costs:") == list(CAST_COSTS)


def test_doc_names_every_condition_kind_in_schema_order() -> None:
    assert _listed("- Condition kinds") == list(CONDITION_KINDS)


def test_doc_defines_the_turn_condition() -> None:
    text = DOC.read_text()
    for phrase in (
        "`{kind: turn, op, n}`",
        "`own_turns_taken`",
        "`TurnMachine.turn_number`",
        "rounds of the table",
        "CR 500.7",
        "never null",
    ):
        assert phrase in text


def test_doc_defines_searches_for_any_and_names_the_basic_land_types() -> None:
    text = DOC.read_text()
    for word in BASIC_LAND_TYPES:
        assert f"`{word}`" in text
    assert "`searches_for_any`" in text
    for phrase in (
        "search your library for a/an T (or T) card",
        "Expedition Map",
        "malformed-play",
    ):
        assert phrase in text.split("### `searches_for_any`", 1)[1].split("\n## ", 1)[0]


def test_doc_names_the_choice_decision_kinds() -> None:
    from magic_cards_model_api.playscripts.schema import CHOICE_DECISION_KINDS

    assert _listed("- Choice decision kinds:") == list(CHOICE_DECISION_KINDS)
    assert "choice?: string" in DOC.read_text()


def test_doc_defines_land_face() -> None:
    text = DOC.read_text()
    assert "`land_face`" in text
    section = text.split("### `land_face`", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "`faces`",
        "types_all: [Land]",
        "own `card_id`",
        "Card.land_face",
    ):
        assert phrase in section


def test_doc_names_every_enters_state_in_schema_order() -> None:
    from magic_cards_model_api.playscripts.schema import ENTERS_STATES

    assert _listed("- Enters states:") == list(ENTERS_STATES)


def test_doc_defines_enters_any() -> None:
    text = DOC.read_text()
    assert "`enters_any`" in text
    section = text.split("### `enters_any` (O4)", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "predict_enters",
        "hypo",
        "lethal",
        "HypotheticalRequired",
    ):
        assert phrase in section


def test_doc_defines_unlocks_unlocks_by_search_and_affordable() -> None:
    text = DOC.read_text()
    section = text.split(
        "### `unlocks`, `unlocks_by_search`, `unlocks_by_basic_search`, `affordable` (O7)", 1
    )[1].split("\n## ", 1)[0]
    for phrase in (
        "payability_matrix",
        "notice",
        "needs_contents",
        "untapped_mana",
        "mana_value",
        "unlocks_by_basic_search",
        "supertype",
        "Basic",
    ):
        assert phrase in section


def test_doc_names_every_payable_state_in_schema_order() -> None:
    assert _listed("- Payable-any words:") == list(PAYABLE_STATES)


def test_doc_defines_payable_any_and_unlocks_next() -> None:
    text = DOC.read_text()
    section = text.split("### `payable_any`, `unlocks_next` (T5)", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "earliest wins",
        "_has_a_payable_targeted_option",
        '"now"',
        '"with_a_land"',
        '"next_turn"',
        '"no"',
        "D-3",
        "land drop",
        "assuming_untapped",
        "before/after",
        "mana hypothetical",
        "not a turn prediction",
        "horizon=next_turn",
        "conditions.needs_next_turn",
        "D-6",
        "no `payable_any` key at all",
    ):
        assert phrase in section, phrase


def test_doc_marks_affordable_deprecated() -> None:
    text = DOC.read_text()
    section = text.split(
        "### `unlocks`, `unlocks_by_search`, `unlocks_by_basic_search`, `affordable` (O7)", 1
    )[1].split("\n## ", 1)[0]
    for phrase in ("DEPRECATED", "`payable_any`", "colour-blind"):
        assert phrase in section


def test_doc_names_every_answerable_decision_kind() -> None:
    assert _listed("- Decision kinds:") == list(DECISION_KINDS)


def test_doc_names_the_decision_kinds_a_script_never_answers() -> None:
    assert _listed("- Never answered:") == list(FORBIDDEN_DECISION_KINDS)


def test_cast_move_line_carries_the_optional_cost_and_bounds() -> None:
    text = DOC.read_text()
    assert (
        f"`cast {{card: {{zone, select, prefer?: [0-{MAX_PREFER} select], ties?: any}}, "
        f"cost?: <cast cost>, targets: [0-{MAX_TARGETS} <target>], mana?: auto, "
        f"answers: [0-{MAX_ANSWERS}]}}`"
    ) in text
    assert f"prefer?: [0-{MAX_PREFER} select]" in text


def test_o2_prefer_and_ties_are_on_play_land_activate_source_and_pick() -> None:
    text = DOC.read_text()
    assert f"`play_land {{card: {{select, prefer?: [0-{MAX_PREFER} select], ties?: any}}" in text
    assert (
        f"`activate {{source: {{zone, select, prefer?: [0-{MAX_PREFER} select], ties?: any}}"
    ) in text
    assert (f"pick?: {{select, count?, prefer?: [0-{MAX_PREFER} select], ties?: any}}") in text
    assert "ties?: any" in text


def test_activate_move_line_carries_targets_and_the_sacrifice_and_discard_choices() -> None:
    assert (
        f"`activate {{source: {{zone, select, prefer?: [0-{MAX_PREFER} select], ties?: any}}, "
        f"ability_text_has, targets: [0-{MAX_TARGETS} <target>], "
        f"sacrifice?: source|none|<sacrifice choice>, discard?: none|<discard choice>, "
        f"answers: [0-{MAX_ANSWERS}]}}`"
    ) in DOC.read_text()


def test_doc_names_the_amount_decision_kinds_and_the_amount_field() -> None:
    assert _listed("- Amount decision kinds:") == list(AMOUNT_DECISION_KINDS)
    assert (
        "amount?: >=0|max, choice?: string, pile_one?: bool, players?: [0-2 me|opponent]}`"
    ) in DOC.read_text()


def test_doc_names_every_runner_exit_in_enum_order() -> None:
    assert _listed("- Exits:") == [exit.value for exit in Exit]


def test_doc_says_an_invalid_input_exits_invalid_input_naming_it() -> None:
    text = DOC.read_text()
    assert "`invalid_input`" in text and "`{name, value, reason}`" in text


def test_doc_names_the_closed_color_vocabulary() -> None:
    assert _listed("- Colors:") == list(COLOR_LETTERS)


def test_doc_defines_the_untapped_mana_counting_rule() -> None:
    text = DOC.read_text()
    for phrase in (
        "`untapped_mana`",
        "mana_amount",
        "mana_cost_value",
        "Restricted mana is not counted",
        "`abilities[].restriction`",
    ):
        assert phrase in text
    assert "Restricted mana counts" not in text


def test_doc_names_the_missing_from_selector_and_its_closed_vocabularies():
    text = DOC.read_text()
    assert (
        f"`missing_from {{zones: [1-{len(MISSING_FROM_ZONES)} zone], side: me|opponent, "
        "by: subtype|name}`"
    ) in text
    assert _listed("- Missing-from zones:") == list(MISSING_FROM_ZONES)
    assert _listed("- Missing-from sides:") == list(MISSING_FROM_SIDES)
    assert _listed("- Missing-from by:") == list(MISSING_FROM_BY)
    for phrase in ("`subtypes_any`", "known information", "`include=contents`"):
        assert phrase in text.split("### `missing_from`", 1)[1].split("\n## ", 1)[0]


def test_doc_defines_note_its_bound_and_its_reader() -> None:
    text = DOC.read_text()
    section = text.split("### `note`", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        f"{MAX_NOTE_LENGTH}",
        "agent",
        "mid-game",
        "`intent`",
        "human reading a diff",
        "1443",
        "3110",
        "`PROSE_KEYS`",
        "may name cards",
    ):
        assert phrase in section


def test_doc_defines_advice_only_plays() -> None:
    text = DOC.read_text()
    section = text.split("## Advice-only plays", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "no moves",
        "commits nothing",
        "hold",
        "by construction",
        "live-together",
    ):
        assert phrase in section
    assert "by construction" in section and "not" in section


def test_doc_names_the_advice_only_caps() -> None:
    text = DOC.read_text()
    assert f"`too-many-plays` {MAX_PLAYS}" in text or f"{MAX_PLAYS}" in text
    for phrase in (
        "`too-many-plays`",
        f"{MAX_PLAYS}",
        "`too-many-advice`",
        f"{MAX_ADVICE}",
        "`too-many-active-for-archetype`",
        "move-bearing",
        "skips advice-only",
    ):
        assert phrase in text


def test_doc_defines_advice_only_chained() -> None:
    text = DOC.read_text()
    section = text.split("### `advice-only-chained`", 1)[1].split("\n## ", 1)[0]
    for phrase in ("chain further", "chained into", "both directions"):
        assert phrase in section


def test_doc_states_there_is_no_autofire() -> None:
    text = DOC.read_text()
    for phrase in (
        "no autofire",
        "arming",
        "extra",
        "permanent and deliberate",
        "no promotion path",
    ):
        assert phrase in text
    assert "`unknown-field`" in text.split("no autofire", 1)[1].split("\n## ", 1)[0]


def test_doc_describes_the_note_migration() -> None:
    text = DOC.read_text()
    section = text.split("## Migration", 1)[1]
    for phrase in (
        "optional",
        "`intent`",
        "sentence boundary",
        f"{MAX_NOTE_LENGTH}",
        "`why`",
        "required",
    ):
        assert phrase in section
