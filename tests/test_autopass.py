"""Auto-passing priority up to a named point in the turn: `seatkit.pass_until`,
and the two front ends onto it (`mcp.pass_until`, `seat_cli pass-until`).

A seat that must call `pass` for every priority it is handed spends most of a
game answering "no" to questions it could never answer any other way, and the
human on the other side watches it think about each one. One call that passes a
whole stretch fixes that - but only if it stops for the things that matter, and
the three that matter most are not symmetrical:

- something new landing on the stack is visible in the state, and a run that
  ignores it has thrown away the seat's chance to respond;
- **neither combat declaration is.** Declaring blockers (CR 509.1) and
  declaring attackers (CR 508.1) are turn-based actions, and each "doesn't use
  the stack", so nothing arrives on the stack for either; the decision mailbox
  never carries them either, because `decisions.py` keeps `DECLARE_BLOCKERS`
  and `DECLARE_ATTACKERS` out of `ANSWERABLE_KINDS` - each has its own action.
  A run that only watched the stack passes a seat straight through both. The
  blockers one takes the whole attack unblocked, which loses games; the
  attackers one costs an attack and does it *silently*, since a player who
  never declares is treated as declaring nobody and nothing anywhere says why.
  Both get the bulk of this file's tests, each with the counterweight that a
  seat which deliberately switches the stop off really does run through, and
  the guard that neither fires on the step a run started in - which would make
  "auto-pass past combat", asked from inside combat, a call that does nothing.

Every CR number here was verified with `knowledge_search(type="rules")` in the
session that wrote this file: CR 509.1 (declaring blockers is a turn-based
action that does not use the stack), CR 508.1 (declaring attackers is the same
kind of action, for the active player), CR 502.4 and CR 514.3 (no priority in
untap or cleanup), CR 500.8 and CR 500.9 (an effect may add a phase or a step,
so one turn can hold two of the same step).

Every board is a real board played through the real API with real Scryfall
printings from tests/fixtures/scryfall - no mocks, per the project's rule.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
from conftest import add_card, find, state
from fastapi.testclient import TestClient
from mcp_types import InputRequiredResult
from test_card_split_labels import boot as boot_the_devourer
from test_card_split_labels import (
    reach_the_devourer_upkeep,
    resolve_top_in_background,
    wait_for_pending,
)

from magic_cards_model_api import seat_cli, seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats, pass_until
from magic_cards_model_api.store import store

#: Short because these tests drive both seats themselves and nothing here waits
#: on a human: the loop's own poll interval is the only thing between two
#: priorities, and 0.2s of it per step turns a four-turn run into a slow test
#: for no extra coverage. The production default (`seatkit.POLL_SECONDS`) is
#: unchanged; this is the one knob `pass_until` exposes for exactly this.
FAST_POLL = 0.01

#: What "returns promptly" means for one `pass_until` call, asserted on every
#: lap of `run_to_stop`. Generous by design - these tests run a real engine
#: over a real HTTP stack - but far below any deadline the old loop could have
#: been given: the version this replaced blocked for a measured 6.0s at
#: `max_seconds=6.0`, and its MCP schema let an agent ask for 570. 5.0 rather
#: than 3.0: a loaded CI runner stretched a call that sleeps 0.6s by design to
#: 3.016s; 5.0 is still under the 6.0s the old loop blocked for.
MAX_CALL_SECONDS = 5.0


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def wired(monkeypatch, game_id: str) -> tuple[Seats, dict[str, str]]:
    """`Seats` pointed at the in-process app, the trick `test_action_flow.py`
    uses."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    return Seats(api="http://testserver", game_id=game_id), seats_for(game_id)


def run_to_stop(seat: Any, *, drive_seconds: float = 30.0, **kwargs: Any) -> dict[str, Any]:
    """Drive `seatkit.pass_until` the way its contract now says a caller must:
    call it, and if it comes back `finished: false`, hand its `resume` token
    straight back and call again.

    `pass_until` used to be a deadline loop that slept until `max_seconds`,
    which is why every test below used to be one call. It is poll-and-return
    now (MCP 2026-07-28 removed resumability - a tool call that holds the
    request open loses it if the stream breaks), so a run that has to wait for
    the opponent hands the board back instead. The *stops* being tested did not
    change; only who does the waiting did, and it is the caller - here, this
    helper, and in production the agent or the CLI user.

    Two things are asserted on every single lap, so the whole file is also a
    test of the no-blocking property rather than only of the stop conditions:
    each individual call returns well inside `MAX_CALL_SECONDS`, and any
    unfinished answer really does carry the `resume` token the next call
    needs. `drive_seconds` bounds this helper - never `pass_until` - and
    failing it is a genuine test failure, not a stop reason.

    Returns the final (finished) answer, plus `total_passes` summed across
    every call and `calls`. `passes` on the returned dict stays the last
    call's own count, which is what `pass_until` actually reports.
    """
    resume = None
    total_passes = 0
    calls = 0
    give_up_at = time.monotonic() + drive_seconds
    while True:
        started = time.monotonic()
        result = pass_until(seat, resume=resume, **kwargs)
        elapsed = time.monotonic() - started
        calls += 1
        total_passes += result["passes"]
        assert elapsed < MAX_CALL_SECONDS, (
            f"one pass_until call took {elapsed:.1f}s - it is supposed to return "
            f"as soon as it would have to wait: {result['stopped_because']}"
        )
        if result["finished"]:
            return {**result, "total_passes": total_passes, "calls": calls}
        assert "resume" in result, (
            "an unfinished run must hand back a resume token, or the next call "
            "re-stamps the starting-occurrence exemption where the seat is now"
        )
        resume = result["resume"]
        assert time.monotonic() < give_up_at, (
            f"the run never finished in {drive_seconds}s across {calls} calls; "
            f"last answer: {result['stopped_because']}"
        )
        # The caller's own pacing - `pass_until` does none of its own any
        # more, deliberately. A real seat paces itself the same way (see
        # `docs/ai_seat_brief.md`); here it is short because both seats are
        # driven in-process.
        time.sleep(FAST_POLL)


class Opponent:
    """The other seat, passing every priority it is given, on its own thread.

    A run that passes priority only moves the game on if somebody answers those
    passes - "all players pass in succession" is what ends a step. This is that
    other player, and it is a real seat taking the real action through the real
    endpoint, not a stub: nothing here decides anything on the opponent's
    behalf beyond "pass", which is a legal choice at every priority it is
    offered (CR 117.3d) - and, since pausing this task added, "declare
    nobody" whenever this seat's own `declare_attackers`/`declare_blockers`
    (CR 508.1, CR 509.1) comes up. Before this task neither kind ever reached
    a live seat at all (`_ask_declaration` silently defaulted to nobody), so
    this thread never had to answer either; now that both genuinely pause,
    an opponent that only knew "pass" would freeze its own combat forever on
    every turn it is active - a run driven by the seat under test would then
    time out waiting for a turn boundary that can never arrive. Nothing here
    still decides anything beyond the same "nobody" default the engine used
    to give for free - a test that wants this opponent to actually attack or
    block stages that itself, the same way `stage_an_attack_on_bob` already
    does for alice.
    """

    def __init__(self, game_id: str, player: str) -> None:
        self.game_id, self.player = game_id, player
        self.uid = seats_for(game_id)[player]
        self.stop = threading.Event()
        self.passes = 0
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        client = TestClient(app)
        while not self.stop.is_set():
            board = client.get(f"/api/games/{self.game_id}").json()
            if board.get("winner") or board.get("drawn"):
                return
            pending = board.get("pending_decision")
            if pending is not None and pending.get("player") == self.player:
                if pending.get("kind") == "declare_attackers":
                    client.post(
                        f"/api/games/{self.game_id}/actions/attack",
                        params={"uid": self.uid},
                        json={"attackers": []},
                    )
                elif pending.get("kind") == "declare_blockers":
                    client.post(
                        f"/api/games/{self.game_id}/actions/declare-blockers",
                        params={"uid": self.uid},
                        json={"player": self.player, "blocks": []},
                    )
                else:
                    # Some other decision this file does not stage for the
                    # opponent (nothing here needed one before this task) -
                    # waiting rather than guessing an answer.
                    time.sleep(FAST_POLL)
                continue
            if board.get("priority_holder") == self.player:
                client.post(
                    f"/api/games/{self.game_id}/actions/pass-priority",
                    json={"player": self.player},
                )
                self.passes += 1
            else:
                time.sleep(FAST_POLL)

    def __enter__(self) -> Opponent:
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop.set()
        self.thread.join(timeout=10)


def set_step(client: TestClient, game_id: str, step: str) -> None:
    response = client.put(f"/api/games/{game_id}/step", json={"step": step})
    assert response.status_code == 200, response.text


def stock_libraries(client: TestClient, game_id: str, cards: int = 6) -> None:
    """Real cards in both libraries, so a run that crosses a turn boundary does
    not end the game by drawing from an empty one.

    The `game` fixture builds a board with no decklists behind it, and CR 121.4
    loses the game for "a player who attempts to draw a card from a library with
    no cards in it" (a state-based action, CR 704.5b), so
    every test here that passes into somebody's draw step needs this. Real
    Forests from the recorded Scryfall fixtures, put in a real zone through the
    harness's own endpoint - staging, not a mock.
    """
    for player in ("alice", "bob"):
        for _ in range(cards):
            add_card(client, game_id, "Forest", player, "library")


