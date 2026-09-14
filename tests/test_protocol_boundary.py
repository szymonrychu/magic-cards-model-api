"""2b T11: the api owns `PROTOCOL_VERSION`; `seatkit` no longer lends it one.

`app.py`'s `/api/health` used to answer with `seatkit.PROTOCOL_VERSION` - the
only remaining import in this repo pointing from the api toward the future
`magic-cards-mcp` package, and pointing the wrong way (the api is what every
other service depends on, never the reverse). `protocol.py` is a new module
the api itself owns; `seatkit.py` keeps its own copy (it cannot import the
api's package once the two are separate repos) and the two are kept in sync
by hand, the same way `agent_settings.py`'s cross-repo spellings already are
(see MEMORY.md).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from magic_cards_model_api import app as app_module
from magic_cards_model_api import protocol, seatkit
from magic_cards_model_api.main import app


def test_app_module_no_longer_imports_seatkit() -> None:
    """The reverse import (api -> mcp) is severed outright, not merely
    unused - `protocol.PROTOCOL_VERSION` replaces its only functional use."""
    assert not hasattr(app_module, "seatkit"), (
        "app.py still imports seatkit - the api must not depend on the future mcp package at all"
    )


def test_health_reports_the_apis_own_protocol_version() -> None:
    with TestClient(app) as client:
        answer = client.get("/api/health").json()
    assert answer["protocol_version"] == protocol.PROTOCOL_VERSION


def test_protocol_version_is_kept_in_sync_by_hand_between_api_and_seatkit() -> None:
    """The two are independent literals post-cut (no import can join them),
    so this is the standing pin that catches one side being bumped without
    the other - see `protocol.py`'s and `seatkit.py`'s own docstrings for
    why a hand-sync is the only option here."""
    assert protocol.PROTOCOL_VERSION == seatkit.PROTOCOL_VERSION


def test_the_version_in_effect_has_a_changelog_entry() -> None:
    """`seatkit.py` carries the changelog: every value this constant has ever
    held is written up there, in its own paragraph, saying what shape moved
    and what an MCP process built before the bump does when it meets the new
    one. A bump that only edits the two literals leaves the next reader with
    a version number and no record of why it moved, which is the whole reason
    the convention exists - so pin the convention rather than trusting it."""
    source = Path(seatkit.__file__).read_text()
    version = protocol.PROTOCOL_VERSION
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    assert f"`{version}`" in changelog, (
        f"{version} is in effect but has no changelog paragraph in seatkit.py - "
        "see that module's own convention: each version says what shape changed "
        "and how a process built before it misbehaves"
    )


def test_seatkit_19_names_the_known_cards_and_contents_blocks() -> None:
    """The bump that added `known_cards` to the state view and the
    own-only `library_contents`/`sideboard_contents` behind
    `?include=contents`: its changelog paragraph names all four."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-19`", 1)[1].split("`seatkit-18`", 1)[0]
    for term in ("known_cards", "library_contents", "sideboard_contents", "include=contents"):
        assert term in paragraph, f"the seatkit-19 paragraph does not name {term}"


def test_seatkit_19_names_the_double_faced_views_and_known_cards_while_paused() -> None:
    """In the seatkit-19 paragraph: a double-faced card view gained `faces`
    and a non-null `oracle_text`, and `known_cards` holds what a paused
    command already showed while its choice is pending."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-19`", 1)[1].split("`seatkit-18`", 1)[0]
    for term in ("faces", "oracle_text", "pending"):
        assert term in paragraph, f"the seatkit-19 paragraph does not name {term}"


def test_seatkit_17_stays_mains_optional_costs_paragraph() -> None:
    """main shipped seatkit-17 to production for `optional_costs`; the DSL
    additions are seatkit-19, so a seatkit-17 client gets a PROTOCOL
    MISMATCH, and main's paragraph is still the one under `seatkit-17`."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    seventeen = changelog.split("`seatkit-17`", 1)[1].split("`seatkit-16`", 1)[0]
    assert "optional_costs" in seventeen
    for term in ("known_cards", "run_script", "/script-runs"):
        assert term not in seventeen, f"the seatkit-17 paragraph still names {term}"


def test_seatkit_18_stays_mains_equip_paragraph() -> None:
    """main shipped seatkit-18 for `equip` and the re-priced rows while the
    DSL additions were still on `script-dsl`; those are seatkit-19, so main's
    paragraph is still the one under `seatkit-18`."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    eighteen = changelog.split("#: `seatkit-18`", 1)[1].split("#: `seatkit-17`", 1)[0]
    for term in ("equip", "printed_cost", "x_costs", "unreadable_modes"):
        assert term in eighteen, f"the seatkit-18 paragraph does not name {term}"
    for term in ("known_cards", "run_script", "/script-runs"):
        assert term not in eighteen, f"the seatkit-18 paragraph still names {term}"


def test_seatkit_19_names_the_script_tools_and_their_exit_block() -> None:
    """In the seatkit-19 paragraph: `run_script` and `list_scripts` are new
    tools over the play-script routes, and `script_exit` is the typed block
    `run_script` answers with."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-19`", 1)[1].split("`seatkit-18`", 1)[0]
    for term in ("run_script", "list_scripts", "script_exit"):
        assert term in paragraph, f"the seatkit-19 paragraph does not name {term}"
    assert "No MCP tool calls any of them" not in paragraph, (
        "the play-script routes paragraph still says no tool calls them"
    )


