"""The UI's own drift guard, mirroring `test_decision_kinds.py`.

`decide` (`app.py`, `decisions.ANSWERABLE_KINDS`) is only half of what stops a
new `DecisionKind` from freezing a *live* game: it also needs a form in
`ui/app.js`'s `DECISION_RENDERERS`, or a human clicking through the browser UI
has no way to answer a question the server-side endpoint would gladly accept.
This test enumerates the engine's own `DecisionKind` members (the same
enumeration `test_decision_kinds.py` walks) and fails, by name, the moment one
of them has no renderer in the UI - so a future engine addition fails a test
here instead of silently hanging a live browser game the way `decisions.py`'s
module docstring already documents happening twice server-side.

`ui/app.js` is not Python-importable (there is deliberately no build step,
`CLAUDE.md`/the task brief both say so), so this reads its source text and
extracts `DECISION_RENDERERS`' own object-literal keys with a regex tied to
that object's exact shape - the same "read the real artifact, do not
re-implement its logic" approach `test_decision_kinds.py` takes reading
`decisions.ANSWERABLE_KINDS` directly rather than hard-coding a copy of it.
"""

from __future__ import annotations

import re
from pathlib import Path

from magic_cards_model.machines.decisions import DecisionKind

from magic_cards_model_api import decisions

UI_APP_JS = Path(__file__).resolve().parents[1] / "ui" / "app.js"

#: `declare_attackers`/`declare_blockers` never reach `decide` at all
#: (`decisions.SERVED_ELSEWHERE`) - the UI answers them through
#: `POST .../actions/attack` and `POST .../actions/declare-blockers`
#: instead, via `openAttackModal`/`openBlockModal` in `ui/app.js`. Checked
#: by name below rather than assumed, the same way `test_decision_kinds.py`
#: proves `decide`'s refusal for them rather than merely citing the module
#: docstring.
_UI_HANDLED_ELSEWHERE = {
    DecisionKind.DECLARE_ATTACKERS: "openAttackModal",
    DecisionKind.DECLARE_BLOCKERS: "openBlockModal",
}

#: `MULLIGAN` is the one `DecisionKind` with no live-decision path *at all* -
#: not a UI gap, an engine/API one: `machines/decisions.py`'s `ask_mulligan`
#: is settled inside `Match.start_game`, before a game (and its uids) even
#: exist, so `pending_decision` can never carry a `mulligan` kind for any
#: seat to see. Nothing in the UI needs to render a form for a question that
#: can never arrive - recorded here, by name, rather than silently absent,
#: matching `decisions.SERVED_ELSEWHERE`'s own reasoning for the same kind.
_NEVER_REACHES_A_LIVE_SEAT = {DecisionKind.MULLIGAN}


def _decision_renderers_object() -> str:
    text = UI_APP_JS.read_text()
    match = re.search(r"const DECISION_RENDERERS = \{(.*?)\n\};", text, re.S)
    assert match is not None, "ui/app.js no longer defines DECISION_RENDERERS - update this test"
    return match.group(1)


def _ui_answerable_kinds() -> set[str]:
    """Every kind name used as a `DECISION_RENDERERS` object key."""
    body = _decision_renderers_object()
    return set(re.findall(r"^\s*(\w+):", body, re.M))


def test_ui_app_js_exists() -> None:
    assert UI_APP_JS.is_file(), "ui/app.js is missing - the browser UI was not built"


def test_ui_decision_renderers_match_decide_answerable_kinds() -> None:
    """Every kind `decide` accepts (`decisions.ANSWERABLE_KINDS`) has a
    `DECISION_RENDERERS` entry in `ui/app.js`, and vice versa - no kind the
    UI claims to handle that the server would actually refuse."""
    server_side = {kind.value for kind in decisions.ANSWERABLE_KINDS}
    ui_side = _ui_answerable_kinds()
    missing_in_ui = server_side - ui_side
    assert not missing_in_ui, (
        f"decide() answers {sorted(missing_in_ui)} but ui/app.js's DECISION_RENDERERS "
        "has no form for it - a human playing through the browser cannot answer this "
        "kind of decision, and the game will sit frozen until it times out"
    )
    stale_in_ui = ui_side - server_side
    assert not stale_in_ui, (
        f"ui/app.js claims to handle {sorted(stale_in_ui)}, which decide() does not "
        "accept - dead code or a kind that was removed from ANSWERABLE_KINDS"
    )


def test_every_engine_decision_kind_is_covered_somewhere_in_the_ui() -> None:
    """Every `DecisionKind` the engine can ask is, from a browser player's own
    point of view, one of: answerable through the decision box
    (`ANSWERABLE_KINDS`), answerable through a dedicated action UI
    (`_UI_HANDLED_ELSEWHERE`), or provably unable to ever reach a live seat
    at all (`_NEVER_REACHES_A_LIVE_SEAT`). A kind in none of the three is the
    exact drift this test exists to catch."""
    app_js_text = UI_APP_JS.read_text()
    for kind in DecisionKind:
        if kind in decisions.ANSWERABLE_KINDS:
            assert kind.value in _ui_answerable_kinds(), (
                f"{kind} is answerable server-side but ui/app.js has no "
                "DECISION_RENDERERS entry for it"
            )
        elif kind in _UI_HANDLED_ELSEWHERE:
            marker = _UI_HANDLED_ELSEWHERE[kind]
            assert marker in app_js_text, (
                f"{kind} is supposed to be handled by {marker}() in ui/app.js, "
                "but that function is no longer there"
            )
        elif kind in _NEVER_REACHES_A_LIVE_SEAT:
            pass
        else:
            raise AssertionError(
                f"{kind} is a new DecisionKind this test has not been taught about: add "
                "it to ANSWERABLE_KINDS+DECISION_RENDERERS, to _UI_HANDLED_ELSEWHERE with "
                "its own dedicated UI, or to _NEVER_REACHES_A_LIVE_SEAT with proof (like "
                "decisions.SERVED_ELSEWHERE's MULLIGAN case) that it can never reach a "
                "live seat's pending_decision at all"
            )
        # Every kind the engine has must be accounted for by one of the three
        # branches above, or `decisions.py`'s own drift test already failed
        # first (this module imports `decisions.ANSWERABLE_KINDS` directly,
        # so the two can never silently disagree about which kinds are
        # server-answerable).
