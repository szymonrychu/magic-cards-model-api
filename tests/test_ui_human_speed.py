"""Human-side speed, quality-neutral: every auto-action here is an opt-out
tick in the stops panel (absent reads as ticked), and every one it takes is
recorded - a toast, and `reasoning` on the request it sends.

Pure logic lives in `ui/priority.js`; the wiring (tick, fire, preferences,
keyboard) in `ui/app.js`. Both are driven as the real modules under Node."""

from __future__ import annotations

from test_ui_app import ACTIONS_JS, APP_JS, PLANESWALKER_CARD, run_app
from ui_node import requires_node, run_module

pytestmark = requires_node


# ---------------------------------------------------------------- stops keys


def test_speed_stops_default_on_and_yield_list_empty() -> None:
    d = run_module("priority.js", "m.DEFAULT_STOPS")
    assert d["autoForced"] is True
    assert d["autoPassNothing"] is True
    assert d["tappedToast"] is True
    assert d["yieldTo"] == []


def test_speed_stops_absent_reads_as_ticked() -> None:
    result = run_module(
        "priority.js",
        "m.loadStops({ getItem: (k) => k === 'mcm.stops.v2' ? raw : null })",
        raw='{"mine": ["upkeep"], "theirs": [], "onStack": true}',
    )
    assert result["autoForced"] is True
    assert result["autoPassNothing"] is True
    assert result["tappedToast"] is True
    assert result["yieldTo"] == []


def test_speed_stops_unticked_and_yield_list_read_back() -> None:
    result = run_module(
        "priority.js",
        "m.loadStops({ getItem: (k) => k === 'mcm.stops.v2' ? raw : null })",
        raw=(
            '{"mine": [], "theirs": [], "autoForced": false, "autoPassNothing": false,'
            ' "tappedToast": false, "yieldTo": [{"controller": "bob", "name": "X", "text": "t"}]}'
        ),
    )
    assert result["autoForced"] is False
    assert result["autoPassNothing"] is False
    assert result["tappedToast"] is False
    assert result["yieldTo"] == [{"controller": "bob", "name": "X", "text": "t"}]


def test_a_malformed_yield_list_reads_as_empty() -> None:
    result = run_module(
        "priority.js",
        "m.normalizeStops(JSON.parse(raw), true)",
        raw='{"mine": [], "theirs": [], "yieldTo": "nope"}',
    )
    assert result["yieldTo"] == []


def test_remote_stops_merge_over_local_with_absent_as_default() -> None:
    result = run_module(
        "priority.js",
        "m.mergeStops(local, remote)",
        local={"mine": ["upkeep"], "theirs": [], "onStack": False, "autoPassNothing": False},
        remote={"mine": ["draw"], "tappedToast": False},
    )
    assert result["mine"] == ["draw"]
    assert result["theirs"] == []
    assert result["tappedToast"] is False
    # absent in the remote payload reads as the default, not as local's value
    assert result["autoPassNothing"] is True
    assert result["onStack"] is True


# ---------------------------------------------------------- onlyPassLegal


def test_only_pass_legal_ignores_pass_concede_mana_and_unaffordable() -> None:
    rows = [
        {"kind": "pass_priority"},
        {"kind": "concede"},
        {"kind": "activate_mana"},
        {"kind": "cast", "affordable": "no"},
    ]
    assert run_module("priority.js", "m.onlyPassLegal(rows)", rows=rows) is True


def test_only_pass_legal_counts_unknown_affordability_as_a_move() -> None:
    rows = [{"kind": "pass_priority"}, {"kind": "cast", "affordable": "unknown"}]
    assert run_module("priority.js", "m.onlyPassLegal(rows)", rows=rows) is False


def test_only_pass_legal_counts_any_other_row_as_a_move() -> None:
    for kind in ("play_land", "special_action", "activate", "cast"):
        rows = [{"kind": "pass_priority"}, {"kind": kind}]
        assert run_module("priority.js", "m.onlyPassLegal(rows)", rows=rows) is False, kind


