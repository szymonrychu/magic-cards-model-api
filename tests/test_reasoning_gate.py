"""Nothing that changes the game may be done by an agent seat without
saying why - and no future route or tool may quietly opt out of that.

Two structural tests carry the weight here, and both are default-deny: a
mutating HTTP route, or an MCP tool, is required to take `reasoning` and run
`records.require_reasoning` unless it is named in an exemption list *with a
written reason*. Adding a new one and forgetting the gate fails the suite;
deciding it genuinely does not need one is a deliberate edit to a list
somebody reviews.

That shape exists because the gate was missed exactly once already:
`PUT .../step` mutates the turn marker and drives the suspend upkeep its own
route runs, was exposed to every agent as an MCP tool at the time, and shipped
with no `reasoning` at all - one hand-written route audit away from never being
noticed. (That tool is gone now: no rule puts the turn marker anywhere, so it
is not a seat verb. The route stays for the harness, and stays gated - a caller
that names an agent seat still owes a reason.)
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, get_args, get_type_hints

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.routing import Mount

from magic_cards_model_api import mcp
from magic_cards_model_api.app import app
from magic_cards_model_api.seatkit import Seats

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: Two legal decklists, for the one test here that needs a lobby-booted game
#: (an `ai: true` seat is only registrable through that boot path or
#: `NewPlayer.is_agent`). Same shape `test_ui_e2e.py` uses.
_DECK_A = "52 Mountain\n4 Grizzly Bears\n4 Lightning Bolt"
_DECK_B = "52 Forest\n4 Grizzly Bears\n4 Lightning Bolt"

#: Mutating routes that deliberately take no `reasoning`, each with the
#: reason it does not. Every entry is a claim somebody has to agree with in
#: review - which is the point. A route absent from here and from the gate
#: fails `test_every_mutating_route_gates_on_reasoning`.
EXEMPT_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/games"): (
        "creates the game. No seat exists yet, so there is no agent seat to "
        "hold to a reason - `is_agent` is what this call *records*."
    ),
    ("DELETE", "/api/games/{game_id}"): (
        "ends the game, on the admin key or a seat uid. An operator action "
        "over a whole game, not a move inside one; it takes no body at all."
    ),
    ("POST", "/api/games/{game_id}/cards"): "board setup: the harness deals a card.",
    ("POST", "/api/games/{game_id}/cards/{card_id}/move"): "board setup.",
    ("POST", "/api/games/{game_id}/cards/{card_id}/tap"): "board setup.",
    ("PATCH", "/api/games/{game_id}/cards/{card_id}"): "board setup.",
    ("PATCH", "/api/games/{game_id}/players/{username}"): "board setup.",
    ("PUT", "/api/games/{game_id}/players/{username}/mana-pool"): "board setup.",
    ("POST", "/api/games/{game_id}/actions/target"): (
        "a legality probe (`engine_api.check_target`) that changes nothing - "
        "the same category as a `dry_run`, which is also un-audited."
    ),
    ("POST", "/api/games/{game_id}/report"): (
        "`ReportProblemRequest.text` *is* the reasoning: the seat's own account "
        "of what it did, what the rules say, and what happened instead. A "
        "second free-text 'why' beside it would be the same sentence twice."
    ),
    ("POST", "/api/games/{game_id}/pregame/settle"): (
        "admin-key only, and it settles the pregame for *every* seat at once - "
        "an operator's call over a game they created, made by no seat, so "
        "there is no agent seat whose reasoning could be required."
    ),
    ("POST", "/api/decks/validate"): "reads a decklist and answers; mutates nothing.",
    ("POST", "/api/lobby/games"): "the lobby boots a game. Same reason as `POST /api/games`.",
    ("DELETE", "/api/lobby/games/{game_id}"): (
        "the lobby ends a game. Same reason as `DELETE /api/games/{game_id}`."
    ),
    ("POST", "/api/lobby/games/{game_id}/ai-seat/start"): (
        "the operator starts or stops an agent process. About a seat, not by one."
    ),
    ("POST", "/api/lobby/games/{game_id}/ai-seat/stop"): (
        "the operator starts or stops an agent process. About a seat, not by one."
    ),
    ("POST", "/api/games/{game_id}/plan"): (
        "the declaration IS the reasoning, and four fields of it - what the "
        "seat reads on the board, its role in the matchup, what it means to "
        "do, and what it expects back and why. A fifth free-text 'why' beside "
        "them would be the same sentence a fifth time. It also mutates no "
        "`Game` at all: `records.require_reasoning` guards actions, and this "
        "is a statement about one."
    ),
    ("POST", "/api/games/{game_id}/seats"): (
        "mints a fresh seat credential for an already-registered player - the "
        "seat back door's replacement (2b's T8), an operator/harness action "
        "about a seat, not a move inside the game."
    ),
    ("POST", "/api/games/{game_id}/reflection"): (
        "the write-up IS the reasoning, the same shape `POST .../plan` "
        "already gets this exemption for. It also mutates no `Game`: this "
        "is a statement made after play, not a move inside it, and it is "
        "refused outright before the game is even over - there is no "
        "in-progress agent turn here for `records.require_reasoning` to "
        "gate."
    ),
    ("POST", "/api/games/{game_id}/ai-review"): (
        "the same exemption `POST .../reflection` gets, for the same reason: "
        "the review text itself is the whole content of the row - there is "
        "no separate action to reason about, it mutates no `Game`, and it "
        "is refused outright before the game is even over, so there is no "
        "in-progress agent turn for `records.require_reasoning` to gate."
    ),
    ("POST", "/api/games/{game_id}/ai-review-failure"): (
        "the same exemption `POST .../ai-review` gets, for the same reason, "
        "and one more of its own: `reason` IS the reasoning, the row is "
        "written by a driver rather than by a seat taking a turn, it mutates "
        "no `Game`, and it is refused outright before the game is over - so "
        "there is no in-progress agent turn for `records.require_reasoning` "
        "to gate. Gating it on a seat's reasoning would also be circular: "
        "this route exists precisely for the case where the seat's own "
        "post-game work could not be done."
    ),
    ("POST", "/api/games/{game_id}/plays-outcome"): (
        "the same exemption `POST .../ai-review` gets: a driver's post-game "
        "record of what the handover did in the guides repo, written after "
        "the game is over (refused before), mutating no `Game` - there is no "
        "in-progress agent turn for `records.require_reasoning` to gate, and "
        "each no-change it carries already has its own `reason`."
    ),
    ("POST", "/api/games/{game_id}/advice"): (
        "`AdviceIn.reasoning` IS the reasoning, and required non-blank by the "
        "shape itself rather than by this gate - which is the stronger "
        "guarantee: `records.require_reasoning` holds only registered agent "
        "seats to it, while an advisor owes its human an argument whatever it "
        "is. It also mutates no `Game`: a proposal is a statement about one, "
        "the same exemption `POST .../plan` gets."
    ),
    ("PUT", "/api/games/{game_id}/seats/{uid}/scripts"): (
        "registers the play scripts a seat Job will run. A corpus is a "
        "statement about what the runner may try, validated script by script; "
        "it mutates no `Game` and writes no ledger row. Every move a script "
        "makes goes through the action routes, which hold it to a reason."
    ),
    ("POST", "/api/games/{game_id}/script-runs"): (
        "reports that a play script started or finished. It mutates no "
        "`Game`, the same exemption `POST .../plan` gets: the moves the run "
        "made are their own action rows, each already gated on `reasoning`."
    ),
    ("POST", "/api/games/{game_id}/advice/{seq}/approve"): (
        "the human saying yes to advice it has already read. One ledger row, "
        "no `Game` touched, and made by the human seat rather than by an "
        "agent - there is no agent turn here to hold to a reason."
    ),
    ("POST", "/api/games/{game_id}/decide-forced"): (
        "takes no answer content at all - it only names which standing "
        "question to check - so there is nothing for a caller to explain. "
        "When it actually mutates the `Game` it does so by calling `decide` "
        "itself, in-process, with a `reasoning` this route builds from "
        "`forced_decisions.forced_reason` (`AUTO_REASONING_PREFIX: <why>`) - "
        "the real mutation is exactly as gated as `POST .../decide` already "
        "is, one call down, and the answer's ledger row proves it."
    ),
    ("PUT", "/api/me/preferences"): (
        "the play UI's own per-user settings (speed-mode stop opt-outs), "
        "keyed on the OIDC subject rather than on any seat. It mutates no "
        "`Game` at all - there is no agent turn, and no ledger row, for "
        "`records.require_reasoning` to gate."
    ),
}

#: MCP tools that read and never mutate, each with the reason.
EXEMPT_TOOLS: dict[str, str] = {
    "wait_for_priority": "one poll for whether it is this seat's moment; reads only.",
    "battlefield_state": "the board as this seat may see it; reads only.",
    "game_log": "this game's log, seat-filtered; reads only.",
    "list_scripts": (
        "this seat's registered play scripts judged against the board; runs and records "
        "nothing, reads only."
    ),
    "report_problem": ("`text` is the reasoning - see the matching note on `POST .../report`."),
    "declare_plan": (
        "the declaration IS the reasoning, and mutates no `Game` at all - see the "
        "matching note on `POST /api/games/{game_id}/plan` in EXEMPT_ROUTES."
    ),
    "stage_advice": (
        "arms this advisor's own next committing call to be captured rather than "
        "posted - that captured call still carries its own `reasoning` (S2 A6), "
        "and this one mutates no `Game` at all; it has no backend route to gate "
        "either, so there is no matching EXEMPT_ROUTES entry."
    ),
    "pass_baton": (
        "the seat-pair orchestrator's tool: it changes no `Game` and writes no ledger "
        "row, only rotates the seat's baton held in the MCP process (`baton.py`); it "
        "has no backend route to gate, so there is no matching EXEMPT_ROUTES entry."
    ),
    "await_baton": (
        "the seat-pair future agent's wait: it changes no `Game` and writes no ledger "
        "row, only blocks until its baton held in the MCP process (`baton.py`) goes "
        "live; it has no backend route to gate, so there is no matching EXEMPT_ROUTES entry."
    ),
}

#: Helpers a route may delegate its gate to rather than calling it inline.
_GATE_DELEGATES = ("records.require_reasoning", "_answer_declaration")


def _api_routes() -> list[APIRoute]:
    """Every `APIRoute` reachable from the app, sub-routers included.

    `app.include_router` leaves an opaque `_IncludedRouter` in `app.routes`
    rather than flattening its children, so a naive one-level walk silently
    misses every `/api/ui/...` route - and a test that silently checks
    fewer things than it claims is worse than no test.
    """
    found: list[APIRoute] = []
    seen: set[int] = set()

    def walk(routes: Any) -> None:
        for route in routes or []:
            if id(route) in seen:
                continue
            seen.add(id(route))
            if isinstance(route, APIRoute):
                found.append(route)
            # `_IncludedRouter` keeps the router it wrapped under
            # `original_router`; a `Mount` (the static UI) holds no API
            # routes worth walking into.
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(getattr(included, "routes", None))
            elif not isinstance(route, Mount):
                walk(getattr(route, "routes", None))

    walk(app.routes)
    return found


def _body_model(endpoint: Any) -> type[BaseModel] | None:
    """The pydantic request-body model this endpoint takes, if any -
    unwrapping `Model | None` for the routes whose body is optional."""
    hints = get_type_hints(endpoint)
    for name, annotation in hints.items():
        if name == "return":
            continue
        candidates = [annotation, *get_args(annotation)]
        for candidate in candidates:
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                return candidate
    return None


def _body_source(endpoint: Any) -> str:
    """This endpoint's source with its docstring removed.

    Not cosmetic: `pregame_mulligan`'s docstring used to *say* "Not gated by
    `records.require_reasoning`", and a plain substring search over the whole
    source read that sentence as evidence the gate was there.
    """
    source = inspect.getsource(endpoint)
    doc = inspect.getdoc(endpoint)
    if doc:
        for line in doc.splitlines():
            source = source.replace(line, "")
    return source


def test_every_mutating_route_gates_on_reasoning() -> None:
    """Default-deny over every mutating route the app serves."""
    missing: list[str] = []
    for route in _api_routes():
        methods = (route.methods or set()) & _MUTATING_METHODS
        if not methods:
            continue
        for method in sorted(methods):
            key = (method, route.path)
            if key in EXEMPT_ROUTES:
                continue
            model = _body_model(route.endpoint)
            source = _body_source(route.endpoint)
            if model is None or "reasoning" not in model.model_fields:
                missing.append(f"{method} {route.path}: its request body has no `reasoning` field")
            elif not any(delegate in source for delegate in _GATE_DELEGATES):
                missing.append(
                    f"{method} {route.path}: takes `reasoning` but never calls "
                    "`records.require_reasoning`"
                )
    assert not missing, (
        "every mutating route must require `reasoning` of an agent seat, or be "
        "listed in EXEMPT_ROUTES with the reason it need not:\n  " + "\n  ".join(missing)
    )


def test_every_mutating_mcp_tool_takes_reasoning() -> None:
    """Default-deny over the MCP surface an agent actually drives. A tool
    with no `reasoning` parameter cannot pass one on to the route, so the
    route's own gate would refuse the agent outright - or, worse, the route
    has no gate either and the action lands unexplained."""
    server = mcp.build_server(Seats(api="http://testserver", game_id="unused"))
    tools = asyncio.run(server.list_tools())
    assert tools, "no tools registered - this test would prove nothing"
    missing = [
        tool.name
        for tool in tools
        if tool.name not in EXEMPT_TOOLS
        and "reasoning" not in ((tool.input_schema or {}).get("properties") or {})
    ]
    assert not missing, (
        "every mutating MCP tool must take `reasoning`, or be listed in "
        f"EXEMPT_TOOLS with the reason it need not: {missing}"
    )


def test_the_exemption_lists_name_only_routes_and_tools_that_exist() -> None:
    """An exemption for something that no longer exists is a stale claim
    nobody notices - and it is exactly how a renamed route slips back
    through the gate under its old name's cover."""
    live_routes = {
        (method, route.path)
        for route in _api_routes()
        for method in (route.methods or set()) & _MUTATING_METHODS
    }
    assert set(EXEMPT_ROUTES) <= live_routes, set(EXEMPT_ROUTES) - live_routes

    server = mcp.build_server(Seats(api="http://testserver", game_id="unused"))
    live_tools = {tool.name for tool in asyncio.run(server.list_tools())}
    assert set(EXEMPT_TOOLS) <= live_tools, set(EXEMPT_TOOLS) - live_tools


