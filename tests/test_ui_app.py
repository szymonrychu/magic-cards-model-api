"""`ui/app.js` under Node: the click planners, run against the real shipped
module rather than a Python restatement of it.

`app.js` ties every pure module (`actions.js`, `priority.js`, `combat.js`,
`decisions.js`, `targeting.js`) and every render module (`board.js`,
`ribbon.js`, `log.js`, `lobby.js`, `panels.js`) together into the running
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
    assert posts[0]["path"] == "/api/games/g1/actions/pass-priority"
    assert result["run"] is not None, "a paused pass must not end the run"
    assert result["run"]["passes"] == 0, "a pass that never completed must not be recorded as one"


# -- fire() / keep priority after casting -------------------------------------
#
# The owner's newest default: casting a spell auto-passes priority to the
# opponent unless the stops panel's "keep priority after casting" tick
# overrides it (`stops.keepPriorityAfterCasting`) - the persistent twin of
# the per-cast "hold priority" checkbox that already existed (`action.
# holdPriority`, reset for every new cast). `fire` is exported test-only,
# alongside `store`/`tryTarget`/`declareNoAttackers` above, and driven here
# the same way: a real, awaited call against the same stubbed `fetch`.


def test_casting_auto_passes_priority_by_default() -> None:
    result = run_app(
        _fetch_stub_body()
        + """
        (async () => {
          const A = await import(pathToFileURL(actionsPath).href);
          m.store.set({
            gameId: "g1", uid: "u1",
            state: { you: "alice", battlefield: [] },
            stops: { mine: [], theirs: [], onStack: true, keepPriorityAfterCasting: false },
            action: A.begin("cast", { id: "c1" }, {}),
          });
          await m.fire();
          return { calls: globalThis.__calls };
        })()
        """,
        actionsPath=str(ACTIONS_JS),
    )
    posts = [c for c in result["calls"] if c["method"] == "POST"]
    paths = [c["path"] for c in posts]
    assert any("actions/cast" in p for p in paths), paths
    assert any("actions/pass-priority" in p for p in paths), (
        "casting must auto-pass priority by default - " + str(paths)
    )


def test_the_keep_priority_after_casting_tick_suppresses_the_auto_pass() -> None:
    result = run_app(
        _fetch_stub_body()
        + """
        (async () => {
          const A = await import(pathToFileURL(actionsPath).href);
          m.store.set({
            gameId: "g1", uid: "u1",
            state: { you: "alice", battlefield: [] },
            stops: { mine: [], theirs: [], onStack: true, keepPriorityAfterCasting: true },
            action: A.begin("cast", { id: "c1" }, {}),
          });
          await m.fire();
          return { calls: globalThis.__calls };
        })()
        """,
        actionsPath=str(ACTIONS_JS),
    )
    posts = [c for c in result["calls"] if c["method"] == "POST"]
    paths = [c["path"] for c in posts]
    assert any("actions/cast" in p for p in paths), paths
    assert not any("actions/pass-priority" in p for p in paths), (
        "the tick must suppress the auto-pass - " + str(paths)
    )


def test_the_per_cast_hold_priority_checkbox_still_overrides_the_tick_off() -> None:
    """The persistent tick sets the default; the existing per-cast checkbox
    (`action.holdPriority`) is still a live override for one specific cast,
    exactly as it already was before this tick existed - either one holding
    is enough to suppress the auto-pass."""
    result = run_app(
        _fetch_stub_body()
        + """
        (async () => {
          const A = await import(pathToFileURL(actionsPath).href);
          let action = A.begin("cast", { id: "c1" }, {});
          action = { ...action, holdPriority: true };
          m.store.set({
            gameId: "g1", uid: "u1",
            state: { you: "alice", battlefield: [] },
            stops: { mine: [], theirs: [], onStack: true, keepPriorityAfterCasting: false },
            action,
          });
          await m.fire();
          return { calls: globalThis.__calls };
        })()
        """,
        actionsPath=str(ACTIONS_JS),
    )
    posts = [c for c in result["calls"] if c["method"] == "POST"]
    paths = [c["path"] for c in posts]
    assert not any("actions/pass-priority" in p for p in paths), paths


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


def test_a_land_with_nothing_else_plays_on_one_click() -> None:
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