def a_bear_that_can_attack(client: TestClient, game_id: str, player: str) -> str:
    """One real Grizzly Bears on `player`'s battlefield, able to attack.

    Summoning sickness is cleared through the harness's own card patch rather
    than by waiting a turn - the creature is a real printing in a real zone,
    and the patch is the same staging `tests/test_flow.py` uses to reach a
    combat without playing four turns first.
    """
    add_card(client, game_id, "Grizzly Bears", player, "battlefield")
    bear = find(state(client, game_id)["battlefield"], "Grizzly Bears")["id"]
    patched = client.patch(f"/api/games/{game_id}/cards/{bear}", json={"summoning_sick": False})
    assert patched.status_code == 200, patched.text
    return bear


def stage_an_attack_on_bob(client: TestClient, game_id: str) -> str:
    """alice, the active player, really attacks bob with a real creature.

    Leaves the game in the declare-attackers step with the attack declared, so
    the very next thing both seats pass into is CR 509.1's declare-blockers
    step - the step this whole file exists for.
    """
    bear = a_bear_that_can_attack(client, game_id, "alice")
    set_step(client, game_id, "beginning_of_combat")
    client.post(f"/api/games/{game_id}/advance", json={"player": "alice"})
    # `advance` paused here rather than silently declaring nobody (CR 508.1) -
    # the committed board still shows the step this call was made *from*
    # (`app.py`'s `advance` docstring), not the one it paused trying to
    # enter, so the sanity check is the pause itself, not `state()["step"]`
    # (which only becomes "declare_attackers" for real once the attack below
    # actually answers it).
    pending = state(client, game_id)["pending_decision"]
    assert pending is not None and pending["kind"] == "declare_attackers", pending
    attack = client.post(
        f"/api/games/{game_id}/actions/attack",
        params={"uid": seats_for(game_id)["alice"]},
        json={"attackers": [{"card_id": bear, "defender": "bob"}]},
    )
    assert attack.json()["allowed"] is True, attack.json()
    return bear


# -- the named destination, and the next-occurrence rule ----------------------


def test_a_run_stops_at_the_named_step(client, game, monkeypatch):
    """The plain case: name a step you are not standing in, and the run passes
    every priority until the board gets there."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    with Opponent(game, "bob"):
        result = run_to_stop(
            alice,
            until_step="end_step",
            whose_turn="mine",
            # This run crosses alice's own combat, and the CR 508.1 stop would
            # halt it there - correctly, and with its own tests below. Switched
            # off so this test is about the destination and nothing else.
            stop_when_you_can_attack=False,
        )

    assert result["stopped_because"] == "reached your end step", result["stopped_because"]
    assert result["finished"] is True
    assert result["total_passes"] > 0, "a run that passed nothing did not run"
    assert result["state"]["step"] == "end_step"
    assert result["state"]["active_player"] == "alice"
    assert result["state"]["turn_number"] == 1, "alice's own end step is still turn 1"


def test_naming_the_step_you_are_standing_in_means_the_next_one(client, game, monkeypatch):
    """The off-by-one the browser's autopilot was fixed for, and the same fix
    here: "keep passing until my upkeep", said during your upkeep, means the
    next one. A run that stopped where it started would be a no-op, which is
    never what the caller meant."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")
    before = state(client, game)
    assert before["step"] == "upkeep" and before["active_player"] == "alice"

    with Opponent(game, "bob"):
        result = run_to_stop(
            alice,
            until_step="upkeep",
            whose_turn="mine",
            # Two of alice's own combats lie between here and her next upkeep;
            # the CR 508.1 stop has its own tests, and this one is about the
            # off-by-one.
            stop_when_you_can_attack=False,
        )

    assert result["stopped_because"] == "reached your upkeep"
    assert result["state"]["step"] == "upkeep"
    assert result["state"]["active_player"] == "alice"
    assert result["state"]["turn_number"] > 1, (
        "the run stopped in the very upkeep it started in - the next occurrence "
        "is a whole turn away"
    )


def test_whose_turn_any_takes_whichever_comes_first(client, game, monkeypatch):
    """`any` is how a seat says "the next upkeep, whosever it is" - here the
    opponent's, which comes first, rather than its own a full round later.

    `turn_number` counts rounds rather than seats in this engine, so bob's
    first upkeep is still turn 1; what says the run went somewhere is the
    active player having changed hands."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    with Opponent(game, "bob"):
        result = run_to_stop(
            alice,
            until_step="upkeep",
            whose_turn="any",
            stop_when_you_can_attack=False,  # alice's own combat is on the way
        )

    assert result["stopped_because"] == "reached the next upkeep"
    assert result["state"]["step"] == "upkeep"
    assert result["state"]["active_player"] == "bob"
    assert result["total_passes"] > 0


# -- something new on the stack ----------------------------------------------


def test_a_spell_landing_on_the_stack_stops_the_run(client, game, monkeypatch):
    """The user's "ability to react to anything on the stack". bob casts a real
    Lightning Bolt while alice is auto-passing toward her end step, and alice
    holds a real answer (her own Mountain and Lightning Bolt); her run must
    hand her back the board with the bolt still on the stack, not resolve it by
    passing through.

    L1: the stop is judged once alice holds priority over the bolt (CR 117.3c
    gives bob priority first, right after he casts it), so bob passes after
    casting here - a bob who never passed would leave alice nothing to judge."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")["id"]
    bolt = board["hands"]["bob"][0]["id"]
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    set_step(client, game, "precombat_main")

    cast = threading.Event()

    def bob_bolts() -> None:
        bob_client = TestClient(app)
        for _ in range(2000):
            if bob_client.get(f"/api/games/{game}").json().get("priority_holder") == "bob":
                bob_client.post(
                    f"/api/games/{game}/actions/cast",
                    json={
                        "player": "bob",
                        "card_id": bolt,
                        "targets": ["alice"],
                        "mana_sources": [mountain],
                    },
                )
                bob_client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
                cast.set()
                return
            time.sleep(FAST_POLL)

    thread = threading.Thread(target=bob_bolts, daemon=True)
    thread.start()
    try:
        result = run_to_stop(alice, until_step="end_step", whose_turn="mine")
    finally:
        thread.join(timeout=10)

    assert cast.is_set(), "bob never got the chance to cast, so nothing was tested"
    assert result["stopped_because"] == "something new went on the stack: Lightning Bolt"
    assert [obj["name"] for obj in result["state"]["stack"]] == ["Lightning Bolt"]
    assert result["state"]["step"] != "end_step", "the run should have stopped short"
    assert result["state"]["priority_holder"] == "alice", "the stop hands alice her response"
    assert not result.get("auto_passed"), "nothing was passed over on alice's behalf"


