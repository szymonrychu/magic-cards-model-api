"""The play-script format doc (design D2) names the schema's closed sets.

`docs/superpowers/specs/2026-09-13-play-script-format-design.md` is the prose a
script author reads. Its `cast.cost` and `answers[].decision` tables must name
exactly what `playscripts.schema` accepts, so a cast option or decision kind
added to the schema fails here until the doc says so too.
"""

from __future__ import annotations

import re
from pathlib import Path

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
    MOVE_KINDS,
    OPERAND_KINDS,
    PAYABLE_STATES,
    POSITION_DECISION_KINDS,
    TARGET_ZONES,
    TOP_ORDER_DECISION_KINDS,
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
            return re.findall(r"`([a-z_-]+)`", line[len(marker) :])
    raise AssertionError(f"no line starting {marker!r} in {DOC.name}")


def test_doc_names_every_cast_cost_in_schema_order() -> None:
    assert _listed("- Cast costs:") == list(CAST_COSTS)


def test_doc_names_every_condition_kind_in_schema_order() -> None:
    assert _listed("- Condition kinds") == list(CONDITION_KINDS)


def test_doc_names_every_move_kind_in_schema_order() -> None:
    assert _listed("- Move kinds:") == list(MOVE_KINDS)


def test_doc_names_every_operand_kind_in_schema_order() -> None:
    assert _listed("- Operand kinds:") == list(OPERAND_KINDS)


def test_doc_names_every_target_zone_in_schema_order() -> None:
    assert _listed("- Target zones:") == list(TARGET_ZONES)


def test_doc_names_the_top_order_and_position_decision_kinds() -> None:
    assert _listed("- Top-order decision kinds:") == list(TOP_ORDER_DECISION_KINDS)
    assert _listed("- Position decision kinds:") == list(POSITION_DECISION_KINDS)


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
    from magic_cards_model_api.playscripts.schema import MAX_MODES, MAX_OPTIONAL_COSTS, MAX_X

    text = DOC.read_text()
    assert (
        f"`cast {{card: {{zone, select, prefer?: [0-{MAX_PREFER} select], ties?: any}}, "
        f"cost?: <cast cost>, face?: <selector>, modes?: [1-{MAX_MODES} {{text_has}}], "
        f"x?: 0-{MAX_X}|max, shares?: [0-{MAX_TARGETS} int], "
        f"optional_costs?: [1-{MAX_OPTIONAL_COSTS} {{text_has}}], sacrifice?: <Pick>, "
        f"discard?: <Pick>, exile?: <Pick>, tap?: <Pick>, convoke?: <Pick>, delve?: <Pick>, "
        f"improvise?: <Pick>, targets: [0-{MAX_TARGETS} <target>], mana?: auto, "
        f"answers: [0-{MAX_ANSWERS}]}}`"
    ) in text
    assert f"prefer?: [0-{MAX_PREFER} select]" in text


def test_o2_prefer_and_ties_are_on_play_land_activate_source_and_pick() -> None:
    text = DOC.read_text()
    assert f"`play_land {{card: {{select, prefer?: [0-{MAX_PREFER} select], ties?: any}}" in text
    assert (
        f"`activate {{source: {{zone, select, prefer?: [0-{MAX_PREFER} select], ties?: any}}"
    ) in text
    assert (f"pick?: {{select, count?: >=0, prefer?: [0-{MAX_PREFER} select], ties?: any}}") in text
    assert "ties?: any" in text


def test_activate_move_line_carries_targets_and_the_sacrifice_and_discard_choices() -> None:
    assert (
        f"`activate {{source: {{zone, select, prefer?: [0-{MAX_PREFER} select], ties?: any}}, "
        f"ability_text_has?: string, ability_index?: 0-15, targets: [0-{MAX_TARGETS} <target>], "
        f"sacrifice?: source|none|<sacrifice choice>, discard?: none|<discard choice>, "
        f"exile?: <Pick>, x?: 0-30|max, color?: W|U|B|R|G|C, "
        f"answers: [0-{MAX_ANSWERS}]}}`"
    ) in DOC.read_text()


def test_activate_ability_index_bound_matches_the_schema() -> None:
    from magic_cards_model_api.playscripts.schema import MAX_ABILITY_INDEX

    assert MAX_ABILITY_INDEX == 15
    assert "ability_index?: 0-15" in DOC.read_text()


def test_doc_names_the_amount_decision_kinds_and_the_amount_field() -> None:
    assert _listed("- Amount decision kinds:") == list(AMOUNT_DECISION_KINDS)
    assert (
        "amount?: >=0|max, choice?: string, choice_from?: {zone, side, select, "
        "prefer?: [0-4 select], ties?: any}, pile_one?: bool, to_hand?: bool, "
        "players?: [0-2 me|opponent], "
        "top_order?: [0-4 select], position?: top|bottom, targets?: [0-6 <target>], "
        "order?: [1-6 select], damage?: {tiers: [0-6 select], lethal_first?: bool, "
        "rest_to?: player|last_blocker}}`"
    ) in DOC.read_text()


def test_doc_names_the_order_and_damage_decision_kinds() -> None:
    from magic_cards_model_api.playscripts.schema import (
        DAMAGE_DECISION_KINDS,
        ORDER_DECISION_KINDS,
    )

    assert _listed("- Order decision kinds:") == list(ORDER_DECISION_KINDS)
    assert _listed("- Damage decision kinds:") == list(DAMAGE_DECISION_KINDS)


def test_doc_names_every_runner_exit_in_enum_order() -> None:
    assert _listed("- Exits:") == [exit.value for exit in Exit]


def test_doc_says_an_invalid_input_exits_invalid_input_naming_it() -> None:
    text = DOC.read_text()
    assert "`invalid_input`" in text and "`{name, value, reason}`" in text