def test_only_pass_legal_is_false_for_a_malformed_row() -> None:
    assert run_module("priority.js", "m.onlyPassLegal(rows)", rows=["x"]) is False


# ---------------------------------------------------------------- yielding

BOB_TRIGGER = {"id": "s1", "controller": "bob", "name": "Ocelot Pride", "text": "At the beginning"}


def test_yield_toggle_adds_then_removes() -> None:
    result = run_module(
        "priority.js",
        "(() => { const a = m.toggleYield({ yieldTo: [] }, o);"
        " const b = m.toggleYield(a, o);"
        " return [a.yieldTo, b.yieldTo, m.isYielded(o, a), m.isYielded(o, b)]; })()",
        o=BOB_TRIGGER,
    )
    assert result == [
        [{"controller": "bob", "name": "Ocelot Pride", "text": "At the beginning"}],
        [],
        True,
        False,
    ]


def test_yield_matches_controller_name_and_text() -> None:
    other = {**BOB_TRIGGER, "controller": "alice"}
    result = run_module(
        "priority.js",
        "m.isYielded(other, { yieldTo: [{ controller: 'bob', name: 'Ocelot Pride',"
        " text: 'At the beginning' }] })",
        other=other,
    )
    assert result is False


def _board(**over):
    state = {
        "you": "alice",
        "priority_holder": "alice",
        "active_player": "bob",
        "turn_number": 3,
        "step": "upkeep",
        "first_strike_damage_step": False,
        "stack": [],
        "pending_decision": None,
        "combat": {"attacks": []},
        "battlefield": [],
        "winner": None,
        "drawn": None,
    }
    state.update(over)
    return state


RUN = {
    "verb": "pass_until",
    "startedTurn": 3,
    "startedStep": "upkeep",
    "startedActive": "bob",
    "knownStackIds": [],
    "passes": 0,
    "stillAtStart": False,
}


def test_a_yielded_arrival_does_not_stop_pass_until() -> None:
    reason = run_module(
        "priority.js",
        "m.stopReason(state, run, stops)",
        state=_board(stack=[BOB_TRIGGER]),
        run=RUN,
        stops={
            "mine": [],
            "theirs": [],
            "onStack": True,
            "yieldTo": [{"controller": "bob", "name": "Ocelot Pride", "text": "At the beginning"}],
        },
    )
    assert reason is None


def test_an_unyielded_arrival_still_stops_pass_until() -> None:
    reason = run_module(
        "priority.js",
        "m.stopReason(state, run, stops)",
        state=_board(stack=[BOB_TRIGGER]),
        run=RUN,
        stops={"mine": [], "theirs": [], "onStack": True, "yieldTo": []},
    )
    assert reason == "something new is on the stack"


# ------------------------------------------------------ autoPassWindow


def test_auto_pass_window_open_on_their_turn_with_their_object() -> None:
    assert (
        run_module("priority.js", "m.autoPassWindow(state, [])", state=_board(stack=[BOB_TRIGGER]))
        is True
    )


def test_auto_pass_window_closed_without_priority_or_with_a_decision() -> None:
    assert (
        run_module(
            "priority.js", "m.autoPassWindow(state, [])", state=_board(priority_holder="bob")
        )
        is False
    )
    pending = {"player": "alice", "kind": "may", "generation": 1}
    assert (
        run_module(
            "priority.js", "m.autoPassWindow(state, [])", state=_board(pending_decision=pending)
        )
        is False
    )
    assert (
        run_module("priority.js", "m.autoPassWindow(state, [])", state=_board(winner="bob"))
        is False
    )


