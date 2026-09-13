"""The play-script format doc (design D2) names the schema's closed sets.

`docs/superpowers/specs/2026-09-13-play-script-format-design.md` is the prose a
script author reads. Its `cast.cost` and `answers[].decision` tables must name
exactly what `playscripts.schema` accepts, so a cast option or decision kind
added to the schema fails here until the doc says so too.
"""

from __future__ import annotations

import re
from pathlib import Path

from magic_cards_model_api.playscripts.schema import (
    CAST_COSTS,
    DECISION_KINDS,
    FORBIDDEN_DECISION_KINDS,
    MAX_ANSWERS,
    MAX_PREFER,
    MAX_TARGETS,
)

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
        f"targets: [0-{MAX_TARGETS} {{select, side}}], mana?: auto, answers: [0-{MAX_ANSWERS}]}}`"
    ) in text
    assert f"prefer?: [0-{MAX_PREFER} select]" in text