# ---------------------------------------------------------------------------
# the four routes the audit found ungated, each proven at the wire
# ---------------------------------------------------------------------------


def _agent_game(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/games",
        json={
            "players": [
                {"username": "bob", "is_agent": True},
                {"username": "alice", "is_agent": False},
            ],
            "active_player": "bob",
        },
    )
    assert response.status_code == 200, response.text
    info = response.json()
    # bob is on the clock here, and `records.require_plan` refuses an agent
    # seat's first mutating action of its own turn until it has declared a
    # plan for that turn. This file is about the *reasoning* gate, so the
    # declaration is made once, here, rather than restated in each test - a
    # bare call still fails on `reasoning` first, which is what these tests
    # then assert.
    plan = client.post(
        f"/api/games/{info['game_id']}/plan",
        json={
            "player": "bob",
            "uid": info["seat_uids"]["bob"],
            "board_read": "nothing on either board yet",
            "my_role": "beatdown",
            "plan": "develop and attack when I can",
            "prediction": {
                "expecting": "alice passes with no play",
                "because": "she has no permanents",
                "confidence": "low",
            },
        },
    )
    assert plan.status_code == 200, plan.text
    return info


def _missing_reasoning(response: Any) -> bool:
    if response.status_code != 422:
        return False
    detail = response.json()["detail"]
    return detail == [
        {
            "loc": ["body", "reasoning"],
            "msg": (
                "bob is an agent seat, and this action needs `reasoning` - why "
                "it is doing this, in its own words"
            ),
            "type": "missing",
        }
    ]