def test_seatkit_20_names_the_script_applicability_hint() -> None:
    """The bump that put `scripts.applicable` on every wait/wake and action
    answer: its changelog paragraph names the hint's shape and the kill
    switch it checks."""
    assert protocol.PROTOCOL_VERSION == "seatkit-20"
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-20`", 1)[1].split("`seatkit-19`", 1)[0]
    for term in ("scripts", "applicable", "call", "conflict", "MAX_HINT_SCRIPTS", "run_script"):
        assert term in paragraph, f"the seatkit-20 paragraph does not name {term}"


def test_seatkit_20_names_turns_taken_and_invoked_via() -> None:
    """S1's own bundle: `turns_taken` on the state view, and `invoked_via`
    on a script run - now including its S2 `"armed"` value."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-20`", 1)[1].split("`seatkit-19`", 1)[0]
    for term in ("turns_taken", "invoked_via", "armed"):
        assert term in paragraph, f"the seatkit-20 paragraph does not name {term}"


def test_seatkit_20_names_plan_ahead() -> None:
    """S2 A1: `declare_plan(for_turn="next")`, the 409 `own_turn_in_progress`
    it can now answer with, and the upkeep/draw empty-stack pass
    exemption from the require-a-plan gate."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-20`", 1)[1].split("`seatkit-19`", 1)[0]
    for term in (
        'declare_plan(for_turn="next")',
        "own_turn_in_progress",
        "pass_priority",
        "upkeep",
        "draw",
        "stack is empty",
    ):
        assert term in paragraph, f"the seatkit-20 paragraph does not name {term}"


def test_seatkit_20_names_arming() -> None:
    """S2 A2-A5: `arm`/`arm_reasoning`/`opposing_archetype`/`inputs` on
    `wait_for_priority` and `pass_until`; the `armed`/`armed_conflict`/
    `armed_error`/`armed_skipped` answer keys; `scripts.armable` and
    `arm_call`; and `pass_until`'s `finished: false` plus `resume` while it
    waits for this seat's own priority at an armed destination."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-20`", 1)[1].split("`seatkit-19`", 1)[0]
    for term in (
        "arm",
        "arm_reasoning",
        "opposing_archetype",
        "inputs",
        "wait_for_priority",
        "pass_until",
        "armed",
        "armed_conflict",
        "armed_error",
        "armed_skipped",
        "armable",
        "arm_call",
        "finished: false",
        "resume",
    ):
        assert term in paragraph, f"the seatkit-20 paragraph does not name {term}"


def test_seatkit_20_names_buddy_drafts() -> None:
    """S2 A6: `wait_for_priority(wake_to_draft=...)`, the `stage_advice`
    tool, `draft.status`'s values, and `AdviceIn.drafted_at_seq`."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-20`", 1)[1].split("`seatkit-19`", 1)[0]
    for term in (
        "wake_to_draft",
        "stage_advice",
        "draft",
        "posted",
        "held",
        "stale",
        "superseded_by_script",
        "failed",
        "drafted_at_seq",
    ):
        assert term in paragraph, f"the seatkit-20 paragraph does not name {term}"


def test_seatkit_20_names_the_stage_advice_pregame_refusal_and_cli_draft_note() -> None:
    """S2 A6 re-review: `stage_advice` refuses outright, `{"staged": false,
    "reason": ...}`, before the game has a turn (`STAGE_ADVICE_PREGAME`) -
    client-visible and distinct from a draft going stale later. And the CLI
    transport's own `draft.note` (folded into a printed answer's `draft`
    block by `seat_cli.py`'s `emit`, never by the MCP transport) surfaces a
    filesystem problem persisting or loading a staged draft rather than
    raising past the command's own JSON answer."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-20`", 1)[1].split("`seatkit-19`", 1)[0]
    for term in ("pregame", "staged", "draft.note", "CLI"):
        assert term in paragraph, f"the seatkit-20 paragraph does not name {term}"


def test_seatkit_20_names_the_ledger_metrics_stamps() -> None:
    """S2 A7: `active_player`, `priority_holder` and `turns_taken` are
    stamped on every ledger row, not only the state view."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-20`", 1)[1].split("`seatkit-19`", 1)[0]
    for term in ("active_player", "priority_holder", "turns_taken"):
        assert term in paragraph, f"the seatkit-20 paragraph does not name {term}"


def test_seatkit_20_names_the_opening_dsl_additions() -> None:
    """o-opening-dsl's client-visible additions, merged into seatkit-20:
    `own_turns_taken` on the player view, the hypothetical-facts route (hand,
    candidate and battlefield facts, never library ids or order), the new
    selector fields, the `turn` condition, `prefer`/`ties`,
    `searches_for_any`, `land_face`, `Answer.choice`, and a MAY answered no
    when its yes is illegal."""
    source = Path(seatkit.__file__).read_text()
    changelog = source.split("PROTOCOL_VERSION = ", 1)[1]
    paragraph = changelog.split("`seatkit-20`", 1)[1].split("`seatkit-19`", 1)[0]
    for term in (
        "own_turns_taken",
        "/playscripts/hypothetical-facts",
        "hand",
        "candidate",
        "battlefield",
        "library ids",
        "enters_any",
        "unlocks",
        "unlocks_by_search",
        "unlocks_by_basic_search",
        "unlocks_life_paid",
        "affordable",
        "castable_now",
        '"turn"',
        "prefer",
        "ties",
        "searches_for_any",
        "land_face",
        "Answer.choice",
        "MAY",
        "affirmative_is_legal",
    ):
        assert term in paragraph, f"the seatkit-20 paragraph does not name {term}"
