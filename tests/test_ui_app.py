"""`ui/app.js` under Node: the click planners, run against the real shipped
module rather than a Python restatement of it.

`app.js` ties every pure module (`actions.js`, `priority.js`, `combat.js`,
`decisions.js`, `targeting.js`) and every render module (`board.js`,
`ribbon.js`, `advice.js`, `lobby.js`, `panels.js`) together into the running
page - most of what it does is DOM and `fetch`, neither of which this file
can exercise under Node. What it *can* exercise, and what actually decides
the click flows the task brief asks for, is the two pure planners
`app.js` factors its click handlers through: `planHandCardClick(card, app)`
("what does clicking this card in my hand do") and
`planPermanentClick(card, app, state)` ("what does clicking this permanent
on the battlefield do"). `onHandCard`/`onPermanent` are thin wrappers that
act on whichever plan comes back - so testing the planner is testing the
real decision, not a shadow copy of it.

Importing the module at all needs `localStorage` on `globalThis` before the
`import` resolves - `app.js`'s own top-level `createStore({... stops:
P.loadStops(localStorage) ...})` runs at import time, and `P.loadStops`
reads it immediately. `window` and `document` are deliberately left
undefined (not merely unstubbed) so `app.js`'s own closing guard -
`if (typeof window !== "undefined" && typeof document !== "undefined")
main();` - never fires: that guard exists precisely so a real browser runs
`main()` and this import does not. `fetch` is stubbed to throw, so any test
that accidentally exercises a network path fails loudly rather than hanging.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "ui" / "app.js"
ACTIONS_JS = Path(__file__).resolve().parents[1] / "ui" / "actions.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def run_app(expression: str, **bindings: Any) -> Any:
    consts = "\n".join(f"const {name} = {json.dumps(value)};" for name, value in bindings.items())
    script = f"""
    import {{ pathToFileURL }} from "node:url";
    globalThis.localStorage = {{
      getItem: () => null,
      setItem() {{}},
      removeItem() {{}},
    }};
    globalThis.fetch = () => {{
      throw new Error("test_ui_app.py's planners must never touch the network");
    }};
    const m = await import(pathToFileURL({json.dumps(str(APP_JS))}).href);
    {consts}
    const __result = await eval({json.dumps(expression)});
    process.stdout.write(JSON.stringify(__result === undefined ? null : __result));
    """
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout or "null")


def test_app_js_still_exports_start_game_for_the_routes_test() -> None:
    """`tests/test_ui_routes.py` asserts the served `/app.js` contains the
    literal string `startGame` - this is the same guarantee, checked against
    the actual export rather than a substring in the served text."""
    assert run_app("typeof m.startGame") == "function"


def test_starting_a_game_navigates_to_its_own_bare_uuid_path_by_default() -> None:
    """T7: the old in-page `#lobby`/`#game` hidden toggle became a real
    navigation to `/g/<uuid>/` in T4; T7 makes the bare `/<uuid>/` form the
    navigation target instead - the owner's own literal ask - what the
    lobby's Start/Open buttons trigger via `handlers.startGame`, and what a
    browser reload of that URL then lands back on (`document.baseURI`
    resolves the game from there, not from `store`). `localStorage` is still
    written first, so the entry is there for the reloaded page to find."""
    result = run_app(
        """(async () => {
          globalThis.window = { location: { href: "https://host/" } };
          const saves = [];
          globalThis.localStorage = {
            getItem: () => null,
            setItem: (k, v) => saves.push([k, v]),
            removeItem() {},
          };
          await m.startGame({ game_id: "g99", uid: "u1", aiUid: "u2" });
          return { href: window.location.href, saves };
        })()"""
    )
    assert result["href"] == "/g99/"
    assert len(result["saves"]) == 1, "the game must still be saved before navigating away"


def test_starting_a_game_does_not_navigate_when_told_not_to() -> None:
    """The one caller that must NOT navigate: booting straight into the game
    screen on a page load that is already at `/<uuid>/` (the id came from
    `document.baseURI`, not from a Start/Open click) - navigating again would
    loop."""
    result = run_app(
        """(async () => {
          globalThis.window = { location: { href: "https://host/g99/" } };
          globalThis.document = {
            querySelector: (sel) => {
              if (sel === "#lobby") return { hidden: false };
              if (sel === "#game") return { hidden: true };
              return null;
            },
          };
          globalThis.fetch = (path) => {
            if (path.includes("/log")) {
              return Promise.resolve({ ok: true, json: async () => ({ log: [], last_seq: 0 }) });
            }
            return Promise.resolve({
              ok: true,
              json: async () => ({
                you: "alice", pending_decision: null, step: "precombat_main",
                active_player: "alice", priority_holder: "alice", battlefield: [],
                hands: { alice: [] }, stack: [], combat: { attacks: [] },
                winner: null, drawn: false, pregame: null,
              }),
            });
          };
          await m.startGame({ game_id: "g99", uid: "u1", aiUid: "u2" }, { navigate: false });
          return { href: window.location.href };
        })()"""
    )
    assert result["href"] == "https://host/g99/", "must not have navigated anywhere"


# -- the lobby-unreachable bug: `main()`'s boot decision, factored out ------
#
# Owner report: "it's not possible to open lobby while game is running -> I
# get permanently redirected back to game". `main()` used to special-case
# "exactly one saved game" and auto-`startGame` (navigate: true) into it on
# *every* boot at the bare `/` root, not once - so as long as `mcm.games.v1`
# held one entry (the common case: one game, never dropped from storage
# short of the lobby's own End button), visiting `/` immediately bounced
# back into the game and the lobby could never be seen. `bootTarget` is the
# pure decision `main()` now defers to, the same reason `planHandCardClick`/
# `planPermanentClick` are factored out above: exercised here with no DOM
# and no network, rather than reimplemented in Python.


def test_boot_target_for_the_lobby_root_is_always_the_lobby_regardless_of_saved_count() -> None:
    """The bug, pinned directly: `bootTarget(null, saved)` - `null` is
    `gameIdFromBaseURI`'s own answer for the bare lobby root - must say
    "show the lobby" whether zero, one, or several games are saved. The
    single-saved-game case is the one the old code special-cased into an
    auto-navigate; it must now behave exactly like every other count."""
    one = [{"gameId": "g1", "uid": "u1", "aiUid": "u2"}]
    two = [
        {"gameId": "g1", "uid": "u1", "aiUid": "u2"},
        {"gameId": "g2", "uid": "u3", "aiUid": "u4"},
    ]
    assert run_app("m.bootTarget(null, [])") == {"kind": "lobby"}
    assert run_app("m.bootTarget(null, saved)", saved=one) == {"kind": "lobby"}
    assert run_app("m.bootTarget(null, saved)", saved=two) == {"kind": "lobby"}


def test_boot_target_for_a_game_url_resolves_straight_into_it_with_its_saved_seat() -> None:
    """A reload of a game tab must stay in that game - the URL names it, not
    stored state (`document.baseURI`, resolved by `gameIdFromBaseURI`
    upstream of this function). The saved seat for that id is attached when
    there is one; an unrecognised id still resolves to the game, just with
    no seat (a spectator) - it does not fall back to the lobby."""
    saved = [{"gameId": "g1", "uid": "u1", "aiUid": "u2"}]
    assert run_app("m.bootTarget('g1', saved)", saved=saved) == {
        "kind": "game",
        "gameId": "g1",
        "uid": "u1",
        "aiUid": "u2",
    }
    assert run_app("m.bootTarget('g9', saved)", saved=saved) == {
        "kind": "game",
        "gameId": "g9",
        "uid": None,
        "aiUid": None,
    }


def test_leaving_the_game_navigates_to_the_lobby_root() -> None:
    result = run_app(
        """(() => {
          globalThis.window = { location: { href: "https://host/g99/" } };
          m.store.set({ gameId: "g99", uid: "u1", state: { you: "alice" } });
          m.leaveGame();
          return { href: window.location.href };
        })()"""
    )
    assert result["href"] == "/"


def test_send_catches_a_rejected_fetch_and_toasts_instead_of_throwing() -> None:
    """`send` is the one place every action path in `app.js` goes through
    instead of calling `api()` directly - a transport failure (a rejected
    `fetch`: the network is down, the server unreachable) must resolve to
    `null` and toast the error, never escape as an unhandled rejection.

    `document` is stubbed here, locally to this one test, with just enough
    (`querySelector("#toast")`) for `toast()` to run - `window` stays
    undefined, so `app.js`'s own `main()` guard still never fires."""
    result = run_app(
        """(async () => {
          const toastEl = { textContent: "", hidden: true };
          globalThis.document = { querySelector: (sel) => (sel === "#toast" ? toastEl : null) };
          globalThis.fetch = () => Promise.reject(new Error("network is down"));
          const res = await m.send("GET", "/api/games/x");
          return { res, toastText: toastEl.textContent, toastHidden: toastEl.hidden };
        })()"""
    )
    assert result["res"] is None, "a transport failure must resolve to null, not throw"
    assert result["toastHidden"] is False
    assert "network is down" in result["toastText"]


# Electrolyze: "up to two target creatures and/or players" - CR 115.6's "up
# to" shape, the case where the regression this file guards actually bit.
# Once both slots are filled, `activeSlotIndex` (and so `targetSlotIndex`)
# has nowhere left to offer (CR 601.2c never forces every "up to" slot filled
# all the way) - a second click on an already-chosen target still has to
# un-choose it rather than being swallowed by that "nowhere to fill" case.
SPLIT = {
    "id": "sp1",
    "name": "Electrolyze",
    "type_line": "Instant",
    "zone": "hand",
    "controller": "alice",
    "cast": {
        "mana_cost": "{1}{U}{R}",
        "needs_x": False,
        "modes": None,
        "targeting": {"targets_anything": True},
    },
}
SPLIT_AFF = {
    "targets_anything": True,
    "understood": True,
    "minimum": 1,
    "maximum": 2,
    "requirements": [
        {
            "phrase": "up to two target creatures and/or players",
            "count": 2,
            "minimum": 1,
            "unbounded": False,
            "variable_count": False,
            "mode": None,
            "mode_text": None,
        }
    ],
    "division": None,
    "candidates": [
        {"id": "bob", "kind": "player", "name": "bob", "requirements": [0]},
        {"id": "c9", "kind": "card", "name": "Memnite", "requirements": [0]},
    ],
}


def test_a_second_click_on_a_chosen_target_still_unchooses_it_once_the_slot_is_full() -> None:
    """The regression: `tryTarget` started resolving every fresh click
    through `targetSlotIndex`/`canFill`, and once an "up to two" slot holds
    both its targets, `targetSlotIndex` has nowhere left to offer (-1) - a
    second click on one of the two already-chosen targets was silently
    swallowed before it ever reached `actions.js`'s own `chooseTarget`, which
    already knows how to unfill an already-chosen target regardless of how
    much room is left. Drives the real, exported `tryTarget` against the
    real, exported `store` (both test-only exports - see their own comments
    in `app.js`) rather than a Python restatement of either."""
    result = run_app(
        """(async () => {
          const A = await import(pathToFileURL(actionsPath).href);
          globalThis.document = { querySelector: () => null };
          const state = { you: "alice", battlefield: [], pending_decision: null };
          m.store.set({ gameId: "g1", uid: "u1", state });
          let action = A.withAffordance(A.begin("cast", split, {}), aff);
          action = A.chooseTarget(action, "bob");
          action = A.chooseTarget(action, "c9");
          m.store.set({ action });
          const fullSlotIndex = A.targetSlotIndex(m.store.get().action);
          await m.tryTarget("bob");
          const after = m.store.get().action;
          return { fullSlotIndex, targetsAfter: after.slots.flatMap((s) => s.targets) };
        })()""",
        split=SPLIT,
        aff=SPLIT_AFF,
        actionsPath=str(ACTIONS_JS),
    )
    assert result["fullSlotIndex"] == -1, (
        "the slot must already be at its own cap for this to be the regression"
    )
    assert result["targetsAfter"] == ["c9"], "a second click on bob must remove it, not no-op"


# -- declareNoAttackers / declareNoBlockers -----------------------------------
#
# CR 508.1a/509.1a's "if any"/"may" made declaring nobody a real declaration
# that pauses the game until somebody makes it (`decisions.ANSWERABLE_KINDS`
# covers both `declare_attackers` and `declare_blockers` now), rather than a
# no-op the engine defaulted to on its own - these two are new for that
# reason, and had no test at all. `declareNoAttackers`/`declareNoBlockers`
# are exported test-only, alongside `store`/`tryTarget` above - see `store`'s
# own comment - and driven here as real, awaited async calls against a
# stubbed `fetch` that also answers `poll()`'s own state/log reads
# afterward, the same way `tryTarget`'s own test drives a real exported
# function rather than a Python restatement of it.


def _fetch_stub_body() -> str:
    """A `fetch` stub for a POST-then-`poll()` flow: records every call
    (method, path, parsed body) into `globalThis.__calls`, answers the two
    GETs `poll()` issues afterward (state, log) with just enough shape for
    `app.js` to finish its own poll cycle without throwing, and answers any
    other POST with `{allowed: true}` - a plain acceptance, since these two
    functions only branch on `res === null` (a transport failure) and
    `res.allowed === false` (a refusal), never on the rest of the body."""
    return """
      globalThis.__calls = [];
      globalThis.document = { querySelector: () => null };
      globalThis.fetch = (path, opts) => {
        const body = opts && opts.body ? JSON.parse(opts.body) : undefined;
        globalThis.__calls.push({ method: opts ? opts.method : "GET", path, body });
        if (opts && opts.method === "POST") {
          return Promise.resolve({ ok: true, json: async () => ({ allowed: true }) });
        }
        if (path.includes("/log")) {
          return Promise.resolve({ ok: true, json: async () => ({ log: [], last_seq: 0 }) });
        }
        return Promise.resolve({
          ok: true,
          json: async () => ({
            you: "alice", pending_decision: null, step: "declare_attackers",
            active_player: "alice", priority_holder: "alice", battlefield: [],
            hands: { alice: [] }, stack: [], combat: { attacks: [] },
            winner: null, drawn: false, pregame: null,
          }),
        });
      };
    """


def test_declare_no_attackers_sends_an_empty_attack_declaration() -> None:
    result = run_app(
        _fetch_stub_body()
        + """
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", combatDraft: { a1: "bob" },
            state: { you: "alice" },
          });
          await m.declareNoAttackers();
          return { calls: globalThis.__calls, combatDraft: m.store.get().combatDraft };
        })()
        """
    )
    posts = [c for c in result["calls"] if c["method"] == "POST"]
    assert len(posts) == 1, result["calls"]
    assert posts[0]["path"] == "/api/games/g1/actions/attack?uid=u1"
    assert posts[0]["body"] == {"attackers": []}, "must override the draft, not send it"
    assert result["combatDraft"] == {}, "a declared attack (even 'none') clears the draft"


def test_declare_no_blockers_sends_an_empty_block_declaration() -> None:
    result = run_app(
        _fetch_stub_body()
        + """
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", combatDraft: { c1: "atk1" },
            state: { you: "alice" },
          });
          await m.declareNoBlockers();
          return { calls: globalThis.__calls, combatDraft: m.store.get().combatDraft };
        })()
        """
    )
    posts = [c for c in result["calls"] if c["method"] == "POST"]
    assert len(posts) == 1, result["calls"]
    assert posts[0]["path"] == "/api/games/g1/actions/declare-blockers?uid=u1"
    assert posts[0]["body"] == {"player": "alice", "blocks": []}, (
        "must override the draft, not send it"
    )
    assert result["combatDraft"] == {}, "a declared block (even 'none') clears the draft"


# -- tickRun() / unticking your own declare_attackers must not just fail to --
# -- stop, it must actually unfreeze the game -------------------------------
#
# `stopReason` not stopping for an unticked `declare_attackers` pause is not
# the same as the game progressing: the pause is a `pending_decision`
# (CR 508.1 is a turn-based action, not a priority move), so passing
# priority cannot answer it and nothing else clears it on its own.
# `tickRun` is exported test-only, alongside `store`/`declareNoAttackers`
# above, and driven here the same way: a real, awaited call against the
# stubbed `fetch`.


def test_ticking_run_declares_no_attackers_when_the_tick_is_off() -> None:
    result = run_app(
        _fetch_stub_body()
        + """
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1",
            run: { verb: "pass_until", startedTurn: 1, startedStep: "upkeep",
                   startedActive: "alice", knownStackIds: [], passes: 0, stillAtStart: false },
            stops: { mine: [], theirs: [], onStack: true },
            state: {
              you: "alice", step: "beginning_of_combat", turn_number: 1,
              active_player: "alice", priority_holder: "bob", battlefield: [],
              stack: [], combat: { attacks: [] }, winner: null, drawn: false,
              pending_decision: {
                player: "alice", kind: "declare_attackers", prompt: "Declare attackers",
              },
            },
          });
          await m.tickRun();
          return { calls: globalThis.__calls, run: m.store.get().run };
        })()
        """
    )
    posts = [c for c in result["calls"] if c["method"] == "POST"]
    # `declareNoAttackers` calls `poll()` when it is done, which naturally
    # continues the run (`poll()`'s own `tickRun()` call) into whatever the
    # stub's canned post-declaration state offers next - a real `pass-priority`
    # is expected here, not asserted against; what this test pins is that the
    # *first* thing that happens is the attack declaration, with the right
    # body, exactly once.
    attacks = [p for p in posts if p["path"] == "/api/games/g1/actions/attack?uid=u1"]
    assert len(attacks) == 1, result["calls"]
    assert posts[0] is attacks[0], "declaring no attackers must be the first thing tickRun does"
    assert attacks[0]["body"] == {"attackers": []}
    assert result["run"] is not None, (
        "declaring no attackers is not itself a stop - the run stays armed"
    )


def test_ticking_run_does_not_declare_no_attackers_when_the_tick_is_on() -> None:
    """The pair to the test above: ticked, the pause is an ordinary checked
    stop (`stopReason`'s own job) and `tickRun` must not race ahead and
    answer it on the seat's behalf."""
    result = run_app(
        _fetch_stub_body()
        + """
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1",
            run: { verb: "pass_until", startedTurn: 1, startedStep: "upkeep",
                   startedActive: "alice", knownStackIds: [], passes: 0, stillAtStart: false },
            stops: { mine: ["declare_attackers"], theirs: [], onStack: true },
            state: {
              you: "alice", step: "beginning_of_combat", turn_number: 1,
              active_player: "alice", priority_holder: "bob", battlefield: [],
              stack: [], combat: { attacks: [] }, winner: null, drawn: false,
              pending_decision: {
                player: "alice", kind: "declare_attackers", prompt: "Declare attackers",
              },
            },
          });
          await m.tickRun();
          return { calls: globalThis.__calls, run: m.store.get().run };
        })()
        """
    )
    posts = [c for c in result["calls"] if c["method"] == "POST"]
    assert posts == [], result["calls"]


def test_ticking_run_never_passes_while_a_decision_belongs_to_someone_else() -> None:
    """The other half of the owner's overrule: `stopReason` no longer halts
    the run for a decision addressed to bob, but that must never be read as
    "so pass on the strength of whatever `priority_holder` says" - a pause
    rolls the committed game back to before the attempt, so `priority_holder`
    can still read as alice's own even while the pause is on bob's decision
    (set that way here, deliberately, to prove the guard does not depend on
    it). Passing anyway would not merely fail: it crosses the same unanswered
    question, pauses too, and `app.py`'s `_run_command` stores that second
    pause over the first - clobbering the stored pause and discarding the
    question bob was about to answer. `tickRun` must not attempt it."""
    result = run_app(
        _fetch_stub_body()
        + """
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1",
            run: { verb: "pass_until", startedTurn: 1, startedStep: "upkeep",
                   startedActive: "alice", knownStackIds: [], passes: 0, stillAtStart: false },
            stops: { mine: [], theirs: [], onStack: true },
            state: {
              you: "alice", step: "draw", turn_number: 2,
              active_player: "bob", priority_holder: "alice", battlefield: [],
              stack: [], combat: { attacks: [] }, winner: null, drawn: false,
              pending_decision: { player: "bob", kind: "may", prompt: "Pay 2 life?" },
            },
          });
          await m.tickRun();
          return { calls: globalThis.__calls, run: m.store.get().run };
        })()
        """
    )
    posts = [c for c in result["calls"] if c["method"] == "POST"]
    assert posts == [], result["calls"]
    assert result["run"] is not None, "not this run's decision to end its own run over"


def test_ticking_run_never_ends_over_a_pass_that_paused_instead_of_completing() -> None:
    """The owner's report, verbatim: "pass_until is also interrupted on
    opponent's attack step" - and the exact way it happened, found by
    driving the real, shipped `ui/priority.js` and `ui/app.js` against a
    live server (see the session's own manual verification): this seat's
    priority round runs on the opponent's turn too, so a `pass-priority`
    this seat sends can itself be the one that crosses into the OPPONENT's
    own `declare_attackers`/`declare_blockers` turn-based action (CR 508.1,
    CR 509.1) - not a `pending_decision` `stopReason` ever saw beforehand,
    since it did not exist until this very pass triggered it. The server
    answers `{allowed: null, paused: true, paused_on: {...}}`: the pass
    never completed, the committed game rolled back to before the attempt
    (`commands.py`), and the old code's `if (!out.ok) store.set({run:
    null})` cleared the run over it regardless of whose decision it was -
    ending the run on exactly "the opponent's attack step", matching the
    owner's report. A paused pass must never end the run, own decision or
    the opponent's: the very next tick sees the fresh pause through the
    normal checks (`ownUnansweredAttackDeclaration` for this seat's own
    combat, or the "someone else's decision" guard for anyone else's)."""
    result = run_app(
        """
        globalThis.__calls = [];
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        globalThis.fetch = (path, opts) => {
          const body = opts && opts.body ? JSON.parse(opts.body) : undefined;
          globalThis.__calls.push({ method: opts ? opts.method : "GET", path, body });
          if (opts && opts.method === "POST" && path.includes("pass-priority")) {
            return Promise.resolve({
              ok: true,
              json: async () => ({
                allowed: null, paused: true,
                paused_on: { player: "bob", kind: "declare_attackers" },
              }),
            });
          }
          return Promise.resolve({ ok: true, json: async () => ({ allowed: true }) });
        };
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1",
            run: { verb: "pass_until", startedTurn: 1, startedStep: "upkeep",
                   startedActive: "alice", knownStackIds: [], passes: 0, stillAtStart: false },
            stops: { mine: [], theirs: [], onStack: true },
            state: {
              you: "alice", step: "draw", turn_number: 2,
              active_player: "bob", priority_holder: "alice", battlefield: [],
              stack: [], combat: { attacks: [] }, winner: null, drawn: false,
              pending_decision: null,
            },
          });
          await m.tickRun();
          return { calls: globalThis.__calls, run: m.store.get().run };
        })()
        """
    )
    posts = [c for c in result["calls"] if c["method"] == "POST"]
    assert len(posts) == 1, result["calls"]
    # `?uid=u1`: `send()` puts this seat's own credential on every request
    # it makes, because the server refuses a mutating call that presents
    # none (`game_access.py`'s positive rule). Before that, nine of this
    # file's action paths - `pass-priority` among them - sent nothing.
    assert posts[0]["path"] == "/api/games/g1/actions/pass-priority?uid=u1"
    assert result["run"] is not None, "a paused pass must not end the run"
    assert result["run"]["passes"] == 0, "a pass that never completed must not be recorded as one"