def test_auto_pass_window_closed_in_own_main_with_an_empty_stack() -> None:
    for step in ("precombat_main", "postcombat_main"):
        state = _board(active_player="alice", step=step)
        assert run_module("priority.js", "m.autoPassWindow(state, [])", state=state) is False, step
    # own main with something on the stack is an ordinary response window
    state = _board(active_player="alice", step="precombat_main", stack=[BOB_TRIGGER])
    assert run_module("priority.js", "m.autoPassWindow(state, [])", state=state) is True
    # own upkeep with an empty stack is not a main phase
    state = _board(active_player="alice", step="upkeep")
    assert run_module("priority.js", "m.autoPassWindow(state, [])", state=state) is True


def test_auto_pass_window_closed_over_this_seats_own_new_object() -> None:
    own = {"id": "s2", "controller": "alice", "name": "Bolt", "text": ""}
    state = _board(stack=[own, BOB_TRIGGER])
    assert run_module("priority.js", "m.autoPassWindow(state, ['s1'])", state=state) is False
    # already seen (it was there when this seat last held priority): not new
    assert run_module("priority.js", "m.autoPassWindow(state, ['s1', 's2'])", state=state) is True


# ------------------------------------------------------------- shortcuts


def test_shortcuts_map_f2_to_pass_and_f6_to_pass_until_when_offered() -> None:
    state = _board()
    assert run_module("priority.js", "m.shortcutVerb('F2', state)", state=state) == "pass"
    assert run_module("priority.js", "m.shortcutVerb('F6', state)", state=state) == "pass_until"
    assert run_module("priority.js", "m.shortcutVerb('F3', state)", state=state) is None


def test_shortcuts_need_the_verb_offered() -> None:
    state = _board(priority_holder="bob")
    assert run_module("priority.js", "m.shortcutVerb('F2', state)", state=state) is None
    pending = {"player": "alice", "kind": "may", "generation": 1}
    state = _board(pending_decision=pending)
    assert run_module("priority.js", "m.shortcutVerb('F2', state)", state=state) == "pass"
    assert run_module("priority.js", "m.shortcutVerb('F6', state)", state=state) is None


# ======================================================= app.js wiring

_STUB = """
globalThis.__calls = [];
globalThis.__toasts = [];
globalThis.__toast = {
  set textContent(v) { globalThis.__toasts.push(v); },
  get textContent() { return ""; },
  hidden: true,
};
globalThis.document = { querySelector: (sel) => (sel === "#toast" ? globalThis.__toast : null) };
globalThis.__routes = ROUTES;
globalThis.fetch = (path, opts) => {
  const method = opts ? opts.method : "GET";
  const body = opts && opts.body ? JSON.parse(opts.body) : undefined;
  globalThis.__calls.push({ method, path, body });
  for (const [needle, answer] of globalThis.__routes) {
    if (path.includes(needle) && (!answer.method || answer.method === method)) {
      return Promise.resolve({ ok: (answer.status || 200) < 400, status: answer.status || 200,
                               json: async () => answer.body });
    }
  }
  const answer = (payload) => Promise.resolve({ ok: true, status: 200, json: async () => payload });
  if (path.includes("/log")) return answer({ log: [], last_seq: 0 });
  if (method === "POST") return answer({ allowed: true });
  return answer(AFTER);
};
"""


def _drive(script: str, *, routes=None, after=None, **bindings):
    return run_app(
        _STUB + script,
        ROUTES=routes or [],
        AFTER=after or _board(priority_holder="bob"),
        **bindings,
    )


def _posts(result, needle):
    return [c for c in result["calls"] if c["method"] == "POST" and needle in c["path"]]


def _gets(result, needle):
    return [c for c in result["calls"] if c["method"] == "GET" and needle in c["path"]]


_TICK = """
(async () => {
  m.store.set({ gameId: "g1", uid: "u1", run: null, stops: STOPS, state: STATE });
  await m.tickRun();
  if (TWICE) await m.tickRun();
  return { calls: globalThis.__calls, toasts: globalThis.__toasts };
})()
"""

DEFAULT_STOPS = {"mine": [], "theirs": [], "onStack": True}


