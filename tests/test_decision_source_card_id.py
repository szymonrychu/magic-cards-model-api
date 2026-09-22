"""`pending_decision.source_card_id`: which object on the board is asking.

`source` is the asking card's printed name, and a name cannot tell two copies
of one card apart or anchor a question to the card that raised it. The engine
now stamps `Decision.source_id` - the id of the card or token asking, or the
card behind an ability on the stack, and `None` for a turn-based or pregame
question - and this repo carries it on the pending decision as
`source_card_id`.

Visibility follows the rest of the block: only the deciding seat (and an
advisor, which reads exactly its human's view) gets it, and even then an id
naming a card in a zone that viewer cannot see (CR 400.2: libraries and
hands are hidden zones; CR 402.3: "a player can't look at the cards in
another player's hand") is replaced by `None`.

Real boards, real Scryfall printings, played through the HTTP API.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from conftest import add_card, card_id, find, force_turn_position, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from magic_cards_model.machines.decisions import Decision, DecisionKind
from test_baseline_seat import battlefield_creature, into_declare_blockers
from test_card_split_labels import boot as boot_the_devourer
from test_card_split_labels import reach_the_devourer_upkeep
from test_decisions import advance_into_cleanup, stuff_hand

from magic_cards_model_api import commands, visibility
from magic_cards_model_api.decisions import PendingDecision
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.repository_postgres import _decision_from_json, _decision_to_json
from magic_cards_model_api.serialize import state_view
from magic_cards_model_api.store import store

#: The module, not the `FastAPI` object the package re-exports under the same
#: name - the paused-response tests patch its command runners.
app_module = importlib.import_module("magic_cards_model_api.app")


def seats_for(game_id: str) -> dict[str, str]:
    return {name: uid for uid, name in store.seat_uids(game_id).items()}


def play_the_shockland(client: TestClient, game_id: str) -> tuple[str, dict[str, Any]]:
    """Steam Vents from alice's hand in her main phase: "As this land enters,
    you may pay 2 life. If you don't, it enters tapped." - a `pay_life`
    question the land itself asks. Returns the land's id and the paused
    play-land response."""
    vents = card_id(client, game_id, "Steam Vents", "alice", "hand")
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, "alice")
    played = client.post(
        f"/api/games/{game_id}/actions/play-land",
        json={"player": "alice", "card_id": vents},
    )
    assert played.status_code == 200, played.text
    body = played.json()
    assert body.get("paused") is True, body
    assert body["pending_decision"]["kind"] == "pay_life", body
    return vents, body


def test_a_shocklands_pay_life_names_the_land(client, game) -> None:
    vents, body = play_the_shockland(client, game)
    uids = seats_for(game)

    assert body["pending_decision"]["source_card_id"] == vents
    pending = state(client, game, uid=uids["alice"])["pending_decision"]
    assert pending["source"] == "Steam Vents"
    assert pending["source_card_id"] == vents, (
        "the deciding seat is told which land is asking, not only its name"
    )


def test_the_advisor_reads_the_same_source_card_id_as_its_human(client, game) -> None:
    vents, _ = play_the_shockland(client, game)
    store.register_advisors(game, {"advisor-uid": Advisor(player="alice")})

    pending = state(client, game, uid="advisor-uid")["pending_decision"]
    assert pending["source_card_id"] == vents


def test_the_opponent_is_never_told_which_card_asks(client, game) -> None:
    """Bob sees that alice is deciding, never which object is asking - the
    rest of the block is withheld from him, and so is this."""
    vents, _ = play_the_shockland(client, game)
    uids = seats_for(game)

    bobs = state(client, game, uid=uids["bob"])["pending_decision"]
    assert bobs["player"] == "alice"
    assert bobs.get("source_card_id") is None, bobs

    alices = state(client, game, uid=uids["alice"])["pending_decision"]
    assert alices["source_card_id"] == vents


def test_a_fetchlands_search_names_the_fetch(client, game) -> None:
    """Asked while the ability resolves, after the fetch was sacrificed to pay
    for it - so the id names a card in a graveyard, a public zone."""
    uids = seats_for(game)
    add_card(client, game, "Wooded Foothills", "alice", "battlefield")
    add_card(client, game, "Forest", "alice", "library")
    foothills = find(state(client, game)["battlefield"], "Wooded Foothills")["id"]
    activated = client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": foothills, "ability_index": 0},
    )
    assert activated.status_code == 200, activated.text
    resolved = client.post(f"/api/games/{game}/resolve-top", params={"uid": uids["alice"]})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["pending_decision"]["kind"] == "choose_cards", resolved.json()

    pending = state(client, game, uid=uids["alice"])["pending_decision"]
    assert pending["source"] == "Wooded Foothills"
    assert pending["source_card_id"] == foothills


def test_the_cleanup_discard_has_no_source_card(client, game) -> None:
    """CR 514.1: a turn-based action, asked by no object."""
    stuff_hand(client, game, "alice", 8)
    uids = seats_for(game)
    paused = advance_into_cleanup(game)
    assert paused is not None

    pending = state(client, game, uid=uids["alice"])["pending_decision"]
    assert pending["kind"] == "choose_cards"
    assert "source_card_id" in pending, pending
    assert pending["source_card_id"] is None


def test_a_source_in_a_zone_the_viewer_cannot_see_is_withheld(client, game) -> None:
    """The deciding seat itself loses the id when the asking card sits in a
    hidden zone it cannot look at. A real board: alice's shockland question,
    with the land put back into her library (CR 400.2) and then into bob's
    hand (CR 402.3), redacted for alice each time."""
    vents, _ = play_the_shockland(client, game)
    view = state_view(store.get(game), game)
    assert view["pending_decision"]["source_card_id"] == vents
    alices = visibility.redact_view(view, "alice")["pending_decision"]
    assert alices["source_card_id"] == vents, "in her own hand: she can see it"

    in_library = _move_card(view, vents, into=("libraries", "alice"))
    assert visibility.redact_view(in_library, "alice")["pending_decision"]["source_card_id"] is None

    in_bobs_hand = _move_card(view, vents, into=("hands", "bob"))
    assert (
        visibility.redact_view(in_bobs_hand, "alice")["pending_decision"]["source_card_id"] is None
    )
    assert (
        visibility.redact_view(in_bobs_hand, visibility.ADMIN)["pending_decision"]["source_card_id"]
        == vents
    )


def _move_card(view: dict[str, Any], card: str, *, into: tuple[str, str]) -> dict[str, Any]:
    """`view` with the card whose id is `card` taken out of every per-player
    zone and the battlefield, and put into `into` (zone key, player)."""
    moved: dict[str, Any] = dict(view)
    found: dict[str, Any] | None = None
    for zone_key in ("hands", "libraries", "graveyards", "sideboards"):
        zones = {}
        for player, cards in view.get(zone_key, {}).items():
            kept = []
            for entry in cards:
                if entry["id"] == card:
                    found = entry
                else:
                    kept.append(entry)
            zones[player] = kept
        moved[zone_key] = zones
    moved["battlefield"] = [entry for entry in view.get("battlefield", []) if entry["id"] != card]
    for entry in view.get("battlefield", []):
        if entry["id"] == card:
            found = entry
    assert found is not None, f"{card} is on no zone of this view"
    zone_key, player = into
    moved[zone_key] = {**moved[zone_key], player: [*moved[zone_key].get(player, []), found]}
    return moved


def test_source_card_id_survives_the_postgres_pause_slot_round_trip() -> None:
    """The postgres store writes the paused question as hand-written JSON
    (`repository_postgres._decision_to_json`); a field it forgets is a field
    every state read after a restart silently loses."""
    source = str(uuid4())
    decision = Decision(
        kind=DecisionKind.PAY_LIFE,
        player="alice",
        prompt="you may pay 2 life",
        source="Steam Vents",
        source_id=source,
    )
    stored = json.loads(json.dumps(_decision_to_json(decision)))

    assert PendingDecision.from_engine(_decision_from_json(stored)).source_card_id == source


# -- the paused responses of the acting routes ---------------------------------
#
# `GET /api/games/{id}` is not the only door a pending decision leaves by: every
# acting route that pauses (`_respond`'s routes, `concede`, `pass-priority`,
# the two declaration aliases, `decide`) hands the new question straight back
# in its own response. Those copies obey the same rule the state view does -
# the rest of the block only for the deciding seat, and `source_card_id` never
# naming a card in a zone the caller cannot see.


def _alices_question_from(source_id: str) -> SimpleNamespace:
    """A paused result's `pending` as the app reads it (`result.pending
    .decision`): a real engine `Decision`, alice's, asked by `source_id`."""
    return SimpleNamespace(
        decision=Decision(
            kind=DecisionKind.CHOOSE_CARDS,
            player="alice",
            prompt="choose a card",
            source="Steam Vents",
            source_id=source_id,
            cards=[source_id],
        )
    )


def _pause_every_command_on(monkeypatch, pending: SimpleNamespace) -> None:
    """Every fresh command and every resumed one pauses on `pending`."""
    paused = commands.RunResult(status=commands.RunStatus.PAUSED, pending=pending)

    def run(game_id: str, kind: str, request: Any, **_: Any):  # noqa: ANN202
        return paused, store.get(game_id), None, None

    def resume(*args: Any, **kwargs: Any):  # noqa: ANN202
        return paused, None, None

    monkeypatch.setattr(app_module, "_run_command", run)
    monkeypatch.setattr(app_module, "_resume", resume)


def _is_the_opponents_shrink(pending: dict[str, Any], player: str) -> None:
    assert pending["player"] == player, pending
    assert set(pending) <= {"player", "kind", "prompt"}, (
        f"a seat that is not deciding gets {{player, kind, prompt}} and nothing else: {pending}"
    )


def test_bobs_pass_priority_never_names_the_card_asking_alice(client) -> None:
    """A real board: bob's pass resolves alice's Devourer of Destiny trigger,
    which asks *her* - and bob's own response to that pass carried the whole
    question, the four cards she is looking at and the id of the Devourer
    sitting in her hand (CR 402.3) included."""
    info = boot_the_devourer(client)
    game_id = info["game_id"]
    uids = info["seat_uids"]
    try:
        reach_the_devourer_upkeep(client, info)
        passed = client.post(
            f"/api/games/{game_id}/actions/pass-priority",
            params={"uid": uids["alice"]},
            json={"player": "alice"},
        )
        assert passed.json()["allowed"] is True, passed.json()
        paused = client.post(
            f"/api/games/{game_id}/actions/pass-priority",
            params={"uid": uids["bob"]},
            json={"player": "bob"},
        ).json()
        assert paused["paused"] is True, paused

        _is_the_opponents_shrink(paused["pending_decision"], "alice")
        alices = state(client, game_id, uid=uids["alice"])["pending_decision"]
        assert alices["source_card_id"] is not None, alices
    finally:
        client.delete(f"/api/games/{game_id}")


def test_an_acting_route_shrinks_a_question_that_is_not_the_callers(
    client, game, monkeypatch
) -> None:
    """`_respond`'s routes and `concede`: bob acts, the command pauses on a
    question for alice asked by a card in her hand."""
    vents = card_id(client, game, "Steam Vents", "alice", "hand")
    _pause_every_command_on(monkeypatch, _alices_question_from(vents))

    played = client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "bob", "card_id": vents}
    )
    assert played.status_code == 200, played.text
    _is_the_opponents_shrink(played.json()["pending_decision"], "alice")

    conceded = client.post(f"/api/games/{game}/actions/concede", json={"player": "bob"})
    assert conceded.status_code == 200, conceded.text
    _is_the_opponents_shrink(conceded.json()["pending_decision"], "alice")


def test_an_answer_that_resumes_into_the_other_seats_question_is_shrunk(
    client, game, monkeypatch
) -> None:
    """`decide` and `actions/declare-blockers`: bob answers his own
    declare_blockers question, and the resumed command pauses on alice's."""
    battlefield_creature(client, game, "Grizzly Bears", "alice")
    attacker = battlefield_creature(client, game, "Savannah Lions", "alice")
    into_declare_blockers(client, game, "alice", [{"card_id": attacker, "defender": "bob"}])
    vents = card_id(client, game, "Steam Vents", "alice", "hand")
    bob = seats_for(game)["bob"]
    _pause_every_command_on(monkeypatch, _alices_question_from(vents))

    decided = client.post(
        f"/api/games/{game}/decide", params={"uid": bob}, json={"player": "bob", "pairs": []}
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["accepted"] is True, decided.json()
    _is_the_opponents_shrink(decided.json()["pending_decision"], "alice")


def test_a_declaration_that_resumes_into_the_other_seats_question_is_shrunk(
    client, game, monkeypatch
) -> None:
    attacker = battlefield_creature(client, game, "Savannah Lions", "alice")
    into_declare_blockers(client, game, "alice", [{"card_id": attacker, "defender": "bob"}])
    vents = card_id(client, game, "Steam Vents", "alice", "hand")
    bob = seats_for(game)["bob"]
    _pause_every_command_on(monkeypatch, _alices_question_from(vents))

    blocked = client.post(
        f"/api/games/{game}/actions/declare-blockers",
        params={"uid": bob},
        json={"player": "bob", "blocks": []},
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["allowed"] is True, blocked.json()
    _is_the_opponents_shrink(blocked.json()["pending_decision"], "alice")


def test_an_acting_route_withholds_a_source_in_a_zone_the_actor_cannot_see(
    client, game, monkeypatch
) -> None:
    """Alice's own question, but asked by a card in her library (CR 400.2):
    she keeps the question and loses only the id. From her hand, she keeps
    both."""
    add_card(client, game, "Steam Vents", "alice", "hand")
    vents = card_id(client, game, "Steam Vents", "alice", "hand")
    add_card(client, game, "Island", "alice", "library")
    island = find(state(client, game)["libraries"]["alice"], "Island")["id"]

    _pause_every_command_on(monkeypatch, _alices_question_from(island))
    from_library = client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "alice", "card_id": vents}
    ).json()["pending_decision"]
    assert from_library["kind"] == "choose_cards", from_library
    assert from_library["cards"] == [island]
    assert from_library.get("source_card_id") is None, from_library

    _pause_every_command_on(monkeypatch, _alices_question_from(vents))
    from_hand = client.post(
        f"/api/games/{game}/actions/play-land", json={"player": "alice", "card_id": vents}
    ).json()["pending_decision"]
    assert from_hand["source_card_id"] == vents, from_hand