# -- fire() / keep priority by default ---------------------------------------
#
# The owner's words: casting a spell without keeping priority was too easy,
# and the same must apply to activating an ability. The stops panel's "Keep
# priority by default" tick (`stops.keepPriorityByDefault`, on unless
# unticked) is the standing default; the casting strip's per-action tick
# (`action.holdPriority`, `null` until touched, meaning "follow the standing
# default") overrides it for that one action. `fire` is exported test-only,
# alongside `store`/`tryTarget`/`declareNoAttackers` above, and driven here
# the same way: a real, awaited call against the same stubbed `fetch`.

PLANESWALKER_CARD = {
    "id": "c1",
    "abilities": [{"index": 0, "text": "+1: draw a card", "is_mana_ability": False}],
}


def _fire_paths(kind: str, keep_by_default: bool | None, hold: bool | None = None) -> list[str]:
    """POST paths `fire()` sends for one `kind` of action begun the real way
    (`A.begin`), under a standing default (`None` leaves the key out of
    `stops` entirely) and an optional per-action tick."""
    stops = {"mine": [], "theirs": [], "onStack": True}
    if keep_by_default is not None:
        stops["keepPriorityByDefault"] = keep_by_default
    result = run_app(
        _fetch_stub_body()
        + """
        (async () => {
          const A = await import(pathToFileURL(actionsPath).href);
          let action = kind === "activate"
            ? A.begin("activate", card, { abilityIndex: 0 })
            : A.begin("cast", card, {});
          if (hold !== null) action = { ...action, holdPriority: hold };
          m.store.set({
            gameId: "g1", uid: "u1",
            state: { you: "alice", battlefield: [] },
            stops,
            action,
          });
          await m.fire();
          return { calls: globalThis.__calls };
        })()
        """,
        actionsPath=str(ACTIONS_JS),
        kind=kind,
        card=PLANESWALKER_CARD,
        stops=stops,
        hold=hold,
    )
    return [c["path"] for c in result["calls"] if c["method"] == "POST"]


def test_casting_keeps_priority_by_default() -> None:
    paths = _fire_paths("cast", True)
    assert any("actions/cast" in p for p in paths), paths
    assert not any("actions/pass-priority" in p for p in paths), (
        "casting must keep priority by default - " + str(paths)
    )


def test_activating_keeps_priority_by_default() -> None:
    paths = _fire_paths("activate", True)
    assert any("actions/activate" in p for p in paths), paths
    assert not any("actions/pass-priority" in p for p in paths), (
        "activating must keep priority by default - " + str(paths)
    )


def test_a_stops_object_without_the_setting_still_keeps_priority() -> None:
    paths = _fire_paths("cast", None)
    assert not any("actions/pass-priority" in p for p in paths), paths


def test_unticking_the_per_action_tick_passes_after_that_cast() -> None:
    paths = _fire_paths("cast", True, hold=False)
    assert any("actions/cast" in p for p in paths), paths
    assert any("actions/pass-priority" in p for p in paths), (
        "an unticked per-action tick must pass after the cast - " + str(paths)
    )


def test_unticking_the_per_action_tick_passes_after_that_activation() -> None:
    paths = _fire_paths("activate", True, hold=False)
    assert any("actions/activate" in p for p in paths), paths
    assert any("actions/pass-priority" in p for p in paths), paths


def test_standing_off_passes_after_casting() -> None:
    paths = _fire_paths("cast", False)
    assert any("actions/cast" in p for p in paths), paths
    assert any("actions/pass-priority" in p for p in paths), paths


def test_standing_off_passes_after_activating() -> None:
    paths = _fire_paths("activate", False)
    assert any("actions/activate" in p for p in paths), paths
    assert any("actions/pass-priority" in p for p in paths), paths


def test_the_per_action_tick_still_holds_with_the_standing_default_off() -> None:
    """The standing setting sets where the per-action tick starts; ticking it
    for one action is still a live override when the standing one is off."""
    paths = _fire_paths("cast", False, hold=True)
    assert not any("actions/pass-priority" in p for p in paths), paths


# -- auto-pass after a stack action that pauses on a decision ----------------
#
# The owner's report: with "Keep priority by default" off, activating an
# ability should put it on the stack and then pass priority, like casting -
# and it did not. An activation (or a cast) that pauses on a decision - a
# target nobody named yet, a search - answers `{allowed: null, paused: true,
# pending_decision}`. `fire()` read that as done and passed straight away: the
# pass was refused (a decision is still open) with a toast, and when the
# player then answered through `decide()` the action completed and nothing
# passed at all. The pass now waits for the decide that completes the action,
# through `store.pendingRelease`. `cycle()` never passed at all, though
# cycling puts an activated ability on the stack. These drive the real
# `app.js` against a scripted `fetch`: every POST is answered from a
# per-route queue, and every board read answers `globalThis.__state`, which a
# test moves the way the server would.

TARGETING_ABILITY_CARD = {
    "id": "c1",
    "name": "Goblin Bombardment",
    "abilities": [
        {"index": 0, "text": "Sacrifice a creature: deal 1 damage", "is_mana_ability": False}
    ],
}

MANA_ABILITY_CARD = {
    "id": "m1",
    "name": "Llanowar Elves",
    "abilities": [{"index": 0, "text": "{T}: Add {G}.", "is_mana_ability": True}],
}


def _pending(generation: int, player: str = "alice") -> dict[str, Any]:
    return {"player": player, "kind": "choose_targets", "generation": generation}


def _paused(generation: int, player: str = "alice") -> dict[str, Any]:
    return {
        "allowed": None,
        "reason": None,
        "paused": True,
        "pending_decision": _pending(generation, player),
    }


def _release_flow(script: str, keep_by_default: bool, **bindings: Any) -> Any:
    """Run `script` (the body of an async function) against `app.js` with a
    scripted fetch. `respond(route, ...bodies)` queues answers for one POST
    route (`"actions/activate"`, `"decide"`, ...); an unqueued POST is a plain
    `{allowed: true}`. `passes()` counts the pass-priority POSTs so far.
    `board(pending)` sets what the next board read returns."""
    stops = {"mine": [], "theirs": [], "onStack": True, "keepPriorityByDefault": keep_by_default}
    return run_app(
        """
        globalThis.__calls = [];
        globalThis.__queues = {};
        globalThis.__state = {
          you: "alice", pending_decision: null, step: "precombat_main",
          active_player: "alice", priority_holder: "alice", battlefield: [],
          hands: { alice: [] }, stack: [], combat: { attacks: [] },
          winner: null, drawn: false, pregame: null,
        };
        const toastEl = { hidden: true, textContent: "" };
        globalThis.document = { querySelector: (sel) => (sel === "#toast" ? toastEl : null) };
        globalThis.fetch = (path, opts) => {
          const method = opts ? opts.method : "GET";
          const body = opts && opts.body ? JSON.parse(opts.body) : undefined;
          globalThis.__calls.push({ method, path, body });
          if (method === "POST") {
            const route = path.split("/api/games/g1/")[1].split("?")[0];
            const queue = globalThis.__queues[route] || [];
            const answer = queue.length ? queue.shift() : { allowed: true };
            return Promise.resolve({ ok: true, json: async () => answer });
          }
          if (path.includes("/log")) {
            return Promise.resolve({ ok: true, json: async () => ({ log: [], last_seq: 0 }) });
          }
          const snapshot = JSON.parse(JSON.stringify(globalThis.__state));
          return Promise.resolve({ ok: true, json: async () => snapshot });
        };
        const respond = (route, ...bodies) => {
          globalThis.__queues[route] = [...(globalThis.__queues[route] || []), ...bodies];
        };
        const board = (pending) => { globalThis.__state.pending_decision = pending; };
        const passes = () =>
          globalThis.__calls.filter(
            (c) => c.method === "POST" && c.path.includes("actions/pass-priority")
          ).length;
        (async () => {
          const A = await import(pathToFileURL(actionsPath).href);
          m.store.set({
            gameId: "g1", uid: "u1",
            state: JSON.parse(JSON.stringify(globalThis.__state)),
            stops,
          });
          const out = {};
          const toastSeen = () => !toastEl.hidden;
          """
        + script
        + """
          out.posts = globalThis.__calls.filter((c) => c.method === "POST").map((c) => c.path);
          return out;
        })()
        """,
        actionsPath=str(ACTIONS_JS),
        stops=stops,
        **bindings,
    )


_BEGIN_ACTIVATE = """
          m.store.set({ action: A.begin("activate", card, { abilityIndex: 0 }) });
"""

_PAUSED_ACTIVATE = (
    _BEGIN_ACTIVATE
    + """
          respond("actions/activate", paused1);
          board(pending1);
          await m.fire();
          out.afterFire = passes();
          out.toastAfterFire = toastSeen();
          out.armed = m.store.get().pendingRelease ?? null;
"""
)

_COMPLETING_DECIDE = """
          respond("decide", { accepted: true, log_seq: 9 });
          board(null);
          await m.decide({ targets: ["bob"] });
          out.afterDecide = passes();
          out.releaseAfterDecide = m.store.get().pendingRelease ?? null;
"""


def _paused_bindings() -> dict[str, Any]:
    return {"card": TARGETING_ABILITY_CARD, "paused1": _paused(1), "pending1": _pending(1)}


def test_the_store_starts_with_no_pending_release() -> None:
    assert run_app("(async () => m.store.get().pendingRelease)()") is None


def test_a_paused_activation_with_the_default_off_passes_once_the_decide_completes() -> None:
    out = _release_flow(_PAUSED_ACTIVATE + _COMPLETING_DECIDE, False, **_paused_bindings())
    assert out["afterFire"] == 0, "a paused activation must not pass yet - " + str(out["posts"])
    assert out["toastAfterFire"] is False, "no refused premature pass, so no toast"
    assert out["armed"] is not None
    assert out["afterDecide"] == 1, out["posts"]
    assert out["releaseAfterDecide"] is None


def test_a_paused_activation_with_the_default_on_never_passes() -> None:
    out = _release_flow(_PAUSED_ACTIVATE + _COMPLETING_DECIDE, True, **_paused_bindings())
    assert out["afterFire"] == 0
    assert out["armed"] is None
    assert out["afterDecide"] == 0, out["posts"]


def test_the_per_action_hold_tick_holds_a_paused_activation_with_the_default_off() -> None:
    script = (
        """
          const begun = A.begin("activate", card, { abilityIndex: 0 });
          m.store.set({ action: { ...begun, holdPriority: true } });
          respond("actions/activate", paused1);
          board(pending1);
          await m.fire();
          out.afterFire = passes();
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, **_paused_bindings())
    assert out["afterFire"] == 0
    assert out["afterDecide"] == 0, out["posts"]


def test_a_decide_that_pauses_again_waits_for_the_final_completing_decide() -> None:
    script = (
        _PAUSED_ACTIVATE
        + """
          respond("decide", {
            accepted: true, paused: true, pending_decision: pending2, log_seq: 8,
          });
          board(pending2);
          await m.decide({ targets: ["bob"] });
          out.afterFirstDecide = passes();
          out.stillArmed = m.store.get().pendingRelease ?? null;
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, pending2=_pending(2), **_paused_bindings())
    assert out["afterFire"] == 0
    assert out["afterFirstDecide"] == 0, out["posts"]
    assert out["stillArmed"] is not None, "a decide that pauses again keeps the release armed"
    assert out["afterDecide"] == 1, out["posts"]


def test_a_refused_decide_after_a_pause_passes_nothing_and_clears_the_release() -> None:
    script = (
        _PAUSED_ACTIVATE
        + """
          respond("decide", { accepted: false, reason: "not a legal target" });
          await m.decide({ targets: ["nobody"] });
          out.afterRefused = passes();
          out.releaseAfterRefused = m.store.get().pendingRelease ?? null;
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, **_paused_bindings())
    assert out["afterRefused"] == 0, out["posts"]
    assert out["releaseAfterRefused"] is None
    assert out["afterDecide"] == 0, "a cleared release never fires later - " + str(out["posts"])


def test_cancel_clears_a_pending_release() -> None:
    script = (
        _PAUSED_ACTIVATE
        + """
          m.cancel();
          out.afterCancel = m.store.get().pendingRelease ?? null;
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, **_paused_bindings())
    assert out["armed"] is not None
    assert out["afterCancel"] is None
    assert out["afterDecide"] == 0, out["posts"]


def test_a_new_action_clears_a_pending_release() -> None:
    script = (
        _PAUSED_ACTIVATE
        + """
          respond("actions/play-land", { allowed: false, reason: "a decision is pending" });
          await m.playLand({ id: "land1", name: "Mountain" });
          out.afterNewAction = m.store.get().pendingRelease ?? null;
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, **_paused_bindings())
    assert out["armed"] is not None
    assert out["afterNewAction"] is None
    assert out["afterDecide"] == 0, out["posts"]


def test_a_release_clears_when_the_decision_goes_away_and_never_fires_on_a_later_decide() -> None:
    """The pause ended without this seat's decide (the game moved on), then a
    decision this seat did not start by its own action - an opponent's
    trigger asking alice - is answered: nothing passes."""
    script = (
        _PAUSED_ACTIVATE
        + """
          board(null);
          await m.poll();
          out.afterMovedOn = m.store.get().pendingRelease ?? null;
          board(pending5);
          await m.poll();
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, pending5=_pending(5), **_paused_bindings())
    assert out["armed"] is not None
    assert out["afterMovedOn"] is None
    assert out["afterDecide"] == 0, out["posts"]