def test_a_run_told_not_to_stop_on_the_stack_runs_through_it(client, game, monkeypatch):
    """The counterweight, and a real choice an agent is allowed to make: with
    `stop_on_stack=False` the bolt resolves underneath the run and it carries on
    to its destination."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")["id"]
    bolt = board["hands"]["bob"][0]["id"]
    set_step(client, game, "precombat_main")

    def bob_bolts_then_passes() -> None:
        bob_client = TestClient(app)
        bolted = False
        for _ in range(4000):
            board = bob_client.get(f"/api/games/{game}").json()
            if board.get("priority_holder") != "bob":
                time.sleep(FAST_POLL)
                continue
            if not bolted:
                bob_client.post(
                    f"/api/games/{game}/actions/cast",
                    json={
                        "player": "bob",
                        "card_id": bolt,
                        "targets": ["alice"],
                        "mana_sources": [mountain],
                    },
                )
                bolted = True
                continue
            bob_client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})

    thread = threading.Thread(target=bob_bolts_then_passes, daemon=True)
    thread.start()
    try:
        result = run_to_stop(
            alice,
            until_step="end_step",
            whose_turn="mine",
            stop_on_stack=False,
            stop_when_you_can_attack=False,  # alice's own combat is on the way
        )
    finally:
        thread.join(timeout=1)

    assert result["stopped_because"] == "reached your end step", result["stopped_because"]
    assert result["state"]["stack"] == [], "the bolt should have resolved on the way past"


# -- CR 509.1: being attacked, which the stack can never tell you about -------


def test_a_run_stops_when_this_seat_is_being_attacked(client, game, monkeypatch):
    """The condition that decides games. alice attacks with a real creature;
    bob is auto-passing toward his own upkeep, a whole turn away. Declaring
    blockers is a turn-based action that "doesn't use the stack" (CR 509.1),
    so the stack condition cannot see the attack; pausing does carry the
    decision now (`decisions.ANSWERABLE_KINDS`), but only as far as freezing
    the game until answered - without its own check here, bob's run would
    answer it with "nobody" itself (CR 509.1a's "if any") and pass through
    his declare-blockers step, taking the hit unblocked."""
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    stock_libraries(client, game)
    stage_an_attack_on_bob(client, game)
    # A creature bob could block with, so the declaration is a real choice:
    # one with no legal blocker is answered for him (`forced_decisions`).
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")

    with Opponent(game, "alice"):
        # T2: this stop is opt-in now - the test names the toggle it is
        # actually testing rather than leaning on a default.
        result = run_to_stop(bob, until_step="upkeep", whose_turn="mine", stop_when_attacked=True)

    # CR 509.1: declaring blockers is the first thing that happens in the
    # declare-blockers step, and a read during the pause shows the step the
    # question stands in (`serialize._paused_overlay`, battle 3 B3-08).
    assert result["state"]["step"] == "declare_blockers", result["stopped_because"]
    assert "you are being attacked by 1 creature" in result["stopped_because"]
    assert "CR 509.1" in result["stopped_because"], (
        "the stop reason has to say why nothing else woke this seat"
    )
    assert result["state"]["combat"]["attacks"], "the attack is still live to be blocked"
    assert result["available_actions"]["blockers_requested"] is True


def test_a_run_told_not_to_stop_when_attacked_runs_through_its_own_blockers_step(
    client, game, monkeypatch
):
    """The toggle the human has as a checkbox, and the agent has as a
    parameter: a seat that has genuinely decided not to block may switch this
    off and run through combat. It is a real choice, so it has to really work -
    and the damage really lands, which is the cost the docstring warns about."""
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    stock_libraries(client, game)
    stage_an_attack_on_bob(client, game)

    with Opponent(game, "alice"):
        result = run_to_stop(
            bob,
            until_step="upkeep",
            whose_turn="mine",
            stop_when_attacked=False,
        )

    assert result["stopped_because"] == "reached your upkeep", result["stopped_because"]
    assert result["state"]["step"] == "upkeep"
    assert result["state"]["active_player"] == "bob"
    bob_life = next(p for p in result["state"]["players"] if p["username"] == "bob")["life"]
    assert bob_life == 18, f"the unblocked bear should have hit for 2, life is {bob_life}"


def test_a_run_does_not_echo_its_own_declare_blockers_decision_when_the_commit_is_slow(
    client, game, monkeypatch
):
    """X1's stale-echo fix (`seatkit.py`'s `_call_watching_for_self_decision`),
    exercised here for `_pass_until_once`'s own CR 509.1 "declare nobody" call
    (`perform_declare`) rather than `decide`: the same test above proves bob's
    run auto-answers his own `declare_blockers` with "nobody" and the
    unblocked bear connects, but that only ever exercised the fast path -
    `perform_declare`'s commit lands well inside `POLL_SECONDS` every time,
    so the poll never got a chance to see the still-pending decision it is
    itself in the middle of answering. Slowed past `POLL_SECONDS`, the same
    race `test_decision_kinds.py::test_decide_does_not_echo_the_decision_it_
    is_answering` reproduces for `decide` reproduces here too: before the
    fix, the poll sees bob's own `declare_blockers` still standing, reports
    it back as a decision this very "nobody" call raised, and the run hands
    the board back instead of the turn-based action actually completing -
    the bear never connects because combat itself never left the frozen
    declare-blockers step."""
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    stock_libraries(client, game)
    stage_an_attack_on_bob(client, game)

    _orig_watch = seatkit._call_watching_for_self_decision

    def _slow_watch(seat, perform, poll_seconds=seatkit.POLL_SECONDS, **kwargs):
        def _slowed():
            time.sleep(seatkit.POLL_SECONDS * 3)
            return perform()

        return _orig_watch(seat, _slowed, poll_seconds=seatkit.POLL_SECONDS / 4, **kwargs)

    monkeypatch.setattr(seatkit, "_call_watching_for_self_decision", _slow_watch)

    with Opponent(game, "alice"):
        result = run_to_stop(
            bob,
            until_step="upkeep",
            whose_turn="mine",
            stop_when_attacked=False,
        )

    assert "raised by this very call" not in result["stopped_because"], result["stopped_because"]
    assert result["stopped_because"] == "reached your upkeep", result["stopped_because"]
    assert result["state"]["step"] == "upkeep"
    bob_life = next(p for p in result["state"]["players"] if p["username"] == "bob")["life"]
    assert bob_life == 18, f"the unblocked bear should have hit for 2, life is {bob_life}"


def test_the_attacked_stop_does_not_fire_on_the_attacking_seat(client, game, monkeypatch):
    """The same board from the other side. alice is the *attacking* player, so
    her own declare-blockers step is not a step she owes an action in - the
    stop is for the defending player only (CR 509.1: "the defending player
    declares blockers"), and firing it for the attacker would strand every
    attack the seat itself made."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    stage_an_attack_on_bob(client, game)

    with Opponent(game, "bob"):
        # T2: on, so the guard being proven is "does not fire on the
        # attacker", not merely "the stop was off anyway".
        result = run_to_stop(
            alice, until_step="end_step", whose_turn="mine", stop_when_attacked=True
        )

    assert result["stopped_because"] == "reached your end step", result["stopped_because"]
    assert result["state"]["step"] == "end_step"


# -- CR 508.1: your own attack, which the stack cannot tell you about either --


def test_a_run_stops_in_your_own_declare_attackers_step(client, game, monkeypatch):
    """The sibling condition to the blockers one, and the same shape of hole:
    declaring attackers is a turn-based action that "doesn't use the stack"
    (CR 508.1). Pausing does carry the decision now
    (`decisions.ANSWERABLE_KINDS` covers `DECLARE_ATTACKERS`), but only as
    far as freezing the game until answered - a run aimed past combat would,
    without its own check, answer it with "nobody" itself and pass its own
    attack by exactly as silently as the old unscripted default did.

    It costs an attack rather than the game - what earns it a stop is that it is
    silent: an unscripted player declares nobody (CR 508.1a's "if any" makes
    that legal), and the turn simply produces no attack.

    Note the board: a real creature that really could attack, so the stop lands
    on a turn where skipping it would genuinely have cost something.
    """
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    a_bear_that_can_attack(client, game, "alice")
    set_step(client, game, "precombat_main")

    with Opponent(game, "bob"):
        result = run_to_stop(
            alice,
            until_step="postcombat_main",
            whose_turn="mine",
            stop_when_you_can_attack=True,  # T2: opt-in now
        )

    # CR 508.1: declaring attackers is the first thing in the declare-attackers
    # step, which is the step the paused question is read in (B3-08).
    assert result["state"]["step"] == "declare_attackers", result["stopped_because"]
    assert result["state"]["active_player"] == "alice"
    assert "it is your declare-attackers step" in result["stopped_because"]
    assert "CR 508.1" in result["stopped_because"], (
        "the stop reason has to say why nothing else woke this seat"
    )
    assert result["available_actions"]["attackers_requested"] is True


def test_the_attackers_stop_does_not_guess_whether_anything_could_attack(client, game, monkeypatch):
    """Deliberately no creature on the board at all, and the run still stops.

    Summoning sickness, defender, tap state and CR 508.1's restrictions and
    requirements are engine questions; this file defers to the engine rather
    than reimplementing them, exactly as `ui/priority.js`/`ui/combat.js` do, so the human's
    checkbox and this parameter cannot mean two different things. "It is your
    declare-attackers step" is the whole condition.
    """
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "precombat_main")
    assert state(client, game)["battlefield"] == [], "the point is an empty board"

    with Opponent(game, "bob"):
        result = run_to_stop(
            alice,
            until_step="postcombat_main",
            whose_turn="mine",
            stop_when_you_can_attack=True,  # T2: opt-in now
        )

    # See `test_a_run_stops_in_your_own_declare_attackers_step`'s note: the
    # question is read in the declare-attackers step it stands in.
    assert result["state"]["step"] == "declare_attackers", result["stopped_because"]


def test_a_run_told_not_to_stop_when_you_can_attack_runs_through_its_own_combat(
    client, game, monkeypatch
):
    """The deliberate "I am not attacking this turn". It has to really work, and
    the cost has to really land: the run reaches its destination and the bear
    that could have attacked is still untapped at the end of it, having done
    nothing."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    a_bear_that_can_attack(client, game, "alice")
    set_step(client, game, "precombat_main")

    with Opponent(game, "bob"):
        result = run_to_stop(
            alice,
            until_step="postcombat_main",
            whose_turn="mine",
            stop_when_you_can_attack=False,
        )

    assert result["stopped_because"] == "reached your postcombat main", result["stopped_because"]
    assert result["state"]["combat"]["attacks"] == [], "nothing attacked, which is the cost"
    bear = find(result["state"]["battlefield"], "Grizzly Bears")
    assert bear["tapped"] is False, "an attacker taps (CR 508.1f); this one never attacked"


def test_the_attackers_stop_does_not_fire_on_the_defending_seat(client, game, monkeypatch):
    """CR 508.1: "the active player declares attackers". bob is not the active
    player, so alice's declare-attackers step is not a step bob owes anything
    in, and stopping him there would be a run that halts on every one of his
    opponent's turns for nothing."""
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    stock_libraries(client, game)
    a_bear_that_can_attack(client, game, "alice")
    set_step(client, game, "precombat_main")

    with Opponent(game, "alice"):
        # T2: on, so the guard being proven is "does not fire on the
        # defender", not merely "the stop was off anyway".
        result = run_to_stop(
            bob, until_step="upkeep", whose_turn="mine", stop_when_you_can_attack=True
        )

    assert result["stopped_because"] == "reached your upkeep", result["stopped_because"]
    assert result["state"]["active_player"] == "bob"


# -- a combat stop must not fire on the step the run started in ---------------


