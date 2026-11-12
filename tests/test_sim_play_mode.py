"""Play mode: the chat holds one seat of a decklist game, the baseline plays the other."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api.baseline_seat import decide_body
from magic_cards_model_api.main import app
from magic_cards_model_api.sim import sandbox_game
from magic_cards_model_api.sim.config import SimLimits
from magic_cards_model_api.sim.manager import SandboxManager, SimRefused
from magic_cards_model_api.sim.sandbox_game import PLAY_REFUSAL, SandboxGame
from magic_cards_model_api.sim.server import build_sim_server

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
RED = (FIXTURES / "red_burn.txt").read_text()
GREEN = (FIXTURES / "green_stompy.txt").read_text()
KEEP = {"take": False}


@pytest.fixture
def game():
    g = SandboxGame(TestClient(app))
    yield g
    g.close()


def start(g: SandboxGame, **kw):
    kw = {"seed": 1, "me_first": True, **kw}
    return g.settle_play(g.create_play(RED, GREEN, **kw), 30)


def play(g: SandboxGame, action: str, body: dict | None = None, budget: float = 30):
    """What the worker's `act` op does: act, then let the baseline play on."""
    out = g.act(action, "you", body or {})
    return g.settle_play(out, budget, advance=out["result"].get("allowed") is not False)


def keep_and_finish(g: SandboxGame):
    out = play(g, "mulligan", KEEP)
    if out["state"].get("pregame", {}).get("opening_hand_actor") == "you":
        out = play(g, "opening_hand_finish")
    return out


def test_play_mode_hides_the_opponent_hand(game):
    out = start(game)
    assert out["state"]["players"]["you"]["hand"]
    opp = out["state"]["players"]["baseline"]
    assert "hand" not in opp and opp["hand_count"] == 7
    full = game.state(full=True)["state"]
    assert not full["hands"].get("baseline")
    assert game.legal_actions("baseline")["result"]["allowed"] is False
    assert game.legal_actions("you")["result"]["actions"]


def test_setup_actions_refused_in_play_mode(game):
    start(game)
    for action in ("add_card", "move_card", "set_step", "resolve_top", "settle", "patch_player"):
        out = game.act(action, None, {"card": "Shock", "player": "you"})
        assert out["result"] == {"allowed": False, "reason": PLAY_REFUSAL}, action


def test_acting_for_the_opponent_refused(game):
    start(game)
    out = game.act("pass", "baseline", {})
    assert out["result"]["allowed"] is False
    assert "you play you" in out["result"]["reason"]


def take_one_step(g: SandboxGame):
    """One move of a minimal player: answer a pending question, else pass priority."""
    pending = g.state()["state"]["pending_decision"]
    if pending and pending["kind"].startswith("declare"):
        return play(g, "decide", {"pairs": []})
    if pending:
        return play(g, "decide", decide_body(pending, "you"))
    return play(g, "pass")


def test_opponent_moves_until_your_turn(game):
    start(game, me_first=False)
    out = keep_and_finish(game)
    assert out["opponent_moves"], "the baseline plays its mulligan and opening hand first"
    moves = list(out["opponent_moves"])
    turns = {out["state"]["turn"]}
    for _ in range(80):
        if out["game_over"] or len(turns) > 2:
            break
        assert out["your_turn"] or out.get("note"), out
        out = take_one_step(game)
        moves += out["opponent_moves"]
        turns.add(out["state"]["turn"])
    assert len(turns) > 2, "two turns went by"
    assert out["your_turn"] or out["game_over"]
    assert any(m["detail"].get("player") == "baseline" for m in moves)
    # only the opponent's entries: none of the chat's own moves
    assert not [m for m in moves if m["detail"].get("player") == "you"]
    assert [m["seq"] for m in moves] == sorted({m["seq"] for m in moves}), "no entry twice"


def test_advance_stays_the_seats_own_move_in_play_mode(game):
    start(game)
    out = game.act("advance", None, {})
    assert out["result"].get("reason") != PLAY_REFUSAL


def test_full_game_can_be_finished_by_conceding(game):
    start(game)
    keep_and_finish(game)
    out = play(game, "concede")
    assert out["game_over"] and not out["your_turn"]
    assert out["state"]["winner"] == "baseline"
    assert out["opponent_moves"] == []


def test_play_mode_answers_have_no_credentials(game):
    answers = [start(game), keep_and_finish(game), game.state(), game.legal_actions("you")]
    answers.append(game.act("add_card", None, {}))
    text = json.dumps(answers)
    for uid in game._seat_uids.values():
        assert uid not in text
    for key in ("admin_key", "seat_uids", "advisor_uids", '"uid"'):
        assert key not in text


def test_opponent_budget_note_when_time_runs_out(game):
    start(game)
    out = game.act("mulligan", "you", KEEP)
    out = game.settle_play(out, 0)
    assert out["note"] == sandbox_game.OPPONENT_STILL_PLAYING
    assert not out["your_turn"]
    # the next call continues from where the opponent stopped
    out = game.settle_play(game.state(), 30)
    assert "note" not in out


