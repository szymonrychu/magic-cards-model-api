"""2b's T2: `fixtures/payloads/<case>.json` is a generated, committed corpus -
this is the generator's own drift test, and the guard for the shape of bug
that made it necessary in the first place.

Before this task, `tests/test_ui_decisions.py`'s `MAY` fixture and its kin
were hand-written `PendingDecision` dicts, and two of their fields drifted
silently from what the engine actually emits: `target_minimum: None` and
`moved_label`/`kept_label: None`, none of which `PendingDecision.to_view()`
ever produces (`target_minimum` is an `int`, the labels default to `""` - see
`magic_cards_model.machines.decisions.Decision` and
`magic_cards_model_api.decisions.PendingDecision`). Nothing caught it because
`ui/app.js`'s own `pending.target_minimum || 0` reads a real `0` and a wrong
`None` identically - a fixture with the wrong shape passed a `decisions.js`
test for the wrong reason.

`scripts/generate_payload_fixtures.py` is the fix: every fixture in
`fixtures/payloads/` is captured from a real board played through the real
HTTP API, never typed by hand. This file is what keeps the committed corpus
honest - `mise run fixtures:payloads` regenerating it is always the fix for a
red test below, the same "one command fixes it" shape
`test_openapi_drift.py`/`test_tools_export_drift.py` already use for their
own committed artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_payload_fixtures import CASES, build_all, case_path, render_all


def test_every_case_file_is_committed() -> None:
    missing = [name for name in CASES if not case_path(name).is_file()]
    assert not missing, (
        f"fixtures/payloads/ is missing {missing} - run `mise run fixtures:payloads` "
        "and commit the result"
    )


def test_the_committed_corpus_matches_a_fresh_regeneration() -> None:
    """The drift test proper. Red means a committed payload no longer
    matches what the API actually emits for that case - regenerate with
    `mise run fixtures:payloads` and commit the result; that is always the
    whole fix."""
    committed = {name: case_path(name).read_text() for name in CASES}
    fresh = render_all(build_all())
    assert fresh == committed, (
        "fixtures/payloads/ is stale: at least one committed case no longer "
        "matches what the live API emits. Fix: run `mise run fixtures:payloads` "
        "and commit the regenerated files."
    )


def test_regenerating_reproduces_the_committed_corpus_byte_for_byte(tmp_path: Path) -> None:
    """Proves the drift test works in the OTHER direction too - and proves
    the generator is actually deterministic, not merely stable against
    itself in-process: given a deliberately stale file on disk, regenerating
    it (exactly what `mise run fixtures:payloads` does) reproduces the
    committed file back, byte for byte."""
    name = next(iter(CASES))
    stale = tmp_path / f"{name}.json"
    stale.write_text(json.dumps({"stale": "fixture"}, indent=2) + "\n")
    assert stale.read_text() != case_path(name).read_text()

    regenerated = render_all(build_all())[name]
    stale.write_text(regenerated)

    assert stale.read_text() == case_path(name).read_text()


def test_every_case_file_is_valid_json_on_disk() -> None:
    for name in CASES:
        json.loads(case_path(name).read_text())


def test_generating_the_corpus_twice_is_byte_identical() -> None:
    """The property that lets `mise run fixtures:payloads` run repeatedly
    with a clean `git status` - each case's ids are random per run
    (`game_id`, every `CardState`/ability id), so this is really a test of
    `canonicalize()`, not of the engine holding still."""
    first = render_all(build_all())
    second = render_all(build_all())
    assert first == second


def test_a_hand_corrupted_fixture_is_caught() -> None:
    """The regression this whole task exists to make impossible, reproduced
    directly: hand-editing a committed fixture the way the reported bug
    hand-wrote one - flipping a real value to `None` the engine would never
    emit - must turn the drift test red. Written to a *copy*, never the real
    committed file, and always restored: this proves the guard exists
    without leaving a corrupted fixture behind for a real run to trip over."""
    name = "split_card_labels"
    path = case_path(name)
    original = path.read_text()
    payload = json.loads(original)
    assert payload["pending_decision"]["target_minimum"] == 0, payload["pending_decision"]
    payload["pending_decision"]["target_minimum"] = None
    try:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        committed = {n: case_path(n).read_text() for n in CASES}
        fresh = render_all(build_all())
        assert fresh != committed, (
            "corrupting a committed fixture by hand did not turn the drift "
            "test red - it is not actually guarding anything"
        )
        assert fresh[name] != committed[name]
    finally:
        path.write_text(original)
    assert path.read_text() == original