def test_a_run_started_in_its_own_declare_attackers_step_still_moves(client, game, monkeypatch):
    """The dead-call defect. Without the guard, a combat condition fires on the
    very state the run starts on, so "auto-pass past combat", asked while
    standing in combat, stops before it moves and the caller is handed back the
    board it already had. Asking from inside that step is the seat having
    looked at combat and chosen to move on."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    a_bear_that_can_attack(client, game, "alice")
    set_step(client, game, "beginning_of_combat")
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    # Answers the pause with "nobody attacks" (CR 508.1a's "if any") so the
    # board really is standing in a *real* declare-attackers step (not a
    # frozen one) by the time the run starts - this test is about the
    # started-in guard, not about whether the bear attacks.
    client.post(
        f"/api/games/{game}/actions/attack", params={"uid": uids["alice"]}, json={"attackers": []}
    )
    assert state(client, game)["step"] == "declare_attackers"

    with Opponent(game, "bob"):
        result = run_to_stop(
            alice,
            until_step="postcombat_main",
            whose_turn="mine",
            stop_when_you_can_attack=True,  # T2: opt-in now - this is its own test
        )

    assert result["stopped_because"] == "reached your postcombat main", result["stopped_because"]
    assert result["total_passes"] > 0, "the run stopped on the state it started on - a dead call"


def test_a_run_started_in_its_own_declare_blockers_step_still_moves(client, game, monkeypatch):
    """The same guard on the same wart in the blockers condition, which shipped
    with it. Both seats pass out of declare attackers first, so the run really
    does begin inside a declare-blockers step with a live attack on it."""
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    stock_libraries(client, game)
    stage_an_attack_on_bob(client, game)
    for player in ("alice", "bob"):
        client.post(f"/api/games/{game}/actions/pass-priority", json={"player": player})
    # The second pass paused on bob's own declare-blockers decision (CR
    # 509.1) rather than silently entering it - answered here with "nobody
    # blocks" (CR 509.1a's "if any") so the board is standing in a *real*
    # declare-blockers step, live attack included, by the time the run
    # starts.
    client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": uids["bob"]},
        json={"player": "bob", "blocks": []},
    )
    board = state(client, game)
    assert board["step"] == "declare_blockers" and board["combat"]["attacks"]

    with Opponent(game, "alice"):
        result = run_to_stop(
            bob,
            until_step="postcombat_main",
            whose_turn="any",
            stop_when_attacked=True,  # T2: opt-in now - this is its own test
        )

    assert result["stopped_because"] == "reached the next postcombat main", result[
        "stopped_because"
    ]
    assert result["total_passes"] > 0


def test_the_stamp_only_covers_the_occurrence_the_run_started_in(client, game, monkeypatch):
    """And the guard is scoped, not a blanket exemption: once the board leaves
    the starting occurrence the stop comes back.

    `started_in` here is the *pause itself* - `run` begins already sitting on
    a live, unanswered `declare_attackers` decision, not one already resolved
    (`test_a_run_started_in_its_own_declare_attackers_step_still_moves`
    resolves it first for a different reason - see that test). The exemption
    is keyed on `run["started_pending_kind"]` matching the live decision as
    well as on the `(turn_number, step)` pair. Answering the decision leaves
    the board in that same declare-attackers step, so the stamp stands; moving
    on out of the step is what leaves the occurrence, and
    `forget_starting_occurrence`'s own `(turn, step)` comparison notices that.
    """
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    a_bear_that_can_attack(client, game, "alice")
    set_step(client, game, "beginning_of_combat")
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    started_in = alice.fetch_state()
    assert started_in["step"] == "declare_attackers"
    pending = started_in["pending_decision"]
    assert pending is not None and pending["kind"] == "declare_attackers", pending

    run = seatkit.start_autopass(started_in, "postcombat_main", "mine")
    # T2: opt-in now - passed explicitly since this test is about the
    # started-in exemption on this very condition, not about its default.
    assert (
        seatkit.autopass_stop_reason(started_in, run, "alice", stop_when_you_can_attack=True)
        is None
    ), "the pause the run began already sitting on is exempt"

    # Answer the pause for real - CR 508.1a's "if any" makes "nobody" legal.
    # The board is still in the same declare-attackers step, so the stamp
    # stands; both seats passing then moves the game out of it.
    client.post(
        f"/api/games/{game}/actions/attack", params={"uid": uids["alice"]}, json={"attackers": []}
    )
    answered = alice.fetch_state()
    assert answered["step"] == "declare_attackers"
    seatkit.forget_starting_occurrence(run, answered)
    assert run["started_step"] == "declare_attackers", "the same occurrence keeps its stamp"
    for player in ("alice", "bob"):
        client.post(f"/api/games/{game}/actions/pass-priority", json={"player": player})
    later = alice.fetch_state()
    assert later["step"] != "declare_attackers", "passing should have moved the step on"

    seatkit.forget_starting_occurrence(run, later)
    assert run["started_step"] is None, "the stamp survived the board leaving its occurrence"
    assert (
        seatkit.autopass_stop_reason(started_in, run, "alice", stop_when_you_can_attack=True)
        is not None
    ), "with the stamp cleared, the very same pause must stop the run again"


# -- the other stops ---------------------------------------------------------


def test_a_decision_addressed_to_this_seat_stops_the_run(client, monkeypatch):
    """A pending decision freezes the game and answering it is the only legal
    move (CR 608.2d), so a run must hand it straight back rather than keep
    passing at it. The board is the real Devourer of Destiny one
    `tests/test_card_split_labels.py` builds - a real decklist, the engine's own
    seeded shuffle, CR 103.6b's reveal taken, run to the first upkeep."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    info = boot_the_devourer(client)
    game_id, uid = info["game_id"], info["seat_uids"]["alice"]
    try:
        reach_the_devourer_upkeep(client, info)
        thread = resolve_top_in_background(game_id)
        try:
            pending = wait_for_pending(client, game_id, uid)
            alice = Seats(api="http://testserver", game_id=game_id).seat_for(uid)
            result = run_to_stop(
                alice,
                until_step="end_step",
                whose_turn="mine",
            )
            answered = client.post(
                f"/api/games/{game_id}/decide",
                params={"uid": uid},
                json={"player": "alice", "moved": [pending["cards"][0]], "top_order": None},
            )
        finally:
            thread.join(timeout=15)
        assert answered.status_code == 200, answered.text
        assert result["stopped_because"].startswith("a decision is waiting for you:")
        assert result["total_passes"] == 0, "nothing may be passed while a decision is outstanding"
        assert result["finished"] is True
    finally:
        client.delete(f"/api/games/{game_id}")


# -- it returns rather than waits ---------------------------------------------


def test_a_run_with_nothing_to_pass_hands_the_board_straight_back(client, game, monkeypatch):
    """The replacement for the old "ran out of `max_seconds`" answer, and the
    core of what this call now is.

    bob has no priority to pass at alice's precombat main and nobody is moving.
    The old loop slept here, one `poll_seconds` at a time, until its deadline -
    a measured 6.0s at `max_seconds=6.0`, and an MCP client could ask for 570.
    Now it looks once, sees there is nothing it can do without somebody else
    acting, and returns: `finished: false`, a `resume` token, and a reason
    saying so. This asserts against wall-clock time deliberately - the whole
    defect was time spent inside one call."""
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    set_step(client, game, "precombat_main")

    started = time.monotonic()
    result = pass_until(bob, until_step="end_step", whose_turn="mine")
    elapsed = time.monotonic() - started

    assert elapsed < MAX_CALL_SECONDS, f"it waited {elapsed:.1f}s instead of returning"
    assert result["finished"] is False
    assert "you do not hold priority" in result["stopped_because"]
    assert "Call again" in result["stopped_because"]
    assert result["passes"] == 0
    assert result["resume"], "an unfinished run has to say how to continue it"


def test_a_run_returns_promptly_even_though_the_opponent_never_acts(client, game, monkeypatch):
    """The same property while the seat genuinely does have work to do: alice
    holds priority, passes it, and then - with no `Opponent` thread anywhere in
    this test - has nothing left to do but wait. It does not wait. This is the
    exact shape the finding measured at 6.0s."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    started = time.monotonic()
    result = pass_until(
        alice, until_step="end_step", whose_turn="mine", stop_when_you_can_attack=False
    )
    elapsed = time.monotonic() - started

    assert elapsed < MAX_CALL_SECONDS, f"it waited {elapsed:.1f}s for an opponent that never moved"
    assert result["finished"] is False
    assert result["passes"] >= 1, "it should still have passed the priority it did hold"
    assert result["state"]["priority_holder"] == "bob"


# -- the bounded wait (`max_seconds`) -----------------------------------------
#
# The owner's report: an agent seat busy-polling with `Bash sleep` between
# `pass_until` calls, because a run that hands the board back for the
# opponent had nothing to sit on but a hand-rolled sleep-then-call-again loop.
# `max_seconds` (capped at `seatkit.MAX_WAIT_SECONDS`) lets one call ride out
# that wait itself instead - `WAIT_POLL_SECONDS` is monkeypatched small here
# so the tests run fast without changing the mechanism under test.


def test_max_seconds_left_at_zero_still_hands_back_immediately(client, game, monkeypatch):
    """The default (`max_seconds=0.0`) must behave exactly like the existing,
    unbounded-wait-free `pass_until` always has - `test_a_run_with_nothing_
    to_pass_hands_the_board_straight_back` above, restated to pin that adding
    the parameter did not change what every existing caller (nothing passes
    it yet) gets."""
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    set_step(client, game, "precombat_main")

    started = time.monotonic()
    result = pass_until(bob, until_step="end_step", whose_turn="mine")
    elapsed = time.monotonic() - started

    assert elapsed < MAX_CALL_SECONDS
    assert result["finished"] is False
    assert "you do not hold priority" in result["stopped_because"]


def test_max_seconds_lets_one_call_ride_out_the_opponents_turn(client, game, monkeypatch):
    """The owner's ask, proven with a real opponent on a real thread: without
    `max_seconds`, a run that must wait for bob hands the board back and
    needs a separate call once he acts (`test_a_run_returns_promptly_even_
    though_the_opponent_never_acts` above, with no `Opponent` thread, is the
    same shape for the no-progress case). With `max_seconds` and a real
    `Opponent` actually passing on the other side, one call rides out the
    wait itself and reaches the destination without a second call."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    with Opponent(game, "bob"):
        started = time.monotonic()
        result = pass_until(
            alice,
            until_step="postcombat_main",
            whose_turn="mine",
            stop_when_you_can_attack=False,
            max_seconds=5.0,
        )
        elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"took {elapsed:.1f}s - should finish well inside the 5.0s bound"
    assert result["finished"] is True, (
        f"one bounded call, with the opponent really acting, should reach the "
        f"destination without a second call: {result}"
    )
    assert "reached" in result["stopped_because"], result["stopped_because"]


