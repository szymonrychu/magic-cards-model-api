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
    CAST_COSTS,
    DECISION_KINDS,
    FORBIDDEN_DECISION_KINDS,
    MAX_ANSWERS,
    MAX_PREFER,
    MAX_TARGETS,
    MISSING_FROM_BY,
    MISSING_FROM_SIDES,
    MISSING_FROM_ZONES,
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


def test_doc_names_every_answerable_decision_kind() -> None:
    assert _listed("- Decision kinds:") == list(DECISION_KINDS)


def test_doc_names_the_decision_kinds_a_script_never_answers() -> None:
    assert _listed("- Never answered:") == list(FORBIDDEN_DECISION_KINDS)


def test_cast_move_line_carries_the_optional_cost_and_bounds() -> None:
    text = DOC.read_text()
    assert (
        f"`cast {{card: {{zone, select}}, cost?: <cast cost>, "
        f"targets: [0-{MAX_TARGETS} <target>], mana?: auto, answers: [0-{MAX_ANSWERS}]}}`"
    ) in text
    assert f"prefer?: [0-{MAX_PREFER} select]" in text


def test_activate_move_line_carries_targets_and_the_sacrifice_choice() -> None:
    assert (
        f"`activate {{source: {{zone, select}}, ability_text_has, "
        f"targets: [0-{MAX_TARGETS} <target>], sacrifice?: source|none|<sacrifice choice>, "
        f"answers: [0-{MAX_ANSWERS}]}}`"
    ) in DOC.read_text()


def test_doc_names_the_amount_decision_kinds_and_the_amount_field() -> None:
    assert _listed("- Amount decision kinds:") == list(AMOUNT_DECISION_KINDS)
    assert "amount?: >=0|max}`" in DOC.read_text()


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
