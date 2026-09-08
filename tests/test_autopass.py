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
#: `max_seconds=6.0`, and its MCP schema let an agent ask for 570.
MAX_CALL_SECONDS = 3.0


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
    Lightning Bolt while alice is auto-passing toward her end step; her run must
    hand her back the board with the bolt still on the stack, not resolve it by
    passing through."""
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    add_card(client, game, "Mountain", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "bob", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")["id"]
    bolt = board["hands"]["bob"][0]["id"]
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

    with Opponent(game, "alice"):
        result = run_to_stop(bob, until_step="upkeep", whose_turn="mine")

    # The pause freezes the board on the step `advance`/`pass-priority` was
    # called *from* - CR 509.1's turn-based action never finished, so the
    # step pointer never moved past `declare_attackers` either (see
    # `stage_an_attack_on_bob`'s own note, and `autopass_stop_reason`'s
    # docstring in `seatkit.py`).
    assert result["state"]["step"] == "declare_attackers", result["stopped_because"]
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
        result = run_to_stop(alice, until_step="end_step", whose_turn="mine")

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
        )

    # Frozen on the step the pausing call was made *from* (`beginning_of_
    # combat`), not the one it paused trying to enter - CR 508.1's
    # turn-based action never finished, so the step pointer never moved
    # (see `seatkit.autopass_stop_reason`'s docstring).
    assert result["state"]["step"] == "beginning_of_combat", result["stopped_because"]
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
        )

    # See `test_a_run_stops_in_your_own_declare_attackers_step`'s note: the
    # pause freezes the board on `beginning_of_combat`, the step the call
    # was made from, not `declare_attackers` itself.
    assert result["state"]["step"] == "beginning_of_combat", result["stopped_because"]


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
        result = run_to_stop(bob, until_step="upkeep", whose_turn="mine")

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
    resolves it first for a different reason - see that test). A paused
    state freezes `state["step"]` at whatever it was *before* the pause
    (`seatkit.autopass_stop_reason`'s docstring), so the exemption this test
    checks is really keyed on `run["started_pending_kind"]` matching the live
    decision, not on the frozen `(turn_number, step)` pair alone - answering
    the decision is what actually leaves the occurrence, and it does that by
    changing `state["step"]` for real (frozen no longer), which is enough for
    `forget_starting_occurrence`'s own `(turn, step)` comparison to notice.
    """
    seats, uids = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    stock_libraries(client, game)
    a_bear_that_can_attack(client, game, "alice")
    set_step(client, game, "beginning_of_combat")
    client.post(f"/api/games/{game}/advance", json={"player": "alice"})
    started_in = alice.fetch_state()
    assert started_in["step"] == "beginning_of_combat"
    pending = started_in["pending_decision"]
    assert pending is not None and pending["kind"] == "declare_attackers", pending

    run = seatkit.start_autopass(started_in, "postcombat_main", "mine")
    assert seatkit.autopass_stop_reason(started_in, run, "alice") is None, (
        "the pause the run began already sitting on is exempt"
    )

    # Answer the pause for real - CR 508.1a's "if any" makes "nobody" legal -
    # which is what actually leaves the occurrence: `state["step"]` moves
    # from the frozen `beginning_of_combat` to the real `declare_attackers`.
    client.post(
        f"/api/games/{game}/actions/attack", params={"uid": uids["alice"]}, json={"attackers": []}
    )
    later = alice.fetch_state()
    assert later["step"] == "declare_attackers", "answering the pause should have moved the step"

    seatkit.forget_starting_occurrence(run, later)
    assert run["started_step"] is None, "the stamp survived the board leaving its occurrence"
    assert seatkit.autopass_stop_reason(started_in, run, "alice") is not None, (
        "with the stamp cleared, the very same pause must stop the run again"
    )


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
            "resume": {"step": "postcombat_main"},
            "retryable": False,
        }

    monkeypatch.setattr(seatkit, "_pass_until_once", fake_once)
    monkeypatch.setattr(seatkit, "WAIT_POLL_SECONDS", 0.01)

    started = time.monotonic()
    result = pass_until(object(), until_step="postcombat_main", max_seconds=2.0)
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
        result = run_to_stop(alice, until_step="postcombat_main", whose_turn="mine")

    assert result["calls"] > 1, (
        "this test is only meaningful if the run really did span several calls"
    )
    assert "it is your declare-attackers step" in result["stopped_because"], result[
        "stopped_because"
    ]
    assert "CR 508.1" in result["stopped_because"]


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
        # Frozen on `declare_attackers`, the step the pausing call was made
        # from - see `test_a_run_stops_when_this_seat_is_being_attacked`.
        assert answer["state"]["step"] == "declare_attackers", front_end
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


#: Every stop a caller may switch off, as (MCP parameter, CLI flag). Each
#: defends against something the caller cannot see going wrong, so each
#: defaults on, and neither front end may offer one the other does not.
SWITCHABLE_STOPS = (
    ("stop_on_stack", "--no-stop-on-stack"),
    ("stop_when_attacked", "--no-stop-when-attacked"),
    ("stop_when_you_can_attack", "--no-stop-when-you-can-attack"),
)


def test_the_cli_and_the_tool_default_every_stop_to_on():
    """Each stop defends against something a seat cannot see going wrong, so
    each defaults on in both front ends. Read off the live signature and the
    live parser, not off a list kept by hand."""
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
    for parameter, flag in SWITCHABLE_STOPS:
        assert parameters[parameter].default is True, f"{parameter} must default on"
        assert flag in flags, f"the CLI cannot switch off {parameter}, which the tool can"

    # And the tool exposes nothing switchable that the pairs above missed - a
    # fourth stop added to one front end only would be exactly this bug again.
    switchable = {
        name
        for name, parameter in parameters.items()
        if name.startswith("stop_") and parameter.default is True
    }
    assert switchable == {name for name, _ in SWITCHABLE_STOPS}


def test_the_tool_docstring_says_what_switching_each_stop_off_costs():
    """The docstring is an agent's only instructions. A flag it does not
    understand is a seat that stops blocking without meaning to."""
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
    for parameter, _ in SWITCHABLE_STOPS:
        assert f"`{parameter}=false`" in docstring, f"{parameter} is undocumented"
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
    back to before that attempt (`commands.py`), so what every client now reads
    is `priority_holder: bob` with a `pending_decision` addressed to alice.

    `priority_holder` is simply stale there: nobody holds priority in a frozen
    game. A run that trusts it posts another `pass-priority`, which pauses
    again and stores a *second* pause slot over the first - discarding the
    question alice was about to answer. So the run hands the board back
    instead. The half addressed to this seat is
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
        assert board["priority_holder"] == "bob", (
            "this test is only meaningful while the stale priority holder is bob"
        )
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