def test_max_seconds_still_hands_back_once_the_bound_runs_out(client, game, monkeypatch):
    """The other half: bounded, not indefinite. No `Opponent` thread here -
    bob never acts - so the call must give up once `max_seconds` elapses and
    hand back exactly as it always has, just later."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.05)
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    started = time.monotonic()
    result = pass_until(
        alice,
        until_step="postcombat_main",
        whose_turn="mine",
        stop_when_you_can_attack=False,
        max_seconds=0.4,
    )
    elapsed = time.monotonic() - started

    assert result["finished"] is False
    assert elapsed >= 0.4, f"gave up early at {elapsed:.2f}s"
    assert elapsed < 2.0, f"overshot the bound badly: {elapsed:.2f}s for max_seconds=0.4"
    assert "resume" in result


def test_a_non_retryable_hand_back_is_never_retried_by_the_bounded_wait(monkeypatch):
    """`MAX_PASSES_PER_CALL` hands the board back so the caller can look at
    it - a work cap, not a wait - and marks its own answer `retryable:
    False` (`handed_back`'s own comment) for exactly this reason: `max_seconds`
    must not swallow that and spin the same call over and over on its own.

    `_pass_until_once` (the one-shot logic `pass_until` wraps) is stubbed
    here rather than driven through a real game: what is under test is the
    wrapper's own retry decision, not any particular reason a hand-back can
    have, and a fake makes "exactly one non-retryable answer, nothing else"
    deterministic instead of hoping a live opponent never resolves it, which
    is exactly the class of test the no-mocks rule (engine-only) does not
    reach - this is the API's own harness code, not a card-rules test."""
    calls: list[Any] = []

    def fake_once(seat: Any, *, resume: Any = None, **kwargs: Any) -> dict[str, Any]:
        calls.append(resume)
        return {
            "stopped_because": "capped",
            "passes": 1,
            "finished": False,
            "until": {"step": "postcombat_main", "whose_turn": "mine"},
            "state": {},
            "available_actions": {},
            # T1: a real `_pass_until_once` call always carries this too
            # (`_fill_answer_extras` only fetches it when it is missing) -
            # this fake's own shape has to match or `pass_until` tries to
            # fetch it for real off a bare `SimpleNamespace` seat.
            "recent_log": {},
            "resume": {"step": "postcombat_main"},
            "retryable": False,
        }

    monkeypatch.setattr(seatkit, "_pass_until_once", fake_once)
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.01)

    started = time.monotonic()
    # `_pass_until_once` is faked out, so the only thing `pass_until` itself
    # reads off the seat is `advise` - the one guard that runs before the
    # retry loop (an advise seat is refused a run outright).
    result = pass_until(
        SimpleNamespace(advise=False), until_step="postcombat_main", max_seconds=2.0
    )
    elapsed = time.monotonic() - started

    assert result["finished"] is False
    assert len(calls) == 1, "a non-retryable hand-back must not be retried"
    assert elapsed < 0.5, f"took {elapsed:.2f}s - must return at once, not ride out max_seconds"
    assert "retryable" not in result, (
        "the internal retry marker must not leak into the public answer"
    )


def test_a_multi_call_run_still_stops_at_its_own_declare_attackers_step(client, game, monkeypatch):
    """What the `resume` token is for, and why `pass_until` cannot simply
    re-stamp itself on every call.

    `start_autopass` exempts the occurrence a run *began* in from every stop -
    otherwise "auto-pass past combat", asked from inside combat, would stop
    before it moved at all. Under one long blocking call that stamp was taken
    once. Under poll-and-return the run spans several calls, and a call that
    happened to begin while sitting on this seat's own `declare_attackers`
    pause would re-stamp the exemption onto it and pass straight through the
    attack - silently, which is the whole reason CR 508.1's stop exists
    (`autopass_stop_reason`'s own docstring). Carrying the token forward is
    what keeps it one run.

    alice runs from her upkeep to her postcombat main with every stop on; the
    run must stop at her own declare-attackers step no matter how many calls it
    took to get there."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    a_bear_that_can_attack(client, game, "alice")
    set_step(client, game, "upkeep")

    with Opponent(game, "bob"):
        result = run_to_stop(
            alice,
            until_step="postcombat_main",
            whose_turn="mine",
            # T2: both opt-in now - "every stop on" above means passing both.
            stop_when_attacked=True,
            stop_when_you_can_attack=True,
        )

    assert result["calls"] > 1, (
        "this test is only meaningful if the run really did span several calls"
    )
    assert "it is your declare-attackers step" in result["stopped_because"], result[
        "stopped_because"
    ]
    assert "CR 508.1" in result["stopped_because"]


# -- T1: the retry loop must not pay for hand-backs it throws away -----------


def test_pass_until_retries_fetch_available_actions_and_recent_log_once(client, game, monkeypatch):
    """Every retryable hand-back `pass_until`'s own bounded wait threw away
    and retried used to pay for a `GET .../legal-actions` and a `GET
    .../log` it never got to keep - `_pass_until_once`'s own `answer`/
    `handed_back` fetched both unconditionally, on every intermediate
    hand-back as well as the final one. Only the answer this call actually
    returns should ever pay for either (T1). bob never acts here, so this
    really does retry several times inside one bounded call before giving up
    at the deadline - that repetition is the whole point of the test."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.02)
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    once_calls: list[Any] = []
    real_once = seatkit._pass_until_once

    def counting_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
        once_calls.append(1)
        return real_once(*args, **kwargs)

    monkeypatch.setattr(seatkit, "_pass_until_once", counting_once)

    actions_calls: list[Any] = []
    real_available_actions = seatkit.available_actions

    def counting_available_actions(seat: Any, state: dict[str, Any]) -> dict[str, Any]:
        actions_calls.append(1)
        return real_available_actions(seat, state)

    monkeypatch.setattr(seatkit, "available_actions", counting_available_actions)

    log_calls: list[Any] = []
    real_recent_log = alice.recent_log

    def counting_recent_log(*args: Any, **kwargs: Any) -> dict[str, Any]:
        log_calls.append(1)
        return real_recent_log(*args, **kwargs)

    monkeypatch.setattr(alice, "recent_log", counting_recent_log)

    result = pass_until(
        alice,
        until_step="postcombat_main",
        whose_turn="mine",
        stop_when_you_can_attack=False,
        max_seconds=0.4,
    )

    assert result["finished"] is False, "bob never acts, so this is still waiting on him"
    assert len(once_calls) > 1, "this test is only meaningful if the run really did retry"
    assert len(actions_calls) == 1, (
        f"expected exactly one legal-actions fetch for the final answer, got "
        f"{len(actions_calls)} across {len(once_calls)} internal calls"
    )
    assert len(log_calls) == 1, (
        f"expected exactly one log fetch for the final answer, got "
        f"{len(log_calls)} across {len(once_calls)} internal calls"
    )


def test_pass_until_final_answer_always_carries_available_actions_and_recent_log(
    client, game, monkeypatch
):
    """T1's shape guarantee: whichever return path `pass_until` takes, the
    answer it actually hands back still carries `available_actions`/
    `recent_log` exactly as it always has - only the thrown-away intermediate
    hand-backs inside its own retry loop stop paying for them."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.02)
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    # A single, unretried call (`max_seconds=0.0`, the default) - unchanged.
    single = pass_until(
        alice, until_step="postcombat_main", whose_turn="mine", stop_when_you_can_attack=False
    )
    assert "available_actions" in single
    assert "recent_log" in single

    # bob never acts: a retryable hand-back that rides out the deadline.
    at_deadline = pass_until(
        alice,
        until_step="postcombat_main",
        whose_turn="mine",
        stop_when_you_can_attack=False,
        max_seconds=0.3,
    )
    assert at_deadline["finished"] is False
    assert "available_actions" in at_deadline
    assert "recent_log" in at_deadline


def test_pass_until_does_not_fetch_available_actions_for_a_game_gone_mid_retry(monkeypatch):
    """`_pass_until_once`'s very first check - before `run`/`passes`/`answer`
    exist at all - answers `GAME_GONE` with a bare dict that has never
    carried `available_actions`, regardless of `include_actions`: there is
    nothing left to enumerate for a game already known gone (404/410), so
    that path never asked. `_fill_answer_extras` must not read this bare
    dict's missing key as "extras this run skipped and I should go fetch" -
    a fake seat whose game disappears between `pass_until`'s own retries
    proves the final answer never grows `available_actions` and never pays
    for a `GET .../legal-actions` on a game already confirmed gone. No mock
    game or client needed - this is the API's own harness code, not a
    card-rules test, and what is under test is `pass_until`'s own retry
    plumbing, not any particular board."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.01)

    actions_calls: list[Any] = []
    real_available_actions = seatkit.available_actions

    def counting_available_actions(seat: Any, state: dict[str, Any]) -> dict[str, Any]:
        actions_calls.append(1)
        return real_available_actions(seat, state)

    monkeypatch.setattr(seatkit, "available_actions", counting_available_actions)

    class _GoneHTTP:
        def get(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(status_code=404, json=lambda: {"detail": "gone"}, text="gone")

    class SeatGoneMidRetry:
        advise = False
        player = "alice"
        uid = "seat-1"
        http = _GoneHTTP()

        def __init__(self) -> None:
            self.calls = 0
            self.last_state_status = 200

        def fetch_state(self) -> dict[str, Any]:
            # The first two fetches (this call's `before`, then the while
            # loop's own) find bob holding priority and nothing to do -
            # pass_until's own retry loop then sleeps and calls
            # `_pass_until_once` again, whose own first fetch (the third
            # overall) is where the game is found gone.
            self.calls += 1
            if self.calls >= 3:
                self.last_state_status = 404
                return {}
            self.last_state_status = 200
            return {
                "priority_holder": "bob",
                "step": "upkeep",
                "turn_number": 1,
                "pending_decision": None,
                "stack": [],
            }

        def recent_log(self) -> dict[str, Any]:
            return {"entries": [], "last_seq": 0, "omitted": 0}

        def game_id(self) -> str:
            return "gone-game-id"

    seat = SeatGoneMidRetry()
    result = pass_until(seat, until_step="postcombat_main", whose_turn="mine", max_seconds=0.3)

    assert result["finished"] is True
    assert result["stopped_because"] == seatkit.GAME_GONE
    assert actions_calls == [], "a game already known gone must never be asked for legal actions"
    assert "available_actions" not in result, (
        "this path never carried available_actions, with or without retries"
    )
    assert "recent_log" in result


# -- T3: an honest hand-back once the run has actually waited -----------------


def test_a_hand_back_that_never_waited_still_says_so(client, game, monkeypatch):
    """The `max_seconds=0.0` case: the wording `pass_until` has always used
    for a hand-back its own retry loop never got the chance to retry, which
    is still true for it (T3)."""
    seats, uids = wired(monkeypatch, game)
    bob = seats.seat_for(uids["bob"])
    set_step(client, game, "precombat_main")

    result = pass_until(bob, until_step="end_step", whose_turn="mine")

    assert result["finished"] is False
    assert "this call is not waiting for it" in result["stopped_because"]
    assert "Call again" in result["stopped_because"]


def test_a_hand_back_that_actually_waited_says_how_long_and_how_many(client, game, monkeypatch):
    """T3: once `pass_until`'s own bounded wait has genuinely retried at
    least once, the hand-back it finally returns must stop claiming "this
    call is not waiting for it" - that became false the moment this loop
    slept and tried again - and say instead how long this call waited and how
    many priorities it passed in total, summed across every internal retry
    rather than merely the last one's own count."""
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.02)
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    result = pass_until(
        alice,
        until_step="postcombat_main",
        whose_turn="mine",
        stop_when_you_can_attack=False,
        max_seconds=0.3,
    )

    assert result["finished"] is False
    assert "this call is not waiting for it" not in result["stopped_because"], (
        "false the moment this loop actually waited and retried"
    )
    assert "this call waited about" in result["stopped_because"]
    assert "Call again" in result["stopped_because"]
    assert result["passes"] >= 1, (
        "alice held priority at least once before bob's silence handed the run back"
    )