def test_set_step_refuses_an_agent_seat_with_no_reasoning(client: TestClient) -> None:
    info = _agent_game(client)
    try:
        game_id = info["game_id"]
        bare = client.put(
            f"/api/games/{game_id}/step", json={"step": "declare_attackers", "player": "bob"}
        )
        assert _missing_reasoning(bare), bare.text
        assert (
            client.get(f"/api/games/{game_id}", params={"uid": info["admin_key"]}).json()["step"]
            == "precombat_main"
        ), "the marker must not have moved"

        with_why = client.put(
            f"/api/games/{game_id}/step",
            json={
                "step": "declare_attackers",
                "player": "bob",
                "reasoning": "my board is bigger, I want combat",
            },
        )
        assert with_why.status_code == 200, with_why.text
        log = client.get(f"/api/games/{game_id}/log", params={"uid": info["seat_uids"]["bob"]})
        entry = [e for e in log.json()["log"] if e["action"] == "step"][-1]
        assert entry["reasoning"] == "my board is bigger, I want combat"
        assert entry["legal_actions_for"] == "bob"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_state_based_actions_refuses_an_agent_seat_with_no_reasoning(client: TestClient) -> None:
    """It also still takes no body at all - the harness's own poke, made by
    nobody, which no agent-seat gate can apply to."""
    info = _agent_game(client)
    try:
        game_id = info["game_id"]
        bare = client.post(f"/api/games/{game_id}/state-based-actions", json={"player": "bob"})
        assert _missing_reasoning(bare), bare.text

        with_why = client.post(
            f"/api/games/{game_id}/state-based-actions",
            json={"player": "bob", "reasoning": "checking nothing died"},
        )
        assert with_why.status_code == 200, with_why.text

        bodyless = client.post(f"/api/games/{game_id}/state-based-actions")
        assert bodyless.status_code == 200, bodyless.text

        log = client.get(f"/api/games/{game_id}/log", params={"uid": info["seat_uids"]["bob"]})
        entries = [e for e in log.json()["log"] if e["action"] == "state_based_actions"]
        assert entries[-2]["reasoning"] == "checking nothing died"
        assert entries[-1]["reasoning"] is None, "a bodyless poke has no seat and owes nothing"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_the_pregame_declarations_refuse_an_agent_seat_with_no_reasoning(
    client: TestClient,
) -> None:
    """CR 103.5's mulligan and CR 103.6's opening-hand window are gated like
    every other seat action. The exemption they used to have rested on a
    claim about `ai_pregame.py`'s backstop that was simply not true of these
    routes - it calls `opening.py` directly and never travels through them.
    """
    boot = client.post(
        "/api/lobby/games",
        json={
            "players": [
                {"username": "bob", "decklist_text": _DECK_A, "ai": True},
                {"username": "alice", "decklist_text": _DECK_B, "ai": False},
            ],
            "seed": 7,
        },
    )
    assert boot.status_code == 200, boot.text
    info = boot.json()
    game_id = info["game_id"]
    try:
        uids = info["seat_uids"]
        bare = client.post(
            f"/api/games/{game_id}/pregame/mulligan",
            params={"uid": uids["bob"]},
            json={"player": "bob", "take": False},
        )
        assert _missing_reasoning(bare), bare.text

        kept = client.post(
            f"/api/games/{game_id}/pregame/mulligan",
            params={"uid": uids["bob"]},
            json={"player": "bob", "take": False, "reasoning": "two lands and a one-drop, keep"},
        )
        assert kept.status_code == 200, kept.text
        client.post(
            f"/api/games/{game_id}/pregame/mulligan",
            params={"uid": uids["alice"]},
            json={"player": "alice", "take": False},
        )

        bare_finish = client.post(
            f"/api/games/{game_id}/pregame/opening-hand/finish",
            params={"uid": uids["bob"]},
            json={"player": "bob"},
        )
        assert _missing_reasoning(bare_finish), bare_finish.text
    finally:
        client.delete(f"/api/lobby/games/{game_id}", params={"uid": info["seat_uids"]["alice"]})