def test_a_decide_for_a_different_decision_does_not_fire_a_release() -> None:
    """Armed on generation 1, but the question on the board is a different one
    (generation 5, raised by something else): answering it passes nothing."""
    script = (
        _PAUSED_ACTIVATE
        + """
          board(pending5);
          await m.poll();
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, pending5=_pending(5), **_paused_bindings())
    assert out["afterDecide"] == 0, out["posts"]


def test_a_decide_with_nothing_armed_never_passes() -> None:
    """An opponent-triggered decision, a mulligan-era prompt, anything this
    seat's own action did not start: `decide()` is just an answer."""
    script = (
        """
          board(pending1);
          await m.poll();
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, pending1=_pending(1))
    assert out["afterDecide"] == 0, out["posts"]


def test_a_paused_cast_behaves_like_a_paused_activation() -> None:
    script = (
        """
          m.store.set({ action: A.begin("cast", card, {}) });
          respond("actions/cast", paused1);
          board(pending1);
          await m.fire();
          out.afterFire = passes();
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, **_paused_bindings())
    assert any("actions/cast" in p for p in out["posts"]), out["posts"]
    assert out["afterFire"] == 0, out["posts"]
    assert out["afterDecide"] == 1, out["posts"]


def test_a_paused_equip_behaves_like_a_paused_activation() -> None:
    script = (
        """
          respond("actions/equip", paused1);
          board(pending1);
          await m.fireEquip({ id: "eq1", name: "Bonesplitter" }, "bear1", 4);
          out.afterFire = passes();
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, **_paused_bindings())
    assert any("actions/equip" in p for p in out["posts"]), out["posts"]
    assert out["afterFire"] == 0, out["posts"]
    assert out["afterDecide"] == 1, out["posts"]


def test_a_paused_equip_with_the_default_on_never_passes() -> None:
    script = (
        """
          respond("actions/equip", paused1);
          board(pending1);
          await m.fireEquip({ id: "eq1", name: "Bonesplitter" }, "bear1", 4);
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, True, **_paused_bindings())
    assert out["afterDecide"] == 0, out["posts"]


def test_cycling_with_the_default_off_passes_once() -> None:
    out = _release_flow(
        """
          await m.cycle({ id: "cy1", name: "Lorien Revealed" });
          out.after = passes();
""",
        False,
    )
    assert any("actions/cycle" in p for p in out["posts"]), out["posts"]
    assert out["after"] == 1, out["posts"]


def test_cycling_with_the_default_on_passes_nothing() -> None:
    out = _release_flow(
        """
          await m.cycle({ id: "cy1", name: "Lorien Revealed" });
          out.after = passes();
""",
        True,
    )
    assert out["after"] == 0, out["posts"]


def test_a_paused_cycle_passes_once_the_decide_completes() -> None:
    script = (
        """
          respond("actions/cycle", paused1);
          board(pending1);
          await m.cycle({ id: "cy1", name: "Lorien Revealed" });
          out.afterFire = passes();
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(script, False, **_paused_bindings())
    assert out["afterFire"] == 0, out["posts"]
    assert out["afterDecide"] == 1, out["posts"]


def test_a_refused_cycle_passes_nothing() -> None:
    out = _release_flow(
        """
          respond("actions/cycle", { allowed: false, reason: "cannot cycle now" });
          await m.cycle({ id: "cy1", name: "Lorien Revealed" });
          out.after = passes();
""",
        False,
    )
    assert out["after"] == 0, out["posts"]


def test_a_mana_ability_never_passes_even_with_the_default_off_and_the_tick_off() -> None:
    out = _release_flow(
        """
          const begun = A.begin("activate", card, { abilityIndex: 0 });
          m.store.set({ action: { ...begun, holdPriority: false } });
          respond("actions/activate", { allowed: true, mana_ability: true });
          await m.fire();
          out.after = passes();
""",
        False,
        card=MANA_ABILITY_CARD,
    )
    assert out["after"] == 0, out["posts"]


def test_a_paused_mana_ability_arms_nothing() -> None:
    script = (
        """
          m.store.set({ action: A.begin("activate", card, { abilityIndex: 0 }) });
          respond("actions/activate", paused1);
          board(pending1);
          await m.fire();
          out.armed = m.store.get().pendingRelease ?? null;
"""
        + _COMPLETING_DECIDE
    )
    out = _release_flow(
        script, False, card=MANA_ABILITY_CARD, paused1=_paused(1), pending1=_pending(1)
    )
    assert out["armed"] is None
    assert out["afterDecide"] == 0, out["posts"]


def test_playing_a_land_never_passes() -> None:
    out = _release_flow(
        """
          await m.playLand({ id: "land1", name: "Mountain" });
          out.after = passes();
""",
        False,
    )
    assert any("actions/play-land" in p for p in out["posts"]), out["posts"]
    assert out["after"] == 0, out["posts"]


def test_a_refused_activation_neither_passes_nor_arms() -> None:
    script = (
        _BEGIN_ACTIVATE
        + """
          respond("actions/activate", { allowed: false, reason: "not now" });
          await m.fire();
          out.after = passes();
          out.armed = m.store.get().pendingRelease ?? null;
"""
    )
    out = _release_flow(script, False, card=TARGETING_ABILITY_CARD)
    assert out["after"] == 0, out["posts"]
    assert out["armed"] is None


def test_a_completed_plain_activation_still_passes_exactly_once() -> None:
    script = (
        _BEGIN_ACTIVATE
        + """
          await m.fire();
          out.after = passes();
"""
    )
    out = _release_flow(script, False, card=TARGETING_ABILITY_CARD)
    assert out["after"] == 1, out["posts"]


# -- fixtures ----------------------------------------------------------------

MOUNTAIN = {
    "id": "land1",
    "name": "Mountain",
    "type_line": "Basic Land - Mountain",
    "zone": "hand",
    "controller": "alice",
}

MEMNITE = {
    "id": "mm",
    "name": "Memnite",
    "type_line": "Artifact Creature",
    "zone": "hand",
    "controller": "alice",
    "cast": {
        "mana_cost": "{0}",
        "needs_x": False,
        "modes": None,
        "targeting": {"targets_anything": False},
    },
}

KOZILEK = {
    "id": "kc",
    "name": "Kozilek's Command",
    "type_line": "Kindred Instant",
    "zone": "hand",
    "controller": "alice",
    "cast": {
        "mana_cost": "{X}{C}{C}",
        "needs_x": True,
        "modes": {"instruction": "Choose two -", "minimum": 2, "maximum": 2, "options": []},
        "targeting": {"targets_anything": True},
    },
}

CYCLING_LAND = {
    "id": "cl1",
    "name": "Tranquil Thicket",
    "type_line": "Land",
    "zone": "hand",
    "controller": "alice",
    "special_actions": {"cycling": [{"keyword": "Cycling", "cost": "{2}", "index": 0}]},
}

FORETELL_CARD = {
    "id": "f1",
    "name": "Alrund's Epiphany",
    "type_line": "Sorcery",
    "zone": "hand",
    "controller": "alice",
    "special_actions": {"foretell": {"cost": "{2}"}},
}

ONE_ABILITY_LAND = {
    "id": "d1",
    "name": "Sunbaked Canyon",
    "type_line": "Land",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [
        {"index": 0, "text": "{T}, Pay 1 life: Add {R} or {W}.", "is_mana_ability": True},
    ],
}

ONE_ACTIVATED_ABILITY_PW = {
    "id": "pw1",
    "name": "Test Planeswalker",
    "type_line": "Legendary Planeswalker",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [{"index": 0, "text": "+1: draw a card", "is_mana_ability": False}],
}

TWO_ABILITY_PW = {
    "id": "pw2",
    "name": "Two-Ability Planeswalker",
    "type_line": "Legendary Planeswalker",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [
        {"index": 0, "text": "+1: draw a card", "is_mana_ability": False},
        {"index": 1, "text": "-2: destroy target creature", "is_mana_ability": False},
    ],
}

VEHICLE = {
    "id": "sc",
    "name": "Smuggler's Copter",
    "type_line": "Artifact Vehicle",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [],
}

CREWED_VEHICLE_WITH_ABILITY = {
    "id": "vv",
    "name": "Skysovereign, Consul Flagship",
    "type_line": "Legendary Artifact Vehicle",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [
        {"index": 0, "text": "Whenever this Vehicle attacks...", "is_mana_ability": False}
    ],
}

ATTACKING_CREATURE = {
    "id": "bears1",
    "name": "Grizzly Bears",
    "type_line": "Creature - Bear",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [],
}

EQUIPMENT = {
    "id": "eq1",
    "name": "Blade of the Bloodchief",
    "type_line": "Artifact - Equipment",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [],
}


def _equip_row(target_line: str = "bears1 Grizzly Bears (battlefield, alice)") -> dict:
    """CR 702.6a's engine-provided row, shaped exactly as `GET .../legal-
    actions?candidates=true` serves it (`action_rows.legal_action_rows`,
    `schemas.ActionWithTargets`) - `ability_index: None` is what `action_rows.
    is_equip_row` requires of it."""
    return {
        "card_id": "eq1",
        "kind": "equip",
        "ability_index": None,
        "keyword": "Equip",
        "rule": "CR 702.6a",
        "cost": "{1}",
        "affordable": "yes",
        "target_slots": [{"minimum": 1, "maximum": 1}],
        "targets": {
            "understood": True,
            "slots": [{"slot": 0, "count": 1, "candidates": [target_line]}],
        },
    }


def base_app(**overrides: Any) -> dict:
    return {"action": None, "decisionDraft": {}, **overrides}


def base_state(**overrides: Any) -> dict:
    return {
        "you": "alice",
        "pending_decision": None,
        "step": "precombat_main",
        "active_player": "alice",
        "priority_holder": "alice",
        **overrides,
    }


# -- planHandCardClick --------------------------------------------------------


def test_a_land_with_nothing_else_still_plans_a_plain_land_play() -> None:
    """T5 changed what the click *does*, not what it means: the plan is still
    the bare `{kind: "land"}` it always was - no menu, no specials, nothing
    to choose - and `onHandCard` turns that into a one-option confirm rather
    than an immediate `playLand`. The surface half is
    `test_playing_a_land_opens_a_one_option_confirm_and_fires_nothing_yet`
    below; this stays the planner's own contract."""
    plan = run_app("m.planHandCardClick(land, app)", land=MOUNTAIN, app=base_app())
    assert plan == {"kind": "land"}


def test_a_nonland_with_no_specials_goes_straight_to_cast() -> None:
    plan = run_app("m.planHandCardClick(memnite, app)", memnite=MEMNITE, app=base_app())
    assert plan == {"kind": "cast"}


def test_a_modal_x_spell_also_goes_to_cast_first() -> None:
    """The planner only decides *which flow* a click starts, not whether that
    flow will need to ask for modes/X/targets afterward - that is
    `actions.js`'s `needsChoices`, consulted downstream in `beginCast`. A
    spell as choice-heavy as Kozilek's Command still starts the same way a
    Memnite does."""
    plan = run_app("m.planHandCardClick(koz, app)", koz=KOZILEK, app=base_app())
    assert plan == {"kind": "cast"}


def test_clicking_the_card_already_mid_cast_cancels_it() -> None:
    plan = run_app(
        "m.planHandCardClick(memnite, app)",
        memnite=MEMNITE,
        app=base_app(action={"cardId": "mm"}),
    )
    assert plan == {"kind": "cancel"}


def test_clicking_a_different_card_while_one_action_is_pending_is_a_target_click() -> None:
    plan = run_app(
        "m.planHandCardClick(memnite, app)",
        memnite=MEMNITE,
        app=base_app(action={"cardId": "something-else"}),
    )
    assert plan == {"kind": "target", "id": "mm"}


def test_a_cycling_land_offers_a_menu_naming_it_a_land() -> None:
    """A land whose only special action is cycling used to skip straight to
    `playLand` (the old `!specials.length` check never counted `cycling`),
    which meant a cycling land could never actually be cycled from the hand
    click. The menu case now checks both lists."""
    plan = run_app("m.planHandCardClick(land, app)", land=CYCLING_LAND, app=base_app())
    assert plan["kind"] == "menu"
    assert plan["isLand"] is True
    assert plan["specials"] == []
    assert len(plan["cycling"]) == 1


def test_a_nonland_special_action_offers_a_menu_naming_it_not_a_land() -> None:
    plan = run_app("m.planHandCardClick(card, app)", card=FORETELL_CARD, app=base_app())
    assert plan["kind"] == "menu"
    assert plan["isLand"] is False
    assert plan["specials"] == ["foretell"]
    assert plan["cycling"] == []


# -- planPermanentClick -------------------------------------------------------


def test_a_one_ability_land_during_a_pending_cast_pins_rather_than_targets() -> None:
    """Clicking a mana-ability land while a spell is being cast pins it as a
    mana source, as long as it is not itself a legal target (candidate) of
    the spell - CR 601.2c targeting takes priority over paying for a land
    that could genuinely be targeted."""
    action = {"cardId": "spell1", "affordance": {"candidates": [{"id": "someone-else"}]}}
    plan = run_app(
        "m.planPermanentClick(land, app, state)",
        land=ONE_ABILITY_LAND,
        app=base_app(action=action),
        state=base_state(),
    )
    assert plan == {"kind": "pin"}


def test_a_land_already_a_legal_target_is_a_target_click_not_a_pin() -> None:
    action = {"cardId": "spell1", "affordance": {"candidates": [{"id": "d1"}]}}
    plan = run_app(
        "m.planPermanentClick(land, app, state)",
        land=ONE_ABILITY_LAND,
        app=base_app(action=action),
        state=base_state(),
    )
    assert plan == {"kind": "target", "id": "d1"}


def test_a_choose_targets_decision_routes_board_clicks_to_target_even_with_no_action() -> None:
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature=ATTACKING_CREATURE,
        app=base_app(),
        state=base_state(pending_decision={"player": "alice", "kind": "choose_targets"}),
    )
    assert plan == {"kind": "target", "id": "bears1"}


def test_a_creature_in_your_declare_attackers_step_toggles_into_the_red_zone() -> None:
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature=ATTACKING_CREATURE,
        app=base_app(),
        state=base_state(step="declare_attackers"),
    )
    assert plan == {"kind": "attack"}


def test_the_defending_players_creature_is_a_block_click() -> None:
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature=ATTACKING_CREATURE,
        app=base_app(),
        state=base_state(step="declare_blockers", active_player="bob"),
    )
    assert plan == {"kind": "block"}


def test_a_paused_declare_attackers_decision_still_offers_the_attack_click() -> None:
    """A `declare_attackers` pause freezes `state.step` at whatever it was
    *before* the pause (`beginning_of_combat`, here) and `priority_holder`
    stays equally stale - neither field ever moved, since the turn-based
    action that would have moved them never finished. A plain
    `state.step === "declare_attackers"` check can never see the pause
    itself, only what results after someone answers it - too late to offer
    the click at all. `pendingDeclareKind` reads the live decision instead,
    which this pins directly against a state shaped exactly like a real
    pause: `step` still `beginning_of_combat`, `priority_holder` still
    whoever it was, but `pending_decision` naming this seat's own
    `declare_attackers`."""
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature=ATTACKING_CREATURE,
        app=base_app(),
        state=base_state(
            step="beginning_of_combat",
            priority_holder="bob",
            pending_decision={"player": "alice", "kind": "declare_attackers"},
        ),
    )
    assert plan == {"kind": "attack"}


def test_a_paused_declare_blockers_decision_still_offers_the_block_click() -> None:
    """The same pause-freezes-the-step shape as the attackers test above,
    for the defending player's own `declare_blockers` pause instead."""
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature=ATTACKING_CREATURE,
        app=base_app(),
        state=base_state(
            step="declare_attackers",
            active_player="bob",
            priority_holder="bob",
            pending_decision={"player": "alice", "kind": "declare_blockers"},
        ),
    )
    assert plan == {"kind": "block"}


def test_an_opponents_permanent_with_nothing_pending_is_none() -> None:
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature={**ATTACKING_CREATURE, "controller": "bob"},
        app=base_app(),
        state=base_state(),
    )
    assert plan == {"kind": "none"}


def test_a_no_ability_permanent_of_yours_is_none_with_a_reason() -> None:
    plan = run_app(
        "m.planPermanentClick(creature, app, state)",
        creature=ATTACKING_CREATURE,
        app=base_app(),
        state=base_state(),
    )
    assert plan == {"kind": "none", "reason": "Grizzly Bears has no activated abilities"}


def test_a_single_activated_ability_activates_directly() -> None:
    plan = run_app(
        "m.planPermanentClick(pw, app, state)",
        pw=ONE_ACTIVATED_ABILITY_PW,
        app=base_app(),
        state=base_state(),
    )
    assert plan == {"kind": "activate", "abilityIndex": 0}


def test_two_activated_abilities_open_a_menu() -> None:
    plan = run_app(
        "m.planPermanentClick(pw, app, state)",
        pw=TWO_ABILITY_PW,
        app=base_app(),
        state=base_state(),
    )
    assert plan["kind"] == "menu"
    assert plan["vehicle"] is False
    assert len(plan["abilities"]) == 2


def test_a_vehicle_with_no_abilities_still_offers_a_crew_menu() -> None:
    """CR 702.122a: crewing is a Vehicle's own thing to do even with no
    printed activated ability at all - `vehicle: true` is what tells
    `onPermanent` to add the "Crew" item to an otherwise-empty menu."""
    plan = run_app(
        "m.planPermanentClick(vehicle, app, state)",
        vehicle=VEHICLE,
        app=base_app(),
        state=base_state(),
    )
    assert plan == {"kind": "menu", "abilities": [], "vehicle": True}


def test_an_equipment_with_no_abilities_still_routes_to_a_menu() -> None:
    """CR 702.6a: equip has no entry in `card.abilities` (`Game.
    activated_abilities` never synthesises it), so the single-ability
    auto-activate and no-op branches must not swallow an Equipment the way
    they would swallow an ordinary permanent with nothing printed. Whether
    Equip actually belongs in the menu is `openPermanentMenu`'s own question,
    answered from the engine rather than the card - this planner only routes
    the click there."""
    plan = run_app(
        "m.planPermanentClick(equipment, app, state)",
        equipment=EQUIPMENT,
        app=base_app(),
        state=base_state(),
    )
    assert plan == {"kind": "menu", "abilities": [], "vehicle": False, "equipment": True}


def test_a_vehicle_with_an_ability_also_menus_rather_than_auto_activating() -> None:
    """A Vehicle with exactly one activated ability still isn't the
    single-ability auto-activate case (`abilities.length === 1 && !vehicle`)
    - crewing is always a second choice standing next to it."""
    plan = run_app(
        "m.planPermanentClick(vehicle, app, state)",
        vehicle=CREWED_VEHICLE_WITH_ABILITY,
        app=base_app(),
        state=base_state(),
    )
    assert plan["kind"] == "menu"
    assert plan["vehicle"] is True
    assert len(plan["abilities"]) == 1


# -- a poll read that predates this seat's own action must never drive a tick --
#
# The `pass_until` bug the owner reported from a live game, reproduced against
# a real server before it was written down here (see MEMORY.md): every "Pass
# until" click passed exactly once, and ~1.5-2.2s later the browser sent a
# second `pass-priority` that the engine refused with "you does not have
# priority, claude does" - which the old last line of `tickRun` turned into
# `store.set({run: null})`, so the run was dead before it had gone anywhere.
#
# The second pass is not a second click. `poll()` reads the board and the log
# in sequence and only then calls `tickRun()`; anything this seat does in
# between - the "Pass until" click itself, a blocker declaration, a cast -
# lands on a server whose answer to the already-issued board read was written
# BEFORE it. `poll()` then wrote that board into the store as current and ran
# `tickRun()` on it, which still read `priority_holder: alice` and passed
# again. In the late game one poll cycle ran longer than `POLL_MS`, so a poll
# was in flight at every instant and every click landed in that window.


def _stale_poll_body() -> str:
    """A `fetch` stub whose `/log` read is held open until the test releases
    it - the window a real poll spends between reading the board and acting
    on it. The board read answers "alice still holds priority", because that
    is what it was when it was answered; the pass endpoint answers like the
    engine does, allowing the first pass and refusing any later one."""
    return """
      globalThis.__calls = [];
      globalThis.__toast = { textContent: "", hidden: true };
      globalThis.document = {
        querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
      };
      globalThis.__passes = 0;
      let releaseLog;
      globalThis.__releaseLog = () => releaseLog();
      const logRead = new Promise((resolve) => { releaseLog = resolve; });
      const board = (holder) => ({
        you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
        active_player: "bob", priority_holder: holder, battlefield: [],
        hands: { alice: [] }, stack: [], combat: { attacks: [] },
        winner: null, drawn: false, pregame: null,
      });
      globalThis.__board = board;
      globalThis.fetch = (path, opts) => {
        const method = opts ? opts.method : "GET";
        globalThis.__calls.push({ method, path });
        if (method === "POST" && path.includes("pass-priority")) {
          globalThis.__passes += 1;
          const first = globalThis.__passes === 1;
          return Promise.resolve({ ok: true, json: async () => (first
            ? { allowed: true, outcome: "passed" }
            : { allowed: false, reason: "alice does not have priority, bob does" }) });
        }
        if (method === "POST") {
          return Promise.resolve({ ok: true, json: async () => ({ allowed: true }) });
        }
        if (path.includes("/log")) {
          return logRead.then(() => ({ ok: true, json: async () => ({ log: [], last_seq: 1 }) }));
        }
        return Promise.resolve({ ok: true, json: async () => board("alice") });
      };
    """


def _armed_run() -> str:
    return (
        '{ verb: "pass_until", startedTurn: 4, startedStep: "upkeep", '
        'startedActive: "bob", knownStackIds: [], passes: 0, stillAtStart: false }'
    )


def test_a_board_read_taken_before_this_seats_own_pass_never_drives_a_second_pass() -> None:
    result = run_app(
        _stale_poll_body()
        + """
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", run: null,
            stops: { mine: ["postcombat_main"], theirs: [], onStack: true },
            state: globalThis.__board("alice"),
            log: { entries: [], lastSeq: 0 },
          });
          // A poll starts. Its board read is answered at once; its log read
          // is held open, so `tickRun()` will not run on that board yet.
          const inFlight = m.poll();
          await new Promise((r) => setTimeout(r, 0));
          // ... and the human clicks "Pass until" inside that window.
          m.store.set({ run: """
        + _armed_run()
        + """ });
          await m.tickRun();
          globalThis.__releaseLog();
          await inFlight;
          return { calls: globalThis.__calls, run: m.store.get().run, passes: globalThis.__passes };
        })()
        """
    )
    passes = [c for c in result["calls"] if c["method"] == "POST" and "pass-priority" in c["path"]]
    assert len(passes) == 1, (
        "the poll acted on a board it read before this seat's own pass, and passed again: "
        f"{result['calls']}"
    )
    assert result["run"] is not None, "a run must not die on a pass it should never have sent"


def test_a_board_read_that_predates_this_seats_own_action_is_not_written_as_current() -> None:
    """The same defect's other half, and the one the human sees directly: the
    stale board is what `poll()` puts on the screen, so the play UI shows the
    move as not having happened. That is what had the owner clicking "No
    blocks" five times into a single combat (`#239`-`#240` and the three
    refusals after them in the reported game)."""
    result = run_app(
        _stale_poll_body()
        + """
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", run: null,
            stops: { mine: [], theirs: [], onStack: true },
            state: globalThis.__board("alice"),
            log: { entries: [], lastSeq: 0 },
          });
          const inFlight = m.poll();
          await new Promise((r) => setTimeout(r, 0));
          // Any action of this seat's own, not only a pass: this one is the
          // declaration, and it moves the board the poll already read.
          await m.declareNoBlockers();
          m.store.set({ state: globalThis.__board("bob") });
          globalThis.__releaseLog();
          await inFlight;
          return { priority: m.store.get().state.priority_holder };
        })()
        """
    )
    assert result["priority"] == "bob", (
        "the poll overwrote a board known to have moved on with the one it read first"
    )


# -- which refused passes end a run, and which are only a lost race ----------
#
# A refusal is two different things wearing one word. "This seat does not hold
# priority any more" means the board moved between the read this tick acted on
# and the pass it sent - a lost race, and the very next tick will do the right
# thing. "This move is not available" means the run cannot proceed. Collapsing
# them, which `if (!out.ok) store.set({run: null})` did, is how a run dies
# silently. The board itself is asked which one happened, rather than the
# refusal text being pattern-matched: engine wording is not a client contract.


def _refusal_body(after_refusal_holder: str) -> str:
    return """
      globalThis.__calls = [];
      globalThis.__toast = { textContent: "", hidden: true };
      globalThis.document = {
        querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
      };
      const board = (holder) => ({
        you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
        active_player: "bob", priority_holder: holder, battlefield: [],
        hands: { alice: [] }, stack: [], combat: { attacks: [] },
        winner: null, drawn: false, pregame: null,
      });
      globalThis.__board = board;
      globalThis.fetch = (path, opts) => {
        const method = opts ? opts.method : "GET";
        globalThis.__calls.push({ method, path });
        if (method === "POST" && path.includes("pass-priority")) {
          return Promise.resolve({ ok: true, json: async () => ({
            allowed: false, reason: "alice does not have priority, bob does" }) });
        }
        if (method === "POST") {
          return Promise.resolve({ ok: true, json: async () => ({ allowed: true }) });
        }
        if (path.includes("/log")) {
          return Promise.resolve({ ok: true, json: async () => ({ log: [], last_seq: 0 }) });
        }
        return Promise.resolve({ ok: true, json: async () => board(HOLDER) });
      };
    """.replace("HOLDER", json.dumps(after_refusal_holder))


def test_a_refused_pass_on_a_board_that_has_since_moved_on_leaves_the_run_armed() -> None:
    result = run_app(
        _refusal_body("bob")
        + """
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1",
            stops: { mine: ["postcombat_main"], theirs: [], onStack: true },
            state: globalThis.__board("alice"),
            run: """
        + _armed_run()
        + """,
            log: { entries: [], lastSeq: 0 },
          });
          await m.tickRun();
          return { calls: globalThis.__calls, run: m.store.get().run,
                   toast: globalThis.__toast.textContent };
        })()
        """
    )
    posts = [c for c in result["calls"] if c["method"] == "POST"]
    assert len(posts) == 1, result["calls"]
    assert result["run"] is not None, (
        "the board says another seat holds priority now - a lost race, not a stop"
    )


def test_a_refused_pass_the_board_still_says_was_this_seats_to_make_ends_the_run() -> None:
    """The other side of the same rule, so narrowing it does not become
    "never stop": if a fresh read still says this seat holds priority and the
    engine still refuses the pass, the disagreement is real and the run has
    nowhere to go. It ends, and says so."""
    result = run_app(
        _refusal_body("alice")
        + """
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1",
            stops: { mine: ["postcombat_main"], theirs: [], onStack: true },
            state: globalThis.__board("alice"),
            run: """
        + _armed_run()
        + """,
            log: { entries: [], lastSeq: 0 },
          });
          await m.tickRun();
          return { run: m.store.get().run, toast: globalThis.__toast.textContent };
        })()
        """
    )
    assert result["run"] is None
    assert "alice does not have priority" in result["toast"]


def test_a_pass_that_never_reached_the_server_leaves_the_run_armed() -> None:
    """A rejected `fetch` is not a refusal at all - nothing was decided, and
    `send()` has already told the player the connection is down. The old code
    read `send`'s `null` as `ok: false` and ended the run over a dropped
    packet."""
    result = run_app(
        """
        globalThis.__calls = [];
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        globalThis.fetch = (path, opts) => {
          const method = opts ? opts.method : "GET";
          globalThis.__calls.push({ method, path });
          if (method === "POST") return Promise.reject(new Error("network is down"));
          return Promise.resolve({ ok: true, json: async () => ({
            you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
            active_player: "bob", priority_holder: "alice", battlefield: [],
            hands: { alice: [] }, stack: [], combat: { attacks: [] },
            winner: null, drawn: false, pregame: null,
          }) });
        };
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1",
            stops: { mine: ["postcombat_main"], theirs: [], onStack: true },
            state: {
              you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
              active_player: "bob", priority_holder: "alice", battlefield: [],
              stack: [], combat: { attacks: [] }, winner: null, drawn: false,
            },
            run: """
        + _armed_run()
        + """,
            log: { entries: [], lastSeq: 0 },
          });
          await m.tickRun();
          return { run: m.store.get().run };
        })()
        """
    )
    assert result["run"] is not None, "a dropped request is not a refusal"


# -- cross-device: a bookmarked game URL opened on a second computer --------
#
# "User must be able to start a game on one computer and finish on the other."
# `bootTarget` above answers `uid: null` for a game URL this browser has never
# saved - correct, since `mcm.games.v1` genuinely holds nothing - and until
# now that was the end of it: the page rendered as a spectator with no seat
# and the player could look at the board and not act. The seat is recoverable
# though, and from the server rather than from storage: the logged-in owner
# gets the full `seats` map for her own games from `GET /api/lobby/games`
# (`decks_api.list_lobby_games`), so `main()` asks for it before deciding the
# player has no seat. `seatFromServer` is that lookup, factored out of
# `main()` for the same reason every other decision in this file is - so it
# can be exercised with no DOM.


def test_seat_from_server_recovers_the_owners_seat_with_nothing_saved() -> None:
    """The bookmarked-URL case, end to end at the module level: empty
    `localStorage`, and the server's own lobby listing is the only thing that
    knows which uid belongs to this player. The human seat is the one with no
    entry in `agents` - here the AI seat is listed first, so picking by key
    order would seat the player as the bot."""
    result = run_app(
        """(async () => {
          globalThis.fetch = async (path) => ({
            ok: true,
            status: 200,
            json: async () => ({
              games: [
                {
                  game_id: "g1",
                  players: ["you", "bot"],
                  turn_number: 3,
                  phase: "precombat_main",
                  is_over: false,
                  agents: { bot: { model: "opus", effort: "max", max_thinking_tokens: null } },
                  seats: { bot: "u2", you: "u1" },
                },
              ],
            }),
          });
          return await m.seatFromServer("g1");
        })()"""
    )
    assert result == {"uid": "u1", "aiUid": "u2"}


def test_seat_from_server_is_silent_when_the_game_is_not_the_callers() -> None:
    """A game the server will not list for this caller - somebody else's, or
    an anonymous deployment with no OIDC configured at all - resolves to no
    seat rather than to an error. The page then renders exactly as it does
    today: the board, as a spectator."""
    result = run_app(
        """(async () => {
          globalThis.fetch = async () => ({
            ok: true, status: 200, json: async () => ({ games: [] }),
          });
          return await m.seatFromServer("g1");
        })()"""
    )
    assert result == {"uid": None, "aiUid": None}


def test_seat_from_server_survives_a_failing_lookup() -> None:
    """The lookup is a best-effort recovery, never a gate: a 500, an
    unreachable server, or a deployment whose lobby route is not there at all
    must leave the game page rendering, not throw on boot."""
    result = run_app(
        """(async () => {
          globalThis.fetch = async () => { throw new Error("network is down"); };
          return await m.seatFromServer("g1");
        })()"""
    )
    assert result == {"uid": None, "aiUid": None}


def test_seat_from_server_reads_a_human_vs_human_game_without_crashing() -> None:
    """No AI seat means `agents` is empty and "the seat with no agent"
    matches both. It must still hand back one usable uid and no `aiUid`,
    rather than throw or answer null."""
    result = run_app(
        """(async () => {
          globalThis.fetch = async () => ({
            ok: true,
            status: 200,
            json: async () => ({
              games: [
                {
                  game_id: "g1",
                  players: ["alice", "bob"],
                  turn_number: 1,
                  phase: "beginning",
                  is_over: false,
                  agents: {},
                  seats: { alice: "u1", bob: "u2" },
                },
              ],
            }),
          });
          return await m.seatFromServer("g1");
        })()"""
    )
    assert result["uid"] in ("u1", "u2")
    assert result["aiUid"] is None


# -- planPin: WHICH mana ability to tap (API2) --------------------------------
#
# `planPermanentClick` already answers "this mid-cast click pins a mana
# source" (`{kind: "pin"}`); `planPin` is the step after it, and it is where
# a source with more than one mana ability gets to say which. Eldrazi Temple
# taps for {C} (index 0) or for {C}{C} that may only be spent on Eldrazi
# spells (index 1) - the second was unreachable while pinning always took the
# first `is_mana_ability` entry it found.

BASIC_MOUNTAIN = {
    "id": "b1",
    "name": "Mountain",
    "type_line": "Basic Land - Mountain",
    "zone": "battlefield",
    "controller": "alice",
    "tapped": False,
    "abilities": [
        {"index": 0, "text": "{T}: Add {R}.", "is_mana_ability": True, "mana_options": None},
    ],
}

DUAL_LAND = {
    "id": "dl",
    "name": "Steam Vents",
    "type_line": "Land - Island Mountain",
    "zone": "battlefield",
    "controller": "alice",
    "tapped": False,
    "abilities": [
        {
            "index": 0,
            "text": "{T}: Add {U} or {R}.",
            "is_mana_ability": True,
            "mana_options": [["U"], ["R"]],
        },
    ],
}

ELDRAZI_TEMPLE = {
    "id": "et1",
    "name": "Eldrazi Temple",
    "type_line": "Land",
    "zone": "battlefield",
    "controller": "alice",
    "tapped": False,
    "abilities": [
        {
            "index": 0,
            "text": "{T}: Add {C}.",
            "cost": "{T}",
            "is_mana_ability": True,
            "mana_options": None,
        },
        {
            "index": 1,
            "text": "{T}: Add {C}{C}. Spend this mana only to cast Eldrazi spells.",
            "cost": "{T}",
            "is_mana_ability": True,
            "mana_options": None,
        },
    ],
}

CASCADE_BLUFFS = {
    "id": "cb",
    "name": "Cascade Bluffs",
    "type_line": "Land",
    "zone": "battlefield",
    "controller": "alice",
    "tapped": False,
    "abilities": [
        {"index": 0, "text": "{T}: Add {C}.", "is_mana_ability": True, "mana_options": None},
        {
            "index": 1,
            "text": "{U/R}, {T}: Add {U}{U}, {U}{R}, or {R}{R}.",
            "is_mana_ability": True,
            "mana_options": [["U"], ["R"]],
        },
    ],
}


SHANG_CHI = {
    "id": "sc",
    "name": "Shang-Chi, Master of Kung Fu",
    "type_line": "Legendary Creature - Human Warrior Hero",
    "zone": "battlefield",
    "controller": "alice",
    "tapped": False,
    "abilities": [
        {
            "index": 0,
            "text": (
                "{T}: Add two mana of any one color. Spend this mana only to "
                "activate abilities of creature sources."
            ),
            "is_mana_ability": True,
            "mana_options": ["W", "U", "B", "R", "G"],
        },
    ],
}


def pinning_action(**overrides: Any) -> dict:
    return {"cardId": "spell1", "pinned": [], **overrides}


def test_a_single_mana_ability_source_asks_nothing_extra() -> None:
    """The hard requirement: a basic land keeps its one click. No ability
    menu, no colour menu - it pins straight away, and it names no ability
    index, so the wire shape stays the bare id it always was."""
    plan = run_app("m.planPin(land, action)", land=BASIC_MOUNTAIN, action=pinning_action())
    assert plan == {"kind": "pin", "abilityIndex": None, "color": None}


def test_a_single_mana_ability_source_with_colours_still_goes_to_the_colour_menu() -> None:
    """Unchanged behaviour for a shockland: one ability, so nothing to ask
    about which - straight to the colour choice it always offered, and still
    with no ability index named."""
    plan = run_app("m.planPin(land, action)", land=DUAL_LAND, action=pinning_action())
    assert plan == {"kind": "colour", "abilityIndex": None, "colors": ["U", "R"]}


def test_a_source_with_two_mana_abilities_asks_which_one_first() -> None:
    plan = run_app("m.planPin(land, action)", land=ELDRAZI_TEMPLE, action=pinning_action())
    assert plan["kind"] == "ability"
    assert [(a["index"], a["text"]) for a in plan["abilities"]] == [
        (0, "{T}: Add {C}."),
        (1, "{T}: Add {C}{C}. Spend this mana only to cast Eldrazi spells."),
    ]


def test_choosing_eldrazi_temples_restricted_ability_pins_index_one() -> None:
    plan = run_app("m.planPinnedAbility(land, 1)", land=ELDRAZI_TEMPLE)
    assert plan == {"kind": "pin", "abilityIndex": 1, "color": None}


def test_a_chosen_ability_that_also_offers_colours_asks_for_both() -> None:
    plan = run_app("m.planPinnedAbility(land, 1)", land=CASCADE_BLUFFS)
    assert plan == {"kind": "colour", "abilityIndex": 1, "colors": ["U", "R"]}


def test_clicking_an_already_pinned_source_unpins_it_rather_than_asking_again() -> None:
    plan = run_app(
        "m.planPin(land, action)",
        land=ELDRAZI_TEMPLE,
        action=pinning_action(pinned=[{"card_id": "et1", "color": None, "ability_index": 1}]),
    )
    assert plan == {"kind": "unpin"}


def test_eldrazi_temples_second_ability_reaches_the_cast_request() -> None:
    """The whole point, end to end: the mid-cast click plans a pin, the pin
    plans an ability choice, choosing index 1 pins it, and the cast POST
    carries `{card_id, ability_index: 1}` in `mana_sources` while the other
    untapped source stays a bare id."""
    result = run_app(
        _fetch_stub_body()
        + """
        (async () => {
          const A = await import(pathToFileURL(actionsPath).href);
          const state = { you: "alice", battlefield: [temple, mountain] };
          const casting = { action: A.begin("cast", { id: "c1" }, {}) };
          const click = m.planPermanentClick(temple, casting, state);
          const step = m.planPin(temple, { cardId: "c1", pinned: [] });
          const chosen = m.planPinnedAbility(temple, step.abilities[1].index);
          let action = A.begin("cast", { id: "c1" }, {});
          action = A.pinSource(action, temple.id, chosen.color, chosen.abilityIndex);
          m.store.set({
            gameId: "g1", uid: "u1", state,
            stops: { mine: [], theirs: [], onStack: true, keepPriorityByDefault: true },
            action,
          });
          await m.fire();
          return { click, step: step.kind, chosen, calls: globalThis.__calls };
        })()
        """,
        actionsPath=str(ACTIONS_JS),
        temple=ELDRAZI_TEMPLE,
        mountain=BASIC_MOUNTAIN,
    )
    assert result["click"] == {"kind": "pin"}
    assert result["step"] == "ability"
    assert result["chosen"] == {"kind": "pin", "abilityIndex": 1, "color": None}
    casts = [c for c in result["calls"] if c["method"] == "POST" and "actions/cast" in c["path"]]
    assert len(casts) == 1, result["calls"]
    assert casts[0]["body"]["mana_sources"] == [{"card_id": "et1", "ability_index": 1}, "b1"]


# -- the pin menus, driven through the real popover ---------------------------
#
# The planners above decide; `pinLand` acts, and the acting is the half that
# has to open one anchored popover, then a second one from inside the first
# `onSelect` - a flow no pure function can prove. `render/ribbon.js`'s
# `showPopover` is the real surface here, running against a fake DOM small
# enough to be obviously honest: elements that record their listeners, a
# `#popover` for `showPopover` to find, and a `fetch` that records the
# dry-run probe `previewAndMaybeFire` sends once the pin is made (a pinned
# source means `needsExplicitFire`, so the probe is where the wire shape
# shows up, and nothing is cast until the player presses Cast).


def _fake_dom_body() -> str:
    return """
      globalThis.__calls = [];
      class El {
        constructor(tag) {
          this.tagName = tag;
          this.children = [];
          this.dataset = {};
          this.attrs = {};
          this.style = {};
          this.hidden = true;
          // A real `<button>` has this property, so `dom.js`'s `h` assigns it
          // rather than falling back to `setAttribute` - and `showPopover`'s
          // own `optionButtons` reads it to skip a disabled option.
          this.disabled = false;
          this.listeners = {};
          this.classes = new Set();
          this.classList = {
            add: (c) => this.classes.add(c),
            remove: (c) => this.classes.delete(c),
            contains: (c) => this.classes.has(c),
          };
        }
        set className(v) { this.classes = new Set(String(v).split(/\\s+/).filter(Boolean)); }
        get className() { return [...this.classes].join(" "); }
        appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
        // A real `removeChild` un-parents the child - which is the whole
        // difference between a node that is still in the page and one the
        // player's press has nothing left to land on, so the fake cannot
        // leave a stale `parentNode` behind either.
        removeChild(c) {
          this.children = this.children.filter((x) => x !== c);
          if (c.parentNode === this) c.parentNode = null;
          return c;
        }
        setAttribute(k, v) { this.attrs[k] = v; }
        addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
        removeEventListener() {}
        contains(node) {
          if (node === this) return true;
          return this.children.some((c) => c.contains && c.contains(node));
        }
        focus() { globalThis.document.activeElement = this; }
        getBoundingClientRect() {
          return { left: 10, top: 10, right: 60, bottom: 40, width: 50, height: 30 };
        }
        get firstChild() { return this.children[0] || null; }
        get childNodes() { return this.children; }
        // Real `textContent`: reading it aggregates the subtree, and writing
        // it REPLACES the subtree with one text node. `dom.js`'s `setText`
        // writes it, so a fake that kept it as a plain property would take
        // the write and go on rendering the old text - the harness lying
        // about a redraw it was built to observe.
        get textContent() {
          return this.children
            .map((c) => (c.textContent === undefined ? "" : c.textContent))
            .join("");
        }
        set textContent(v) {
          const text = v === null || v === undefined ? "" : String(v);
          this.children = [];
          if (text !== "") {
            this.appendChild({ nodeType: 3, textContent: text, contains: () => false });
          }
        }
        // The option's own label, sublabel excluded: its direct text, not
        // the `.hint` span's - `deepText` below is the whole of what the
        // player reads.
        get label() {
          return this.children.filter((c) => c.nodeType === 3).map((c) => c.textContent).join("");
        }
        // The whole rendered text, sublabel span included - what a player
        // actually reads on the option.
        get deepText() {
          const walk = (n) =>
            n.nodeType === 3 ? n.textContent : (n.children || []).map(walk).join("");
          return walk(this);
        }
        // Whether this node is still reachable from something the page
        // itself hands out - the popover, the toast, a node a test put in
        // `__nodes`. A child that its parent has since dropped is detached
        // even if the parent is not, which is exactly what `clear(el)` does
        // to every option of a menu it is about to rebuild.
        get attached() {
          let node = this;
          while (node.parentNode) {
            if (!node.parentNode.children.includes(node)) return false;
            node = node.parentNode;
          }
          return isRoot(node);
        }
        // Real dispatch, not "call this node's handlers". A browser sends
        // `click` to the nearest inclusive ancestor common to the mousedown
        // and the mouseup target, so a node detached between the two never
        // sees the event at all and the press is silently eaten - the defect
        // a fake that just invoked `this.listeners.click` could not observe,
        // and the reason 46 tests missed it. From the node the event really
        // lands on, it bubbles, as it does in a page.
        click() {
          let target = this;
          while (target && !target.attached) target = target.parentNode || null;
          if (!target) return;
          for (let node = target; node; node = node.parentNode) {
            for (const fn of node.listeners.click || []) fn({ target, currentTarget: node });
          }
        }
      }
      const popover = new El("div");
      globalThis.__popover = popover;
      const toastEl = new El("div");
      globalThis.__toast = toastEl;
      // A test that needs the page to hold more than the popover and the
      // toast - an open `#modal`, a board node a `[data-key]` lookup has to
      // find - puts it here under its own selector. Everything else answers
      // exactly what it always did.
      globalThis.__nodes = {};
      // The page's own roots: a node is "in the tree" exactly when walking
      // its parents reaches one of these. Nothing else counts - an `anchor`
      // a test built with `new El(...)` and never attached is as detached as
      // a button a redraw has thrown away.
      function isRoot(node) {
        return (
          node === popover ||
          node === toastEl ||
          Object.values(globalThis.__nodes).includes(node)
        );
      }
      globalThis.document = {
        createElement: (t) => new El(t),
        createTextNode: (s) => ({ nodeType: 3, textContent: s, contains: () => false }),
        querySelector: (sel) =>
          globalThis.__nodes[sel] ||
          (sel === "#popover" ? popover : sel === "#toast" ? toastEl : null),
        addEventListener() {},
        removeEventListener() {},
        activeElement: null,
        // `showInFlight` puts `.busy` here for as long as a request is out;
        // `body.classList` is what `ui/style.css`'s in-flight rules hang off,
        // so a test can read which of them would be in force.
        body: {
          classList: {
            set: new Set(),
            add: (c) => document.body.classList.set.add(c),
            remove: (c) => document.body.classList.set.delete(c),
            contains: (c) => document.body.classList.set.has(c),
            toggle: (c, on) =>
              on ? document.body.classList.set.add(c) : document.body.classList.set.delete(c),
          },
          setAttribute() {},
          removeAttribute() {},
          focus() { globalThis.document.activeElement = null; },
        },
      };
      globalThis.window = {
        innerWidth: 1024,
        innerHeight: 768,
        addEventListener() {},
        removeEventListener() {},
      };
      globalThis.fetch = (path, opts) => {
        const method = opts ? opts.method : "GET";
        const body = opts && opts.body ? JSON.parse(opts.body) : undefined;
        globalThis.__calls.push({ method, path, body });
        // A test that needs one route to answer something in particular (or
        // to answer late, or never) sets `__respond`; anything it declines
        // gets the same bland success every call got before it existed.
        const custom = globalThis.__respond && globalThis.__respond(method, path, body);
        if (custom) return custom;
        return Promise.resolve({ ok: true, json: async () => ({ allowed: true, tapped: [] }) });
      };
      const options = () => globalThis.__popover.children.filter((c) => c.classes.has("option"));
      // Let every already-resolved promise in the chain run. A click handler
      // is not awaited by the DOM, so the flow it starts finishes on its own
      // turns of the loop.
      const settle = async (turns) => {
        for (let i = 0; i < (turns || 30); i += 1) await new Promise((r) => setTimeout(r, 0));
      };
    """


def _pin_setup(land: dict) -> str:
    return """
      const A = await import(pathToFileURL(actionsPath).href);
      m.store.set({
        gameId: "g1", uid: "u1",
        state: { you: "alice", battlefield: [land, mountain] },
        stops: { mine: [], theirs: [], onStack: true },
        action: A.begin("cast", { id: "c1" }, {}),
      });
      const anchor = new El("div");
    """


def test_a_basic_land_pins_on_one_click_with_no_menu_at_all() -> None:
    """The hard requirement, at the surface rather than in the planner: a
    source with one mana ability opens nothing. No popover, no second click,
    and the request still names it as a bare id."""
    result = run_app(
        _fake_dom_body()
        + """
        (async () => {"""
        + _pin_setup(BASIC_MOUNTAIN)
        + """
          await m.pinLand(mountain, { currentTarget: anchor });
          return {
            popoverHidden: globalThis.__popover.hidden,
            optionCount: options().length,
            pinned: m.store.get().action.pinned,
            calls: globalThis.__calls,
          };
        })()
        """,
        actionsPath=str(ACTIONS_JS),
        land=BASIC_MOUNTAIN,
        mountain=BASIC_MOUNTAIN,
    )
    assert result["popoverHidden"] is True, "a basic land must not open a menu"
    assert result["optionCount"] == 0
    assert result["pinned"] == [{"card_id": "b1", "color": None, "ability_index": None}]
    probes = [c for c in result["calls"] if c["method"] == "POST"]
    assert len(probes) == 1, result["calls"]
    assert probes[0]["body"]["dry_run"] is True
    assert probes[0]["body"]["mana_sources"] == ["b1"]


def test_eldrazi_temple_offers_both_abilities_and_pins_the_restricted_one() -> None:
    """Two mana abilities, so the menu asks which - each item showing the
    ability's own text, restriction sentence and all - and choosing the
    second sends `ability_index: 1`."""
    result = run_app(
        _fake_dom_body()
        + """
        (async () => {"""
        + _pin_setup(ELDRAZI_TEMPLE)
        + """
          await m.pinLand(land, { currentTarget: anchor });
          const offered = options().map((b) => b.label);
          options()[1].click();
          await new Promise((r) => setTimeout(r, 0));
          return {
            offered,
            pinned: m.store.get().action.pinned,
            calls: globalThis.__calls,
          };
        })()
        """,
        actionsPath=str(ACTIONS_JS),
        land=ELDRAZI_TEMPLE,
        mountain=BASIC_MOUNTAIN,
    )
    assert result["offered"] == [
        "{T}: Add {C}.",
        "{T}: Add {C}{C}. Spend this mana only to cast Eldrazi spells.",
    ]
    assert result["pinned"] == [{"card_id": "et1", "color": None, "ability_index": 1}]
    probes = [c for c in result["calls"] if c["method"] == "POST"]
    assert probes[-1]["body"]["mana_sources"] == [{"card_id": "et1", "ability_index": 1}, "b1"]


def test_an_ability_that_also_offers_colours_asks_for_both_and_sends_both() -> None:
    """Cascade Bluffs: its second ability is the one that filters, and it
    still has a colour to choose afterwards. Both menus open, in that order,
    and both answers reach the request."""
    result = run_app(
        _fake_dom_body()
        + """
        (async () => {"""
        + _pin_setup(CASCADE_BLUFFS)
        + """
          await m.pinLand(land, { currentTarget: anchor });
          const abilities = options().map((b) => b.label);
          options()[1].click();
          await new Promise((r) => setTimeout(r, 0));
          const colours = options().map((b) => b.label);
          options()[1].click();
          await new Promise((r) => setTimeout(r, 0));
          return {
            abilities,
            colours,
            pinned: m.store.get().action.pinned,
            calls: globalThis.__calls,
          };
        })()
        """,
        actionsPath=str(ACTIONS_JS),
        land=CASCADE_BLUFFS,
        mountain=BASIC_MOUNTAIN,
    )
    assert result["abilities"] == [
        "{T}: Add {C}.",
        "{U/R}, {T}: Add {U}{U}, {U}{R}, or {R}{R}.",
    ]
    assert result["colours"] == ["Tap for {U}", "Tap for {R}"]
    assert result["pinned"] == [{"card_id": "cb", "color": "R", "ability_index": 1}]
    probes = [c for c in result["calls"] if c["method"] == "POST"]
    assert probes[-1]["body"]["mana_sources"] == [
        {"card_id": "cb", "colors": ["R"], "ability_index": 1},
        "b1",
    ]


def test_clicking_a_pinned_source_again_takes_it_back_off_the_request() -> None:
    result = run_app(
        _fake_dom_body()
        + """
        (async () => {"""
        + _pin_setup(ELDRAZI_TEMPLE)
        + """
          await m.pinLand(land, { currentTarget: anchor });
          options()[1].click();
          await new Promise((r) => setTimeout(r, 0));
          const pinnedBefore = m.store.get().action.pinned;
          await m.pinLand(land, { currentTarget: anchor });
          return { pinnedBefore, action: m.store.get().action, calls: globalThis.__calls };
        })()
        """,
        actionsPath=str(ACTIONS_JS),
        land=ELDRAZI_TEMPLE,
        mountain=BASIC_MOUNTAIN,
    )
    assert result["pinnedBefore"] == [{"card_id": "et1", "color": None, "ability_index": 1}]
    # Nothing is pinned any more, so `needsExplicitFire` no longer holds the
    # cast back and it goes through - exactly what taking the last pin off
    # already did before an ability could be named. The choice is gone with
    # it: the sources are bare ids again.
    assert result["action"] is None
    probes = [c for c in result["calls"] if c["method"] == "POST" and c["body"].get("dry_run")]
    assert probes[-1]["body"]["mana_sources"] == ["et1", "b1"]


def test_a_source_that_adds_several_mana_of_one_colour_asks_only_which_colour() -> None:
    """Shang-Chi's `{T}: Add two mana of any one color.` is one ability and one
    choice worth two mana. There is nothing to ask about *which* ability, so no
    ability menu and no index on the wire; the colour menu is the whole
    question, and the pin that comes out of it names that colour once.

    The count is deliberately not the browser's business - it is nowhere in
    `AbilityView`, and `engine_api._expanded_to_the_abilitys_own_count` fills
    it in from the ability itself, so every client gets it right rather than
    only this one. A UI that repeated the colour here would be guessing at a
    number it cannot see."""
    plan = run_app("m.planPin(land, action)", land=SHANG_CHI, action=pinning_action())
    assert plan == {"kind": "colour", "abilityIndex": None, "colors": ["W", "U", "B", "R", "G"]}


# -- a click that registers must say so before the server answers -----------
#
# T2, the second half of the reported sluggishness. The click that DOES
# register gave the player nothing back for 0.4-2.4s: no spinner, no disable,
# no double-submit guard, and a post-action `poll()` that was frequently a
# silent no-op (it returns immediately whenever `app.polling` is set, and the
# background poll holding that flag then discarded its own reads through the
# `mutations` guard). Indistinguishable from a lost click, which is what
# provoked the second one - and that second click was a genuine, unguarded
# double submit.


def _click_body() -> str:
    """A fake DOM and a `fetch` stub with two gates.

    `__holdPost` keeps every POST outstanding until `__releasePost()` - the
    window a click spends waiting on the server, where the in-flight state
    has to be visible and the second click has to be swallowed. `__holdLog`
    does the same for `/log`, which is what a background poll sits in
    (`_stale_poll_body` above, and `poll()`'s own comment on why one cycle
    outlives `POLL_MS` late-game).

    A POST moves the board, exactly as the server would: the board read
    answers `bob` from then on, so a refresh that actually happened is
    visible in the store and one that silently no-op'd is not."""
    return """
      globalThis.__calls = [];
      globalThis.__toast = { textContent: "", hidden: true };
      globalThis.__classes = new Set();
      globalThis.document = {
        body: {
          classList: {
            toggle: (name, on) => {
              if (on) globalThis.__classes.add(name);
              else globalThis.__classes.delete(name);
            },
          },
          setAttribute: () => {},
          removeAttribute: () => {},
        },
        querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
      };
      const board = (holder) => ({
        you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
        active_player: "bob", priority_holder: holder, battlefield: [],
        hands: { alice: [] }, stack: [], combat: { attacks: [] },
        winner: null, drawn: false, pregame: null,
      });
      globalThis.__board = board;
      globalThis.__priority = "alice";
      globalThis.__holdPost = false;
      globalThis.__holdLog = false;
      let releasePost;
      let releaseLog;
      const postGate = new Promise((r) => { releasePost = r; });
      const logGate = new Promise((r) => { releaseLog = r; });
      globalThis.__releasePost = () => releasePost();
      globalThis.__releaseLog = () => releaseLog();
      globalThis.fetch = (path, opts) => {
        const method = opts ? opts.method : "GET";
        globalThis.__calls.push({ method, path });
        if (method === "POST") {
          globalThis.__priority = "bob";
          const answer = { ok: true, json: async () => ({ allowed: true }) };
          return globalThis.__holdPost ? postGate.then(() => answer) : Promise.resolve(answer);
        }
        const log = { ok: true, json: async () => ({ log: [], last_seq: 1 }) };
        if (path.includes("/log")) {
          return globalThis.__holdLog ? logGate.then(() => log) : Promise.resolve(log);
        }
        return Promise.resolve({ ok: true, json: async () => board(globalThis.__priority) });
      };
      const seated = () => m.store.set({
        gameId: "g1", uid: "u1", run: null, busy: 0,
        stops: { mine: [], theirs: [], onStack: true },
        state: globalThis.__board("alice"),
        log: { entries: [], lastSeq: 0 },
      });
    """