# -- naming a destination that cannot be run to ------------------------------


def test_an_unknown_step_is_refused_with_the_legal_ones_named():
    problem = seatkit.autopass_target_error("second_main", "mine")
    assert problem is not None
    assert "postcombat_main" in problem, "a caller who guessed wrong needs the real list"


def test_untap_and_cleanup_are_refused_with_the_reason():
    """No player receives priority in either (CR 502.4, CR 514.3), so a run
    could pass forever and never be given the chance to stop there."""
    for step in ("untap", "cleanup"):
        problem = seatkit.autopass_target_error(step, "mine")
        assert problem is not None
        assert "CR 502.4" in problem and "CR 514.3" in problem


def test_an_unknown_turn_owner_is_refused():
    problem = seatkit.autopass_target_error("upkeep", "theirs")
    assert problem is not None
    assert "mine, opponent, any" in problem


# -- both front ends, one loop ------------------------------------------------


def a_server_only_introspected() -> Seats:
    """A `Seats` whose tools are only ever read, never called - so nothing
    behind it has to reach an API. Same shape `test_front_end_parity.py` uses.
    """
    seats = Seats.__new__(Seats)
    seats.api = "http://testserver"
    seats._pinned_game_id = "unused"
    return seats


def fresh_game(client: TestClient) -> str:
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    assert response.status_code == 200, response.text
    return response.json()["game_id"]


def answer_text(raw: Any) -> str:
    """One tool answer as text, whichever wire shape it arrived in.

    A run that stops *on* a decision addressed to this seat now comes back as
    an `InputRequiredResult` (`mcp._tool_result`), not a `CallToolResult` - the
    CR 509.1 stop this parity test uses is exactly such a decision, so the
    comparison has to read both shapes rather than assume the plain one.
    """
    if isinstance(raw, InputRequiredResult):
        assert raw.request_state is not None
        return raw.request_state
    return raw.content[0].text


def drive_mcp(server: Any, uid: str) -> dict[str, Any]:
    """`pass_until` through the MCP tool, driven to its stop the way an agent
    must now drive it: hand `resume` back until `finished`."""
    resume = ""
    for _ in range(200):
        answer = json.loads(
            answer_text(
                asyncio.run(
                    server.call_tool(
                        "pass_until",
                        {
                            "player_uid": uid,
                            "step": "upkeep",
                            "whose_turn": "mine",
                            # T2: opt-in now - this run is the CR 509.1
                            # blocking-stop parity check, so it has to ask
                            # for that stop rather than lean on a default.
                            "stop_when_attacked": True,
                            "resume": resume,
                        },
                    )
                )
            )
        )
        if answer["finished"]:
            return answer
        resume = json.dumps(answer["resume"])
        time.sleep(FAST_POLL)
    raise AssertionError("the mcp front end never finished its run")


def drive_cli(capsys: Any, uid: str, game_id: str) -> dict[str, Any]:
    """The same run through `seat_cli pass-until`, driven the same way."""
    resume = ""
    for _ in range(200):
        argv = [
            "--uid",
            uid,
            "--game",
            game_id,
            "pass-until",
            "upkeep",
            "--whose-turn",
            "mine",
            # T2: opt-in now - see `drive_mcp`'s own comment.
            "--stop-when-attacked",
        ]
        if resume:
            argv += ["--resume", resume]
        rc = seat_cli.run(argv)
        printed = capsys.readouterr().out
        assert rc == 0, printed
        answer = json.loads(printed)
        if answer["finished"]:
            return answer
        resume = json.dumps(answer["resume"])
        time.sleep(FAST_POLL)
    raise AssertionError("the cli front end never finished its run")


def test_both_front_ends_stop_in_the_same_place_for_the_same_run(client, monkeypatch, capsys):
    """The parity half, in the style of `tests/test_front_end_parity.py`: the
    MCP tool and the CLI command run the same board to the same stop, because
    both call `seatkit.pass_until` rather than each carrying its own loop. The
    board is the CR 509.1 one, so what is being compared is the stop that costs
    a game if the two ever drift."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    answers = {}
    for front_end in ("mcp", "cli"):
        game_id = fresh_game(client)
        try:
            uids = seats_for(game_id)
            stock_libraries(client, game_id)
            stage_an_attack_on_bob(client, game_id)
            with Opponent(game_id, "alice"):
                if front_end == "mcp":
                    server = build_server(Seats(api="http://testserver", game_id=game_id))
                    answers[front_end] = drive_mcp(server, uids["bob"])
                else:
                    answers[front_end] = drive_cli(capsys, uids["bob"], game_id)
        finally:
            client.delete(f"/api/games/{game_id}")

    for front_end, answer in answers.items():
        assert "you are being attacked by 1 creature" in answer["stopped_because"], front_end
        # The declare-blockers step the question stands in - see
        # `test_a_run_stops_when_this_seat_is_being_attacked`.
        #
        # The two front ends name that block differently on purpose, and this
        # is the one place the difference shows: the CLI prints
        # `seatkit.pass_until`'s own `state` (a human at a terminal can scroll
        # a whole board), while the MCP answer carries the narrowed `board`
        # (`mcp._action_answer` - a seat's client could not read the full one).
        # The step itself has to agree, which is what this compares.
        board = answer.get("board") or answer["state"]
        assert board["step"] == "declare_blockers", front_end
        assert answer["until"] == {"step": "upkeep", "whose_turn": "mine"}, front_end


def test_both_front_ends_refuse_the_same_bad_destination(client, game, monkeypatch, capsys):
    """And they refuse it with the same words, from the same validator - a
    mistyped step must never take a long-lived MCP server down, and must never
    be silently treated as "no destination"."""
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    uids = seats_for(game)

    server = build_server(Seats(api="http://testserver", game_id=game))
    raw = asyncio.run(
        server.call_tool("pass_until", {"player_uid": uids["alice"], "step": "cleanup"})
    )
    from_mcp = raw.content[0].text

    rc = seat_cli.run(["--uid", uids["alice"], "--game", game, "pass-until", "cleanup"])
    from_cli = capsys.readouterr().out.strip()

    assert rc == 1, "a destination that cannot be reached is an error, not a no-op"
    assert from_mcp.strip() == from_cli
    assert "CR 514.3" in from_cli


#: The stack stop, opt-out (MCP parameter, CLI flag, off-switch): it defends
#: against something the caller cannot see going wrong, so it defaults on.
OPT_OUT_STOP = ("stop_on_stack", "--no-stop-on-stack")

#: T2: the two combat stops, opt-in now (MCP parameter, CLI flag, on-switch):
#: each costs a stop on every run through combat unless the caller actually
#: wants the safety net, so each defaults off and is switched *on*. Neither
#: front end may offer one the other does not.
OPT_IN_STOPS = (
    ("stop_when_attacked", "--stop-when-attacked"),
    ("stop_when_you_can_attack", "--stop-when-you-can-attack"),
)


def test_the_cli_and_the_tool_agree_on_which_stops_default_on_and_off():
    """T2: `stop_on_stack` still defaults on (opt-out); the two combat stops
    default off now (opt-in). Read off the live signature and the live
    parser, not off a list kept by hand, and pin that both front ends agree -
    a mismatch here is a seat that stops in one and sails through in the
    other for the exact same call."""
    import inspect

    server = build_server(a_server_only_introspected())
    tool = server._tool_manager._tools["pass_until"].fn
    parameters = inspect.signature(tool).parameters
    assert parameters["whose_turn"].default == "mine"

    sub = next(a for a in seat_cli.build_parser()._actions if getattr(a, "choices", None))
    flags = {
        option
        for action in sub.choices["pass-until"]._actions
        for option in getattr(action, "option_strings", ())
    }
    stop_on_stack, no_stop_on_stack = OPT_OUT_STOP
    assert parameters[stop_on_stack].default is True, f"{stop_on_stack} must default on"
    assert no_stop_on_stack in flags, f"the CLI cannot switch off {stop_on_stack}"
    for parameter, flag in OPT_IN_STOPS:
        assert parameters[parameter].default is False, f"{parameter} must default off (T2)"
        assert flag in flags, f"the CLI cannot switch on {parameter}, which the tool can"

    # And the tool exposes nothing switchable that the pairs above missed - a
    # fourth stop added to one front end only would be exactly this bug again.
    switchable = {name for name, parameter in parameters.items() if name.startswith("stop_")}
    assert switchable == {stop_on_stack} | {name for name, _ in OPT_IN_STOPS}


def test_the_tool_docstring_says_what_each_stop_costs():
    """The docstring is an agent's only instructions. A flag it does not
    understand is a seat that stops blocking without meaning to, or one that
    never learns it has to turn a stop on."""
    import inspect

    server = build_server(a_server_only_introspected())
    # Whitespace-normalised: these are sentences wrapped for a source file, and
    # a line break landing mid-phrase must not read as the phrase being gone.
    docstring = " ".join(
        (inspect.getdoc(server._tool_manager._tools["pass_until"].fn) or "").split()
    )

    assert "CR 509.1" in docstring, "the reason the stack stop cannot cover blocking"
    assert "block nothing and take the whole attack" in docstring
    assert "CR 508.1" in docstring, "the reason the stack stop cannot cover attacking"
    assert "attack with nothing that turn" in docstring
    assert "I am not attacking this turn" in docstring, (
        "switching the attackers stop off is a decision, not a speed setting"
    )
    assert "NEXT occurrence" in docstring
    assert "stopped_because" in docstring
    stop_on_stack, _ = OPT_OUT_STOP
    assert f"`{stop_on_stack}=false`" in docstring, f"{stop_on_stack} is undocumented"
    for parameter, _ in OPT_IN_STOPS:
        assert f"`{parameter}=" in docstring and "true" in docstring, f"{parameter} is undocumented"
        assert "off by default" in docstring or "default false" in docstring, (
            f"{parameter}'s new default is not explained"
        )
    # The bluffing half of the feature: auto-passing everything is itself a tell.
    assert "represents a trick" in docstring
    assert "pass by hand" in docstring


def test_the_two_briefs_tell_a_player_when_not_to_auto_pass():
    """The bluffing half is a documentation job, and it is half the feature: a
    seat that auto-passes every window tells the opponent it has nothing, just
    as reliably as one that never does. Both briefs an agent reads - the CLI
    seat's and the MCP driver's - have to carry it, and both have to carry
    CR 509.1's cost of switching the blocking stop off."""
    from pathlib import Path

    from magic_cards_model_api import ai_seat
    from mtg_agent.driver import prompt as prompt_module

    brief = ai_seat.load_brief()
    driver_prompt = Path(prompt_module.__file__).read_text()

    for text, name in ((brief, "ai_seat_brief.md"), (driver_prompt, "prompt.py")):
        flat = " ".join(text.split())
        assert "CR 509.1" in flat, f"{name} never says why the blocking stop exists"
        assert "block nothing and take the whole attack" in flat, name
        assert "CR 508.1" in flat, f"{name} never says why the attacking stop exists"
        assert "attack with nothing that turn" in flat, name
        assert "I am not attacking this turn" in flat, (
            f"{name} lets a seat read the attackers stop as a speed setting"
        )
        assert "stop you every turn" in flat, (
            f"{name} never warns that a run past combat stops every turn by default"
        )
        assert "represents a trick" in flat, f"{name} never says a pass can be a bluff"
        assert "NEXT occurrence" in flat, f"{name} never says naming this step means the next"


