"""The UI's own drift guard, mirroring `test_decision_kinds.py`.

`decide` (`app.py`, `decisions.ANSWERABLE_KINDS`) is only half of what stops a
new `DecisionKind` from freezing a *live* game: it also needs a form in the
browser UI's own decision-kind table, or a human clicking through cannot
answer a question the server-side endpoint would gladly accept. This test
enumerates the engine's own `DecisionKind` members (the same enumeration
`test_decision_kinds.py` walks) and fails, by name, the moment one of them has
no entry in that table - so a future engine addition fails a test here instead
of silently hanging a live browser game the way `decisions.py`'s module
docstring already documents happening twice server-side.

**Two tables, both live right now.** `ui/app.js`'s `DECISION_RENDERERS` is the
table that predates Task 12, and it is still what a live browser actually
answers a decision through today - Task 12 added `ui/decisions.js`'s
`DECISION_KINDS` as a pure view-model table, but no screen is wired to it yet
(that is a later task in this plan), and `ui/app.js` is deliberately not
edited by this task, so its own decision box keeps serving real games in the
meantime. A kind missing from *either* table would hang a live game right
now, so `_TABLES` below parametrizes every kind-coverage check over both -
`ui/app.js` is deleted in a later task of this same plan, so its case is
skipped (not failed) once the file is gone, rather than blocking that
deletion.

Neither table is Python-importable (there is deliberately no build step,
`CLAUDE.md`/the task brief both say so), so this reads each file's source
text and extracts its own object-literal keys with a regex tied to that
object's exact shape - the same "read the real artifact, do not re-implement
its logic" approach `test_decision_kinds.py` takes reading
`decisions.ANSWERABLE_KINDS` directly rather than hard-coding a copy of it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from magic_cards_model.machines.decisions import DecisionKind

from magic_cards_model_api import decisions

UI_APP_JS = Path(__file__).resolve().parents[1] / "ui" / "app.js"
UI_DECISIONS_JS = Path(__file__).resolve().parents[1] / "ui" / "decisions.js"
UI_COMBAT_JS = Path(__file__).resolve().parents[1] / "ui" / "combat.js"

#: (path, object-literal const name), one per place a decision-kind table
#: lives right now. See the module docstring for why both are checked.
_TABLES = [
    pytest.param(UI_APP_JS, "DECISION_RENDERERS", id="app.js:DECISION_RENDERERS"),
    pytest.param(UI_DECISIONS_JS, "DECISION_KINDS", id="decisions.js:DECISION_KINDS"),
]

#: `declare_attackers`/`declare_blockers` never reach `decide` at all
#: (`decisions.SERVED_ELSEWHERE`) - the UI answers them through
#: `POST .../actions/attack` and `POST .../actions/declare-blockers`
#: instead, via `attackBody`/`blockBody` in `ui/combat.js`. Checked by name
#: below rather than assumed, the same way `test_decision_kinds.py` proves
#: `decide`'s refusal for them rather than merely citing the module
#: docstring.
_UI_HANDLED_ELSEWHERE = {
    DecisionKind.DECLARE_ATTACKERS: "attackBody",
    DecisionKind.DECLARE_BLOCKERS: "blockBody",
}

#: Empty, and that is the point: every `DecisionKind` the engine can ask now
#: reaches a live seat somehow.
#:
#: `MULLIGAN` used to be the one kind with no live path at all - CR 103.5's
#: declaration was genuinely asked of each player, but
#: `machines/decisions.py`'s `ask_mulligan` answered it from
#: `DecisionScript.take`/`default_answer` without ever consulting
#: `Game.decisions.provider`, the mailbox `pending_decision` is built from.
#: The engine closed that: `ask_mulligan` now consults the provider between
#: the script and the default, so a `mulligan` really does land in a seat's
#: `pending_decision` whenever `Game.settle_pregame` walks the pregame. It is
#: in `decisions.ANSWERABLE_KINDS` and has its own entry in both of
#: `_TABLES` (a plain yes/no, like `may`). A seat declaring for *itself* has
#: a second, non-blocking transport that raises no decision at all - the
#: pregame panel over `POST .../pregame/mulligan` - which is what `/play/`
#: actually uses.
#:
#: `OPENING_HAND_ACTION` sat here before that and moved out for the same
#: reason: `Game.take_opening_hand_actions` asks it through `Game.ask`.
_NEVER_REACHES_A_LIVE_SEAT: set[DecisionKind] = set()


def _object_literal_keys(path: Path, const_name: str) -> set[str]:
    """Every kind name used as `const_name`'s object-literal key in `path`."""
    text = path.read_text()
    match = re.search(rf"const {const_name} = \{{(.*?)\n\}};", text, re.S)
    assert match is not None, f"{path.name} no longer defines {const_name} - update this test"
    return set(re.findall(r"^\s*(\w+):", match.group(1), re.M))