# -- forced decisions


def test_a_pending_decision_asks_decide_forced_and_toasts_the_auto_answer() -> None:
    pending = {"player": "alice", "kind": "choose_cards", "generation": 7}
    routes = [
        [
            "decide-forced",
            {
                "body": {
                    "accepted": True,
                    "auto_answered": {
                        "kind": "choose_cards",
                        "answer": {},
                        "why": "only one legal choice",
                    },
                }
            },
        ]
    ]
    result = _drive(
        _TICK,
        routes=routes,
        STOPS=DEFAULT_STOPS,
        STATE=_board(pending_decision=pending),
        TWICE=True,
    )
    forced = _posts(result, "decide-forced")
    assert len(forced) == 1, result["calls"]
    assert forced[0]["path"].startswith("/api/games/g1/decide-forced")
    assert forced[0]["body"] == {"player": "alice", "generation": 7}
    assert "auto: only one legal choice" in result["toasts"]


def test_a_not_forced_answer_toasts_nothing() -> None:
    pending = {"player": "alice", "kind": "may", "generation": 2}
    routes = [["decide-forced", {"body": {"forced": False}}]]
    result = _drive(
        _TICK,
        routes=routes,
        STOPS=DEFAULT_STOPS,
        STATE=_board(pending_decision=pending),
        TWICE=True,
    )
    assert len(_posts(result, "decide-forced")) == 1
    assert not [t for t in result["toasts"] if t.startswith("auto:")]


def test_auto_forced_unticked_never_asks() -> None:
    pending = {"player": "alice", "kind": "may", "generation": 2}
    stops = {**DEFAULT_STOPS, "autoForced": False}
    result = _drive(_TICK, STOPS=stops, STATE=_board(pending_decision=pending), TWICE=False)
    assert _posts(result, "decide-forced") == []


def test_forced_never_asks_for_someone_elses_decision_or_combat_declarations() -> None:
    bobs = {"player": "bob", "kind": "may", "generation": 2}
    result = _drive(_TICK, STOPS=DEFAULT_STOPS, STATE=_board(pending_decision=bobs), TWICE=False)
    assert _posts(result, "decide-forced") == []
    combat = {"player": "alice", "kind": "declare_blockers", "generation": 3}
    result = _drive(_TICK, STOPS=DEFAULT_STOPS, STATE=_board(pending_decision=combat), TWICE=False)
    assert _posts(result, "decide-forced") == []


# -- auto-pass


ONLY_PASS = {"body": {"actions": [{"kind": "pass_priority"}, {"kind": "concede"}], "last_seq": 41}}
CAN_RESPOND = {
    "body": {
        "actions": [{"kind": "pass_priority"}, {"kind": "cast", "affordable": "yes"}],
        "last_seq": 41,
    }
}


def test_only_pass_legal_auto_passes_with_reasoning_and_a_toast() -> None:
    result = _drive(
        _TICK,
        routes=[["legal-actions", ONLY_PASS]],
        STOPS=DEFAULT_STOPS,
        STATE=_board(stack=[BOB_TRIGGER]),
        TWICE=False,
    )
    passes = _posts(result, "pass-priority")
    assert len(passes) == 1, result["calls"]
    assert passes[0]["body"]["player"] == "alice"
    assert passes[0]["body"]["reasoning"] == "auto-passed by the UI: no legal action but pass"
    assert passes[0]["body"]["expected_seq"] == 41
    assert any(t.startswith("auto: passed") for t in result["toasts"]), result["toasts"]


def test_a_legal_response_never_auto_passes() -> None:
    result = _drive(
        _TICK,
        routes=[["legal-actions", CAN_RESPOND]],
        STOPS=DEFAULT_STOPS,
        STATE=_board(stack=[BOB_TRIGGER]),
        TWICE=False,
    )
    assert _posts(result, "pass-priority") == []