# -- the stack stop is a modifier, not a destination ---------------------------


def test_a_spell_cast_in_a_window_the_run_passed_through_hands_priority_back(
    client, game, monkeypatch
):
    """The owner's own example, end to end: "I'm passing until my
    pre-combat-main, but opponent casts a spell in my upkeep - I need to have a
    way to respond to it."

    The destination (a step) and the stack stop are two different kinds of
    thing, and this is why they combine rather than compete: the run is aimed
    at alice's own precombat main and must still hand the board back the moment
    bob's Lightning Bolt lands in her upkeep, with the bolt still on the stack
    and alice holding priority - because CR 117.4 only resolves it once *all*
    players have passed in succession, and alice's chance to respond is the
    pass she has not made yet.

    The response is taken for real, not merely asserted to be possible: alice
    casts her own bolt on top of his.
    """
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    bobs_mountain = next(card["id"] for card in board["battlefield"] if card["controller"] == "bob")
    alices_mountain = next(
        card["id"] for card in board["battlefield"] if card["controller"] == "alice"
    )
    bobs_bolt = board["hands"]["bob"][0]["id"]
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    cast = threading.Event()

    def bob_bolts_in_alices_upkeep() -> None:
        bob_client = TestClient(app)
        for _ in range(2000):
            board = bob_client.get(f"/api/games/{game}").json()
            if board.get("priority_holder") == "bob" and board.get("step") == "upkeep":
                bob_client.post(
                    f"/api/games/{game}/actions/cast",
                    json={
                        "player": "bob",
                        "card_id": bobs_bolt,
                        "targets": ["alice"],
                        "mana_sources": [bobs_mountain],
                    },
                )
                bob_client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
                cast.set()
                return
            time.sleep(FAST_POLL)

    thread = threading.Thread(target=bob_bolts_in_alices_upkeep, daemon=True)
    thread.start()
    try:
        result = run_to_stop(alice, until_step="precombat_main", whose_turn="mine")
    finally:
        thread.join(timeout=10)

    assert cast.is_set(), "bob never got the chance to cast, so nothing was tested"
    assert result["stopped_because"] == "something new went on the stack: Lightning Bolt"
    assert result["state"]["step"] == "upkeep", (
        "the run was aimed at precombat_main and must have stopped short of it"
    )
    assert [obj["name"] for obj in result["state"]["stack"]] == ["Lightning Bolt"]
    # Read from the settled board, not from the snapshot the stop happened to
    # carry, and it is a race rather than a nicety: bob's thread makes TWO
    # independent calls - the cast, then his own pass - and the run's last
    # poll can land between them. The stop is right either way (the bolt is on
    # the stack the instant the cast returns, and CR 117.4 makes alice's
    # unmade pass her whole chance to answer it), but `result["state"]` is
    # then the board as it stood while bob still held his own priority, and
    # this assertion read `priority_holder: "bob"` and failed - intermittently,
    # on CI, for a run in which nothing was actually wrong. `thread.join`
    # above has already returned, so both of bob's calls have landed and the
    # board below is settled: the property this defends - the run hands the
    # seat a board it can act on - is asserted without depending on which of
    # bob's two calls the poll fell between, and the response cast below
    # proves it for real.
    settled = state(client, game)
    assert settled["priority_holder"] == "alice", (
        "a stop that does not hand priority back is not a chance to respond"
    )

    alices_bolt = settled["hands"]["alice"][0]["id"]
    responded = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": alices_bolt,
            "targets": ["bob"],
            "mana_sources": [alices_mountain],
        },
    )
    assert responded.json()["allowed"] is True, responded.json()
    assert len(state(client, game)["stack"]) == 2, "alice's response is on top of bob's spell"