def test_an_action_shows_in_flight_state_before_the_server_answers() -> None:
    """The whole point: the feedback is on the click, not on the round trip.
    `send()` is the seam every mutating request in `app.js` goes through, so
    marking in-flight there covers `playLand`, `fireSpecial`, `cycle`,
    `advanceStep`, `decide`, `onPregame`, `concede`, `fire` and the pass
    verbs at once rather than nine hand-wired spinners."""
    result = run_app(
        _click_body()
        + """
        (async () => {
          seated();
          globalThis.__holdPost = true;
          const inFlight = m.declareNoBlockers();
          await new Promise((r) => setTimeout(r, 0));
          const during = { busy: m.store.get().busy, marked: globalThis.__classes.has("busy") };
          globalThis.__releasePost();
          await inFlight;
          const after = { busy: m.store.get().busy, marked: globalThis.__classes.has("busy") };
          return { during, after };
        })()
        """
    )
    assert result["during"] == {"busy": 1, "marked": True}, (
        "the click gave the player nothing back until the server answered"
    )
    assert result["after"] == {"busy": 0, "marked": False}


def test_a_second_click_while_the_same_action_is_outstanding_sends_nothing() -> None:
    """The reported symptom's own second half: the instinctive second click
    on an unresponsive-looking control was a real, unguarded double submit."""
    result = run_app(
        _click_body()
        + """
        (async () => {
          seated();
          globalThis.__holdPost = true;
          const first = m.declareNoBlockers();
          const second = m.declareNoBlockers();
          await new Promise((r) => setTimeout(r, 0));
          globalThis.__releasePost();
          await Promise.all([first, second]);
          return globalThis.__calls.filter(
            (c) => c.method === "POST" && c.path.includes("declare-blockers")
          ).length;
        })()
        """
    )
    assert result == 1, "the same action was submitted twice while one was outstanding"


