"""T7a: the command runner (`magic_cards_model_api.commands`) that replaces
the blocking `Game.ask`/`DecisionMailbox` path (`decisions.py`) with
pause-and-resume replay against the engine's own `machines.decisions`
machinery.

Additive only - see `commands.py`'s own module docstring. No route is
touched here: every game is driven straight against the engine `Game` object
this API's own `store` already holds, the same object the old blocking
routes still drive unchanged.

Cards come from the engine's own bundled Scryfall printing corpus
(`magic-cards-model/src/magic_cards_model/data/printings`), pointed at
directly through `scryfall.fetch_card`'s pool tier
(`MCM_FIXTURE_CARDS_DIR`) rather than this repo's `tests/fixtures/scryfall`
cache, which does not carry Malevolent Rumble, Wall of Omens or Counterspell
- pointing there instead of leaving the default in place is what keeps this
module off the `tests/test_fixture_pool.py` real-network fallback (this
repo's `CLAUDE.md` names that trap explicitly) without adding a single new
file under a tracked fixtures directory.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from conftest import add_card
from magic_cards_model.machines.decisions import Answer, DecisionKind
from magic_cards_model.models import Game, Zone

from magic_cards_model_api.commands import (
    Command,
    CommandDivergenceError,
    RunStatus,
    resume_command,
    run_command,
)
from magic_cards_model_api.repository import InMemoryRepository
from magic_cards_model_api.schemas import ActivateRequest, CastRequest, PlayLandRequest
from magic_cards_model_api.store import store

#: The worktree this test runs in has the engine checked out (or symlinked)
#: as a sibling of this repo - `tests/test_command_runner.py` ->
#: `<worktree>/magic-cards-model-api/tests` -> parents[2] is `<worktree>`,
#: the same arithmetic `scryfall._SIBLING_ENGINE_ROOT` does from its own
#: module location.
_ENGINE_PRINTINGS = (
    Path(__file__).resolve().parents[2]
    / "magic-cards-model"
    / "src"
    / "magic_cards_model"
    / "data"
    / "printings"
)


@pytest.fixture(autouse=True)
def _engine_printing_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _ENGINE_PRINTINGS.is_dir(), f"expected the engine printing pool at {_ENGINE_PRINTINGS}"
    monkeypatch.setenv("MCM_FIXTURE_CARDS_DIR", str(_ENGINE_PRINTINGS))


def _forest_ids(game_obj, player: str = "alice") -> list[UUID]:
    """Battlefield is a shared zone (`Game.shared_zones`), not a per-player
    one - `Player` carries no `.battlefield` attribute at all."""
    return [
        state.id
        for state in game_obj.shared_zones[Zone.BATTLEFIELD]
        if state.name == "Forest" and state.controller == player
    ]


def _hand_card(game_obj, name: str, player: str = "alice"):
    return next(state for state in game_obj.player(player).hand if state.name == name)


def _malevolent_rumble_setup(client, game_id: str) -> None:
    """Two untapped Forests, Malevolent Rumble in hand, and a library whose
    top four hold two permanent cards (Grizzly Bears, the second Forest) and
    two non-permanents (Wall of Omens is itself a permanent too, kept as the
    named `CHOOSE_CARDS` answer; Counterspell is the one non-permanent) -
    mirrors the engine's own `_malevolent_rumble_board` in
    `tests/scenarios/test_decisions_during_resolution.py`.
    """
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Forest", "alice", "battlefield")
    add_card(client, game_id, "Malevolent Rumble", "alice", "hand")
    for name in ("Grizzly Bears", "Wall of Omens", "Forest", "Counterspell"):
        add_card(client, game_id, name, "alice", "library")


def _cast_malevolent_rumble(client, game_id: str):
    """Casts Malevolent Rumble through the command runner and returns the
    resulting committed `Game` (the spell now on the stack, unresolved)."""
    live = store.get(game_id)
    rumble = _hand_card(live, "Malevolent Rumble")
    cast_cmd = Command.build(
        "cast",
        CastRequest(player="alice", card_id=rumble.id, mana_sources=list(_forest_ids(live))),
    )
    result = run_command(live, cast_cmd)
    assert result.status is RunStatus.COMPLETED, result.outcome
    # `run_command` must never mutate `live` - a card keeps its own `.id`
    # across a zone move in this engine (CR 400.7's "new object" is marked by
    # `zone_change_stamp`, not a fresh `.id`), so the check that matters is
    # `live`'s own copy of the card never left the hand, not that the id is
    # unknown to it.
    assert live.find_by_id(rumble.id).zone.value == "hand"
    return result.game


# -- Command: the JSON round trip --------------------------------------------


def test_command_round_trips_through_json():
    request = CastRequest(
        player="alice",
        card="Grizzly Bears",
        mana_sources=[uuid4(), uuid4()],
        targets=["bob"],
    )
    command = Command.build("cast", request)

    blob = json.dumps(command.to_json())
    restored = Command.from_json(json.loads(blob))

    assert restored == command
    assert restored.payload["player"] == "alice"
    assert restored.payload["targets"] == ["bob"]


def test_command_from_json_tolerates_an_absent_payload():
    restored = Command.from_json({"kind": "resolve_top"})
    assert restored == Command(kind="resolve_top", payload={})


# -- a command that asks no decision -----------------------------------------


def test_a_command_with_no_decision_completes(client, game):
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    live = store.get(game)
    bears = _hand_card(live, "Grizzly Bears")

    cast_cmd = Command.build(
        "cast",
        CastRequest(player="alice", card_id=bears.id, mana_sources=list(_forest_ids(live))),
    )
    result = run_command(live, cast_cmd)

    assert result.status is RunStatus.COMPLETED
    assert result.pending is None
    assert result.outcome.allowed is True
    spell = result.game.find_by_id(UUID(result.outcome.detail["spell_id"]))
    assert spell is not None
    assert spell.zone.value == "stack"
    # `live`, the Game passed in, was never mutated - its own copy of the
    # card never left the hand, even though the card keeps the same `.id`
    # across the zone move (CR 400.7's "new object" is marked by
    # `zone_change_stamp`, not a fresh `.id`, in this engine).
    assert live.find_by_id(spell.id).zone.value == "hand"


def test_play_land_command_with_no_decision_completes(client, game):
    add_card(client, game, "Forest", "alice", "hand")
    live = store.get(game)
    forest = _hand_card(live, "Forest")

    command = Command.build("play_land", PlayLandRequest(player="alice", card_id=forest.id))
    result = run_command(live, command)

    assert result.status is RunStatus.COMPLETED
    assert result.outcome.allowed is True
    assert result.game.find_by_id(forest.id).zone.value == "battlefield"


def test_activate_ability_command_with_no_decision_completes(client, game):
    add_card(client, game, "Forest", "alice", "battlefield")
    live = store.get(game)
    forest = next(state for state in live.shared_zones[Zone.BATTLEFIELD] if state.name == "Forest")

    command = Command.build(
        "activate_ability",
        ActivateRequest(player="alice", card_id=forest.id, ability_index=0),
    )
    result = run_command(live, command)

    assert result.status is RunStatus.COMPLETED
    assert result.outcome.allowed is True
    assert result.game.find_by_id(forest.id).tapped is True


# -- a command that asks exactly one decision --------------------------------


def test_a_command_with_one_decision_pauses_then_resumes_to_completion(client, game):
    _malevolent_rumble_setup(client, game)
    committed = _cast_malevolent_rumble(client, game)

    resolve_cmd = Command(kind="resolve_top")
    first = run_command(committed, resolve_cmd)

    assert first.status is RunStatus.PAUSED
    assert first.game is None
    assert first.pending is not None
    assert first.pending.decision.kind is DecisionKind.MAY
    assert first.pending.decision.player == "alice"
    assert first.pending.decision.source == "Malevolent Rumble"
    assert first.pending.seq == 0

    decline = Answer(seq=0, kind=DecisionKind.MAY, player="alice", accept=False)
    resumed = resume_command(
        committed, resolve_cmd, tape=(), expected_pending=first.pending, new_answer=decline
    )

    assert resumed.status is RunStatus.COMPLETED
    assert resumed.outcome["resolved"] == "Malevolent Rumble"
    # declined: no permanent card was ever asked about, so alice's hand is
    # exactly what it was before the spell was cast.
    assert [state.name for state in resumed.game.player("alice").hand] == []


# -- a command that asks two decisions ---------------------------------------


def test_a_command_with_two_decisions_pauses_twice_then_resumes_to_completion(client, game):
    _malevolent_rumble_setup(client, game)
    committed = _cast_malevolent_rumble(client, game)
    wall = next(
        state for state in committed.player("alice").library if state.name == "Wall of Omens"
    )

    resolve_cmd = Command(kind="resolve_top")
    first = run_command(committed, resolve_cmd)
    assert first.status is RunStatus.PAUSED
    assert first.pending.decision.kind is DecisionKind.MAY

    accept = Answer(seq=0, kind=DecisionKind.MAY, player="alice", accept=True)
    second = resume_command(
        committed, resolve_cmd, tape=(), expected_pending=first.pending, new_answer=accept
    )

    assert second.status is RunStatus.PAUSED
    assert second.game is None
    assert second.pending is not None
    assert second.pending.decision.kind is DecisionKind.CHOOSE_CARDS
    assert second.pending.decision.player == "alice"
    assert second.pending.seq == 1
    assert wall.id in second.pending.decision.cards

    choose_wall = Answer(seq=1, kind=DecisionKind.CHOOSE_CARDS, player="alice", card_ids=(wall.id,))
    third = resume_command(
        committed,
        resolve_cmd,
        tape=(accept,),
        expected_pending=second.pending,
        new_answer=choose_wall,
    )

    assert third.status is RunStatus.COMPLETED
    assert third.outcome["resolved"] == "Malevolent Rumble"
    assert [state.name for state in third.game.player("alice").hand] == ["Wall of Omens"]


def test_the_two_answer_tape_replays_straight_through_to_completion(client, game):
    """The engine's own longest tape in any scenario test is 2 answers
    (Malevolent Rumble, `tests/scenarios/test_decisions_during_resolution.py
    :822-825`, a MAY followed by a CHOOSE_CARDS) - fed here up front, so
    `run_command` never pauses at all and both are consumed from the tape."""
    _malevolent_rumble_setup(client, game)
    committed = _cast_malevolent_rumble(client, game)
    wall = next(
        state for state in committed.player("alice").library if state.name == "Wall of Omens"
    )
    tape = (
        Answer(seq=0, kind=DecisionKind.MAY, player="alice", accept=True),
        Answer(seq=1, kind=DecisionKind.CHOOSE_CARDS, player="alice", card_ids=(wall.id,)),
    )

    resolve_cmd = Command(kind="resolve_top")
    result = run_command(committed, resolve_cmd, tape=tape)

    assert result.status is RunStatus.COMPLETED
    assert result.outcome["resolved"] == "Malevolent Rumble"
    assert [state.name for state in result.game.player("alice").hand] == ["Wall of Omens"]


# -- the equivalence claim the whole design rests on -------------------------


def test_the_paused_path_and_the_answers_up_front_path_reach_the_same_game(client, game):
    """A game played through the paused path (pause on the MAY, answer,
    resume; pause again on the CHOOSE_CARDS, answer, resume to completion)
    ends up in a state equal to the same game played by handing both answers
    to `run_command` up front - the claim the entire pause/resume design
    rests on. Compared via `model_dump(mode="json")`, both starting from the
    identical committed snapshot (`committed`, never mutated by either run).
    """
    _malevolent_rumble_setup(client, game)
    committed = _cast_malevolent_rumble(client, game)
    wall = next(
        state for state in committed.player("alice").library if state.name == "Wall of Omens"
    )
    accept = Answer(seq=0, kind=DecisionKind.MAY, player="alice", accept=True)
    choose_wall = Answer(seq=1, kind=DecisionKind.CHOOSE_CARDS, player="alice", card_ids=(wall.id,))
    resolve_cmd = Command(kind="resolve_top")

    # the paused path
    first = run_command(committed, resolve_cmd)
    second = resume_command(
        committed, resolve_cmd, tape=(), expected_pending=first.pending, new_answer=accept
    )
    paused_path = resume_command(
        committed,
        resolve_cmd,
        tape=(accept,),
        expected_pending=second.pending,
        new_answer=choose_wall,
    )
    assert paused_path.status is RunStatus.COMPLETED

    # the answers-up-front path, from the same committed snapshot
    straight_path = run_command(committed, resolve_cmd, tape=(accept, choose_wall))
    assert straight_path.status is RunStatus.COMPLETED

    assert paused_path.game.model_dump(mode="json") == straight_path.game.model_dump(mode="json")


# -- the replay-divergence guard ---------------------------------------------


def test_the_divergence_guard_rejects_a_stale_expected_pending(client, game):
    """Simulates the failure the guard exists to catch: the caller's stored
    `expected_pending` no longer matches what a fresh replay from the
    committed snapshot re-derives (here, forced by hand - a mutated `seq` -
    rather than by genuine engine nondeterminism, which is not something a
    test can safely manufacture). `resume_command` must refuse to accept
    `new_answer` onto the tape rather than silently feeding it to whatever
    the replay actually paused on.
    """
    _malevolent_rumble_setup(client, game)
    committed = _cast_malevolent_rumble(client, game)

    resolve_cmd = Command(kind="resolve_top")
    first = run_command(committed, resolve_cmd)
    assert first.status is RunStatus.PAUSED

    stale_pending = dataclasses.replace(first.pending, seq=first.pending.seq + 1)
    answer = Answer(seq=stale_pending.seq, kind=DecisionKind.MAY, player="alice", accept=True)

    with pytest.raises(CommandDivergenceError) as excinfo:
        resume_command(
            committed, resolve_cmd, tape=(), expected_pending=stale_pending, new_answer=answer
        )

    assert excinfo.value.status_code == 409
    assert "diverged" in str(excinfo.value)
    assert excinfo.value.expected == stale_pending


def test_the_divergence_guard_rejects_completion_where_a_pause_was_expected(client, game):
    """The other divergence shape: the guard replay runs to completion
    instead of pausing again at all - here forced by handing `resume_command`
    a tape that already answers the only question this command ever asks, so
    the guard's own replay (with that tape) never pauses a second time."""
    _malevolent_rumble_setup(client, game)
    committed = _cast_malevolent_rumble(client, game)

    resolve_cmd = Command(kind="resolve_top")
    first = run_command(committed, resolve_cmd)
    assert first.status is RunStatus.PAUSED

    decline = Answer(seq=0, kind=DecisionKind.MAY, player="alice", accept=False)
    with pytest.raises(CommandDivergenceError) as excinfo:
        resume_command(
            committed,
            resolve_cmd,
            tape=(decline,),  # already answers the only question - the guard completes
            expected_pending=first.pending,
            new_answer=decline,
        )

    assert excinfo.value.status_code == 409
    assert "completed instead of pausing again" in str(excinfo.value)


