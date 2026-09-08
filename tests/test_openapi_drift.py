"""2b's T1: `openapi.json` is a committed, machine-checkable contract.

Three repos this api is about to be cut into (`magic-cards-lobby`,
`magic-cards-mcp`, `magic-cards-frontend`) will consume this file, and later
consumer-side contract tests (M11) validate a request/response body against
one of its named `#/components/schemas/*` entries **by name** - so an inline,
anonymous schema on a route is a silent break nobody downstream can see
coming. This file is the guard for both halves of that: the committed file
must never drift from what `app.py`'s routes actually declare, and the
specific schemas a hand-building client needs must be named components, not
inlined objects.

`mise run openapi:dump` regenerates the committed file - the fix for a red
`test_openapi_json_matches_committed_file` below is always that one command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from magic_cards_model_api.app import app

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMITTED_PATH = REPO_ROOT / "openapi.json"


def _current_schema() -> dict[str, Any]:
    """The schema `app.openapi()` builds right now, from the live route
    declarations - never read from the committed file. `app.openapi_schema`
    is cached on the `FastAPI` instance after its first call in this process
    (e.g. by an earlier test hitting `/openapi.json` through a `TestClient`),
    so the cache is cleared first to guarantee this is freshly computed."""
    app.openapi_schema = None
    return app.openapi()


def test_openapi_json_is_committed() -> None:
    assert COMMITTED_PATH.is_file(), (
        "openapi.json is not committed at the repo root - run `mise run openapi:dump`"
    )


def test_openapi_json_matches_committed_file() -> None:
    """The drift test. Red means the committed file is stale - regenerate it
    with `mise run openapi:dump` and commit the result; that is always the
    whole fix."""
    committed_text = COMMITTED_PATH.read_text()
    committed = json.loads(committed_text)
    current = _current_schema()
    assert current == committed, (
        "openapi.json is stale: it no longer matches what the FastAPI app "
        "actually declares. Fix: run `mise run openapi:dump` and commit the "
        "regenerated openapi.json."
    )
    # The committed file's own formatting is part of the contract too - a
    # dump that reformats it (whitespace, key order) on every regeneration
    # would make every unrelated PR touch this file. Byte-for-byte against
    # what the dump task itself would write.
    assert committed_text == _dumped_text(current), (
        "openapi.json's on-disk formatting does not match what `mise run "
        "openapi:dump` writes - regenerate it with that task rather than "
        "hand-editing or reformatting the file."
    )


def _dumped_text(schema: dict[str, Any]) -> str:
    # sort_keys=True: matches `scripts/dump_openapi.py` exactly, and keeps
    # the committed file's diff stable across a route-registration reorder
    # that changes nothing about the schema itself.
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def test_regenerating_openapi_json_reproduces_the_committed_file(tmp_path: Path) -> None:
    """Proves the drift test works in the OTHER direction too: given a
    deliberately stale file on disk, regenerating it (exactly what `mise run
    openapi:dump` does) produces the committed file back, byte for byte -
    the "one command fixes it" promise `test_openapi_json_matches_committed_file`
    depends on."""
    stale = tmp_path / "openapi.json"
    stale.write_text(json.dumps({"stale": "schema", "openapi": "0.0.0"}, indent=2) + "\n")
    assert stale.read_text() != COMMITTED_PATH.read_text()

    regenerated = _dumped_text(_current_schema())
    stale.write_text(regenerated)

    assert stale.read_text() == COMMITTED_PATH.read_text()


#: The 31 request models `docs/2b-plan.md` T1 names, plus `PendingDecision`,
#: the state view, and the outcome shape - every schema a client hand-builds
#: a request or response against, and so every schema M11's consumer-side
#: contract tests will look up **by name**. Asserted as named components,
#: never inlined, so a future refactor that folds one back into a bare
#: `dict[str, Any]` route annotation fails here instead of silently breaking
#: a test in a repo this one cannot see.
REQUEST_MODEL_COMPONENTS = {
    "NewPlayer",
    "NewGame",
    "PutCard",
    "MoveCard",
    "PatchCard",
    "PatchPlayer",
    "ManaPoolUpdate",
    "StepUpdate",
    "ActorRequest",
    "ReportProblemRequest",
    "ManaSource",
    "CastRequest",
    "ConvokeChoice",
    "CrewRequest",
    "CycleRequest",
    "SpecialActionRequest",
    "TargetRequest",
    "ActivateRequest",
    "AttackPair",
    "AttackRequest",
    "PlayLandRequest",
    "PassPriorityRequest",
    "ConcedeRequest",
    "AdvanceRequest",
    "BlockPair",
    "DeclareBlockersRequest",
    "DeclaredPairRequest",
    "DecideRequest",
    "MulliganRequest",
    "OpeningHandActionRequest",
    "PregameSettleRequest",
}

#: `PendingDecision`, the state view, and the outcome shape - named
#: `PendingDecisionView`, `GameStateView` and `ActionOutcome` in `schemas.py`.
#: See that module's own docstring for why they carry a `View`/`Outcome`
#: suffix rather than reusing `decisions.PendingDecision`'s bare name.
RESPONSE_MODEL_COMPONENTS = {
    "PendingDecisionView",
    "GameStateView",
    "ActionOutcome",
}


def test_the_31_request_models_are_named_components() -> None:
    schema = _current_schema()
    names = set(schema.get("components", {}).get("schemas", {}))
    missing = REQUEST_MODEL_COMPONENTS - names
    assert not missing, f"request models missing from #/components/schemas: {sorted(missing)}"


def test_pending_decision_state_view_and_outcome_are_named_components() -> None:
    schema = _current_schema()
    names = set(schema.get("components", {}).get("schemas", {}))
    missing = RESPONSE_MODEL_COMPONENTS - names
    assert not missing, (
        f"response models missing from #/components/schemas: {sorted(missing)} - "
        "a route is returning one of these as an inline/anonymous schema "
        "instead of a named response_model"
    )


def _is_named_or_a_union_of_named(schema: dict[str, Any]) -> bool:
    """True for a bare `$ref`, or an `anyOf`/`oneOf` composed entirely of
    `$ref`s - a route whose response genuinely differs in shape by branch
    (e.g. `move_card`'s `ActionOutcome | MoveCardSuccessResponse`) is still
    every bit as "named, not inline" as a single-model route: a client
    picks the branch by whichever named schema's required fields match."""
    ref = schema.get("$ref")
    if ref is not None:
        return ref.startswith("#/components/schemas/")
    branches = schema.get("anyOf") or schema.get("oneOf")
    if not branches:
        return False
    return all(
        isinstance(branch, dict) and (branch.get("$ref") or "").startswith("#/components/schemas/")
        for branch in branches
    )


def test_every_mutating_game_route_declares_a_named_response_model() -> None:
    """No route under `/api/games/{game_id}` may fall back to FastAPI's
    default (an inline, untitled `dict` schema, or no response schema at
    all) - every one of them must point `responses.200.content.application/
    json.schema` at a `$ref` (or an `anyOf`/`oneOf` of them) into
    `#/components/schemas`, per this task's requirement that a hand-building
    client never has to read an inline shape."""
    schema = _current_schema()
    checked = []
    for path, methods in schema.get("paths", {}).items():
        if not path.startswith("/api/games"):
            continue
        for method, operation in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            ok = operation.get("responses", {}).get("200", {})
            body = ok.get("content", {}).get("application/json", {})
            response_schema = body.get("schema", {})
            assert _is_named_or_a_union_of_named(response_schema), (
                f"{method.upper()} {path} has no named response_model (schema={response_schema!r})"
            )
            checked.append((path, method))
    # Sanity: this suite actually walked a non-trivial number of routes,
    # rather than the loop above silently matching nothing.
    assert len(checked) >= 30