def test_the_same_action_can_be_sent_again_once_the_first_has_answered() -> None:
    """The guard is per-request-in-flight, never a latch: a player who really
    does want to declare no blockers twice in one game must be able to."""
    result = run_app(
        _click_body()
        + """
        (async () => {
          seated();
          await m.declareNoBlockers();
          await m.declareNoBlockers();
          return globalThis.__calls.filter(
            (c) => c.method === "POST" && c.path.includes("declare-blockers")
          ).length;
        })()
        """
    )
    assert result == 2


def test_an_action_refreshes_the_board_even_with_a_poll_already_in_flight() -> None:
    """`poll()` returns immediately when `app.polling` is set, so an action's
    own `await poll()` was a silent no-op whenever a background poll was in
    flight - and that background poll then discarded its own reads through
    the `mutations` guard, leaving the board inert for up to a full tick
    after a click that had already succeeded. The action's refresh has to be
    a read of its own, not a request to a poll that is already busy."""
    result = run_app(
        _click_body()
        + """
        (async () => {
          seated();
          globalThis.__holdLog = true;
          const inFlight = m.poll();
          await new Promise((r) => setTimeout(r, 0));
          await m.declareNoBlockers();
          const after = m.store.get().state.priority_holder;
          globalThis.__releaseLog();
          await inFlight;
          return after;
        })()
        """
    )
    assert result == "bob", (
        "the board still showed the pre-action world after the action had succeeded"
    )


def test_the_older_of_two_concurrent_board_reads_never_lands_on_the_newer() -> None:
    """`refresh()` sets no `polling` flag on purpose, so an action's own board
    read and a background poll's are routinely outstanding together. The
    `mutations` guard separates reads issued before one of this seat's
    requests from reads issued after it, and cannot order two that were both
    issued after the same one - both pass it, and whichever is ANSWERED last
    wins, which is not the same thing as whichever is newest.

    Here the action's read is issued first and answered last, and its board
    is a turn behind the poll's. Publishing it puts the board backwards for a
    tick, and hands `tickRun()` a world that has already moved - the same
    failure the `mutations` guard exists to prevent, one layer down."""
    result = run_app(
        _click_body()
        + """
        (async () => {
          seated();
          const original = globalThis.fetch;
          let releaseFirst;
          const firstGate = new Promise((r) => { releaseFirst = r; });
          let boards = 0;
          globalThis.fetch = (path, opts) => {
            const method = opts ? opts.method : "GET";
            if (method === "GET" && !path.includes("/log")) {
              boards += 1;
              const which = boards;
              const answer = {
                ok: true,
                json: async () => ({
                  ...globalThis.__board("bob"),
                  turn_number: which === 1 ? 4 : 5,
                }),
              };
              // The first board read issued is the one held open, until the
              // read issued behind it has already been answered and written.
              return which === 1 ? firstGate.then(() => answer) : Promise.resolve(answer);
            }
            return original(path, opts);
          };
          const acting = m.declareNoBlockers();
          await new Promise((r) => setTimeout(r, 0));
          const polling = m.poll();
          await new Promise((r) => setTimeout(r, 0));
          const beforeRelease = m.store.get().state.turn_number;
          releaseFirst();
          await Promise.all([acting, polling]);
          return { boards, beforeRelease, after: m.store.get().state.turn_number };
        })()
        """
    )
    assert result["boards"] == 2, f"the two reads never overlapped: {result}"
    assert result["beforeRelease"] == 5, "the later read's board never landed"
    assert result["after"] == 5, "a board read answered late put the board back a turn"


def test_a_refresh_read_that_predates_a_later_action_is_still_discarded() -> None:
    """The `mutations` guard is load-bearing (MEMORY.md, 2026-09-08) and the
    action's own refresh is subject to it exactly as `poll()` is: a read that
    spans another of this seat's requests describes a board that no longer
    exists, whoever issued it."""
    result = run_app(
        _click_body()
        + """
        (async () => {
          seated();
          m.store.set({ state: globalThis.__board("carol") });
          globalThis.__holdLog = true;
          // The refresh's board read is answered while a *second* request of
          // this seat's own is outstanding, so it must never be published.
          const original = globalThis.fetch;
          let releaseState;
          const stateGate = new Promise((r) => { releaseState = r; });
          globalThis.fetch = (path, opts) => {
            const method = opts ? opts.method : "GET";
            if (method === "GET" && !path.includes("/log")) {
              return stateGate.then(() => original(path, opts));
            }
            return original(path, opts);
          };
          const acting = m.declareNoBlockers();
          await new Promise((r) => setTimeout(r, 0));
          await m.send("POST", m.gamePath("actions/pass-priority"), { player: "alice" });
          releaseState();
          await acting;
          return m.store.get().state.priority_holder;
        })()
        """
    )
    assert result == "carol", "a board read taken across a later action was published anyway"


def test_poll_reads_the_board_and_the_log_concurrently() -> None:
    """`app.js`'s own comment says one poll cycle already outgrows `POLL_MS`
    late-game and names `/log` as the slow half. The two reads are
    independent, so serialising them spends the sum of two latencies where
    the maximum would do - and every extra millisecond there is a
    millisecond the board is out of date after a click."""
    result = run_app(
        """
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          body: {
            classList: { toggle: () => {} },
            setAttribute: () => {},
            removeAttribute: () => {},
          },
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        let logIssued = false;
        let openTheBoardRead;
        const boardGate = new Promise((r) => { openTheBoardRead = r; });
        globalThis.fetch = (path, opts) => {
          const method = opts ? opts.method : "GET";
          if (path.includes("/log")) {
            logIssued = true;
            openTheBoardRead();
            return Promise.resolve({ ok: true, json: async () => ({ log: [], last_seq: 1 }) });
          }
          // The board read answers only once the log read has been issued -
          // or, so a serial implementation fails rather than hangs, after a
          // short wait. `__concurrent` records which of the two happened.
          return Promise.race([boardGate, new Promise((r) => setTimeout(r, 250))]).then(() => ({
            ok: true,
            json: async () => {
              globalThis.__concurrent = logIssued;
              return {
                you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
                active_player: "bob", priority_holder: "bob", battlefield: [],
                hands: { alice: [] }, stack: [], combat: { attacks: [] },
                winner: null, drawn: false, pregame: null,
              };
            },
          }));
        };
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", run: null, busy: 0,
            stops: { mine: [], theirs: [], onStack: true },
            state: null, log: { entries: [], lastSeq: 0 },
          });
          await m.poll();
          return globalThis.__concurrent;
        })()
        """
    )
    assert result is True, "poll() waited for the board read before it issued the log read"


def test_a_polls_log_read_is_dropped_once_a_refresh_has_advanced_the_log() -> None:
    """`refresh()` sets no `polling` flag, so an action's own log read and a
    background poll's log read are routinely in flight together - `/log` runs
    0.4-2.4s against a 1200ms tick. Both were issued with the same `since`,
    so whichever lands second carries a second copy of the same entries.
    `refresh()` has had the `lastSeq` guard for exactly that since it was
    written; `poll()` had none, and `patchChildren` builds its `existing` map
    before it creates anything - a key repeated inside one items list becomes
    a second node that the next patch can no longer see, so the duplicated
    lines stay on the screen for good."""
    result = run_app(
        """
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          body: {
            classList: { toggle: () => {} },
            setAttribute: () => {},
            removeAttribute: () => {},
          },
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        const board = {
          you: "alice", pending_decision: null, step: "declare_blockers", turn_number: 4,
          active_player: "bob", priority_holder: "bob", battlefield: [],
          hands: { alice: [] }, stack: [], combat: { attacks: [] },
          winner: null, drawn: false, pregame: null,
        };
        const ENTRIES = [{ seq: 1, text: "one" }, { seq: 2, text: "two" }];
        let logCalls = 0;
        let openTheActionsLog;
        const actionsLog = new Promise((r) => { openTheActionsLog = r; });
        let releaseThePollsLog;
        const pollsLog = new Promise((r) => { releaseThePollsLog = r; });
        globalThis.__releaseThePollsLog = () => releaseThePollsLog();
        globalThis.fetch = (path, opts) => {
          const method = opts ? opts.method : "GET";
          if (method === "POST") {
            return Promise.resolve({ ok: true, json: async () => ({ allowed: true }) });
          }
          if (path.includes("/log")) {
            const since = Number(new URL(path, "http://t").searchParams.get("since"));
            const log = ENTRIES.filter((e) => e.seq > since);
            const payload = { log, last_seq: log.length ? log[log.length - 1].seq : since };
            logCalls += 1;
            // The action's own read is held until the poll behind it has
            // issued its read - that is the whole window, and it is the
            // ordinary one: the poll reads `lastSeq` while the earlier read
            // that will advance it is still outstanding, so both ask for the
            // same entries.
            if (logCalls === 1) {
              return actionsLog.then(() => ({ ok: true, json: async () => payload }));
            }
            openTheActionsLog();
            return pollsLog.then(() => ({ ok: true, json: async () => payload }));
          }
          return Promise.resolve({ ok: true, json: async () => board });
        };
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", run: null, busy: 0,
            stops: { mine: [], theirs: [], onStack: true },
            state: board, log: { entries: [], lastSeq: 0 },
          });
          const acting = m.declareNoBlockers();
          await new Promise((r) => setTimeout(r, 0));
          const polling = m.poll();
          await new Promise((r) => setTimeout(r, 0));
          globalThis.__releaseThePollsLog();
          await Promise.all([acting, polling]);
          return m.store.get().log;
        })()
        """
    )
    assert [e["seq"] for e in result["entries"]] == [1, 2], (
        f"the poll appended entries the action's own refresh had already written: {result}"
    )
    assert result["lastSeq"] == 2


def test_nothing_the_player_can_click_is_made_inert_while_a_request_is_out() -> None:
    """`pointer-events: none` on a control is the same lost click this whole
    branch exists to remove, arrived at from the other side: a player whose
    press lands on an inert button gets nothing, for the whole 0.4-2.4s.

    It bought nothing either. The real double-submit guard is `send()`'s
    `inFlight` set, keyed on method+path+body, which suppresses the identical
    resubmission and nothing else; the CSS could not tell that apart from a
    *different* action (cancelling a cast, un-picking a mode, picking a
    second target), so it blocked both. The dimming stays - it is honest
    feedback that a submission is out - and the popover drops out of the rule
    entirely, because a menu opened after the submission is not part of it.

    A stylesheet has no behaviour to drive under Node (`.popover button`
    would still be clickable in every test in this file, which is exactly why
    46 of them missed the defect), so this pins the declaration itself. The
    behavioural half - that a read or a probe never marks the page busy in
    the first place - is `test_a_read_and_a_probe_never_mark_the_page_busy`
    below."""
    css = (Path(__file__).resolve().parents[1] / "ui" / "style.css").read_text()
    rules = re.findall(r"^(body\.busy[^{]*button[^{]*)\{(.*?)^\}", css, re.MULTILINE | re.DOTALL)
    assert rules, "the in-flight feedback on the ribbon's buttons is gone entirely"
    for selector, body in rules:
        assert "pointer-events" not in body, f"{selector.strip()} makes a control inert"
        assert ".popover" not in selector, "an open menu is never part of the request in flight"


def test_the_toast_never_intercepts_a_click() -> None:
    """`.toast` is text-only - every call site passes a string - and it sits
    `position: fixed` over the bottom centre of the viewport for 4000ms on
    every refusal, which is exactly where `#my-hand` and `#my-lands` (with
    `#my-badge`) are. The
    card preview `.card-zoom` carries `pointer-events: none` for the same
    reason; this brings the two into step.

    A style rule has no behaviour to drive under Node, so this pins the
    declaration in the stylesheet itself rather than claiming a red-green
    cycle over a running page."""
    css = (Path(__file__).resolve().parents[1] / "ui" / "style.css").read_text()
    rule = re.search(r"^\.toast \{(.*?)^\}", css, re.MULTILINE | re.DOTALL)
    assert rule is not None, ".toast rule not found in ui/style.css"
    assert "pointer-events: none;" in rule.group(1)


# -- T5: every action click opens the confirm, and no selection click does ----
#
# The planners above say what a click *means*; this section drives the real
# click handlers - `onHandCard`/`onPermanent`, through the real
# `render/ribbon.js` `showPopover` against the same fake DOM the pin menus
# use - and asserts what a click actually *opens*. Two halves, and they are
# the whole point of the change:
#
#   * A click that ACTS opens the anchored picker first, single option and
#     all, and fires nothing until that option is picked. The one-option
#     popover IS the confirm: no second component, and no Cancel item, since
#     `showPopover` already leaves on Escape, on a click away, and on a pick.
#   * A click that SELECTS - a target, a mana source mid-cast, an attacker or
#     blocker into the combat draft - is untouched and opens nothing. A
#     confirm on a target pick would make a three-target spell a nine-click
#     affair, and a basic land pinning with no menu at all is a standing
#     commitment (`ui/app.js`'s own `planPin` note, MEMORY.md).
#
# The dialog is enriched from `GET .../legal-actions` in parallel: it opens
# from local state on the click, and the engine's own `affordable` /
# `unaffordable_reason` is folded into the menu already on screen when the
# answer lands. Never on the poll - per-card affordance work is banned from
# the 1.2s tick.

OPPONENT_BEAR = {
    "id": "ob1",
    "name": "Grizzly Bears",
    "type_line": "Creature - Bear",
    "zone": "battlefield",
    "controller": "bob",
    "abilities": [],
}


def _click_state(**overrides: Any) -> dict:
    return {
        "you": "alice",
        "players": [{"username": "alice"}, {"username": "bob"}],
        "pending_decision": None,
        "step": "precombat_main",
        "active_player": "alice",
        "priority_holder": "alice",
        "battlefield": [],
        "hands": {"alice": []},
        "stack": [],
        **overrides,
    }


def _click_setup() -> str:
    """Seat this tab at alice's seat of game `g1` with nothing in flight."""
    return """
      m.store.set({
        gameId: "g1", uid: "u1", state,
        stops: { mine: [], theirs: [], onStack: true, keepPriorityByDefault: true },
        action: null, combatDraft: {},
      });
      const anchor = new El("div");
    """


def _click(expression: str, **bindings: Any) -> Any:
    body = _fake_dom_body() + "(async () => {" + _click_setup() + expression + "})()"
    return run_app(body, **bindings)


def test_playing_a_land_opens_a_one_option_confirm_and_fires_nothing_yet() -> None:
    """`planHandCardClick` still answers `{kind: "land"}`; what used to be an
    immediate `POST actions/play-land` is now one option in the same anchored
    menu a cycling land has always opened, and the land is played when it is
    picked."""
    result = _click(
        """
          const clicked = m.onHandCard(land, { currentTarget: anchor });
          const opened = {
            hidden: globalThis.__popover.hidden,
            labels: options().map((o) => o.label),
            calls: globalThis.__calls.map((c) => `${c.method} ${c.path}`),
          };
          await clicked;
          options()[0].click();
          await settle();
          return { opened, calls: globalThis.__calls };
        """,
        state=_click_state(hands={"alice": [MOUNTAIN]}),
        land=MOUNTAIN,
    )
    assert result["opened"]["hidden"] is False, "the click must open the confirm"
    assert result["opened"]["labels"] == ["Play"]
    opened_calls = result["opened"]["calls"]
    assert not [c for c in opened_calls if c.startswith("POST")], opened_calls
    plays = [
        c for c in result["calls"] if c["method"] == "POST" and "actions/play-land" in c["path"]
    ]
    assert len(plays) == 1, result["calls"]
    assert plays[0]["body"] == {"player": "alice", "card_id": "land1"}


def test_casting_from_hand_opens_a_one_option_confirm_before_the_announcement() -> None:
    """Kozilek's Command: the click used to begin the announcement outright.
    It opens `Cast` first, and only picking it stages the announcement in the
    ribbon (`store.action`)."""
    result = _click(
        """
          const clicked = m.onHandCard(spell, { currentTarget: anchor });
          const opened = {
            labels: options().map((o) => o.label),
            action: m.store.get().action,
          };
          await clicked;
          options()[0].click();
          await settle();
          return { opened, action: m.store.get().action, calls: globalThis.__calls };
        """,
        state=_click_state(hands={"alice": [KOZILEK]}),
        spell=KOZILEK,
    )
    assert result["opened"]["labels"] == ["Cast"]
    assert result["opened"]["action"] is None, "nothing may begin before the confirm is picked"
    assert result["action"]["cardId"] == "kc"
    assert result["action"]["kind"] == "cast"