def test_a_failed_legal_actions_read_never_auto_passes() -> None:
    result = _drive(
        _TICK,
        routes=[["legal-actions", {"status": 500, "body": {"detail": "boom"}}]],
        STOPS=DEFAULT_STOPS,
        STATE=_board(stack=[BOB_TRIGGER]),
        TWICE=False,
    )
    assert _posts(result, "pass-priority") == []


def test_auto_pass_unticked_never_reads_or_passes() -> None:
    stops = {**DEFAULT_STOPS, "autoPassNothing": False}
    result = _drive(
        _TICK,
        routes=[["legal-actions", ONLY_PASS]],
        STOPS=stops,
        STATE=_board(stack=[BOB_TRIGGER]),
        TWICE=False,
    )
    assert _gets(result, "legal-actions") == []
    assert _posts(result, "pass-priority") == []


def test_own_main_phase_with_an_empty_stack_never_auto_passes() -> None:
    result = _drive(
        _TICK,
        routes=[["legal-actions", ONLY_PASS]],
        STOPS=DEFAULT_STOPS,
        STATE=_board(active_player="alice", step="precombat_main"),
        TWICE=False,
    )
    assert _gets(result, "legal-actions") == []
    assert _posts(result, "pass-priority") == []


def test_own_just_cast_object_never_auto_passes() -> None:
    own = {"id": "s9", "controller": "alice", "name": "Bolt", "text": "deal 3"}
    result = _drive(
        _TICK,
        routes=[["legal-actions", ONLY_PASS]],
        STOPS=DEFAULT_STOPS,
        STATE=_board(stack=[own]),
        TWICE=False,
    )
    assert _posts(result, "pass-priority") == []


def test_one_legal_actions_read_per_board() -> None:
    result = _drive(
        _TICK,
        routes=[["legal-actions", CAN_RESPOND]],
        STOPS=DEFAULT_STOPS,
        STATE=_board(stack=[BOB_TRIGGER]),
        TWICE=True,
    )
    assert len(_gets(result, "legal-actions")) == 1


def test_a_yielded_trigger_auto_passes_even_with_legal_responses() -> None:
    stops = {
        **DEFAULT_STOPS,
        "yieldTo": [{"controller": "bob", "name": "Ocelot Pride", "text": "At the beginning"}],
    }
    result = _drive(
        _TICK,
        routes=[["legal-actions", CAN_RESPOND]],
        STOPS=stops,
        STATE=_board(stack=[BOB_TRIGGER]),
        TWICE=False,
    )
    passes = _posts(result, "pass-priority")
    assert len(passes) == 1, result["calls"]
    assert "yield" in passes[0]["body"]["reasoning"]


def test_auto_pass_with_a_run_armed_records_the_pass_on_the_run() -> None:
    script = """
    (async () => {
      m.store.set({ gameId: "g1", uid: "u1", run: RUN, stops: STOPS, state: STATE });
      await m.tickRun();
      return { calls: globalThis.__calls, toasts: globalThis.__toasts, run: m.store.get().run };
    })()
    """
    result = _drive(
        script,
        routes=[["legal-actions", ONLY_PASS]],
        STOPS=DEFAULT_STOPS,
        STATE=_board(stack=[BOB_TRIGGER]),
        RUN=RUN,
    )
    assert len(_posts(result, "pass-priority")) == 1, result["calls"]
    assert result["run"] is not None
    assert result["run"]["passes"] == 1


# -- yield toggle handler


def test_on_yield_toggle_adds_and_removes_and_persists() -> None:
    script = """
    (async () => {
      const saved = [];
      globalThis.localStorage.setItem = (k, v) => saved.push([k, JSON.parse(v)]);
      m.store.set({ stops: { mine: [], theirs: [], yieldTo: [] } });
      m.onYieldToggle(O);
      const added = m.store.get().stops.yieldTo;
      m.onYieldToggle(O);
      return { added, removed: m.store.get().stops.yieldTo, saved: saved.map((s) => s[0]) };
    })()
    """
    result = _drive(script, O=BOB_TRIGGER)
    assert result["added"] == [
        {"controller": "bob", "name": "Ocelot Pride", "text": "At the beginning"}
    ]
    assert result["removed"] == []
    assert result["saved"] == ["mcm.stops.v2", "mcm.stops.v2"]


