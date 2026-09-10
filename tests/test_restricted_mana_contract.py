"""The committed `openapi.json` must actually publish CR 106.6 restricted mana.

`test_openapi_drift.py` only proves the committed file matches whatever the
app declares *right now*: on a checkout whose engine or view models predate
the restricted-mana wire, a stale-but-self-consistent file passes it happily.
That is exactly how this artifact shipped empty once already.

So this file pins the contract itself, field by field, against the committed
bytes. Red here means the artifact was generated against a base that does not
carry the restricted-mana change yet - regenerate with `mise run openapi:dump`
on a checkout that does, rather than hand-editing the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_PATH = REPO_ROOT / "openapi.json"


def _schemas() -> dict[str, Any]:
    return json.loads(COMMITTED_PATH.read_text())["components"]["schemas"]


def _properties(name: str) -> dict[str, Any]:
    schemas = _schemas()
    assert name in schemas, f"{name} is not a named component in openapi.json"
    return schemas[name]["properties"]


def test_mana_source_names_the_ability_it_taps() -> None:
    """A permanent with two mana abilities cannot be paid from without saying
    which one - `ManaSource.ability_index` is how a client says it."""
    assert "ability_index" in _properties("ManaSource")


def test_restriction_view_is_a_named_component() -> None:
    """Named, not inlined: M11's consumer-side contract tests look schemas up
    by name, and a client hand-building a pool renderer needs this one."""
    schemas = _schemas()
    assert "RestrictionView" in schemas
    assert set(schemas["RestrictionView"]["properties"]) >= {"text", "understood"}


def test_mana_pool_view_publishes_the_restricted_subset() -> None:
    """`floating` alone made two restricted {C} look like two free ones."""
    properties = _properties("ManaPoolView")
    assert "restricted" in properties
    assert "spend_effects" in properties


def test_ability_view_publishes_its_restriction() -> None:
    """The same claim one step earlier: before the mana is in the pool."""
    assert "restriction" in _properties("AbilityView")


#: The component the engine's `Action` reaches the wire as. It is the
#: subclass, not `Action` itself: `LegalActionsResponse` answers with
#: `schemas.ActionWithTargets`, which inherits every field of `Action` and
#: adds the api's own per-action legal-target candidates, so `Action` is no
#: longer referenced by any route and FastAPI no longer names it. The claims
#: below are about the fields that reach a client, which is this.
ACTION_COMPONENT = "ActionWithTargets"


def test_action_says_what_it_produces_and_what_binds_the_mana() -> None:
    """The engine's three new `Action` fields, reaching the wire through the
    api's own component for it."""
    properties = _properties(ACTION_COMPONENT)
    assert "produces" in properties
    assert "restriction" in properties
    assert "restriction_understood" in properties


def test_action_says_how_many_colours_a_caller_names() -> None:
    """`color_options` is a flat pool, so it cannot tell "one colour among
    five" from "two colours among five" - `color_choice_count` is the other
    half of that parameter space, and Interdimensional Web Watch's `{W}{U}` is
    unreachable through this contract without it.

    It arrives on the engine `Action` in the same wave as the three fields
    above, so it is pinned in the same place: a regeneration against an engine
    that carries only part of the wave is exactly the failure this file
    exists to catch.
    """
    assert "color_choice_count" in _properties(ACTION_COMPONENT)