def test_a_single_ability_permanent_opens_a_confirm_naming_that_ability() -> None:
    """`{kind: "activate", abilityIndex: 0}` used to reach `beginActivate`
    directly. The confirm says which ability it is about - read off the card,
    because the plan carries no list of its own - which is what an "are you
    sure?" box could not have said."""
    result = _click(
        """
          const clicked = m.onPermanent(pw, { currentTarget: anchor });
          const opened = {
            labels: options().map((o) => o.label),
            calls: globalThis.__calls.filter((c) => c.method === "POST").length,
          };
          await clicked;
          options()[0].click();
          await settle();
          return { opened, calls: globalThis.__calls };
        """,
        state=_click_state(battlefield=[ONE_ACTIVATED_ABILITY_PW]),
        pw=ONE_ACTIVATED_ABILITY_PW,
    )
    assert result["opened"]["labels"] == ["+1: draw a card"]
    assert result["opened"]["calls"] == 0, "nothing may be activated before the confirm is picked"
    activations = [
        c for c in result["calls"] if c["method"] == "POST" and "actions/activate" in c["path"]
    ]
    assert activations, result["calls"]
    assert activations[0]["body"]["card_id"] == "pw1"
    assert activations[0]["body"]["ability_index"] == 0


def test_a_permanent_with_several_abilities_still_opens_the_menu_it_always_did() -> None:
    result = _click(
        """
          await m.onPermanent(pw, { currentTarget: anchor });
          return { labels: options().map((o) => o.label), hidden: globalThis.__popover.hidden };
        """,
        state=_click_state(battlefield=[TWO_ABILITY_PW]),
        pw=TWO_ABILITY_PW,
    )
    assert result["hidden"] is False
    assert result["labels"] == ["+1: draw a card", "-2: destroy target creature"]


def test_a_vehicle_still_reaches_crew_through_its_own_menu() -> None:
    """Crew, cycling and the special actions already routed to a menu and
    keep doing so - the confirm changes the single-option clicks, not these."""
    result = _click(
        """
          await m.onPermanent(vehicle, { currentTarget: anchor });
          return { labels: options().map((o) => o.label) };
        """,
        state=_click_state(battlefield=[VEHICLE]),
        vehicle=VEHICLE,
    )
    assert result["labels"] == ["Crew"]


# -- equip (CR 702.6a): offer, target, post, refuse ---------------------------
#
# Equip is the one card-scoped menu that cannot be decided from local state:
# it has no entry in `card.abilities` at all, so whether it belongs in the
# menu is answered by the engine's own `GET .../legal-actions` rather than by
# the card the way every other item on this menu already is. These drive the
# real `onPermanent` through the same fake popover the tests above use, with
# `globalThis.__respond` standing in for the server.


def test_equip_is_offered_when_the_legal_actions_row_exists() -> None:
    result = _click(
        """
          globalThis.__respond = (method, path) => {
            if (method === "GET" && path.includes("legal-actions")) {
              return Promise.resolve({
                ok: true,
                json: async () => ({ actions: [equipRow], last_seq: 7 }),
              });
            }
            return null;
          };
          await m.onPermanent(equipment, { currentTarget: anchor });
          return {
            labels: options().map((o) => o.label),
            hidden: globalThis.__popover.hidden,
          };
        """,
        state=_click_state(battlefield=[EQUIPMENT, ATTACKING_CREATURE]),
        equipment=EQUIPMENT,
        equipRow=_equip_row(),
    )
    assert result["hidden"] is False
    assert result["labels"] == ["Equip"]


def test_equip_is_not_offered_outside_a_main_phase() -> None:
    """CR 702.6a: "Activate only as a sorcery" makes the engine drop the row
    entirely (`action_rows.legal_action_rows`'s own `functions_where_it_is`
    filter) rather than mark it unaffordable - `rows == []` in the api's own
    `test_equip_at_instant_speed_is_refused_with_the_engines_reason`. The menu
    must stay just as silent: no popover, no option, and no engine request
    made on the player's behalf beyond the one read that answered this."""
    result = _click(
        """
          globalThis.__respond = (method, path) => {
            if (method === "GET" && path.includes("legal-actions")) {
              return Promise.resolve({
                ok: true,
                json: async () => ({ actions: [], last_seq: 7 }),
              });
            }
            return null;
          };
          await m.onPermanent(equipment, { currentTarget: anchor });
          return {
            hidden: globalThis.__popover.hidden,
            options: options().length,
            posts: globalThis.__calls.filter((c) => c.method === "POST"),
          };
        """,
        state=_click_state(battlefield=[EQUIPMENT, ATTACKING_CREATURE]),
        equipment=EQUIPMENT,
    )
    assert result["hidden"] is True
    assert result["options"] == 0
    assert result["posts"] == []


def test_choosing_equip_offers_only_the_rows_own_candidates() -> None:
    result = _click(
        """
          globalThis.__respond = (method, path) => {
            if (method === "GET" && path.includes("legal-actions")) {
              return Promise.resolve({
                ok: true,
                json: async () => ({ actions: [equipRow], last_seq: 7 }),
              });
            }
            return null;
          };
          await m.onPermanent(equipment, { currentTarget: anchor });
          const firstLabels = options().map((o) => o.label);
          options()[0].click();
          await settle();
          return {
            firstLabels,
            targetLabels: options().map((o) => o.label),
            posts: globalThis.__calls.filter((c) => c.method === "POST"),
          };
        """,
        state=_click_state(battlefield=[EQUIPMENT, ATTACKING_CREATURE]),
        equipment=EQUIPMENT,
        equipRow=_equip_row(),
    )
    assert result["firstLabels"] == ["Equip"]
    assert result["targetLabels"] == ["Equip Grizzly Bears"]
    assert result["posts"] == [], "picking Equip only opens the target popover; nothing fires yet"


def test_picking_the_target_posts_the_equip_route_with_the_expected_seq() -> None:
    result = _click(
        """
          globalThis.__respond = (method, path) => {
            if (method === "GET" && path.includes("legal-actions")) {
              return Promise.resolve({
                ok: true,
                json: async () => ({ actions: [equipRow], last_seq: 7 }),
              });
            }
            if (method === "POST" && path.includes("actions/equip")) {
              return Promise.resolve({
                ok: true,
                json: async () => ({ allowed: true, cost: "{1}" }),
              });
            }
            return null;
          };
          await m.onPermanent(equipment, { currentTarget: anchor });
          options()[0].click(); // "Equip"
          await settle();
          options()[0].click(); // the one candidate
          await settle();
          return { calls: globalThis.__calls };
        """,
        state=_click_state(battlefield=[EQUIPMENT, ATTACKING_CREATURE]),
        equipment=EQUIPMENT,
        equipRow=_equip_row(),
    )
    posts = [c for c in result["calls"] if c["method"] == "POST" and "actions/equip" in c["path"]]
    assert len(posts) == 1, result["calls"]
    assert posts[0]["body"] == {
        "player": "alice",
        "card_id": "eq1",
        "target": "bears1",
        "expected_seq": 7,
    }


def test_an_equip_refusal_shows_the_engines_reason() -> None:
    reason = "Blade of the Bloodchief may only be activated as a sorcery (CR 702.6a)"
    result = _click(
        """
          globalThis.__respond = (method, path) => {
            if (method === "GET" && path.includes("legal-actions")) {
              return Promise.resolve({
                ok: true,
                json: async () => ({ actions: [equipRow], last_seq: 7 }),
              });
            }
            if (method === "POST" && path.includes("actions/equip")) {
              return Promise.resolve({ ok: true, json: async () => ({ allowed: false, reason }) });
            }
            return null;
          };
          await m.onPermanent(equipment, { currentTarget: anchor });
          options()[0].click();
          await settle();
          options()[0].click();
          await settle();
          return { toast: globalThis.__toast.textContent, toastHidden: globalThis.__toast.hidden };
        """,
        state=_click_state(battlefield=[EQUIPMENT, ATTACKING_CREATURE]),
        equipment=EQUIPMENT,
        equipRow=_equip_row(),
        reason=reason,
    )
    assert result["toastHidden"] is False
    assert result["toast"] == reason


def test_a_card_with_no_available_action_stays_a_silent_no_op() -> None:
    """The owner's explicit choice: no dialog explaining why nothing
    happened. An opponent's permanent with nothing pending opens no menu,
    says nothing, and asks the server nothing at all."""
    result = _click(
        """
          await m.onPermanent(theirs, { currentTarget: anchor });
          return {
            hidden: globalThis.__popover.hidden,
            options: options().length,
            toast: globalThis.__toast.textContent,
            toastHidden: globalThis.__toast.hidden,
            calls: globalThis.__calls,
          };
        """,
        state=_click_state(battlefield=[OPPONENT_BEAR]),
        theirs=OPPONENT_BEAR,
    )
    assert result["hidden"] is True
    assert result["options"] == 0
    assert result["toast"] == ""
    assert result["toastHidden"] is True
    assert result["calls"] == [], "a click with nothing to do must not even ask the engine"


# -- the enrichment: the engine's own affordability, folded in on arrival -----


UNAFFORDABLE = {
    "kind": "cast",
    "card_id": "kc",
    "ability_index": None,
    "affordable": "no",
    "unaffordable_reason": "cannot pay {X}{C}{C}: 1 mana available",
}


def test_the_confirm_asks_the_engine_for_this_seats_legal_actions_on_the_click() -> None:
    """On the click, never on the poll - per-card affordance work is banned
    from the 1.2s tick. The read is uid-guarded to this seat, so the uid goes
    with it."""
    result = _click(
        """
          await m.onHandCard(spell, { currentTarget: anchor });
          await settle();
          return globalThis.__calls.map((c) => `${c.method} ${c.path}`);
        """,
        state=_click_state(hands={"alice": [KOZILEK]}),
        spell=KOZILEK,
    )
    assert result == ["GET /api/games/g1/legal-actions?player=alice&uid=u1"]


def test_an_unaffordable_option_is_disabled_and_carries_the_engines_own_reason() -> None:
    result = _click(
        """
          globalThis.__respond = (method, path) =>
            path.includes("legal-actions")
              ? Promise.resolve({ ok: true, json: async () => ({ actions: [row] }) })
              : null;
          await m.onHandCard(spell, { currentTarget: anchor });
          await settle();
          return {
            hidden: globalThis.__popover.hidden,
            disabled: options().map((o) => o.disabled),
            text: options().map((o) => o.deepText),
          };
        """,
        state=_click_state(hands={"alice": [KOZILEK]}),
        spell=KOZILEK,
        row=UNAFFORDABLE,
    )
    assert result["hidden"] is False, "the dialog stays open - the answer enriches it in place"
    assert result["disabled"] == [True]
    assert result["text"] == ["Cast cannot pay {X}{C}{C}: 1 mana available"]


def test_a_disabled_option_does_nothing_when_it_is_clicked() -> None:
    result = _click(
        """
          globalThis.__respond = (method, path) =>
            path.includes("legal-actions")
              ? Promise.resolve({ ok: true, json: async () => ({ actions: [row] }) })
              : null;
          await m.onHandCard(spell, { currentTarget: anchor });
          await settle();
          options()[0].click();
          await settle();
          const posts = globalThis.__calls.filter((c) => c.method === "POST");
          return { action: m.store.get().action, posts };
        """,
        state=_click_state(hands={"alice": [KOZILEK]}),
        spell=KOZILEK,
        row=UNAFFORDABLE,
    )
    assert result["action"] is None
    assert result["posts"] == []


def test_the_dialog_opens_before_the_engine_answers_and_never_waits_on_it() -> None:
    """The whole point of the surrounding work is that a click feels
    immediate: the menu is built from local state and is on screen before the
    request that enriches it has been answered - here, before it is answered
    at all."""
    result = _click(
        """
          globalThis.__respond = (method, path) =>
            path.includes("legal-actions") ? new Promise(() => {}) : null;
          m.onHandCard(spell, { currentTarget: anchor });
          const immediately = {
            hidden: globalThis.__popover.hidden,
            labels: options().map((o) => o.label),
            disabled: options().map((o) => o.disabled),
          };
          await settle();
          return {
            immediately,
            stillOpen: !globalThis.__popover.hidden,
            labels: options().map((o) => o.label),
          };
        """,
        state=_click_state(hands={"alice": [KOZILEK]}),
        spell=KOZILEK,
    )
    assert result["immediately"] == {"hidden": False, "labels": ["Cast"], "disabled": [False]}
    assert result["stillOpen"] is True
    assert result["labels"] == ["Cast"]


def test_a_failed_legal_actions_read_leaves_the_dialog_exactly_as_it_opened() -> None:
    """A silent no-op, deliberately: the dialog simply stays as it opened,
    rather than closing or announcing a failure the player cannot act on."""
    result = _click(
        """
          globalThis.__respond = (method, path) =>
            path.includes("legal-actions") ? Promise.reject(new Error("network down")) : null;
          await m.onHandCard(spell, { currentTarget: anchor });
          await settle();
          return {
            hidden: globalThis.__popover.hidden,
            labels: options().map((o) => o.label),
            disabled: options().map((o) => o.disabled),
            connection: m.store.get().connection,
          };
        """,
        state=_click_state(hands={"alice": [KOZILEK]}),
        spell=KOZILEK,
    )
    assert result["hidden"] is False
    assert result["labels"] == ["Cast"]
    assert result["disabled"] == [False]
    assert result["connection"] == "idle", "a failed enrichment is not a connection failure"


def test_an_answer_that_lands_after_the_dialog_closed_never_reopens_it() -> None:
    """The player picked the option (or pressed Escape, or clicked away)
    while the read was in flight. What comes back has nothing to enrich."""
    result = _click(
        """
          let release;
          const held = new Promise((r) => { release = r; });
          globalThis.__respond = (method, path) => (path.includes("legal-actions") ? held : null);
          m.onHandCard(land, { currentTarget: anchor });
          options()[0].click();
          await settle();
          const afterPick = { hidden: globalThis.__popover.hidden, options: options().length };
          release({ ok: true, json: async () => ({ actions: [] }) });
          await settle();
          return {
            afterPick,
            hidden: globalThis.__popover.hidden,
            options: options().length,
            plays: globalThis.__calls.filter((c) => c.method === "POST").length,
          };
        """,
        state=_click_state(hands={"alice": [MOUNTAIN]}),
        land=MOUNTAIN,
    )
    assert result["afterPick"] == {"hidden": True, "options": 0}
    assert result["hidden"] is True, "a closed dialog must not be reopened under the cursor"
    assert result["options"] == 0
    assert result["plays"] == 1


def test_an_answer_about_other_cards_leaves_every_option_enabled() -> None:
    result = _click(
        """
          globalThis.__respond = (method, path) =>
            path.includes("legal-actions")
              ? Promise.resolve({ ok: true, json: async () => ({ actions: rows }) })
              : null;
          await m.onHandCard(spell, { currentTarget: anchor });
          await settle();
          return {
            disabled: options().map((o) => o.disabled),
            text: options().map((o) => o.deepText),
          };
        """,
        state=_click_state(hands={"alice": [KOZILEK]}),
        spell=KOZILEK,
        rows=[{**UNAFFORDABLE, "card_id": "someone-else"}],
    )
    assert result["disabled"] == [False]
    assert result["text"] == ["Cast"]


UNAFFORDABLE_FORETELL_CARD = [
    {
        "kind": "cast",
        "card_id": "f1",
        "ability_index": None,
        "affordable": "no",
        "unaffordable_reason": "cannot pay {5}{U}{U}: 2 mana available",
    }
]


def test_an_enrichment_landing_mid_press_does_not_eat_the_click() -> None:
    """The defect that makes this whole path worth testing, and the reason
    the fake DOM above models real dispatch.

    The player presses Foretell on Alrund's Epiphany; while their finger is
    still down, the `legal-actions` answer lands and greys Cast out. A
    browser dispatches `click` to the nearest inclusive ancestor common to
    the mousedown and the mouseup target - so an option detached between the
    two never sees the press at all, and the player's Foretell is silently
    lost. The enrichment therefore has to patch the menu that is on screen,
    the way `dom.js`'s keyed reconciler patches the board, and never tear it
    down and rebuild it.

    The press is the node captured before the answer is released; the release
    is the `click()` after it. Clicking only after the redraw had settled -
    what this test used to do - would pass with the defect fully present."""
    result = _click(
        """
          let release;
          const held = new Promise((r) => { release = r; });
          globalThis.__respond = (method, path) => (path.includes("legal-actions") ? held : null);
          m.onHandCard(card, { currentTarget: anchor });
          // Mousedown. From here on the browser is holding the press on this
          // exact node, and nothing may take it out of the tree.
          const pressed = options()[1];
          release({ ok: true, json: async () => ({ actions: rows }) });
          await settle();
          const afterEnrichment = {
            offered: options().map((o) => ({ label: o.label, disabled: o.disabled })),
            stillInMenu: options().includes(pressed),
            pressedIsDetached: !pressed.attached,
            text: options().map((o) => o.deepText),
          };
          // Mouseup, and the click it becomes.
          pressed.click();
          await settle();
          return { afterEnrichment, calls: globalThis.__calls };
        """,
        state=_click_state(hands={"alice": [FORETELL_CARD]}),
        card=FORETELL_CARD,
        rows=UNAFFORDABLE_FORETELL_CARD,
    )
    assert result["afterEnrichment"]["stillInMenu"] is True, (
        f"the enrichment replaced the option the player was pressing: {result['afterEnrichment']}"
    )
    assert result["afterEnrichment"]["pressedIsDetached"] is False
    # The enrichment still has to have happened - a menu that is intact
    # because nothing was folded into it proves nothing.
    assert result["afterEnrichment"]["offered"] == [
        {"label": "Cast", "disabled": True},
        {"label": "Foretell", "disabled": False},
    ]
    assert result["afterEnrichment"]["text"] == [
        "Cast cannot pay {5}{U}{U}: 2 mana available",
        "Foretell {2}",
    ]
    foretells = [
        c for c in result["calls"] if c["method"] == "POST" and "actions/foretell" in c["path"]
    ]
    assert len(foretells) == 1, result["calls"]
    assert foretells[0]["body"] == {"player": "alice", "card_id": "f1"}


def test_a_read_and_a_probe_never_mark_the_page_busy() -> None:
    """`busy` has to mean "a submission this seat made is outstanding", not
    "some request is". Both halves of that reach the casting strip: picking a
    mode fires `refreshAffordance()` (a GET), and an action that is ready
    fires a dry-run probe (a POST that changes nothing), and both go through
    `send()`. While either was counted, `#casting-strip` - which lives inside
    `#side-panel` - was dimmed and inert for the whole round trip, so the
    player's next click on it (un-picking the mode they just picked) was
    swallowed for 0.4-2.4s: this branch's own symptom, one panel over."""
    read = _click(
        """
          globalThis.__respond = (method, path) =>
            path.includes("affordances") ? new Promise(() => {}) : null;
          await m.onHandCard(spell, { currentTarget: anchor });
          options()[0].click();
          await settle();
          return {
            reads: globalThis.__calls.filter((c) => c.path.includes("affordances")).length,
            busy: m.store.get().busy,
            marked: document.body.classList.contains("busy"),
          };
        """,
        state=_click_state(hands={"alice": [KOZILEK]}),
        spell=KOZILEK,
    )
    assert read["reads"] == 1, "the affordance read never went out, so this proves nothing"
    assert read["busy"] == 0, "an affordance read counted as a submission in flight"
    assert read["marked"] is False, "an affordance read made the casting strip inert"

    probe = _click(
        """
          const A = await import(pathToFileURL(actionsPath).href);
          m.store.set({ action: A.begin("cast", { id: "c1" }, {}) });
          globalThis.__respond = (method) => (method === "POST" ? new Promise(() => {}) : null);
          m.pinLand(mountain, { currentTarget: anchor });
          await settle();
          const posts = globalThis.__calls.filter((c) => c.method === "POST");
          return {
            probes: posts.filter((c) => c.body && c.body.dry_run).length,
            submissions: posts.filter((c) => !(c.body && c.body.dry_run)).length,
            busy: m.store.get().busy,
            marked: document.body.classList.contains("busy"),
          };
        """,
        actionsPath=str(ACTIONS_JS),
        state=_click_state(battlefield=[BASIC_MOUNTAIN]),
        mountain=BASIC_MOUNTAIN,
    )
    assert probe["probes"] == 1, "the dry-run probe never went out, so this proves nothing"
    assert probe["submissions"] == 0
    assert probe["busy"] == 0, "a dry run counted as a submission in flight"
    assert probe["marked"] is False, "a dry run made the casting strip inert"


THREE_ABILITY_PERMANENT = {
    "id": "pw3",
    "name": "Three-Ability Planeswalker",
    "type_line": "Legendary Planeswalker",
    "zone": "battlefield",
    "controller": "alice",
    "abilities": [
        {"index": 0, "text": "+1: draw a card", "is_mana_ability": False},
        {"index": 1, "text": "-2: destroy target creature", "is_mana_ability": False},
        {"index": 2, "text": "-7: take an extra turn", "is_mana_ability": False},
    ],
}


def test_an_enrichment_does_not_yank_focus_back_to_the_first_option() -> None:
    """The keyboard half of the same defect, on the board case the review
    found: a three-ability permanent whose middle ability the engine reports
    as unaffordable. `showPopover` focuses the first enabled option as it
    opens - right on an open, wrong on a redraw. A player who has arrow-keyed
    down to the third ability and is about to press Enter must not be thrown
    back up to the first because a background read happened to land, which
    would activate an ability they never chose."""
    result = _click(
        """
          let release;
          const held = new Promise((r) => { release = r; });
          globalThis.__respond = (method, path) => (path.includes("legal-actions") ? held : null);
          m.onPermanent(pw, { currentTarget: anchor });
          // The player arrow-keys down twice; `ribbon.js`'s `moveFocus` does
          // exactly this to the option it lands on.
          options()[2].focus();
          release({ ok: true, json: async () => ({ actions: rows }) });
          await settle();
          const focused = document.activeElement;
          return {
            index: options().indexOf(focused),
            label: focused && focused.label,
            disabled: options().map((o) => o.disabled),
          };
        """,
        state=_click_state(battlefield=[THREE_ABILITY_PERMANENT]),
        pw=THREE_ABILITY_PERMANENT,
        rows=[
            {
                "kind": "activate",
                "card_id": "pw3",
                "ability_index": 1,
                "affordable": "no",
                "unaffordable_reason": "not enough loyalty",
            }
        ],
    )
    assert result["index"] == 2, f"the enrichment moved the player's focus: {result}"
    assert result["label"] == "-7: take an extra turn"
    assert result["disabled"] == [False, True, False], "and it still enriched the menu"