def test_no_progress_note_when_neither_seat_can_move(game, monkeypatch):
    start(game)
    out = game.act("mulligan", "you", KEEP)
    monkeypatch.setattr(game._opponent, "step", lambda *a, **k: None)
    out = game.settle_play(out, 30)
    assert out["note"] == sandbox_game.NO_PROGRESS


def test_refused_action_reports_the_turn_without_advancing(game):
    start(game)
    out = game.act("cast", "you", {"card_id": "nope"})
    out = game.settle_play(out, 30, advance=False)
    assert out["result"]["allowed"] is False
    assert out["your_turn"] and out["opponent_moves"] == []


def test_answers_carry_support_status_and_deck_warnings(game):
    out = start(game)
    assert out["support_status"] == "ok"
    assert "support_warnings" in out
    assert out["state"]["players"]["baseline"]["library_count"] > 0


def test_support_lookup_failure_is_marked_unavailable(game, monkeypatch):
    def boom(_names):
        raise RuntimeError("scryfall down")

    game.create_play(RED, GREEN, seed=1, me_first=True)
    monkeypatch.setattr(sandbox_game, "support_block", boom)
    out = game.state()
    assert out["support_status"] == "unavailable"
    assert out["support_warnings"] == [] and out["support_note"]


def test_seed_and_me_first_choose_the_seat_order(game):
    first = game.create_play(RED, GREEN, seed=7, me_first=False)["result"]
    assert first["first_player"] == "baseline" and first["seed"] == 7
    a = game.create_play(RED, GREEN, seed=5)["result"]["first_player"]
    b = game.create_play(RED, GREEN, seed=5)["result"]["first_player"]
    assert a == b


def test_bad_names_are_refused(game):
    with pytest.raises(ValueError, match="different"):
        game.create_play(RED, GREEN, me="x", opponent="x")
    with pytest.raises(ValueError, match="invalid me"):
        game.create_play(RED, GREEN, me="a/b")


# -- through the manager, a real worker and the MCP tool -----------------------


@pytest.fixture
def sim():
    limits = SimLimits.from_env({"MTG_SIM_BASELINE_ENABLED": "1"})
    manager = SandboxManager(limits)
    server = build_sim_server(manager, limits)
    yield manager, server
    manager.shutdown()


def tool(server, name: str, **kw):
    result = asyncio.run(server.call_tool(name, kw))
    text = result.request_state if hasattr(result, "request_state") else result.content[0].text
    return json.loads(str(text))


def test_sim_play_new_tool_end_to_end(sim):
    _, server = sim
    assert "sim_play_new" in {t.name for t in asyncio.run(server.list_tools())}
    out = tool(server, "sim_play_new", deck_me=RED, deck_opponent=GREEN, seed=2, me_first=True)
    assert out["sandbox"]["mode"] == "play"
    assert out["your_turn"] is True and out["opponent_moves"] == []
    sid = out["sandbox"]["id"]
    assert "hand" not in out["state"]["players"]["baseline"]
    out = tool(server, "sim_act", sandbox_id=sid, action="mulligan", player="you", body=KEEP)
    assert "opponent_moves" in out and "your_turn" in out
    out = tool(server, "sim_act", sandbox_id=sid, action="add_card", body={})
    assert out["result"]["reason"] == PLAY_REFUSAL
    out = tool(server, "sim_act", sandbox_id=sid, action="concede")
    assert out["game_over"] is True
    text = json.dumps(out)
    assert "admin_key" not in text and "seat_uids" not in text
    assert tool(server, "sim_end", sandbox_id=sid) == {"ended": True}


def test_sim_play_new_unknown_deck_is_a_text_refusal(sim):
    _, server = sim
    out = tool(server, "sim_play_new", deck_me="nope.txt", deck_opponent="nope.txt")
    assert "Unknown deck" in out["error"]


def test_play_new_uses_the_single_sandbox_slot(sim):
    manager, _ = sim
    manager.new({"deck_me": RED, "deck_opponent": GREEN, "seed": 1, "me_first": True}, mode="play")
    with pytest.raises(SimRefused, match="slot is in use"):
        manager.new({"deck_me": RED, "deck_opponent": GREEN}, mode="play")


def test_opponent_error_after_accepted_act_keeps_allowed_true(game, monkeypatch):
    start(game)

    def boom(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(game._opponent, "step", boom)
    out = play(game, "mulligan", KEEP)
    assert out["result"].get("allowed") is not False
    assert "RuntimeError" in out["note"]
    assert "secret detail" not in out["note"]
    assert out["your_turn"] is False and out["game_over"] is False
    text = json.dumps(out)
    for uid in game._seat_uids.values():
        assert uid not in text
    for key in ("admin_key", "seat_uids", "advisor_uids", '"uid"'):
        assert key not in text


def test_opponent_error_on_state_read_keeps_the_state(game, monkeypatch):
    start(game)
    game.act("mulligan", "you", KEEP)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(game._opponent, "step", boom)
    out = game.settle_play(game.state(), 30)
    assert "RuntimeError" in out["note"] and "state" in out


def test_play_mode_refuses_snapshots(game):
    start(game, seed=1, me_first=True)
    out = game.snapshot("root")
    assert out["result"]["allowed"] is False and "board mode" in out["result"]["reason"]
    assert game.restore("root")["result"]["allowed"] is False