def test_toggle_stop_flag_flips_a_speed_tick_from_its_default() -> None:
    script = """
    (async () => {
      m.store.set({ stops: { mine: [], theirs: [] } });
      m.toggleStopFlag("autoPassNothing");
      const first = m.store.get().stops.autoPassNothing;
      m.toggleStopFlag("autoPassNothing");
      return [first, m.store.get().stops.autoPassNothing];
    })()
    """
    assert _drive(script) == [False, True]


# -- tapped toast


_FIRE = """
(async () => {
  const A = await import(pathToFileURL(ACTIONS).href);
  const action = { ...A.begin("cast", CARD, {}), holdPriority: true };
  m.store.set({ gameId: "g1", uid: "u1", stops: STOPS, state: STATE, action });
  await m.fire();
  return { calls: globalThis.__calls, toasts: globalThis.__toasts };
})()
"""


_FIRE_ARGS = {"ACTIONS": str(ACTIONS_JS), "CARD": PLANESWALKER_CARD}


def test_a_fire_that_tapped_sources_toasts_them() -> None:
    routes = [
        [
            "actions/cast",
            {
                "method": "POST",
                "body": {
                    "allowed": True,
                    "tapped": [
                        {"card_id": "l1", "name": "Mountain"},
                        {"card_id": "l2", "name": "Forest"},
                    ],
                },
            },
        ]
    ]
    result = _drive(
        _FIRE, routes=routes, STOPS=DEFAULT_STOPS, STATE=_board(active_player="alice"), **_FIRE_ARGS
    )
    assert "tapped: Mountain, Forest" in result["toasts"], result


def test_tapped_toast_unticked_says_nothing() -> None:
    routes = [
        [
            "actions/cast",
            {
                "method": "POST",
                "body": {"allowed": True, "tapped": [{"card_id": "l1", "name": "Mountain"}]},
            },
        ]
    ]
    stops = {**DEFAULT_STOPS, "tappedToast": False}
    result = _drive(
        _FIRE, routes=routes, STOPS=stops, STATE=_board(active_player="alice"), **_FIRE_ARGS
    )
    assert not [t for t in result["toasts"] if t.startswith("tapped")]


# -- preferences


def test_remote_preferences_merge_into_the_store() -> None:
    script = """
    (async () => {
      m.store.set({ stops: { mine: ["upkeep"], theirs: [], autoPassNothing: true } });
      await m.loadRemoteStops();
      return { stops: m.store.get().stops, calls: globalThis.__calls };
    })()
    """
    routes = [
        [
            "/api/me/preferences",
            {"body": {"stops": {"mine": ["draw"], "theirs": [], "autoPassNothing": False}}},
        ]
    ]
    result = _drive(script, routes=routes)
    assert result["stops"]["mine"] == ["draw"]
    assert result["stops"]["autoPassNothing"] is False
    assert result["stops"]["autoForced"] is True


def test_no_oidc_user_leaves_local_stops_and_never_redirects() -> None:
    script = """
    (async () => {
      const assign = () => { throw new Error("redirected"); };
      globalThis.window = { location: { assign, pathname: "/", search: "" } };
      m.store.set({ stops: { mine: ["upkeep"], theirs: [] } });
      await m.loadRemoteStops();
      return m.store.get().stops.mine;
    })()
    """
    for status in (401, 404):
        routes = [["/api/me/preferences", {"status": status, "body": {"detail": "no"}}]]
        assert _drive(script, routes=routes) == ["upkeep"]