def test_the_stack_stop_combines_with_the_destination_rather_than_replacing_it(
    client, game, monkeypatch
):
    """The other half of "a modifier, not a tick of its own": with nothing
    landing on the stack, the same run with the same stack stop switched on
    still reaches the step it named. If the stack stop were a *destination*
    this run would have no destination left to reach."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    set_step(client, game, "upkeep")

    with Opponent(game, "bob"):
        result = run_to_stop(
            alice,
            until_step="precombat_main",
            whose_turn="mine",
            stop_on_stack=True,
        )

    assert result["stopped_because"] == "reached your precombat main"
    assert result["state"]["step"] == "precombat_main"


# -- never re-post a command while the game is paused on a decision -----------


def test_a_run_never_passes_at_a_game_frozen_on_another_seats_decision(client, monkeypatch):
    """A paused command is a game that no pass can move, and passing at it is
    actively destructive.

    The board here is the exact shape that produced the bug: on the real
    Devourer of Destiny board, alice passes in her upkeep, bob passes, and
    CR 117.4 resolves the trigger on top of the stack - which asks *alice* a
    question. bob's pass pauses on it, and a pause rolls the committed game
    back to before that attempt (`commands.py`). What a client reads used to
    be `priority_holder: bob` - stale; the read now carries the paused board's
    holder (battle 3, B3-08) - with a `pending_decision` addressed to alice.

    Whatever the holder reads, nobody may move a frozen game. A run that
    posted another `pass-priority` would pause again and store a *second*
    pause slot over the first - discarding the question alice was about to
    answer. So the run hands the board back instead. The half addressed to this seat is
    `test_a_decision_addressed_to_this_seat_stops_the_run`; this is the other
    seat's, which used to fall through every check and pass anyway.
    """
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: TestClient(app))
    info = boot_the_devourer(client)
    game_id = info["game_id"]
    alice_uid, bob_uid = info["seat_uids"]["alice"], info["seat_uids"]["bob"]
    try:
        reach_the_devourer_upkeep(client, info)
        assert (
            client.post(
                f"/api/games/{game_id}/actions/pass-priority", json={"player": "alice"}
            ).json()["allowed"]
            is True
        )
        paused = client.post(
            f"/api/games/{game_id}/actions/pass-priority", json={"player": "bob"}
        ).json()
        assert paused["paused"] is True, paused
        assert paused["pending_decision"]["player"] == "alice", paused

        board = client.get(f"/api/games/{game_id}", params={"uid": bob_uid}).json()
        assert board["priority_holder"] != "bob", "the paused read is not the stale board"
        assert board["pending_decision"]["player"] == "alice"

        bob = Seats(api="http://testserver", game_id=game_id).seat_for(bob_uid)
        result = pass_until(bob, until_step="end_step", whose_turn="any")

        assert result["passes"] == 0, (
            "not one command may be posted while the game is paused on a decision"
        )
        assert result["finished"] is False, "the run can be continued once alice answers"
        assert "alice" in result["stopped_because"], result["stopped_because"]
        assert "only they can answer" in result["stopped_because"], result["stopped_because"]

        # And the question alice was asked really did survive the run. Read
        # from alice's own view: `visibility.py` redacts the four cards she is
        # being shown out of bob's copy of the same decision, which is right.
        asked = client.get(f"/api/games/{game_id}", params={"uid": alice_uid}).json()
        answered = client.post(
            f"/api/games/{game_id}/decide",
            params={"uid": alice_uid},
            json={
                "player": "alice",
                "moved": [asked["pending_decision"]["cards"][0]],
                "top_order": None,
            },
        )
        assert answered.status_code == 200, answered.text
    finally:
        client.delete(f"/api/games/{game_id}")


# -- L1: an opponent's stack object this seat can only pass over --------------
#
# Prod game 9cec3866 (2026-09-23): the opponent's Karn and Bridge casts each
# stopped the seat's run and cost a full model inference (27-82s) although the
# seat's only legal moves were pass and concede. A stack stop now asks the
# seat's own legal-actions source (`available_actions`, the engine's
# enumeration) first, and passes on when nothing but pass/concede/a mana
# ability (CR 605.1a - it does nothing for a stack of the opponent's objects)
# is open to it. What it passed over is reported as `auto_passed`.


def bob_acts_then_passes(game: str, act: Any) -> tuple[threading.Thread, threading.Event]:
    """bob takes `act(bob_client)` once, the first time he holds priority,
    then passes every priority after it - the opponent the L1 runs need, who
    puts one object on the stack and then lets the game move on."""
    done = threading.Event()

    def loop() -> None:
        bob_client = TestClient(app)
        for _ in range(4000):
            board = bob_client.get(f"/api/games/{game}").json()
            if board.get("winner") or board.get("drawn"):
                return
            if board.get("priority_holder") != "bob":
                time.sleep(FAST_POLL)
                continue
            if not done.is_set():
                outcome = act(bob_client)
                assert outcome.get("allowed") is True, outcome
                done.set()
                continue
            bob_client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread, done


def bob_bolts_alice(client: TestClient, game: str) -> Any:
    """A real Mountain and Lightning Bolt for bob; returns the cast to run."""
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    board = state(client, game)
    mountain = next(c["id"] for c in board["battlefield"] if c["controller"] == "bob")
    bolt = board["hands"]["bob"][0]["id"]

    def cast(bob_client: TestClient) -> Any:
        return bob_client.post(
            f"/api/games/{game}/actions/cast",
            json={
                "player": "bob",
                "card_id": bolt,
                "targets": ["alice"],
                "mana_sources": [mountain],
            },
        ).json()

    return cast


def test_an_opponents_spell_you_can_only_pass_over_is_auto_passed(client, game, monkeypatch):
    """alice holds a Forest (a mana ability only) and a Lightning Bolt she has
    no red mana for: nothing she could do answers bob's bolt, so her run
    passes over it and reaches its destination - and says what it passed."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    cast = bob_bolts_alice(client, game)
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    set_step(client, game, "precombat_main")

    thread, done = bob_acts_then_passes(game, cast)
    try:
        result = run_to_stop(
            alice, until_step="end_step", whose_turn="mine", stop_when_you_can_attack=False
        )
    finally:
        thread.join(timeout=1)

    assert done.is_set(), "bob never cast, so nothing was tested"
    assert result["stopped_because"] == "reached your end step", result["stopped_because"]
    alice_life = next(p for p in result["state"]["players"] if p["username"] == "alice")["life"]
    assert alice_life == 17, "the bolt resolved on the way past"
    passed = result.get("auto_passed") or []
    assert len(passed) == 1 and "Lightning Bolt" in passed[0], passed
    assert "bob" in passed[0], "an auto-passed entry names whose object it was"


def test_an_opponents_ability_you_can_only_pass_over_is_auto_passed(client, game, monkeypatch):
    """The same for an activated ability: bob cracks a real Sunbaked Canyon
    to draw, alice has nothing to answer with, and the run carries on."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Sunbaked Canyon", "bob", "battlefield")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")["id"]
    canyon = find(board["battlefield"], "Sunbaked Canyon")["id"]
    set_step(client, game, "precombat_main")

    def crack(bob_client: TestClient) -> Any:
        return bob_client.post(
            f"/api/games/{game}/actions/activate",
            json={
                "player": "bob",
                "card_id": canyon,
                "ability_index": 1,
                "mana_sources": [mountain],
            },
        ).json()

    thread, done = bob_acts_then_passes(game, crack)
    try:
        result = run_to_stop(
            alice, until_step="end_step", whose_turn="mine", stop_when_you_can_attack=False
        )
    finally:
        thread.join(timeout=1)

    assert done.is_set(), "bob never activated, so nothing was tested"
    assert result["stopped_because"] == "reached your end step", result["stopped_because"]
    passed = result.get("auto_passed") or []
    assert len(passed) == 1 and "Sunbaked Canyon" in passed[0], passed


def test_an_activatable_ability_that_spends_mana_still_stops_the_run(client, game, monkeypatch):
    """alice's own Sunbaked Canyon and Mountain: `{1}, {T}, Sacrifice: Draw a
    card` is a real instant-speed move, paid for with mana, so bob's bolt
    stops her run exactly as a castable instant would."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    cast = bob_bolts_alice(client, game)
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Sunbaked Canyon", "alice", "battlefield")
    set_step(client, game, "precombat_main")

    thread, done = bob_acts_then_passes(game, cast)
    try:
        result = run_to_stop(alice, until_step="end_step", whose_turn="mine")
    finally:
        thread.join(timeout=1)

    assert done.is_set(), "bob never cast, so nothing was tested"
    assert result["stopped_because"] == "something new went on the stack: Lightning Bolt"
    assert result["state"]["priority_holder"] == "alice"
    assert not result.get("auto_passed")


def test_an_armed_script_that_applies_still_stops_the_run(client, game, monkeypatch):
    """Nothing but pass is open to alice, but an armed play applies with the
    bolt on the stack: arming is judged before any auto-pass, as before."""
    from magic_cards_model_api.playscripts import arming

    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    cast = bob_bolts_alice(client, game)
    set_step(client, game, "precombat_main")

    def judged(seat: Any, *_: Any, **__: Any) -> dict[str, Any]:
        board = seat.fetch_state()
        return {"applying": ["dsl:respond"] if board.get("stack") else [], "state": board}

    monkeypatch.setattr(arming, "validate_arm_request", lambda *a, **k: None)
    monkeypatch.setattr(arming, "arm", judged)

    thread, done = bob_acts_then_passes(game, cast)
    try:
        result = run_to_stop(
            alice,
            until_step="end_step",
            whose_turn="mine",
            arm=["dsl:respond"],
            arm_reasoning="respond to the bolt",
        )
    finally:
        thread.join(timeout=1)

    assert done.is_set(), "bob never cast, so nothing was tested"
    assert result["stopped_because"].startswith("an armed play now applies: dsl:respond")
    assert [obj["name"] for obj in result["state"]["stack"]] == ["Lightning Bolt"]


def _view(*actions: dict[str, Any], error: str | None = None) -> dict[str, Any]:
    view: dict[str, Any] = {"actions": list(actions)}
    if error is not None:
        view["actions_error"] = error
    return view


PASS_ROW = {"kind": "pass_priority"}
CONCEDE_ROW = {"kind": "concede"}
THEIRS = [{"id": "s1", "name": "Karn, the Great Creator", "controller": "bob"}]
BOARD = {"priority_holder": "alice"}


def test_only_pass_concede_and_mana_abilities_let_the_stack_stop_pass():
    view = _view(PASS_ROW, CONCEDE_ROW, {"kind": "activate_mana", "affordable": "yes"})
    assert seatkit.stack_stop_needs_you(THEIRS, view, BOARD, "alice") is False
    assert seatkit.stack_stop_needs_you(THEIRS, _view(), BOARD, "alice") is False


def test_an_unaffordable_cast_is_not_a_response():
    view = _view(PASS_ROW, {"kind": "cast", "affordable": "no"})
    assert seatkit.stack_stop_needs_you(THEIRS, view, BOARD, "alice") is False


def test_any_other_open_move_stops_for_the_stack():
    for row in (
        {"kind": "cast", "affordable": "yes"},
        {"kind": "cast", "affordable": "unknown"},
        {"kind": "activate", "affordable": "yes"},
        {"kind": "activate_off_zone", "affordable": "yes"},
        {"kind": "answer_decision"},
    ):
        view = _view(PASS_ROW, CONCEDE_ROW, row)
        assert seatkit.stack_stop_needs_you(THEIRS, view, BOARD, "alice") is True, row


def test_a_failed_legal_actions_read_stops_for_the_stack():
    view = _view(error="the legal-actions call timed out")
    assert seatkit.stack_stop_needs_you(THEIRS, view, BOARD, "alice") is True


def test_the_seats_own_stack_object_always_stops():
    mine = [{"id": "s2", "name": "Lightning Bolt", "controller": "alice"}]
    assert seatkit.stack_stop_needs_you(mine, _view(PASS_ROW), BOARD, "alice") is True
