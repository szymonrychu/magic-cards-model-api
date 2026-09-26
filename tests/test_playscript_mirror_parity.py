"""FD2 item 9: the mtg-decks stdlib mirror (`scripts/check_plays.py`) shares
the api's own vocabulary constants verbatim - `CAST_COSTS`, `STEPS`, move
kinds, decision kinds, the amount-answer field set and the shared bounds.
Skipped outright when the mtg-decks checkout is not present (this repo does
not depend on it); marked `xfail(strict=True)` otherwise, because the mirror
is known to be behind today (missing emerge/prowl/sneak/spectacle/surge/
web-slinging in its own `CAST_COSTS`, missing `first_strike_damage` in its
own `STEPS`, and missing every FD2 addition this task's own vocabulary just
added) - FD8 brings it into parity and removes this marker in the same
landing, or the merger does when FD8 lands."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from magic_cards_model_api.playscripts import schema

MIRROR_PATH = Path("/Users/szymonri/Documents/mtg-decks/scripts/check_plays.py")

pytestmark = pytest.mark.skipif(
    not MIRROR_PATH.is_file(), reason="mtg-decks checkout not present at " + str(MIRROR_PATH)
)


def _load_mirror() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_plays_mirror", MIRROR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.xfail(strict=True, reason="FD8 mirror")
def test_the_mirror_matches_the_api_schemas_vocabulary_and_bounds() -> None:
    mirror = _load_mirror()
    problems: list[str] = []

    def check(name: str, mirror_value: Any, api_value: Any) -> None:
        if tuple(mirror_value) != tuple(api_value):
            problems.append(f"{name}: mirror={mirror_value!r} api={api_value!r}")

    check("CAST_COSTS", mirror.CAST_COSTS, schema.CAST_COSTS)
    check("STEPS", mirror.STEPS, schema.STEPS)
    check("MOVE_KINDS", mirror.MOVE_KINDS, schema.MOVE_KINDS)
    check("DECISION_KINDS", mirror.DECISION_KINDS, schema.DECISION_KINDS)
    check(
        "FORBIDDEN_DECISION_KINDS", mirror.FORBIDDEN_DECISION_KINDS, schema.FORBIDDEN_DECISION_KINDS
    )
    check("COMBAT_DECISION_KINDS", mirror.COMBAT_DECISION_KINDS, schema.COMBAT_DECISION_KINDS)
    check("AMOUNT_DECISION_KINDS", mirror.AMOUNT_DECISION_KINDS, schema.AMOUNT_DECISION_KINDS)
    check(
        "NON_AMOUNT_ANSWER_FIELDS", mirror.NON_AMOUNT_ANSWER_FIELDS, schema.NON_AMOUNT_ANSWER_FIELDS
    )
    check("CHOICE_DECISION_KINDS", mirror.CHOICE_DECISION_KINDS, schema.CHOICE_DECISION_KINDS)
    check(
        "TOP_ORDER_DECISION_KINDS", mirror.TOP_ORDER_DECISION_KINDS, schema.TOP_ORDER_DECISION_KINDS
    )
    check("POSITION_DECISION_KINDS", mirror.POSITION_DECISION_KINDS, schema.POSITION_DECISION_KINDS)
    check("TARGETS_DECISION_KINDS", mirror.TARGETS_DECISION_KINDS, schema.TARGETS_DECISION_KINDS)
    # FD2's own new dimensions - present on the api side now, and only on the
    # mirror once FD8 lands.
    check(
        "ORDER_DECISION_KINDS",
        getattr(mirror, "ORDER_DECISION_KINDS", ()),
        schema.ORDER_DECISION_KINDS,
    )
    check(
        "DAMAGE_DECISION_KINDS",
        getattr(mirror, "DAMAGE_DECISION_KINDS", ()),
        schema.DAMAGE_DECISION_KINDS,
    )
    check(
        "CHOICE_FROM_DECISION_KINDS",
        getattr(mirror, "CHOICE_FROM_DECISION_KINDS", ()),
        schema.CHOICE_FROM_DECISION_KINDS,
    )
    check("KEYWORD_ACTIONS", getattr(mirror, "KEYWORD_ACTIONS", ()), schema.KEYWORD_ACTIONS)

    for name in (
        "MAX_TARGETS",
        "MAX_PREFER",
        "MAX_ANSWERS",
        "MAX_ABILITY_INDEX",
        "MAX_ATTACK_GROUPS",
        "MAX_BLOCKS",
        "MAX_TOP_ORDER",
        "MIN_ALL_OF_CONDITIONS",
        "MAX_ALL_OF_CONDITIONS",
        "MAX_MODES",
        "MAX_OPTIONAL_COSTS",
        "MAX_ORDER",
        "MAX_X",
        "MAX_DAMAGE_TIERS",
    ):
        mirror_value = getattr(mirror, name, None)
        api_value = getattr(schema, name)
        if mirror_value != api_value:
            problems.append(f"{name}: mirror={mirror_value!r} api={api_value!r}")

    assert not problems, "\n".join(problems)