def test_ui_decisions_js_exists() -> None:
    assert UI_DECISIONS_JS.is_file(), "ui/decisions.js is missing - the browser UI was not built"


@pytest.mark.parametrize("path,const_name", _TABLES)
def test_ui_decision_kinds_match_decide_answerable_kinds(path: Path, const_name: str) -> None:
    """Every kind `decide` accepts (`decisions.ANSWERABLE_KINDS`) has an entry
    in this table, and vice versa - no kind the UI claims to handle that the
    server would actually refuse. See the module docstring for why both
    `ui/app.js`'s `DECISION_RENDERERS` and `ui/decisions.js`'s
    `DECISION_KINDS` are checked."""
    if not path.is_file():
        pytest.skip(f"{path.name} is gone - already removed from the served UI")
    server_side = {kind.value for kind in decisions.ANSWERABLE_KINDS}
    ui_side = _object_literal_keys(path, const_name)
    missing_in_ui = server_side - ui_side
    assert not missing_in_ui, (
        f"decide() answers {sorted(missing_in_ui)} but {path.name}'s {const_name} "
        "has no form for it - a human playing through the browser cannot answer this "
        "kind of decision, and the game will sit frozen until it times out"
    )
    stale_in_ui = ui_side - server_side
    assert not stale_in_ui, (
        f"{path.name} claims to handle {sorted(stale_in_ui)}, which decide() does not "
        "accept - dead code or a kind that was removed from ANSWERABLE_KINDS"
    )


@pytest.mark.parametrize("path,const_name", _TABLES)
def test_every_engine_decision_kind_is_covered_somewhere_in_the_ui(
    path: Path, const_name: str
) -> None:
    """Every `DecisionKind` the engine can ask is, from a browser player's own
    point of view, one of: answerable through the decision box
    (`ANSWERABLE_KINDS`), answerable through a dedicated action UI
    (`_UI_HANDLED_ELSEWHERE`), or provably unable to ever reach a live seat
    at all (`_NEVER_REACHES_A_LIVE_SEAT`). A kind in none of the three is the
    exact drift this test exists to catch. Parametrized the same way as
    `test_ui_decision_kinds_match_decide_answerable_kinds` above - see the
    module docstring."""
    if not path.is_file():
        pytest.skip(f"{path.name} is gone - already removed from the served UI")
    ui_side = _object_literal_keys(path, const_name)
    combat_js_text = UI_COMBAT_JS.read_text()
    for kind in DecisionKind:
        if kind in decisions.ANSWERABLE_KINDS:
            assert kind.value in ui_side, (
                f"{kind} is answerable server-side but {path.name}'s {const_name} has "
                "no entry for it"
            )
        elif kind in _UI_HANDLED_ELSEWHERE:
            marker = _UI_HANDLED_ELSEWHERE[kind]
            assert marker in combat_js_text, (
                f"{kind} is supposed to be handled by {marker}() in ui/combat.js, "
                "but that function is no longer there"
            )
        elif kind in _NEVER_REACHES_A_LIVE_SEAT:
            pass
        else:
            raise AssertionError(
                f"{kind} is a new DecisionKind this test has not been taught about: add "
                "it to ANSWERABLE_KINDS and every table in _TABLES, to _UI_HANDLED_ELSEWHERE "
                "with its own dedicated UI, or to _NEVER_REACHES_A_LIVE_SEAT with proof (like "
                "decisions.SERVED_ELSEWHERE's MULLIGAN case) that it can never reach a "
                "live seat's pending_decision at all"
            )
        # Every kind the engine has must be accounted for by one of the three
        # branches above, or `decisions.py`'s own drift test already failed
        # first (this module imports `decisions.ANSWERABLE_KINDS` directly,
        # so the two can never silently disagree about which kinds are
        # server-answerable).
