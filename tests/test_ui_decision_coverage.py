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

**Two tables, until Task 17.** `ui/app.js`'s `DECISION_RENDERERS` was the
table that predated Task 12, and it was what a live browser actually answered
a decision through for a while - Task 12 added `ui/decisions.js`'s
`DECISION_KINDS` as a pure view-model table, but no screen was wired to it yet
at the time, so `ui/app.js`'s own decision box kept serving real games in the
meantime, and `_TABLES` below parametrized every kind-coverage check over
both so a kind missing from *either* would fail loudly. Task 17 rewrote
`ui/app.js` as the new buildless module (`render/ribbon.js` and
`render/decision_controls.js` render every decision through
`ui/decisions.js`'s `DECISION_KINDS` now), and that rewrite carries no
`DECISION_RENDERERS` object at all - so `_TABLES` still parametrizes over both
paths (a regression that reintroduced a stray table would still be caught),
but `_object_literal_keys` skips (not fails) a path whose own `const_name`
object literal is genuinely absent, exactly the way it already skipped a
path that was deleted outright.

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

#: `declare_attackers`/`declare_blockers` reach `decide` now (pausing put
#: both in `decisions.ANSWERABLE_KINDS` alongside every other kind), but the
#: UI never routes a human through the generic decision box for either: it
#: already has its own dedicated attack/block screen (`app.js`'s
#: `planPermanentClick` -> `{kind: "attack"}`/`{kind: "block"}`,
#: `confirmAttack`/`confirmBlocks`), driven by `attackBody`/`blockBody` in
#: `ui/combat.js` and posted straight to `actions/attack`/
#: `actions/declare-blockers` - the same two aliases `decide()` itself
#: resumes the paused command through (`app.py`'s `_answer_declaration`), not
#: a second transport around it. So `ui/decisions.js`'s `DECISION_KINDS`
#: deliberately carries no entry for either: a flat yes/no-shaped control has
#: nothing useful to render for "pick which creatures attack, and whom", and
#: the dedicated screen already exists and already works. Checked by name
#: below rather than assumed, the same way `test_decision_kinds.py` proves
#: `decide`'s answerability for them rather than merely citing the module
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
#: `DecisionScript.take`/`default_answer` without ever consulting the live
#: blocking transport the mailbox `pending_decision` was built from.
#: Pausing closed that, and T8 then deleted the transport outright:
#: `ask_mulligan` consults the scripted answer, then the replay tape, then
#: raises `DecisionPending` when neither has one, so a `mulligan` really does
#: land in a seat's `pending_decision` whenever `Game.settle_pregame` walks
#: the pregame. It is
#: in `decisions.ANSWERABLE_KINDS` and has its own entry in both of
#: `_TABLES` (a plain yes/no, like `may`). A seat declaring for *itself* has
#: a second, non-blocking transport that raises no decision at all - the
#: pregame panel over `POST .../pregame/mulligan` - which is what `/play/`
#: actually uses.
#:
#: `OPENING_HAND_ACTION` sat here before that and moved out for the same
#: reason: `Game.take_opening_hand_actions` asks it through `Game.ask`.
_NEVER_REACHES_A_LIVE_SEAT: set[DecisionKind] = set()


def _object_literal_keys(path: Path, const_name: str) -> set[str] | None:
    """Every kind name used as `const_name`'s object-literal key in `path`, or
    `None` when `path` no longer defines that object literal at all (Task 17
    dropped `ui/app.js`'s own `DECISION_RENDERERS` entirely - see the module
    docstring)."""
    text = path.read_text()
    match = re.search(rf"const {const_name} = \{{(.*?)\n\}};", text, re.S)
    if match is None:
        return None
    return set(re.findall(r"^\s*(\w+):", match.group(1), re.M))


def test_ui_decisions_js_exists() -> None:
    assert UI_DECISIONS_JS.is_file(), "ui/decisions.js is missing - the browser UI was not built"


@pytest.mark.parametrize("path,const_name", _TABLES)
def test_ui_decision_kinds_match_decide_answerable_kinds(path: Path, const_name: str) -> None:
    """Every kind `decide` accepts (`decisions.ANSWERABLE_KINDS`) has an entry
    in this table, and vice versa - no kind the UI claims to handle that the
    server would actually refuse. See the module docstring for why both
    `ui/app.js`'s `DECISION_RENDERERS` and `ui/decisions.js`'s
    `DECISION_KINDS` are checked.

    `_UI_HANDLED_ELSEWHERE` kinds are excluded from the "missing" half on
    purpose: `decide` genuinely answers `declare_attackers`/
    `declare_blockers` now, but this flat table is not how a human answers
    either - see `_UI_HANDLED_ELSEWHERE`'s own docstring for the dedicated
    screen that is. They stay *in* the "stale" half below: if this table ever
    grew an entry for one anyway, that would be dead code (the dedicated
    screen would still be the one actually posting the answer), worth
    catching the same way any other stale entry is.
    """
    if not path.is_file():
        pytest.skip(f"{path.name} is gone - already removed from the served UI")
    server_side = {kind.value for kind in decisions.ANSWERABLE_KINDS}
    handled_elsewhere = {kind.value for kind in _UI_HANDLED_ELSEWHERE}
    ui_side = _object_literal_keys(path, const_name)
    if ui_side is None:
        pytest.skip(f"{path.name} no longer defines {const_name} - its own table is gone")
    missing_in_ui = server_side - ui_side - handled_elsewhere
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
    point of view, one of: answerable through a dedicated action UI
    (`_UI_HANDLED_ELSEWHERE`), answerable through the generic decision box
    (`ANSWERABLE_KINDS`), or provably unable to ever reach a live seat at all
    (`_NEVER_REACHES_A_LIVE_SEAT`). A kind in none of the three is the exact
    drift this test exists to catch. Parametrized the same way as
    `test_ui_decision_kinds_match_decide_answerable_kinds` above - see the
    module docstring.

    `_UI_HANDLED_ELSEWHERE` is checked *before* `ANSWERABLE_KINDS`, not
    `elif`-excluded by it: `declare_attackers`/`declare_blockers` are both
    server-answerable (in `ANSWERABLE_KINDS`) *and* UI-handled-elsewhere at
    once now, which the two-branch, mutually-exclusive shape this test used
    to have could not express - it would have demanded a flat `DECISION_KINDS`
    entry for a kind the dedicated combat screen already owns. Checking the
    dedicated-UI membership first is what lets a kind be both without this
    test contradicting itself.
    """
    if not path.is_file():
        pytest.skip(f"{path.name} is gone - already removed from the served UI")
    ui_side = _object_literal_keys(path, const_name)
    if ui_side is None:
        pytest.skip(f"{path.name} no longer defines {const_name} - its own table is gone")
    combat_js_text = UI_COMBAT_JS.read_text()
    for kind in DecisionKind:
        if kind in _UI_HANDLED_ELSEWHERE:
            marker = _UI_HANDLED_ELSEWHERE[kind]
            assert marker in combat_js_text, (
                f"{kind} is supposed to be handled by {marker}() in ui/combat.js, "
                "but that function is no longer there"
            )
        elif kind in decisions.ANSWERABLE_KINDS:
            assert kind.value in ui_side, (
                f"{kind} is answerable server-side but {path.name}'s {const_name} has "
                "no entry for it"
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


#: The kinds answered in `ui/render/decision_window.js`'s floating window
#: (several cards to see, arrange or pick) - every other `DECISION_KINDS`
#: entry keeps rendering in `#prompt-box`.
_WINDOW_KINDS = {
    "scry",
    "surveil",
    "choose_cards",
    "order_triggers",
    "mulligan",
    "assign_combat_damage",
}


def test_every_ui_decision_kind_has_exactly_one_surface() -> None:
    """Routing is decided by `ui/decisions.js`'s `usesDecisionWindow`, and a
    kind with a renderer must land on exactly one surface: the decision
    window for the complex kinds, the prompt box for the rest. The combat
    declarations are on neither - their dedicated screen owns them."""
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    from ui_node import run_module

    ui_side = _object_literal_keys(UI_DECISIONS_JS, "DECISION_KINDS")
    assert ui_side is not None
    routed = run_module(
        "decisions.js",
        "Object.fromEntries(ks.map((k) => [k, m.usesDecisionWindow({ kind: k })]))",
        ks=sorted(ui_side | {kind.value for kind in _UI_HANDLED_ELSEWHERE}),
    )
    window = {kind for kind, uses in routed.items() if uses}
    assert window == _WINDOW_KINDS
    assert not window & {kind.value for kind in _UI_HANDLED_ELSEWHERE}
    prompt = ui_side - window
    assert prompt | window == ui_side
    assert prompt == {
        "may",
        "pay_life",
        "pay_energy",
        "pay_mana",
        "pay_energy_amount",
        "pay_life_amount",
        "opening_hand_action",
        "choose_targets",
        "choose_a_quality",
        "choose_a_mode",
        "put_on_top_or_bottom",
        "put_into_hand_or_graveyard",
        "choose_pile",
        "choose_players",
        "dredge",
    }


#: The small kinds that open `ui/render/decision_popover.js`'s popover beside
#: their source card when `pending_decision.source_card_id` names a rendered
#: card, and fall back to `#prompt-box` otherwise. `choose_targets` is small
#: but answered by clicking the board, so it never anchors.
_POPOVER_KINDS = {
    "may",
    "pay_life",
    "pay_energy",
    "pay_mana",
    "pay_energy_amount",
    "pay_life_amount",
    "opening_hand_action",
    "choose_a_quality",
    "choose_a_mode",
    "put_on_top_or_bottom",
    "put_into_hand_or_graveyard",
    "choose_pile",
}


def test_every_popover_kind_has_a_prompt_box_renderer_and_no_window() -> None:
    """`ui/decisions.js`'s `usesDecisionPopover` names exactly the small kinds
    that anchor to their card. Each still has its `DECISION_KINDS` renderer
    (the popover and its ribbon fallback render through it), none of them
    uses the decision window, and the combat declarations are on no surface
    of this table at all."""
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    from ui_node import run_module

    ui_side = _object_literal_keys(UI_DECISIONS_JS, "DECISION_KINDS")
    assert ui_side is not None
    kinds = sorted(ui_side | {kind.value for kind in _UI_HANDLED_ELSEWHERE})
    routed = run_module(
        "decisions.js",
        "Object.fromEntries(ks.map((k) => [k, "
        "[m.usesDecisionPopover({ kind: k }), m.usesDecisionWindow({ kind: k })]]))",
        ks=kinds,
    )
    popover = {kind for kind, (anchors, _) in routed.items() if anchors}
    assert popover == _POPOVER_KINDS
    assert popover <= ui_side, "an anchored kind lost its renderer"
    assert not {kind for kind in popover if routed[kind][1]}, "a kind is on two surfaces"
    assert not popover & {kind.value for kind in _UI_HANDLED_ELSEWHERE}
    assert ui_side - popover - _WINDOW_KINDS == {"choose_targets", "choose_players", "dredge"}