# ---------------------------------------------------------------------------
# the pass-shaped exemption: a pass is not an argument
# ---------------------------------------------------------------------------


def _agent_game_with_active(client: TestClient, active: str) -> dict[str, Any]:
    """`_agent_game`, with `active` on the clock - so a call made by `bob`
    lands on somebody else's turn and never reaches the plan gate
    (`records.require_plan`), which is a different refusal than the one
    these tests are about."""
    response = client.post(
        "/api/games",
        json={
            "players": [
                {"username": "bob", "is_agent": True},
                {"username": "alice", "is_agent": False},
            ],
            "active_player": active,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_pass_priority_from_an_agent_seat_needs_no_reasoning(client: TestClient) -> None:
    """`pass_priority` is exempt at the gate, not in the tool signature.

    Measured on the live ledger, 75 of the 92 rows carrying reasoning were
    `pass_priority`, averaging 80 characters, and one string appeared 38
    times because `pass_until` threads a single reason onto every pass it
    makes. Requiring a sentence for "I have nothing to do" buys a longer
    non-answer, not a better one - so the field stays accepted and stops
    being required.
    """
    info = _agent_game_with_active(client, "alice")
    game_id = info["game_id"]
    try:
        bare = client.post(f"/api/games/{game_id}/actions/pass-priority", json={"player": "bob"})
        assert bare.status_code == 200, bare.text
        assert not _missing_reasoning(bare)

        log = client.get(f"/api/games/{game_id}/log", params={"uid": info["admin_key"]})
        passes = [e for e in log.json()["log"] if e["action"] == "pass_priority"]
        assert passes, "the pass must still be recorded, reasoning or not"
        assert passes[-1]["reasoning"] is None
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_pass_priority_still_records_a_reasoning_when_one_is_given(client: TestClient) -> None:
    """Exempt is not ignored: the field is still accepted and still stored,
    so a seat with something worth saying about a pass can still say it."""
    info = _agent_game_with_active(client, "alice")
    game_id = info["game_id"]
    try:
        passed = client.post(
            f"/api/games/{game_id}/actions/pass-priority",
            json={"player": "bob", "reasoning": "holding up my counterspell"},
        )
        assert passed.status_code == 200, passed.text

        log = client.get(f"/api/games/{game_id}/log", params={"uid": info["admin_key"]})
        passes = [e for e in log.json()["log"] if e["action"] == "pass_priority"]
        assert passes[-1]["reasoning"] == "holding up my counterspell"
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})


def test_every_other_action_still_owes_a_reason(client: TestClient) -> None:
    """The exemption is pass-shaped and nothing else - a `play_land` from the
    same seat in the same game is still refused without one."""
    info = _agent_game_with_active(client, "alice")
    game_id = info["game_id"]
    try:
        bare = client.post(
            f"/api/games/{game_id}/actions/play-land", json={"player": "bob", "card": "Mountain"}
        )
        assert _missing_reasoning(bare), bare.text
    finally:
        client.delete(f"/api/games/{game_id}", params={"key": info["admin_key"]})