# -- the property this whole task exists to buy ------------------------------


def test_a_paused_decision_survives_a_simulated_process_restart(client, game):
    """A paused command's state lives entirely in the repository, not in any
    in-process object - no live provider, no parked thread, no mailbox (see
    this module's own docstring, and `repository.py`'s `InMemoryRepository`).
    So a process that pauses, dies, and comes back with nothing but the
    repository's own contents must still be able to resume correctly.

    Simulated here by round-tripping the paused game and command through
    plain JSON - `Command.to_json`/`from_json` and `Game.model_dump`/
    `model_validate` are both designed for exactly this (see `Command`'s own
    docstring: "that is what lets it become a postgres column later (T9)
    with no second serialisation layer of its own") - then discarding every
    other in-memory object this test built (`committed`, `first.pending`,
    the original `Game` instance) and resuming purely from the rehydrated
    copies against a brand new `InMemoryRepository`, standing in for a fresh
    process.

    The `PendingDecision` itself is not asserted to round-trip through JSON
    directly (T9's own concern, not this task's) - instead it is *re-derived*
    from the rehydrated game and command with an empty tape, which is exactly
    the same replay `resume_command`'s own divergence guard already performs
    on every resume, paused or not. Trusting that re-derivation here is not
    extra trust beyond what every resume already places in the replay
    guarantee.
    """
    _malevolent_rumble_setup(client, game)
    committed = _cast_malevolent_rumble(client, game)

    resolve_cmd = Command(kind="resolve_top")
    first = run_command(committed, resolve_cmd)
    assert first.status is RunStatus.PAUSED
    assert first.pending is not None
    assert first.pending.decision.kind is DecisionKind.MAY

    # Everything a real process would have to persist for this pause to
    # survive a restart - captured as plain JSON, round-tripped through
    # `json.dumps`/`json.loads` for good measure, the same shape a postgres
    # row (T9) would hold.
    game_blob = json.loads(json.dumps(committed.model_dump(mode="json")))
    command_blob = json.loads(json.dumps(resolve_cmd.to_json()))

    # "The process restarts": nothing but the two JSON blobs above survives -
    # not `committed`, not `first`, not this test's own `resolve_cmd` object
    # (a fresh, equal-but-not-identical one is rebuilt from `command_blob`) -
    # and a brand new repository is what a real restart would boot with too.
    fresh_repo = InMemoryRepository()
    restarted_game = Game.model_validate(game_blob)
    restarted_command = Command.from_json(command_blob)
    assert restarted_command == resolve_cmd

    fresh_game_id = fresh_repo.create(restarted_game)

    # Re-derive the pause from the rehydrated game and command - the same
    # replay `resume_command`'s own divergence guard performs on every
    # resume (see this test's own docstring).
    reproved = run_command(restarted_game, restarted_command, tape=())
    assert reproved.status is RunStatus.PAUSED
    assert reproved.pending is not None
    assert reproved.pending.decision.kind is DecisionKind.MAY
    assert reproved.pending.decision.player == first.pending.decision.player
    assert reproved.pending.decision.source == first.pending.decision.source
    assert reproved.pending.seq == first.pending.seq
    fresh_repo.store_paused_command(fresh_game_id, restarted_command, (), reproved.pending)

    # Answer it for real, entirely against the fresh repository - the MAY
    # question is Malevolent Rumble's first of two (see
    # `test_a_command_with_two_decisions_pauses_twice_then_resumes_to_completion`),
    # so accepting pauses again, on the CHOOSE_CARDS that follows it.
    accept = Answer(seq=0, kind=DecisionKind.MAY, player="alice", accept=True)
    stored_command, stored_tape, stored_pending = fresh_repo.paused_command(fresh_game_id)
    with fresh_repo.locked(fresh_game_id) as game_obj:
        resumed = resume_command(
            game_obj,
            stored_command,
            stored_tape,
            expected_pending=stored_pending,
            new_answer=accept,
        )
        if resumed.status is RunStatus.COMPLETED:
            fresh_repo.replace(fresh_game_id, resumed.game)
            fresh_repo.clear_paused_command(fresh_game_id)
        else:
            fresh_repo.store_paused_command(
                fresh_game_id, stored_command, resumed.tape, resumed.pending
            )

    assert resumed.status is RunStatus.PAUSED
    assert resumed.pending is not None
    assert resumed.pending.decision.kind is DecisionKind.CHOOSE_CARDS
    assert resumed.pending.decision.player == "alice"

    # And it can be driven the rest of the way to completion, still entirely
    # within the "restarted" process's own fresh repository.
    wall = next(
        state
        for state in fresh_repo.get(fresh_game_id).player("alice").library
        if state.name == "Wall of Omens"
    )
    choose_wall = Answer(seq=1, kind=DecisionKind.CHOOSE_CARDS, player="alice", card_ids=(wall.id,))
    with fresh_repo.locked(fresh_game_id) as game_obj:
        final = resume_command(
            game_obj,
            stored_command,
            (accept,),
            expected_pending=resumed.pending,
            new_answer=choose_wall,
        )

    assert final.status is RunStatus.COMPLETED
    assert final.outcome["resolved"] == "Malevolent Rumble"
    assert [state.name for state in final.game.player("alice").hand] == ["Wall of Omens"]