# -- the selection clicks, proved untouched ----------------------------------


def test_picking_a_target_mid_cast_opens_no_confirm() -> None:
    """`planHandCardClick`'s `{kind: "target"}` while an action is live. A
    confirm here is exactly what the owner ruled out: three targets would
    become nine clicks."""
    result = _click(
        """
          const A = await import(pathToFileURL(actionsPath).href);
          let action = A.begin("cast", { id: "kc" }, {});
          action = A.withAffordance(action, { understood: false, candidates: [] });
          m.store.set({ action });
          await m.onHandCard(target, { currentTarget: anchor });
          return {
            hidden: globalThis.__popover.hidden,
            options: options().length,
            slots: m.store.get().action.freeTargets || m.store.get().action.slots,
            calls: globalThis.__calls,
          };
        """,
        actionsPath=str(ACTIONS_JS),
        state=_click_state(hands={"alice": [KOZILEK, MEMNITE]}),
        target=MEMNITE,
    )
    assert result["hidden"] is True, "a target pick must not open a menu"
    assert result["options"] == 0
    assert result["calls"] == [], "and must not ask the engine for legal actions either"
    assert "mm" in json.dumps(result["slots"]), result["slots"]


def test_picking_a_permanent_as_a_target_opens_no_confirm() -> None:
    result = _click(
        """
          const A = await import(pathToFileURL(actionsPath).href);
          let action = A.begin("cast", { id: "kc" }, {});
          action = A.withAffordance(action, { understood: false, candidates: [] });
          m.store.set({ action });
          await m.onPermanent(theirs, { currentTarget: anchor });
          return {
            hidden: globalThis.__popover.hidden,
            options: options().length,
            calls: globalThis.__calls,
          };
        """,
        actionsPath=str(ACTIONS_JS),
        state=_click_state(battlefield=[OPPONENT_BEAR]),
        theirs=OPPONENT_BEAR,
    )
    assert result["hidden"] is True
    assert result["options"] == 0
    assert result["calls"] == []


def test_a_choose_targets_decision_click_opens_no_confirm() -> None:
    result = _click(
        """
          await m.onPermanent(theirs, { currentTarget: anchor });
          return {
            hidden: globalThis.__popover.hidden,
            draft: m.store.get().decisionDraft,
            posts: globalThis.__calls.filter((c) => c.method === "POST").length,
          };
        """,
        state=_click_state(
            battlefield=[OPPONENT_BEAR],
            pending_decision={"player": "alice", "kind": "choose_targets"},
        ),
        theirs=OPPONENT_BEAR,
    )
    assert result["hidden"] is True
    assert result["draft"]["targets"] == ["ob1"]


def test_tapping_a_basic_land_for_mana_mid_cast_still_opens_nothing() -> None:
    """The standing commitment: a basic land pins on one click, with no menu
    of any kind. The confirm dialog does not reach the pin flow."""
    result = _click(
        """
          const A = await import(pathToFileURL(actionsPath).href);
          m.store.set({ action: A.begin("cast", { id: "kc" }, {}) });
          await m.onPermanent(mountain, { currentTarget: anchor });
          return {
            hidden: globalThis.__popover.hidden,
            options: options().length,
            pinned: m.store.get().action.pinned,
            gets: globalThis.__calls.filter((c) => c.path.includes("legal-actions")).length,
          };
        """,
        actionsPath=str(ACTIONS_JS),
        state=_click_state(battlefield=[BASIC_MOUNTAIN]),
        mountain=BASIC_MOUNTAIN,
    )
    assert result["hidden"] is True, "pinning a basic land must open no menu"
    assert result["options"] == 0
    assert result["pinned"] == [{"card_id": "b1", "color": None, "ability_index": None}]
    assert result["gets"] == 0


def test_toggling_an_attacker_into_the_red_zone_opens_no_confirm() -> None:
    result = _click(
        """
          await m.onPermanent(bear, { currentTarget: anchor });
          return {
            hidden: globalThis.__popover.hidden,
            options: options().length,
            draft: m.store.get().combatDraft,
            calls: globalThis.__calls,
          };
        """,
        state=_click_state(step="declare_attackers", battlefield=[ATTACKING_CREATURE]),
        bear=ATTACKING_CREATURE,
    )
    assert result["hidden"] is True
    assert result["options"] == 0
    assert result["draft"] == {"bears1": "bob"}
    assert result["calls"] == []


def test_toggling_a_blocker_into_the_draft_opens_no_confirm() -> None:
    result = _click(
        """
          await m.onPermanent(mine, { currentTarget: anchor });
          const afterFirst = {
            hidden: globalThis.__popover.hidden,
            toast: globalThis.__toast.textContent,
          };
          await m.onPermanent(theirs, { currentTarget: anchor });
          return {
            afterFirst,
            hidden: globalThis.__popover.hidden,
            options: options().length,
            draft: m.store.get().combatDraft,
            calls: globalThis.__calls,
          };
        """,
        state=_click_state(
            step="declare_blockers",
            active_player="bob",
            battlefield=[ATTACKING_CREATURE, OPPONENT_BEAR],
        ),
        mine=ATTACKING_CREATURE,
        theirs=OPPONENT_BEAR,
    )
    assert result["afterFirst"]["hidden"] is True
    assert "click the attacker" in result["afterFirst"]["toast"]
    assert result["hidden"] is True
    assert result["options"] == 0
    # `combat.js`'s draft is keyed blocker -> the attacker it blocks.
    assert result["draft"] == {"bears1": "ob1"}
    assert result["calls"] == []


# -- a play permission is an action click too, and gets the same confirm -----
#
# A dynamic play permission (cascade's hit, suspend's own third ability -
# `beginCastFromPermission` carries the rule number this repo verified for
# it) is a card click that ACTS: it begins a cast out of exile or a
# graveyard. It is wired to three surfaces, and two of them are the board
# itself - `#permitted-row` (`render/board.js`'s `patchPermissions`) and the
# zone ribbon under the battlefield - with the zones modal the third. So it
# is not a special case of the modal; it is the same "one click, one thing to
# do, and it happened" the confirm exists to stop, and it opens the same
# one-option picker every other action click opens.


EXILED_CASCADE_HIT = {
    "id": "x1",
    "name": "Tarmogoyf",
    "type_line": "Creature - Lhurgoyf",
    "zone": "exile",
    "controller": "alice",
    # Enough for `actions.js`'s `needsChoices` to say yes, so picking the
    # confirm stages the announcement in the ribbon and fires nothing - the
    # same shape the hand-cast confirm test uses.
    "cast": {"mana_cost": "{1}{G}", "targeting": {"targets_anything": True}},
}

CASCADE_PERMISSION = {
    "card": EXILED_CASCADE_HIT,
    "granted_by": "Bloodbraid Elf",
    "action": "cast",
    "pays_mana_cost": False,
    "from_zone": "exile",
    "usable_now": True,
    "why_not": None,
}

PERMISSION_UNAFFORDABLE = {
    "kind": "cast",
    "card_id": "x1",
    "ability_index": None,
    "affordable": "no",
    "unaffordable_reason": "cannot pay {1}{G}: no untapped sources",
}


def _permission_state() -> dict:
    return _click_state(
        exile=[EXILED_CASCADE_HIT],
        play_permissions={"alice": [CASCADE_PERMISSION]},
    )


def test_a_play_permission_opens_a_one_option_confirm_before_the_cast_begins() -> None:
    """The click used to reach `beginCastFromPermission` outright - a cascade
    hit was cast by the click that landed on it. It opens `Cast` first, and
    only picking that stages the announcement, with the permission's own
    `from_zone` carried onto the request."""
    result = _click(
        """
          const clicked = m.onPermission(permission, { currentTarget: anchor });
          const opened = {
            hidden: globalThis.__popover.hidden,
            labels: options().map((o) => o.label),
            action: m.store.get().action,
            posts: globalThis.__calls.filter((c) => c.method === "POST").length,
          };
          await clicked;
          options()[0].click();
          await settle();
          return { opened, calls: globalThis.__calls };
        """,
        state=_permission_state(),
        permission=CASCADE_PERMISSION,
    )
    assert result["opened"]["hidden"] is False, "the click must open the confirm"
    assert result["opened"]["labels"] == ["Cast"]
    assert result["opened"]["action"] is None, "nothing may begin before the confirm is picked"
    assert result["opened"]["posts"] == 0
    casts = [
        c
        for c in result["calls"]
        if c["method"] == "POST" and "actions/cast" in c["path"] and not c["body"].get("dry_run")
    ]
    assert len(casts) == 1, result["calls"]
    assert casts[0]["body"]["card_id"] == "x1"
    # The permission's own zone, not the hand - `bodyFor` sends `from_zone`
    # only when the action carries one.
    assert casts[0]["body"]["from_zone"] == "exile"


def test_the_permission_confirm_is_enriched_from_the_engines_own_legal_actions() -> None:
    """The same enrichment every other action click gets: the read is keyed
    `card_id` + `cast` + no ability index, which is exactly the row
    `legal_actions` emits for a permission cast."""
    result = _click(
        """
          globalThis.__respond = (method, path) =>
            path.includes("legal-actions")
              ? Promise.resolve({ ok: true, json: async () => ({ actions: [row] }) })
              : null;
          await m.onPermission(permission, { currentTarget: anchor });
          await settle();
          return {
            gets: globalThis.__calls.map((c) => `${c.method} ${c.path}`),
            hidden: globalThis.__popover.hidden,
            disabled: options().map((o) => o.disabled),
            text: options().map((o) => o.deepText),
          };
        """,
        state=_permission_state(),
        permission=CASCADE_PERMISSION,
        row=PERMISSION_UNAFFORDABLE,
    )
    assert result["gets"] == ["GET /api/games/g1/legal-actions?player=alice&uid=u1"]
    assert result["hidden"] is False
    assert result["disabled"] == [True]
    assert result["text"] == ["Cast cannot pay {1}{G}: no untapped sources"]


def _modal_dom() -> str:
    """An open zones modal holding the clicked card, and the board's own node
    for the same card - `#permitted-row` carries one for every permission,
    keyed by card id (`dom.js`'s `patchChildren`)."""
    return """
      const modal = new El("div");
      modal.hidden = false;
      const inModal = new El("div");
      modal.appendChild(inModal);
      const onBoard = new El("div");
      onBoard.dataset.key = "x1";
      globalThis.__nodes["#modal"] = modal;
      globalThis.__nodes['[data-key="x1"]'] = onBoard;
      globalThis.__modal = modal;
      globalThis.__inModal = inModal;
      globalThis.__onBoard = onBoard;
    """


def test_a_permission_clicked_in_the_zones_modal_closes_it_and_anchors_on_the_board() -> None:
    """`.modal` is drawn above `.popover` (z-index 60 over 50), and this
    change may not touch either - so a confirm opened over one of the modal's
    own cards would sit *under* the scrim, dimmed and unclickable. The modal
    is closed first and the confirm anchored over the board's own node for
    that card, which leaves it on top, over the card it is about, and with
    Escape closing it rather than the modal that is no longer open."""
    result = _click(
        _modal_dom()
        + """
          await m.onPermission(permission, { currentTarget: inModal });
          return {
            modalHidden: globalThis.__modal.hidden,
            hidden: globalThis.__popover.hidden,
            labels: options().map((o) => o.label),
            anchoredOnBoard: globalThis.__onBoard.classes.has("menu-open"),
            anchoredInModal: globalThis.__inModal.classes.has("menu-open"),
          };
        """,
        state=_permission_state(),
        permission=CASCADE_PERMISSION,
    )
    assert result["modalHidden"] is True, "the modal must not cover the confirm it opened"
    assert result["hidden"] is False
    assert result["labels"] == ["Cast"]
    assert result["anchoredOnBoard"] is True
    assert result["anchoredInModal"] is False


def test_a_permission_clicked_on_the_board_leaves_a_closed_modal_alone() -> None:
    """The board is the ordinary surface: nothing is closed, and the confirm
    anchors over the node that was actually clicked rather than going looking
    for another one with the same id."""
    result = _click(
        """
          const modal = new El("div");
          modal.hidden = true;
          globalThis.__nodes["#modal"] = modal;
          await m.onPermission(permission, { currentTarget: anchor });
          return {
            modalHidden: modal.hidden,
            hidden: globalThis.__popover.hidden,
            anchored: anchor.classes.has("menu-open"),
          };
        """,
        state=_permission_state(),
        permission=CASCADE_PERMISSION,
    )
    assert result["modalHidden"] is True
    assert result["hidden"] is False
    assert result["anchored"] is True


def test_a_permanent_menu_opened_from_the_zones_modal_closes_it_too() -> None:
    """Not a permission special case: any action menu opened from inside the
    modal has the same problem, so the rule lives in the one place every
    action click goes through."""
    result = _click(
        """
          const modal = new El("div");
          modal.hidden = false;
          const inModal = new El("div");
          modal.appendChild(inModal);
          const onBoard = new El("div");
          onBoard.dataset.key = "pw1";
          globalThis.__nodes["#modal"] = modal;
          globalThis.__nodes['[data-key="pw1"]'] = onBoard;
          await m.onPermanent(pw, { currentTarget: inModal });
          return {
            modalHidden: modal.hidden,
            labels: options().map((o) => o.label),
            anchoredOnBoard: onBoard.classes.has("menu-open"),
          };
        """,
        state=_click_state(battlefield=[ONE_ACTIVATED_ABILITY_PW]),
        pw=ONE_ACTIVATED_ABILITY_PW,
    )
    assert result["modalHidden"] is True
    assert result["labels"] == ["+1: draw a card"]
    assert result["anchoredOnBoard"] is True


# ------------------------------------------------- approving the advisor ---
#
# `ui/render/advice.js` renders the mtg-buddy advisor's proposal and hands
# `{seq, method, path, body}` back on Approve; `approveAdvice` is what
# `app.js` does with it, and the ORDERING is the contract. The approve-record
# goes first so the post-match retrospective can pair an approval with the
# proposal it approved - a play performed without that row recorded is a play
# nothing can attribute to the advisor afterwards, which is the whole reason
# the advisor is being trained against these games.


def _advice_body() -> str:
    """A fake DOM and a `fetch` stub that answers the approve-record however
    the test asks: `__approve = "ok" | "refused" | "error"`. Every call is
    recorded in order, method and path together, so the ordering between the
    record and the action itself is readable rather than inferred."""
    return """
      globalThis.__calls = [];
      globalThis.__toast = { textContent: "", hidden: true };
      globalThis.__classes = new Set();
      globalThis.document = {
        body: {
          classList: {
            toggle: (name, on) => {
              if (on) globalThis.__classes.add(name);
              else globalThis.__classes.delete(name);
            },
          },
          setAttribute: () => {},
          removeAttribute: () => {},
        },
        querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
      };
      globalThis.__approve = "ok";
      const board = () => ({
        you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
        active_player: "alice", priority_holder: "alice", battlefield: [],
        hands: { alice: [] }, stack: [], combat: { attacks: [] },
        winner: null, drawn: false, pregame: null,
      });
      globalThis.fetch = (path, opts) => {
        const method = opts ? opts.method : "GET";
        globalThis.__calls.push({
          method, path, body: opts && opts.body ? JSON.parse(opts.body) : null,
        });
        if (path.includes("/advice/")) {
          if (globalThis.__approve === "error") {
            return Promise.resolve({
              ok: false, status: 409, json: async () => ({ detail: "that proposal is gone" }),
            });
          }
          if (globalThis.__approve === "refused") {
            return Promise.resolve({
              ok: true,
              json: async () => ({
                allowed: false,
                reason: "that advice was not addressed to you",
              }),
            });
          }
          return Promise.resolve({ ok: true, json: async () => ({ allowed: true }) });
        }
        if (method === "POST") {
          return Promise.resolve({ ok: true, json: async () => ({ allowed: true }) });
        }
        if (path.includes("/log")) {
          return Promise.resolve({ ok: true, json: async () => ({ log: [], last_seq: 11 }) });
        }
        return Promise.resolve({ ok: true, json: async () => board() });
      };
      const seated = () => m.store.set({
        gameId: "g1", uid: "u1", run: null, busy: 0,
        stops: { mine: [], theirs: [], onStack: true },
        state: board(),
        log: { entries: [], lastSeq: 10 },
      });
      // What the panel hands over: the ledger row's own `{method, path,
      // body}`, path included, carrying the ADVISOR's uid as recorded.
      const PROPOSAL = {
        seq: 11,
        method: "POST",
        path: "/api/games/g1/actions/cast?uid=advisor-uid",
        body: { player: "alice", card_id: "c1" },
      };
    """


def test_approving_records_the_approval_before_it_performs_the_play() -> None:
    """The ordering, pinned. The routes themselves are a sibling task's work,
    so the fetch is stubbed - but the sequence `app.js` sends them in is
    decided here and is what has to hold."""
    result = run_app(
        _advice_body()
        + """
        (async () => {
          seated();
          await m.approveAdvice(PROPOSAL);
          return globalThis.__calls.filter((c) => c.method === "POST").map((c) => c.path);
        })()
        """
    )
    assert len(result) == 2, f"expected the record then the play, got {result}"
    assert "/advice/11/approve" in result[0], "the approval was not recorded first"
    assert "/actions/cast" in result[1]


def test_an_approval_the_server_would_not_record_performs_nothing_at_all() -> None:
    """Both failure shapes: a non-2xx (`send()` turns it into a toast and a
    `null`) and a 2xx refusal (`allowed: false`). Either way the play must
    NOT be sent - an unrecorded approval is an advised play that looks, in
    the ledger the retrospective reads, exactly like one the human thought of
    themselves."""
    result = run_app(
        _advice_body()
        + """
        (async () => {
          seated();
          globalThis.__approve = "error";
          await m.approveAdvice(PROPOSAL);
          const posts = () =>
            globalThis.__calls.filter((c) => c.method === "POST").map((c) => c.path);
          const afterError = posts();
          globalThis.__calls = [];
          globalThis.__approve = "refused";
          await m.approveAdvice(PROPOSAL);
          const afterRefusal = posts();
          return { afterError, afterRefusal, toast: globalThis.__toast.textContent };
        })()
        """
    )
    assert len(result["afterError"]) == 1
    assert "/advice/11/approve" in result["afterError"][0]
    assert len(result["afterRefusal"]) == 1
    assert "/advice/11/approve" in result["afterRefusal"][0]
    assert result["toast"], "the failure was silent - the player clicked Approve and saw nothing"


def test_the_approved_play_is_sent_under_the_humans_uid_never_the_advisors() -> None:
    """The advisor seat is read-only server-side, and an approved play is a
    play the HUMAN made. The proposal's path arrives carrying the advisor's
    own credential; it comes off, and `send()`'s one idempotent `uidOf` puts
    this seat's uid on instead."""
    result = run_app(
        _advice_body()
        + """
        (async () => {
          seated();
          await m.approveAdvice(PROPOSAL);
          const play = globalThis.__calls.filter((c) => c.path.includes("/actions/cast"))[0];
          return { path: play.path, body: play.body };
        })()
        """
    )
    assert "uid=u1" in result["path"]
    assert "advisor-uid" not in result["path"], "the play went out as the advisor"
    assert result["body"] == {"player": "alice", "card_id": "c1"}, (
        "the proposed body was not sent verbatim"
    )


def test_approving_refreshes_the_board_so_the_play_is_on_screen_at_once() -> None:
    """Every other action path in `app.js` ends in `refresh()` rather than
    waiting up to a full 1.2s tick, and an approved play is not special: the
    whole point of the button is that the human does not have to click the
    board, so the board has to show what happened."""
    result = run_app(
        _advice_body()
        + """
        (async () => {
          seated();
          await m.approveAdvice(PROPOSAL);
          return globalThis.__calls.filter((c) => c.method === "GET").length;
        })()
        """
    )
    assert result >= 1, "nothing re-read the board after the play was performed"


# -- the engine-gap notice: a human otherwise has no way to know -------------


def test_an_unread_spell_notice_from_the_log_toasts_the_engine_gap() -> None:
    """Stock Up resolved as a silent no-op in a live game: the engine
    recorded an `UnmodeledTriggerEffectNotice` (`Game.
    unmodeled_trigger_effect_notices`), the API threaded it onto `GET
    .../log`'s own `detail.notices` (`commands._new_notices`,
    `visibility.redact_log_entry`), and nothing on screen ever said so - the
    game log panel is gone (`ui/render/panels.js`'s own comment), `app.log`
    was read only by `advice_model.js`'s staleness check, and no `ui/*.js`
    module read a `notices` key at all (grep across the tree comes back
    empty). The caster's own command response is not a fix either - the
    resolving pass is routinely the OPPONENT's, not hers (see the API test
    `test_an_unread_spells_notice_reaches_the_casters_own_log_after_the_
    opponents_pass`) - so `poll()`'s own `/log` read, the one thing both
    players' clients already poll every tick, is the path this pins."""
    result = run_app(
        """
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          body: {
            classList: { toggle: () => {} },
            setAttribute: () => {},
            removeAttribute: () => {},
          },
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        const board = {
          you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
          active_player: "bob", priority_holder: "alice", battlefield: [],
          hands: { alice: [] }, stack: [], combat: { attacks: [] },
          winner: null, drawn: false, pregame: null,
        };
        globalThis.fetch = (path) => {
          if (path.includes("/log")) {
            return Promise.resolve({
              ok: true,
              json: async () => ({
                log: [
                  {
                    seq: 1,
                    action: "pass_priority",
                    detail: {
                      notices: [
                        {
                          kind: "unmodeled_trigger_effect_notices",
                          source_id: "s1",
                          source_name: "Ponder",
                          text: "Look at the top three cards of your library, then put "
                            + "them back in any order. You may shuffle your library. Draw a card.",
                          zone: "graveyard",
                          controller: "alice",
                        },
                      ],
                    },
                  },
                ],
                last_seq: 1,
              }),
            });
          }
          return Promise.resolve({ ok: true, json: async () => board });
        };
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", run: null, busy: 0,
            stops: { mine: [], theirs: [], onStack: true },
            state: board, log: { entries: [], lastSeq: 0 },
          });
          await m.poll();
          return { toast: globalThis.__toast.textContent, hidden: globalThis.__toast.hidden };
        })()
        """
    )
    assert result["hidden"] is False, "the notice never reached the player at all"
    assert "Ponder" in result["toast"]
    assert "engine" in result["toast"].lower()