def test_doc_names_the_closed_color_vocabulary() -> None:
    assert _listed("- Colors:") == list(COLOR_LETTERS)


def test_doc_defines_mana_pool() -> None:
    text = DOC.read_text()
    assert "`mana_pool`" in text
    section = text.split("### `mana_pool` (FD9)", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "ManaPoolView.floating",
        "CR 106.4",
        "color",
        "untapped_mana",
        "untapped SOURCES",
        "floating pool",
        "finding seq 191",
        "X=8",
        "compare",
    ):
        assert phrase in section, phrase


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


def test_doc_states_the_play_count_caps_are_removed() -> None:
    text = DOC.read_text()
    section = text.split("### No play-count cap", 1)[1].split("\n### ", 1)[0]
    for phrase in (
        "`too-many-plays`",
        "`too-many-advice`",
        "`too-many-active-for-archetype`",
        "removed",
        "2026-09-24",
        "any number of plays",
        "O(n^2)",
        "60 move-bearing",
        "3 seconds",
    ):
        assert phrase in section


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


def test_doc_describes_the_review_loop() -> None:
    text = DOC.read_text()
    section = text.split("## The review loop", 1)[1].split("\n## ", 1)[0]
    for phrase in (
        "Per-decision review submission",
        "2026-09-24",
        "`declined`",
        "Denied means declined",
        "corner case",
        "never turned down",
        "`lint.exclusivity`",
        "`_conflict_warnings`",
        "conflict_regression",
        "No probability",
    ):
        assert phrase in section or phrase.lower() in section.lower()


def _flat(section: str) -> str:
    """`section` with runs of whitespace (including the line wraps a prose
    paragraph carries) collapsed to single spaces, so a pinned phrase can
    span a line break without the test caring where the doc happens to wrap
    it."""
    return " ".join(section.split())


def _section(text: str, marker: str) -> str:
    return text.split(marker, 1)[1].split("\n## ", 1)[0]


def test_doc_defines_repeat_grammar_and_bounds() -> None:
    from magic_cards_model_api.playscripts.schema import MAX_REPEAT_BODY, MAX_REPEAT_ITERATIONS

    text = DOC.read_text()
    assert "## Repeat (FD9)" in text
    section = _flat(_section(text, "## Repeat (FD9)"))
    for phrase in (
        "DSL is meant to fix missing repeat-until loops.",
        "Instruct agents to generate DSLs instead of manually calling MCPs.",
        "finding seq 191",
        f"moves: [1-{MAX_REPEAT_BODY} move]",
        "until: [1-8 condition]",
        f"max_iterations: 1-{MAX_REPEAT_ITERATIONS}",
        "stop_when_refused?: bool = false",
        "repeat-nested",
        "repeat-pass-in-body",
        "repeat-empty-until",
        "repeat-bound",
        "MAX_MOVES",
        "MAX_MOVES_PER_RUN",
        "phrase-too-narrow",
        "ability_index",
    ):
        assert phrase in section, phrase


def test_doc_says_until_is_judged_fresh_and_zero_iterations() -> None:
    text = DOC.read_text()
    section = _flat(_section(text, "### `until` is judged fresh, before every iteration"))
    for phrase in ("freshly read board", "zero iterations", "re-reads the board"):
        assert phrase in section


def test_doc_names_every_repeat_stop_rule_exactly() -> None:
    text = DOC.read_text()
    section = _flat(_section(text, "### Stop rules (FD3 part 7 implements these exactly)"))
    for phrase in (
        "the script continues with the move after the `repeat`",
        "`unforeseen`, with `stop_reason` `repeat_bound`",
        "`unforeseen`, with `stop_reason` `repeat_no_progress`",
        "`refused`, carrying the engine's own text",
        "`stop_when_refused: true`",
        "`input_required`, resumable into the same loop",
        "`interrupted`",
        "resume token",
        "continues the same loop",
    ):
        assert phrase in section, phrase


def test_doc_gives_the_devoted_druid_and_walking_ballista_repeat_examples() -> None:
    text = DOC.read_text()
    raw_section = _section(text, "### Worked examples (selectors only, no card names)")
    section = _flat(raw_section)
    for phrase in (
        "Devoted Druid",
        "Vizier of Remedies",
        "devoted-combo",
        "Walking Ballista",
        "eldrazi-tron",
        "prison-tron",
        '"kind": "mana_pool"',
        '"kind": "untapped_mana"',
        "resource-guard",
    ):
        assert phrase in section, phrase
    for name in ("Devoted Druid", "Vizier of Remedies", "Walking Ballista"):
        assert f'"{name}"' not in raw_section, (
            f"{name!r} must appear only in prose, never inside a JSON selector"
        )


def test_doc_defines_repeat_until_static() -> None:
    text = DOC.read_text()
    section = _flat(_section(text, "### `repeat-until-static`"))
    for phrase in ("warning", "never a failing error", "turn", "input", "max_iterations"):
        assert phrase in section


def test_doc_extends_the_move_selector_conflict_dimension_to_repeat() -> None:
    text = DOC.read_text()
    section = _flat(
        _section(text, "### Conflicts: a repeat's first body move is the play's first move")
    )
    for phrase in ("move-selector", "first body move", "_move_selectors_disjoint"):
        assert phrase in section


def test_doc_states_iteration_left_the_not_in_the_vocabulary_list() -> None:
    text = DOC.read_text()
    section = _flat(text.split("## Not in the vocabulary", 1)[1])
    assert "Concede." in section
    assert "iteration moved out" in section
    assert "`repeat` is now a real move kind" in section


def test_doc_states_fd9_keeps_schema_version_1() -> None:
    text = DOC.read_text()
    assert "FD9 (2026-09-26) keeps `schema_version` at 1" in text


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