def test_saving_stops_puts_them_to_preferences_debounced() -> None:
    script = """
    (async () => {
      m.toggleStopFlag("tappedToast");
      m.toggleStopFlag("autoForced");
      await new Promise((r) => setTimeout(r, 700));
      return globalThis.__calls.filter((c) => c.method === "PUT");
    })()
    """
    routes = [["/api/me/preferences", {"method": "PUT", "status": 401, "body": {}}]]
    puts = _drive(script, routes=routes)
    assert len(puts) == 1
    assert puts[0]["path"] == "/api/me/preferences"
    assert puts[0]["body"]["stops"]["tappedToast"] is False
    assert puts[0]["body"]["stops"]["autoForced"] is False


# -- keyboard


_KEY = """
(async () => {
  m.store.set({ gameId: "g1", uid: "u1", run: null, stops: STOPS, state: STATE });
  await m.onKeydown({ key: KEY, target: { tagName: TAG }, preventDefault() {} });
  return { calls: globalThis.__calls, run: m.store.get().run };
})()
"""


def test_f2_passes_once() -> None:
    result = _drive(
        _KEY,
        STOPS={**DEFAULT_STOPS, "autoPassNothing": False},
        STATE=_board(),
        KEY="F2",
        TAG="BODY",
    )
    assert len(_posts(result, "pass-priority")) == 1


def test_f6_arms_pass_until() -> None:
    stops = {**DEFAULT_STOPS, "autoPassNothing": False, "theirs": ["upkeep"]}
    result = _drive(_KEY, STOPS=stops, STATE=_board(step="draw"), KEY="F6", TAG="BODY")
    assert len(_posts(result, "pass-priority")) >= 1


def test_shortcuts_ignored_while_typing_or_without_priority() -> None:
    for tag in ("INPUT", "TEXTAREA", "SELECT"):
        result = _drive(_KEY, STOPS=DEFAULT_STOPS, STATE=_board(), KEY="F2", TAG=tag)
        assert _posts(result, "pass-priority") == [], tag
    result = _drive(
        _KEY, STOPS=DEFAULT_STOPS, STATE=_board(priority_holder="bob"), KEY="F2", TAG="BODY"
    )
    assert _posts(result, "pass-priority") == []


def test_shortcuts_ignored_while_a_modal_is_open() -> None:
    script = (
        """
    globalThis.document.querySelector = (sel) => (sel === "#modal" ? { hidden: false } : null);
    """
        + _KEY
    )
    result = _drive(script, STOPS=DEFAULT_STOPS, STATE=_board(), KEY="F2", TAG="BODY")
    assert _posts(result, "pass-priority") == []


# -- the handler bags the renderer calls


def _call_block(source: str, callee: str) -> str:
    start = source.index(f"{callee}(document, store, {{")
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                return source[start:i]
    raise AssertionError(callee)


def test_the_wired_handler_bags_carry_the_speed_handlers() -> None:
    """`render/ribbon.js`'s stops box calls the three tick toggles and
    `onYieldToggle`; `render/board.js`'s stack objects call `onYieldToggle`.
    `main()` only runs in a browser, so the bags are read off the source."""
    source = APP_JS.read_text()
    ribbon = _call_block(source, "mountRibbon")
    for name in (
        "onToggleAutoForced",
        "onToggleAutoPassNothing",
        "onToggleTappedToast",
        "onYieldToggle",
    ):
        assert f"{name}" in ribbon, name
    assert "onYieldToggle" in _call_block(source, "mountBoard")


def test_each_speed_tick_toggle_flips_its_own_key() -> None:
    script = """
    (async () => {
      m.store.set({ stops: { mine: [], theirs: [] } });
      for (const k of ["autoForced", "autoPassNothing", "tappedToast"]) m.toggleStopFlag(k);
      const s = m.store.get().stops;
      return [s.autoForced, s.autoPassNothing, s.tappedToast];
    })()
    """
    assert _drive(script) == [False, False, False]