def test_a_notice_free_log_entry_toasts_nothing() -> None:
    """An ordinary log entry - no `detail.notices` at all, the overwhelming
    common case every tick - must not spam a toast on every poll."""
    result = run_app(
        """
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          body: {
            classList: { toggle: () => {} },
            setAttribute: () => {},
            removeAttribute: () => {},
          },
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        const board = {
          you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
          active_player: "bob", priority_holder: "alice", battlefield: [],
          hands: { alice: [] }, stack: [], combat: { attacks: [] },
          winner: null, drawn: false, pregame: null,
        };
        globalThis.fetch = (path) => {
          if (path.includes("/log")) {
            return Promise.resolve({
              ok: true,
              json: async () => ({
                log: [{ seq: 1, action: "pass_priority", detail: {} }],
                last_seq: 1,
              }),
            });
          }
          return Promise.resolve({ ok: true, json: async () => board });
        };
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", run: null, busy: 0,
            stops: { mine: [], theirs: [], onStack: true },
            state: board, log: { entries: [], lastSeq: 0 },
          });
          await m.poll();
          return globalThis.__toast.hidden;
        })()
        """
    )
    assert result is True, "an ordinary log entry must never toast"


# -- every other engine-gap notice kind toasts its own message too -----------


@pytest.mark.parametrize(
    "kind,expected_phrase",
    [
        ("layer_remainder_notices", "resolving text"),
        ("search_description_notices", "search"),
        ("unreadable_copy_exception_notices", "copy"),
        ("unreadable_counter_amount_notices", "counter"),
        ("keyword_line_notices", "keyword"),
    ],
)
def test_each_wired_notice_kind_toasts_its_own_engine_gap_message(
    kind: str, expected_phrase: str
) -> None:
    """`layer_remainder_notices` (wired into `commands._NOTICE_LIST_NAMES` in
    the same pass that added `CHOOSE_PILE`/`CHOOSE_PLAYERS`, but its own
    toast forgotten) and the four notice lists the Reality Fracture bump
    exposed as unwired entirely (`search_description_notices`,
    `unreadable_copy_exception_notices`, `unreadable_counter_amount_notices`,
    `keyword_line_notices`) each get their own line in
    `ENGINE_GAP_NOTICE_MESSAGES` now, the same reason
    `test_an_unread_spell_notice_from_the_log_toasts_the_engine_gap` pins
    `unmodeled_trigger_effect_notices`'s own message: a fail-closed refusal
    with no toast is one a human playing through the browser never learns
    about at all."""
    result = run_app(
        """
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          body: {
            classList: { toggle: () => {} },
            setAttribute: () => {},
            removeAttribute: () => {},
          },
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        const board = {
          you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
          active_player: "bob", priority_holder: "alice", battlefield: [],
          hands: { alice: [] }, stack: [], combat: { attacks: [] },
          winner: null, drawn: false, pregame: null,
        };
        globalThis.fetch = (path) => {
          if (path.includes("/log")) {
            return Promise.resolve({
              ok: true,
              json: async () => ({
                log: [
                  {
                    seq: 1,
                    action: "pass_priority",
                    detail: {
                      notices: [
                        {
                          kind: KIND,
                          source_id: "s1",
                          source_name: "Echoing Deeps",
                          text: "some unread clause",
                          zone: "graveyard",
                          controller: "alice",
                        },
                      ],
                    },
                  },
                ],
                last_seq: 1,
              }),
            });
          }
          return Promise.resolve({ ok: true, json: async () => board });
        };
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", run: null, busy: 0,
            stops: { mine: [], theirs: [], onStack: true },
            state: board, log: { entries: [], lastSeq: 0 },
          });
          await m.poll();
          return { toast: globalThis.__toast.textContent, hidden: globalThis.__toast.hidden };
        })()
        """,
        KIND=kind,
    )
    assert result["hidden"] is False, f"{kind} never reached the player at all"
    assert "Echoing Deeps" in result["toast"]
    assert expected_phrase in result["toast"].lower(), result["toast"]
    assert "engine gap" in result["toast"].lower()


# -- the drift guard: nothing the server can emit is dropped on the floor -----


def _server_notice_kinds() -> list[str]:
    """Every `kind` string `commands._new_notices` can stamp on a payload -
    the engine's own 23 `Game`-level notice lists plus
    `continuous_effects.layer_notices`, read from the server module itself
    rather than restated here, so this guard follows the engine instead of a
    copy of it."""
    from magic_cards_model_api import commands

    return [*commands._NOTICE_LIST_NAMES, commands._LAYER_NOTICE_KIND]


def test_every_notice_kind_the_server_can_emit_renders_a_human_line() -> None:
    """The cross-surface drift guard, the shape `tests/test_notices.py`'s
    `test_every_game_notice_list_is_wired` has on the server side and
    `tests/test_ui_decision_coverage.py` has for decisions: every notice kind
    `commands._new_notices` can put on the wire must come back out of
    `ui/app.js`'s `noticeMessage` as a line a human can read.

    This is the regression test for the defect itself. The server published
    all 24 kinds; `ENGINE_GAP_NOTICE_MESSAGES` was a hand-written table of
    six, and the other eighteen - `mode_notices`, `cost_modifier_notices`,
    `conditional_clause_notices`, `targeting_notices`,
    `suspend_redirect_notices`, `imprint_notices`, `undefined_power_notices`,
    `opening_hand_notices`, `layer_notices` among them - were looked up,
    missed, and silently dropped. A loud, deliberate refusal nobody renders
    is a silent failure with extra steps.

    The fix is not a longer table: `noticeMessage` falls back to a line built
    from the kind's own name and whichever of the notice's text fields is
    populated, so a twenty-fifth list the engine adds tomorrow is rendered
    the day it ships rather than the day somebody remembers this file. This
    test would still fail the moment that fallback is removed or narrowed
    back to a lookup, which is the failure mode it exists to catch."""
    kinds = _server_notice_kinds()
    assert len(kinds) >= 24, kinds
    messages = run_app(
        "Object.fromEntries(KINDS.map((kind) => [kind, m.noticeMessage("
        '{ kind, source_id: "s1", source_name: "Echoing Deeps", card_name: "Echoing Deeps",'
        ' text: "some unread clause" })]))',
        KINDS=kinds,
    )
    for kind in kinds:
        line = messages.get(kind)
        assert isinstance(line, str) and line.strip(), (
            f"{kind} renders nothing - the server can emit it, so a human playing "
            f"through the browser is the one person who never learns the engine "
            f"refused. Fix: give ui/app.js's noticeMessage a line for it, or (better) "
            f"leave the generic fallback in place so the next kind needs no fix at all."
        )
        assert "Echoing Deeps" in line, (kind, line)


def test_every_notice_kind_names_its_own_gap_rather_than_one_shared_line() -> None:
    """A fallback that rendered the same sentence for all 24 would pass the
    guard above while telling a human nothing - so each kind's line has to be
    distinguishable from every other kind's. The generic half earns that by
    naming the kind ("cost modifier", "opening hand") in the line it builds;
    the six specific lines were already distinct."""
    kinds = _server_notice_kinds()
    messages = run_app(
        "Object.fromEntries(KINDS.map((kind) => [kind, m.noticeMessage("
        '{ kind, source_id: "s1", source_name: "Echoing Deeps", card_name: "Echoing Deeps",'
        ' text: "some unread clause" })]))',
        KINDS=kinds,
    )
    by_line: dict[str, list[str]] = {}
    for kind, line in messages.items():
        by_line.setdefault(line, []).append(kind)
    collisions = {line: names for line, names in by_line.items() if len(names) > 1}
    assert not collisions, f"these kinds render an identical, useless line: {collisions}"


def test_a_notice_kind_this_ui_has_never_seen_still_reaches_the_player() -> None:
    """The twenty-fifth list, tested before it exists: a `kind` that is in no
    table anywhere in this UI still toasts, through the real `poll()` path,
    naming both the card and the kind. This replaces
    `test_an_unwired_notice_kind_toasts_nothing`, which pinned the defect as
    if it were the design - an unrecognised notice used to be dropped, which
    is exactly how eighteen real ones were."""
    result = run_app(
        """
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          body: {
            classList: { toggle: () => {} },
            setAttribute: () => {},
            removeAttribute: () => {},
          },
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        const board = {
          you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
          active_player: "bob", priority_holder: "alice", battlefield: [],
          hands: { alice: [] }, stack: [], combat: { attacks: [] },
          winner: null, drawn: false, pregame: null,
        };
        globalThis.fetch = (path) => {
          if (path.includes("/log")) {
            return Promise.resolve({
              ok: true,
              json: async () => ({
                log: [
                  {
                    seq: 1,
                    action: "pass_priority",
                    detail: {
                      notices: [
                        {
                          kind: "future_widget_notices",
                          source_id: "s1",
                          source_name: "Echoing Deeps",
                          text: "a clause no parser reads yet",
                          zone: "graveyard",
                          controller: "alice",
                        },
                      ],
                    },
                  },
                ],
                last_seq: 1,
              }),
            });
          }
          return Promise.resolve({ ok: true, json: async () => board });
        };
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", run: null, busy: 0,
            stops: { mine: [], theirs: [], onStack: true },
            state: board, log: { entries: [], lastSeq: 0 },
          });
          await m.poll();
          return { toast: globalThis.__toast.textContent, hidden: globalThis.__toast.hidden };
        })()
        """
    )
    assert result["hidden"] is False, "a notice kind nothing names is still a refusal"
    assert "Echoing Deeps" in result["toast"]
    assert "future widget" in result["toast"].lower(), result["toast"]


def test_a_redacted_notice_toasts_without_naming_the_hidden_card() -> None:
    """`visibility.redact_notices` blanks a hidden-zone notice's card to
    `<hidden>` before it ever reaches this seat - the generic line has to
    stay readable on that shape rather than printing a bare `<hidden>:` or
    leaking the text field it just blanked."""
    line = run_app(
        'm.noticeMessage({ kind: "imprint_notices", source_id: null,'
        ' source_name: "<hidden>", text: "<hidden>", zone: "hand", controller: "bob" })'
    )
    assert isinstance(line, str) and line.strip()
    assert "imprint" in line.lower(), line


def test_a_targeting_notice_that_is_not_an_engine_gap_does_not_claim_to_be_one() -> None:
    """`targeting_notices` is the one list carrying two genuinely different
    meanings in its own `kind` field (`commands._new_notices` keeps it as
    `notice_kind`): `unparsed_targets` is an engine gap, `no_legal_targets`
    is CR 608.2d working exactly as written. Rendering the legal one as an
    engine gap would train a player to distrust a correct board."""
    gap = run_app(
        'm.noticeMessage({ kind: "targeting_notices", notice_kind: "unparsed_targets",'
        ' source_name: "White Orchid Phantom", text: "up to one target nonbasic land" })'
    )
    legal = run_app(
        'm.noticeMessage({ kind: "targeting_notices", notice_kind: "no_legal_targets",'
        ' source_name: "White Orchid Phantom", text: "up to one target nonbasic land" })'
    )
    assert "engine gap" in gap.lower(), gap
    assert "engine gap" not in legal.lower(), legal
    assert "White Orchid Phantom" in legal


def test_the_six_already_wired_kinds_keep_their_own_specific_wording() -> None:
    """Clause (d) of the brief, pinned: the generic fallback must never
    swallow a message that already said something more useful. Each of these
    six phrases appears only in that kind's own `ENGINE_GAP_NOTICE_MESSAGES`
    line, never in the line the fallback builds."""
    specific = {
        "unmodeled_trigger_effect_notices": "could not read this card's text",
        "layer_remainder_notices": "part of its resolving text",
        "search_description_notices": "a search's card description",
        "unreadable_copy_exception_notices": "entered as a plain copy",
        "unreadable_counter_amount_notices": "no counters were placed",
        "keyword_line_notices": "computed ability line",
    }
    messages = run_app(
        "Object.fromEntries(KINDS.map((kind) => [kind, m.noticeMessage("
        '{ kind, source_id: "s1", source_name: "Echoing Deeps", text: "some clause" })]))',
        KINDS=list(specific),
    )
    for kind, phrase in specific.items():
        assert phrase in messages[kind], (kind, messages[kind])


# -- a burst of notices: every one of them, not just the last ----------------


def test_two_notices_landing_in_one_poll_tick_both_reach_the_player() -> None:
    """The defect this is written against: `toastEngineGapNotices` called
    `toast()` once per notice in a synchronous loop, and `toast()` overwrote
    `#toast`.textContent and reset one shared timer - so a poll tick carrying
    more than one notice showed only the last of them, with the others
    discarded before any repaint. The log panel is gone
    (`ui/render/panels.js`), so there was nowhere else left to read them.

    One tick carrying two is not exotic: `/log` returns every entry since
    `lastSeq`, so an opponent's or an AI's whole turn arrives in one read,
    and a single resolution can file notices to two different lists at once
    (a cost modifier it could not apply and a mode it could not read). Going
    from six rendered kinds to twenty-four makes it commonplace.

    Both messages have to be on screen together - this is the assertion that
    "nothing is silently discarded" actually holds at the surface, not only
    in the wiring that computes the lines."""
    result = run_app(
        """
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          body: {
            classList: { toggle: () => {} },
            setAttribute: () => {},
            removeAttribute: () => {},
          },
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        const board = {
          you: "alice", pending_decision: null, step: "precombat_main", turn_number: 4,
          active_player: "bob", priority_holder: "alice", battlefield: [],
          hands: { alice: [] }, stack: [], combat: { attacks: [] },
          winner: null, drawn: false, pregame: null,
        };
        globalThis.fetch = (path) => {
          if (path.includes("/log")) {
            return Promise.resolve({
              ok: true,
              json: async () => ({
                log: [
                  {
                    seq: 1,
                    action: "pass_priority",
                    detail: {
                      notices: [
                        {
                          kind: "cost_modifier_notices",
                          source_id: "s1",
                          source_name: "Trinisphere",
                          text: "each spell that would cost less than three",
                          zone: "battlefield",
                          controller: "bob",
                        },
                        {
                          kind: "mode_notices",
                          source_id: "s2",
                          source_name: "Cryptic Command",
                          text: "choose two",
                          zone: "stack",
                          controller: "alice",
                        },
                      ],
                    },
                  },
                ],
                last_seq: 1,
              }),
            });
          }
          return Promise.resolve({ ok: true, json: async () => board });
        };
        (async () => {
          m.store.set({
            gameId: "g1", uid: "u1", run: null, busy: 0,
            stops: { mine: [], theirs: [], onStack: true },
            state: board, log: { entries: [], lastSeq: 0 },
          });
          await m.poll();
          return { toast: globalThis.__toast.textContent, hidden: globalThis.__toast.hidden };
        })()
        """
    )
    assert result["hidden"] is False
    assert "Trinisphere" in result["toast"], (
        "the first of two notices in one tick was overwritten by the second and "
        "is gone for good - the log panel that used to carry it does not exist"
    )
    assert "Cryptic Command" in result["toast"], result["toast"]


def test_a_burst_of_notices_keeps_every_line_on_screen() -> None:
    """The same guarantee at the size a real turn produces: five notices
    filed by one command all read back off `#toast` together, in the order
    the engine filed them. A presentation that showed one and queued the
    rest behind a shared 4000ms timer would pass the two-notice test above
    by luck of ordering and still lose four of these."""
    result = run_app(
        """
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        m.toastNotices([
          { kind: "mode_notices", source_name: "Card One", text: "one" },
          { kind: "imprint_notices", source_name: "Card Two", text: "two" },
          { kind: "layer_notices", source_name: "Card Three", text: "three" },
          { kind: "evasion_notices", source_name: "Card Four", text: "four" },
          { kind: "opening_hand_notices", card_name: "Card Five", text: "five" },
        ]);
        ({ toast: globalThis.__toast.textContent, hidden: globalThis.__toast.hidden })
        """
    )
    assert result["hidden"] is False
    for name in ("Card One", "Card Two", "Card Three", "Card Four", "Card Five"):
        assert name in result["toast"], (name, result["toast"])
    order = [result["toast"].index(name) for name in ("Card One", "Card Three", "Card Five")]
    assert order == sorted(order), f"notices are shown out of the order they were filed: {result}"


def test_an_ordinary_refusal_still_shows_the_moment_it_is_toasted() -> None:
    """The other half of the same surface: a refusal a player's own click
    just earned must not wait behind anything. Whatever holds several
    notices together has to put a newly toasted line on screen immediately,
    not schedule it."""
    result = run_app(
        """
        globalThis.__toast = { textContent: "", hidden: true };
        globalThis.document = {
          querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null),
        };
        m.toastNotices([
          { kind: "mode_notices", source_name: "Card One", text: "one" },
          { kind: "imprint_notices", source_name: "Card Two", text: "two" },
        ]);
        m.toast("you cannot cast that right now");
        globalThis.__toast.textContent
        """
    )
    assert "you cannot cast that right now" in result, result
    assert "Card One" in result, "a refusal must not wipe the notices already on screen"


# -- targeting notices: four outcomes, four lines ---------------------------


def _targeting_notice_kinds() -> list[str]:
    """Every `TargetingNoticeKind` the engine defines, read live off the
    engine module rather than restated here - the same posture
    `_server_notice_kinds` takes for the notice lists themselves."""
    from magic_cards_model.machines.triggers import TargetingNoticeKind

    return [kind.value for kind in TargetingNoticeKind]


#: What each targeting sub-kind actually did to the board, and whether it is
#: an engine gap. Hand-written on purpose: it is the assertion, and a kind
#: the engine adds without an entry here fails
#: `test_every_targeting_notice_kind_is_rendered_as_what_it_actually_is` by
#: name rather than silently inheriting somebody else's sentence.
_TARGETING_OUTCOMES = {
    "unparsed_targets": {
        "gap": True,
        "says": "went on the stack",
        "never_says": "never went on the stack",
    },
    "no_legal_targets": {
        "gap": False,
        "says": "went on the stack",
        "never_says": "never went on the stack",
    },
    "mandatory_target_missing": {
        "gap": False,
        "says": "never went on the stack",
        "never_says": "graveyard",
    },
    "unreadable_aura_target": {
        "gap": True,
        "says": "graveyard",
        "never_says": "went on the stack",
    },
}


def test_every_targeting_notice_kind_is_rendered_as_what_it_actually_is() -> None:
    """`targeting_notices` is one list carrying four genuinely different
    outcomes in its own `kind` field (`commands._notice_payload` keeps it as
    `notice_kind`), and the browser is where a human learns which one
    happened:

    - `unparsed_targets` - the restriction could not be read. Engine gap; the
      ability is on the stack having chosen nothing.
    - `no_legal_targets` - nothing was legal and the target was optional
      (CR 115.6). The rules working as printed; the ability is on the stack
      having chosen nothing.
    - `mandatory_target_missing` - nothing was legal and the target was not
      optional. Also the rules working as printed (CR 603.3d), but the
      ability is *gone*, never placed on the stack at all.
    - `unreadable_aura_target` - a resolving Aura whose attachment could not
      be re-resolved. Engine gap, and the Aura is in a graveyard rather than
      anywhere near the stack.

    The defect this pins: one line was rendered for all of them, claiming
    "its ability went on the stack with no target. That is the rule, not a
    bug." That is true of exactly one of the four. For the other three it
    states the opposite of what happened, and for two of them it actively
    suppresses an engine-gap report - the precise failure this whole change
    exists to remove."""
    kinds = _targeting_notice_kinds()
    assert set(kinds) == set(_TARGETING_OUTCOMES), (
        "the engine's TargetingNoticeKind and this file's expectations have "
        f"drifted apart: engine={sorted(kinds)} expected={sorted(_TARGETING_OUTCOMES)}. "
        "A new sub-kind is a new outcome for a player to be told about - give it "
        "its own line in ui/app.js's TARGETING_NOTICE_MESSAGES and its own row here."
    )
    lines = run_app(
        "Object.fromEntries(KINDS.map((notice_kind) => [notice_kind, m.noticeMessage("
        '{ kind: "targeting_notices", notice_kind, source_name: "Hyena Umbra",'
        ' text: "enchant creature" })]))',
        KINDS=kinds,
    )
    for kind, expected in _TARGETING_OUTCOMES.items():
        line = lines[kind]
        assert "Hyena Umbra" in line, (kind, line)
        assert expected["says"] in line, (kind, line)
        assert expected["never_says"] not in line, (kind, line)
        if expected["gap"]:
            assert "engine gap" in line.lower(), (kind, line)
        else:
            assert "engine gap" not in line.lower(), (kind, line)
            assert "not a bug" in line.lower(), (kind, line)
    assert len(set(lines.values())) == len(lines), f"two outcomes render the same line: {lines}"


def test_a_targeting_sub_kind_this_ui_has_never_seen_fails_loud() -> None:
    """The fifth sub-kind, tested before it exists. An unrecognised
    `notice_kind` must fall back to the engine-gap wording rather than to any
    of the four lines above: over-reporting a gap costs a false bug report,
    while a wrong "that is the rule, not a bug" teaches a player to trust a
    board the engine itself refused to stand behind."""
    line = run_app(
        'm.noticeMessage({ kind: "targeting_notices", notice_kind: "some_future_kind",'
        ' source_name: "Hyena Umbra", text: "enchant creature" })'
    )
    assert "Hyena Umbra" in line, line
    assert "engine gap" in line.lower(), line
    assert "not a bug" not in line.lower(), line
