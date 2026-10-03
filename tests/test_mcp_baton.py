"""T3: the baton guard in the seat MCP server.

A seat's acting tools may be restricted to whoever holds the seat's current
baton: `pass_baton(player_uid)` (the orchestrator's tool) rotates it, and an
acting call carrying an old or missing `baton` is refused with a plain tool
answer naming the code and the fix. Enforcement for a uid starts once a baton
was issued for it (or for every uid under `--require-baton`); with neither,
every tool behaves as it always did.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

import httpx
import pytest
from conftest import add_card, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines.turn import Step
from mcp.client import Client
from test_autopass import Opponent, stock_libraries
from test_decisions import seats_for

from magic_cards_model_api import mcp
from magic_cards_model_api.baton import (
    BATON_EXEMPT_TOOLS,
    BATON_OK,
    BATON_REQUIRED,
    BATON_STALE,
    BatonStore,
)
from magic_cards_model_api.main import app
from magic_cards_model_api.seatkit import Seats


def wired(client: TestClient, monkeypatch, game_id: str, **kwargs: Any):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    seats = Seats(api="http://testserver", game_id=game_id, **kwargs)
    return seats, seats_for(client, game_id)


def call(server: Any, name: str, arguments: dict[str, Any]) -> str:
    return asyncio.run(server.call_tool(name, arguments)).content[0].text


def pass_baton(server: Any, uid: str) -> str:
    return json.loads(call(server, "pass_baton", {"player_uid": uid}))["baton"]


def log_len(client: TestClient, game: str, uid: str) -> int:
    return len(client.get(f"/api/games/{game}/log", params={"uid": uid}).json()["log"])


# ---- the store ----


def test_store_is_open_until_a_baton_is_issued():
    store = BatonStore()
    assert store.check("u1", "") == BATON_OK
    assert store.check("u1", "anything") == BATON_OK
    assert not store.enforced("u1")


def test_issue_returns_a_fresh_unguessable_token_and_invalidates_the_previous():
    store = BatonStore()
    first = store.issue("u1")
    second = store.issue("u1")
    assert first != second
    assert len(first) >= 16 and len(second) >= 16
    assert store.check("u1", second) == BATON_OK
    assert store.check("u1", first) == BATON_STALE
    assert store.check("u1", "") == BATON_REQUIRED
    assert store.enforced("u1")


def test_uids_are_isolated():
    store = BatonStore()
    a = store.issue("a")
    assert store.check("b", "") == BATON_OK
    b = store.issue("b")
    assert store.check("a", b) == BATON_STALE
    assert store.check("b", a) == BATON_STALE
    assert store.check("a", a) == BATON_OK and store.check("b", b) == BATON_OK


def test_require_all_enforces_every_uid_before_any_issue():
    store = BatonStore(require_all=True)
    assert store.check("u1", "") == BATON_REQUIRED
    assert store.check("u1", "guess") == BATON_STALE
    token = store.issue("u1")
    assert store.check("u1", token) == BATON_OK


def test_store_is_thread_safe_one_winner_per_rotation():
    store = BatonStore()
    tokens: list[str] = []

    def rotate() -> None:
        for _ in range(50):
            tokens.append(store.issue("u1"))

    threads = [threading.Thread(target=rotate) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(tokens)) == 400
    assert sum(store.check("u1", token) == BATON_OK for token in tokens) == 1


def test_exempt_set_is_the_documented_read_only_set():
    assert {
        "battlefield_state",
        "game_log",
        "list_scripts",
        "report_problem",
        "pass_baton",
    } == BATON_EXEMPT_TOOLS


# ---- legacy: nothing issued, nothing required ----


def test_without_a_baton_every_tool_behaves_as_before(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    add_card(client, game, "Plains", "alice", "hand")
    answer = json.loads(
        call(
            server,
            "play_land",
            {"player_uid": uids["alice"], "card": "Plains", "reasoning": "legacy path"},
        )
    )
    assert answer["outcome"]["allowed"] is True


# ---- the tool surface ----


def test_pass_baton_is_registered_and_every_acting_tool_takes_baton(client, game, monkeypatch):
    seats, _uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert "pass_baton" in tools
    assert set(tools["pass_baton"].input_schema["properties"]) == {"player_uid"}
    for name, tool in tools.items():
        has = "baton" in tool.input_schema["properties"]
        assert has == (name not in BATON_EXEMPT_TOOLS), name
        if has:
            assert tool.input_schema["properties"]["baton"].get("default") == ""
    for name in ("stage_script", "declare_plan", "wait_for_priority", "pass_until", "pass"):
        assert "baton" in tools[name].input_schema["properties"], name
    for name in ("decide", "mulligan", "opening_hand_action", "cast", "declare_attackers"):
        assert "baton" in tools[name].input_schema["properties"], name


def test_pass_baton_returns_token_and_description_and_never_acts(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    before = log_len(client, game, uids["alice"])
    board = state(client, game)
    answer = json.loads(call(server, "pass_baton", {"player_uid": uids["alice"]}))
    assert len(answer["baton"]) >= 16
    assert answer["baton"] in seats.baton._current.values()
    assert "baton" in answer["description"].lower() and len(answer["description"]) < 400
    assert log_len(client, game, uids["alice"]) == before
    assert state(client, game) == board


def test_pass_baton_for_an_unknown_uid_is_a_plain_answer(client, game, monkeypatch):
    seats, _uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    text = call(server, "pass_baton", {"player_uid": "nope"})
    assert "baton" not in text.lower() or "no such" in text.lower()
    assert not seats.baton.enforced("nope")


# ---- enforcement ----


def test_acting_tool_without_a_baton_is_refused_once_one_was_issued(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    token = pass_baton(server, uids["alice"])
    add_card(client, game, "Plains", "alice", "hand")
    before = log_len(client, game, uids["alice"])
    args = {"player_uid": uids["alice"], "card": "Plains", "reasoning": "x"}

    refused = call(server, "play_land", args)
    assert refused.startswith("BATON_REQUIRED:"), refused
    assert "pass_baton" in refused
    assert log_len(client, game, uids["alice"]) == before
    assert not any(c["name"] == "Plains" for c in state(client, game)["battlefield"])

    ok = json.loads(call(server, "play_land", {**args, "baton": token}))
    assert ok["outcome"]["allowed"] is True


def test_stale_baton_is_refused_with_time_and_guidance(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    old = pass_baton(server, uids["alice"])
    new = pass_baton(server, uids["alice"])
    add_card(client, game, "Plains", "alice", "hand")
    args = {"player_uid": uids["alice"], "card": "Plains", "reasoning": "x"}

    refused = call(server, "play_land", {**args, "baton": old})
    assert refused.startswith("BATON_STALE:"), refused
    assert "newer agent" in refused and "20" in refused  # an iso timestamp
    assert "battlefield_state" in refused and "orchestrator" in refused

    assert json.loads(call(server, "play_land", {**args, "baton": new}))["outcome"]["allowed"]


@pytest.mark.parametrize(
    "tool,extra",
    [
        ("stage_script", {"body": {}, "reasoning": "x"}),
        ("declare_plan", {"for_turn": "next"}),
        ("wait_for_priority", {}),
        ("pass_until", {"step": "upkeep", "whose_turn": "mine"}),
        ("pass", {"reasoning": "x"}),
        ("decide", {"reasoning": "x"}),
        ("mulligan", {"take": False, "reasoning": "x"}),
        ("opening_hand_action", {"card": "Plains", "reasoning": "x"}),
        ("cast", {"card": "Plains", "reasoning": "x"}),
        ("declare_attackers", {"attackers": "", "reasoning": "x"}),
        ("run_script", {"script": "nope", "reasoning": "x"}),
    ],
)
def test_every_acting_tool_is_refused_without_the_baton(client, game, monkeypatch, tool, extra):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    pass_baton(server, uids["alice"])
    before = log_len(client, game, uids["alice"])
    text = call(server, tool, {"player_uid": uids["alice"], **extra})
    assert text.startswith("BATON_REQUIRED:"), (tool, text)
    assert log_len(client, game, uids["alice"]) == before


def test_read_only_tools_work_without_a_baton(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    pass_baton(server, uids["alice"])
    uid = {"player_uid": uids["alice"]}
    assert '"you": "alice"' in call(server, "battlefield_state", uid)
    assert "entries" in call(server, "game_log", uid)
    call(server, "list_scripts", uid)
    assert not call(server, "report_problem", {**uid, "text": "t"}).startswith("BATON_")


def test_two_uids_are_isolated_on_one_server(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game, resolve_by_uid=True)
    server = mcp.build_server(seats)
    alice_token = pass_baton(server, uids["alice"])
    # Bob never had a baton issued: legacy behaviour for his seat.
    bob_text = call(server, "pass", {"player_uid": uids["bob"], "reasoning": "x"})
    assert not bob_text.startswith("BATON_"), bob_text
    # Alice's token does not open bob's seat once bob has his own.
    bob_token = pass_baton(server, uids["bob"])
    refused = call(
        server, "pass", {"player_uid": uids["bob"], "reasoning": "x", "baton": alice_token}
    )
    assert refused.startswith("BATON_STALE:")
    allowed = call(
        server, "pass", {"player_uid": uids["alice"], "reasoning": "x", "baton": alice_token}
    )
    assert not allowed.startswith("BATON_"), allowed
    assert bob_token != alice_token


def test_require_baton_enforces_every_uid_from_the_start(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    seats.baton = BatonStore(require_all=True)
    server = mcp.build_server(seats)
    args = {"player_uid": uids["alice"], "reasoning": "x"}
    assert call(server, "pass", args).startswith("BATON_REQUIRED:")
    token = pass_baton(server, uids["alice"])
    assert not call(server, "pass", {**args, "baton": token}).startswith("BATON_")


def test_parser_flag_and_env_turn_enforcement_on(monkeypatch):
    monkeypatch.delenv("MTG_REQUIRE_BATON", raising=False)
    assert mcp._build_parser().parse_args([]).require_baton is False
    assert mcp._build_parser().parse_args(["--require-baton"]).require_baton is True
    monkeypatch.setenv("MTG_REQUIRE_BATON", "1")
    assert mcp._build_parser().parse_args([]).require_baton is True


# ---- background run ----


def idle_run(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    stock_libraries(client, game)
    force_turn_position(game, Step.UPKEEP, "bob")
    server = mcp.build_server(seats)
    token = pass_baton(server, uids["alice"])
    arguments = {
        "player_uid": uids["alice"],
        "step": "upkeep",
        "whose_turn": "mine",
        "stop_when_attacked": False,
        "reasoning": "nothing to do on their turn",
        "baton": token,
    }
    first = json.loads(call(server, "pass_until", arguments))
    assert first["finished"] is False, first["stopped_because"]
    assert seats.background.status(uids["alice"]) == "running"
    return seats, uids, server, arguments, first, token


def test_a_refused_call_leaves_the_background_run_running(client, game, monkeypatch):
    seats, uids, server, _args, _first, _token = idle_run(client, game, monkeypatch)
    for baton in ("", "wrong"):
        text = call(
            server,
            "wait_for_priority",
            {"player_uid": uids["alice"], "baton": baton},
        )
        assert text.startswith("BATON_"), text
        assert seats.background.status(uids["alice"]) == "running"


def test_rotation_mid_run_lets_the_new_holder_take_the_run_back(client, game, monkeypatch):
    seats, uids, server, arguments, first, old = idle_run(client, game, monkeypatch)
    new = pass_baton(server, uids["alice"])
    resumed = {**arguments, "resume": json.dumps(first["resume"])}

    stale = call(server, "pass_until", resumed)
    assert stale.startswith("BATON_STALE:")
    assert seats.background.status(uids["alice"]) == "running"

    with Opponent(game, "bob"):
        give_up = time.monotonic() + 30
        while seats.background.status(uids["alice"]) == "running":
            assert time.monotonic() < give_up
            time.sleep(0.05)
        final = json.loads(call(server, "pass_until", {**resumed, "baton": new}))
    assert final["stopped_because"] == "reached your upkeep", final["stopped_because"]
    assert final["background"]["passes"] > 0, final["background"]
    assert old != new


def test_decide_generation_guard_is_unchanged_under_a_valid_baton(client, game, monkeypatch):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    token = pass_baton(server, uids["alice"])
    text = call(
        server,
        "decide",
        {
            "player_uid": uids["alice"],
            "reasoning": "x",
            "baton": token,
            "generation": 999,
            "answer": {},
        },
    )
    assert not text.startswith("BATON_"), text


# ---- timing log ----


def test_timing_line_names_the_holder(client, game, monkeypatch, caplog):
    seats, uids = wired(client, monkeypatch, game)
    server = mcp.build_server(seats)
    uid = uids["alice"]
    with caplog.at_level(logging.INFO, logger="mcm.mcp"):
        call(server, "battlefield_state", {"player_uid": uid})  # no baton issued
        token = pass_baton(server, uid)
        call(server, "battlefield_state", {"player_uid": uid})  # reader
        call(server, "pass", {"player_uid": uid, "reasoning": "x", "baton": token})  # current
        call(server, "pass", {"player_uid": uid, "reasoning": "x", "baton": "old"})  # refused
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tool ")]
    holders = [line.split("holder=")[1].split()[0] for line in lines]
    assert holders == ["-", "-", "reader", "current", "reader"], lines


# ---- http transport: one process, many seats keyed by uid ----


def test_http_transport_enforces_per_uid_over_a_real_socket(client, game, monkeypatch):
    import socket

    import uvicorn

    seats, uids = wired(client, monkeypatch, game, resolve_by_uid=True)
    asgi = mcp.build_server(seats).streamable_http_app(stateless_http=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(asgi, host="127.0.0.1", port=port, log_level="warning")
    http_server = uvicorn.Server(config)
    thread = threading.Thread(target=http_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not http_server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert http_server.started

    async def drive() -> list[str]:
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:

            async def run(name: str, **arguments: Any) -> str:
                result = await c.call_tool(name, arguments)
                return result.content[0].text

            token = json.loads(await run("pass_baton", player_uid=uids["alice"]))["baton"]
            return [
                await run("pass", player_uid=uids["alice"], reasoning="x"),
                await run("pass", player_uid=uids["alice"], reasoning="x", baton=token),
                await run("pass", player_uid=uids["bob"], reasoning="x"),
            ]

    try:
        required, accepted, bob = asyncio.run(drive())
    finally:
        http_server.should_exit = True
        thread.join(timeout=5)
    assert required.startswith("BATON_REQUIRED:")
    assert not accepted.startswith("BATON_")
    assert not bob.startswith("BATON_")


# ---- a buddy shares the http MCP process, never the seat's baton ----
#
# On the http `MCP_URL` path one process serves every seat and every buddy
# (advisor uid) of every live game from ONE `BatonStore`, keyed by uid. The
# buddy is a top-level claude with the same tool surface as a seat (advise
# mode forks no toolset), so it can call `pass_baton`. The only uid it holds
# is its own advisor credential, and `pass_baton` refuses an advisor uid: a
# buddy can neither rotate the seat's baton nor start enforcement on itself.


def test_a_buddy_on_the_shared_server_cannot_rotate_a_baton(client, game, monkeypatch):
    from magic_cards_model_api.repository import Advisor
    from magic_cards_model_api.store import store

    seats, uids = wired(client, monkeypatch, game, resolve_by_uid=True)
    server = mcp.build_server(seats)
    store.register_advisors(game, {"buddy-uid": Advisor(player="alice")})
    seat_token = pass_baton(server, uids["alice"])

    refused = call(server, "pass_baton", {"player_uid": "buddy-uid"})

    assert refused.startswith("BATON_ADVISOR:"), refused
    assert "advisor" in refused.lower() and "orchestrator" in refused.lower()
    assert not seats.baton.enforced("buddy-uid")
    assert seats.baton.check(uids["alice"], seat_token) == BATON_OK
    add_card(client, game, "Plains", "alice", "hand")
    played = json.loads(
        call(
            server,
            "play_land",
            {
                "player_uid": uids["alice"],
                "card": "Plains",
                "reasoning": "still the holder",
                "baton": seat_token,
            },
        )
    )
    assert played["outcome"]["allowed"] is True


def test_an_advisor_lookup_never_reveals_the_seat_uid(client, game):
    """The buddy's only way to name the seat's baton is the seat uid, a
    separate credential the advisor lookup does not hand out."""
    from magic_cards_model_api.repository import Advisor
    from magic_cards_model_api.store import store

    store.register_advisors(game, {"buddy-uid": Advisor(player="alice")})
    seat = next(uid for uid, name in store.seat_uids(game).items() if name == "alice")
    answer = client.get("/api/seats/buddy-uid")
    assert answer.status_code == 200
    assert answer.json()["role"] == "advisor"
    assert seat not in answer.text
