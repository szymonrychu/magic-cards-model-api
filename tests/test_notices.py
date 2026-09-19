"""The notices defect: a fail-closed clause the engine recognised and
refused used to resolve silently, with nothing in a seat's own result or the
game log saying so - only `Game`'s own 19 notice lists
(`models/game.py:1148-1306`) carried the record, and nothing in this api ever
read one back out.

`commands.run_command` is the single choke point every mutating command
already replays through (T7b) - `_new_notices`/`_attach_notices` diff each of
those 19 lists before and after one command's handler runs and file whatever
changed onto that command's own `RunResult.outcome`, generically, with no
per-handler wiring. `app.audit_outcome` and the four routes that build their
own response by hand (`pass_priority`, `advance`, `resolve_top`,
`state_based_actions`) are what carries that onto the HTTP response and the
persisted log row.

Cards come from the engine's own bundled Scryfall printing corpus, the same
way `test_command_runner.py` reaches them - see that module's own docstring
for why (`MCM_FIXTURE_CARDS_DIR`, not this repo's smaller `tests/fixtures/
scryfall` cache).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from conftest import add_card
from magic_cards_model.machines.effects import LayerRemainderNotice
from magic_cards_model.machines.opening_hand import OpeningHandNotice
from magic_cards_model.machines.triggers import TargetingNotice, TargetingNoticeKind
from magic_cards_model.models import Game, Zone

from magic_cards_model_api import commands, engine_api, visibility
from magic_cards_model_api.commands import Command, RunStatus, run_command
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import run_action
from magic_cards_model_api.store import store

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


def _ids(game_obj: Game, name: str, player: str = "alice") -> list[UUID]:
    return [
        state.id
        for state in game_obj.shared_zones[Zone.BATTLEFIELD]
        if state.name == name and state.controller == player
    ]


def _hand_card(game_obj: Game, name: str, player: str = "alice"):
    return next(state for state in game_obj.player(player).hand if state.name == name)


# -- a fail-closed clause reaches the seat's own result and the log ---------


def test_a_mode_this_wave_cannot_read_reaches_the_seats_own_result_and_the_log(
    client, game, monkeypatch
):
    """Cryptic Command modes [1, 3] (`test_modal_spells.py`'s own fixture in
    the engine repo): mode 1 ("Counter target spell.") is a shape
    `resolve_modal_spell` reads; mode 3 ("Tap all creatures your opponents
    control.") is not, since it names no target at all, and is recorded as a
    `ModeNotice` (`Game.mode_notices`) instead of silently doing nothing.

    Battle 3, B3-05: the cast route now refuses a cast choosing such a mode,
    with that notice's own words, before anything is paid - so the card is not
    spent for nothing (`engine_api.unreadable_modes`). That refusal is the
    first half here. The second half is the resolution the notice pipeline
    still carries for any spell the cast guard does not stop, driven with the
    guard switched off: Cryptic Command still counters the bolt (mode 1 ran),
    and nothing about the response says mode 3 was skipped unless `notices`
    says so.
    """
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Cryptic Command", "alice", "hand")

    live = store.get(game)
    bolt = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Lightning Bolt",
            "targets": ["bob"],
            "mana_sources": [str(i) for i in _ids(live, "Mountain")],
        },
    )
    assert bolt.status_code == 200, bolt.text
    bolt_body = bolt.json()
    assert bolt_body["allowed"] is True
    assert bolt_body.get("notices", []) == [], "a plain Bolt cast reads its own text cleanly"
    bolt_spell_id = bolt_body["spell_id"]

    live = store.get(game)
    refused = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Cryptic Command",
            "modes": [1, 3],
            "targets": [bolt_spell_id],
            "mana_sources": [str(i) for i in _ids(live, "Island")],
        },
    ).json()
    assert refused["allowed"] is False, refused
    assert "mode 3" in refused["reason"] and "CR 700.2a" in refused["reason"], refused
    live = store.get(game)
    assert _hand_card(live, "Cryptic Command").zone is Zone.HAND
    assert not any(
        state.tapped for state in live.shared_zones[Zone.BATTLEFIELD] if state.name == "Island"
    ), "nothing was paid for the refused cast"
    cast_rows = [
        row for row in client.get(f"/api/games/{game}/log").json()["log"] if row["action"] == "cast"
    ]
    assert cast_rows[-1]["detail"]["reason"] == refused["reason"]

    monkeypatch.setattr(engine_api, "unreadable_modes", lambda card: [])
    live = store.get(game)
    cryptic = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Cryptic Command",
            "modes": [1, 3],
            "targets": [bolt_spell_id],
            "mana_sources": [str(i) for i in _ids(live, "Island")],
        },
    )
    assert cryptic.status_code == 200, cryptic.text
    cryptic_body = cryptic.json()
    assert cryptic_body["allowed"] is True
    assert cryptic_body.get("notices", []) == [], (
        "nothing has resolved yet - the cast itself is clean"
    )

    resolved = client.post(f"/api/games/{game}/resolve-top", json={"player": "alice"})
    assert resolved.status_code == 200, resolved.text
    resolved_body = resolved.json()
    assert resolved_body["resolved"] == "Cryptic Command"

    (notice,) = resolved_body["notices"]
    assert notice["kind"] == "mode_notices"
    assert notice["mode_number"] == 3
    assert notice["source_name"] == "Cryptic Command"
    # Cryptic Command is an instant: CR 608.2m sends it to its owner's
    # graveyard the instant it finishes resolving - a public zone, so this
    # notice is not blanked for anyone.
    assert notice["zone"] == "graveyard"

    # The bolt was in fact countered - mode 1 ran even though mode 3 did not.
    live = store.get(game)
    assert live.find_by_id(UUID(bolt_spell_id)).zone.value == "graveyard"

    log = client.get(f"/api/games/{game}/log").json()["log"]
    resolve_row = next(row for row in log if row["action"] == "resolve_top")
    (logged_notice,) = resolve_row["detail"]["notices"]
    assert logged_notice["mode_number"] == 3
    assert logged_notice["source_name"] == "Cryptic Command"


def test_an_unread_spells_notice_reaches_the_casters_own_log_after_the_opponents_pass(client, game):
    """Stock Up resolved as a silent no-op in a live game: `effect_for` had
    no reader for its text, the spell resolved anyway, and nothing reached
    the human who cast it. The gap was not the notice itself - `Game.resolve`
    already recorded an `UnmodeledTriggerEffectNotice` - it was the path: the
    resolution that raised it was BOB's pass, not alice's own cast, and
    `action_pass_priority` attaches `notices` only to the response of
    whoever called it (`visibility.redact_notices(notices, req.player)`,
    `req.player == "bob"` here). A caster whose own client only ever reads
    the response to her own requests never sees it that way.

    Ponder is the still-unread vehicle on this engine build - see the
    engine's own `tests/scenarios/test_unread_spell_text_fails_closed.py`,
    whose docstring names it as the replacement vehicle now that Stock Up's
    fix (this hotfix's whole reason to exist) has landed. `GET .../log`
    polled with alice's own seat uid - the path `ui/app.js` actually polls,
    never `pass-priority`'s own response - is where this pins the notice
    reaching her.
    """
    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Ponder", "alice", "hand")
    live = store.get(game)
    island_id = str(_ids(live, "Island")[0])

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": "Ponder", "mana_sources": [island_id]},
    )
    assert cast.status_code == 200, cast.text
    assert cast.json()["allowed"] is True
    assert cast.json().get("notices", []) == [], "nothing has resolved yet - the cast is clean"

    uids = {name: uid for uid, name in store.seat_uids(game).items()}

    # CR 117.3b: the caster receives priority right back after casting, so
    # alice's own pass here is expected and carries no notices - it is BOB's
    # pass, on the empty side of the table, that resolves Ponder. That is the
    # part the live game reproduced: the human never calls pass-priority on
    # her own spell, her opponent does.
    alice_pass = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    assert alice_pass.status_code == 200, alice_pass.text
    assert alice_pass.json().get("notices", []) == []

    passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert passed.status_code == 200, passed.text
    assert passed.json().get("notices"), "bob's own pass is what resolved the spell"

    log = client.get(f"/api/games/{game}/log", params={"uid": uids["alice"]}).json()["log"]
    resolve_row = next(
        row
        for row in log
        if row["action"] == "pass_priority" and row.get("detail", {}).get("notices")
    )
    (notice,) = resolve_row["detail"]["notices"]
    assert notice["kind"] == "unmodeled_trigger_effect_notices"
    assert notice["source_name"] == "Ponder"
    assert "top three cards" in notice["text"].lower()
    # a resolved instant/sorcery's graveyard is public - alice's own read is
    # not redacted down to "<hidden>" the way an opponent's hand card is.
    assert notice["source_name"] != "<hidden>"


def test_a_spell_that_legitimately_does_nothing_carries_no_notices(client, game):
    """Lightning Bolt has one clause, and it is one this wave reads in
    full - resolving it must not manufacture a notice out of nothing."""
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    live = store.get(game)
    client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card": "Lightning Bolt",
            "targets": ["bob"],
            "mana_sources": [str(i) for i in _ids(live, "Mountain")],
        },
    )

    resolved = client.post(f"/api/games/{game}/resolve-top", json={"player": "alice"})
    body = resolved.json()
    assert body["resolved"] == "Lightning Bolt"
    assert body.get("notices", []) == []

    log = client.get(f"/api/games/{game}/log").json()["log"]
    resolve_row = next(row for row in log if row["action"] == "resolve_top")
    assert resolve_row["detail"].get("notices", []) == []


# -- capture: a notice's source card's zone/controller, at capture time -----


def test_a_notice_naming_a_card_in_hand_is_captured_with_that_zone_and_controller(
    monkeypatch, client, game
):
    """`commands._notice_source` has to read the source's zone/controller
    while `run_command`'s `working` copy is still a live `Game` - this
    registers a throwaway command handler that appends a real
    `TargetingNotice` about a card genuinely sitting in alice's hand, the
    same way a real handler appends one mid-resolution, to prove the capture
    itself (no real printing in the pool triggers a hidden-zone notice - see
    the report)."""
    add_card(client, game, "Forest", "alice", "hand")
    live = store.get(game)
    forest = _hand_card(live, "Forest")

    def _probe(g: Game, payload: dict) -> dict:
        g.targeting_notices.append(
            TargetingNotice(
                kind=TargetingNoticeKind.NO_LEGAL_TARGET,
                source_id=forest.id,
                source_name="Forest",
                text="test probe",
            )
        )
        return {}

    monkeypatch.setitem(commands.COMMAND_HANDLERS, "test_notice_probe", _probe)
    result = run_command(live, Command(kind="test_notice_probe", payload={}))

    assert result.status is RunStatus.COMPLETED
    (notice,) = result.outcome["notices"]
    assert notice["zone"] == "hand"
    assert notice["controller"] == "alice"


def test_an_opening_hand_notice_is_captured_as_hand_with_no_controller(monkeypatch, client, game):
    """`OpeningHandNotice` carries no `source_id` at all - `commands.
    _new_notices` has nothing to look up, and fails closed: `zone="hand"`
    (CR 103.6's whole premise), `controller=None` (unknown), which
    `visibility.redact_notices` then blanks for every viewer but `ADMIN`."""
    live = store.get(game)

    def _probe(g: Game, payload: dict) -> dict:
        g.opening_hand_notices.append(
            OpeningHandNotice(card_name="Some Card", text="test text", reason="test reason")
        )
        return {}

    monkeypatch.setitem(commands.COMMAND_HANDLERS, "test_opening_hand_probe", _probe)
    result = run_command(live, Command(kind="test_opening_hand_probe", payload={}))

    assert result.status is RunStatus.COMPLETED
    (notice,) = result.outcome["notices"]
    assert notice["kind"] == "opening_hand_notices"
    assert notice["zone"] == "hand"
    assert notice["controller"] is None


def test_a_layer_remainder_notice_reaches_the_seats_own_result(monkeypatch, client, game):
    """`LayerRemainderNotice` (the Reality Fracture bump, engine 03a891c) is
    `Game`'s newest fail-closed list - `_compose_layer_effect_with_remainder`
    records it when a resolving spell's layer effect reads fine but a sibling
    sentence beside it (Academic Ascent's own "Empower Jace 2.") does not,
    the same "unreadable is not the same as no effect" record every other
    list already keeps. `commands._NOTICE_LIST_NAMES` has to name the field
    for `_new_notices`'s generic diff to ever see one - this is the
    regression test for that wiring, built the same white-box way
    `test_a_notice_naming_a_card_in_hand_is_captured_with_that_zone_and_controller`
    proves `targeting_notices`'s own capture, since no printing in the pool
    is guaranteed to stay unreadable as the engine's own parsers keep
    improving."""
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    live = store.get(game)
    bears = next(
        state for state in live.shared_zones[Zone.BATTLEFIELD] if state.name == "Grizzly Bears"
    )

    def _probe(g: Game, payload: dict) -> dict:
        g.layer_remainder_notices.append(
            LayerRemainderNotice(
                source_id=bears.id, source_name="Grizzly Bears", text="Empower Jace 2."
            )
        )
        return {}

    monkeypatch.setitem(commands.COMMAND_HANDLERS, "test_layer_remainder_probe", _probe)
    result = run_command(live, Command(kind="test_layer_remainder_probe", payload={}))

    assert result.status is RunStatus.COMPLETED
    (notice,) = result.outcome["notices"]
    assert notice["kind"] == "layer_remainder_notices"
    assert notice["text"] == "Empower Jace 2."
    assert notice["zone"] == "battlefield"
    assert notice["controller"] == "alice"


# -- reaching an AI seat ------------------------------------------------------


def test_an_unread_spells_notice_reaches_the_casters_own_seat(client, game, monkeypatch) -> None:
    """The AI-seat side of the same gap: `seatkit.Seat.recent_log` rides
    along on `battlefield_state`/`run_action`/`pass_until` (`test_seat_
    round_trips.py`'s whole subject), and its headline fields
    (`LOG_HEADLINE_FIELDS`) keep `detail` whole - `notices` included -
    rather than dropping it, so an unread spell's notice needs no seat-side
    change to reach a seat the way it needs a UI change to reach a human.
    This pins that it actually does, for a real unread spell (Ponder, the
    engine's own replacement vehicle - see the sibling API test above)
    rather than a notice this suite manufactures by hand, and the same shape
    the live bug had: BOB's pass is what resolves it, not alice's own cast."""
    from test_seat_round_trips import call, wired

    add_card(client, game, "Island", "alice", "battlefield")
    add_card(client, game, "Ponder", "alice", "hand")
    live = store.get(game)
    island_id = str(_ids(live, "Island")[0])
    uids = {name: uid for uid, name in store.seat_uids(game).items()}

    seats = wired(monkeypatch, game)
    alice = seats.seat_for(uids["alice"])
    cast_result = run_action(
        alice,
        lambda: alice.call_json(
            "POST",
            f"/api/games/{game}/actions/cast",
            {"player": "alice", "card": "Ponder", "mana_sources": [island_id]},
        ),
    )
    assert cast_result["outcome"]["allowed"] is True
    assert not any(
        entry.get("detail", {}).get("notices") for entry in cast_result["recent_log"]["entries"]
    ), "nothing has resolved yet"

    client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "alice"})
    passed = client.post(f"/api/games/{game}/actions/pass-priority", json={"player": "bob"})
    assert passed.json().get("notices"), "bob's own pass is what resolved the spell"

    server = build_server(seats)
    answer = call(server, "battlefield_state", {"player_uid": uids["alice"]})
    notice_rows = [
        entry for entry in answer["recent_log"]["entries"] if entry.get("detail", {}).get("notices")
    ]
    assert notice_rows, answer["recent_log"]
    (notice,) = notice_rows[-1]["detail"]["notices"]
    assert notice["kind"] == "unmodeled_trigger_effect_notices"
    assert notice["source_name"] == "Ponder"


# -- redaction ---------------------------------------------------------------


def _hand_notice(controller: str | None = "alice") -> dict:
    return {
        "kind": "targeting_notices",
        "source_id": "11111111-1111-1111-1111-111111111111",
        "source_name": "Forest",
        "text": "a card in a hand",
        "zone": "hand",
        "controller": controller,
    }


def _public_notice() -> dict:
    return {
        "kind": "mode_notices",
        "source_id": "22222222-2222-2222-2222-222222222222",
        "source_name": "Cryptic Command",
        "mode_number": 3,
        "text": "Tap all creatures your opponents control.",
        "zone": "graveyard",
        "controller": "alice",
    }


def test_redact_notices_lets_the_owner_see_their_own_hand():
    notices = [_hand_notice(controller="alice")]
    assert visibility.redact_notices(notices, "alice") == notices


def test_redact_notices_blanks_a_hand_card_for_the_opponent():
    notices = [_hand_notice(controller="alice")]
    (redacted,) = visibility.redact_notices(notices, "bob")
    assert redacted["source_id"] is None
    assert redacted["source_name"] == "<hidden>"
    assert redacted["text"] == "<hidden>"
    # the bookkeeping keys stay - only the card's own identity is blanked
    assert redacted["kind"] == "targeting_notices"


def test_redact_notices_blanks_a_hand_card_for_an_anonymous_viewer():
    """`redact_log_entry`'s own snapshot filtering treats a bare, uid-less
    read as the debugger's full eye (`app.game_log`'s own docstring) -
    notices deliberately do not get that exemption: nobody asked to see a
    card in a hand, and `commands._new_notices` cannot tell a genuinely
    anonymous reader from a hostile one."""
    notices = [_hand_notice(controller="alice")]
    (redacted,) = visibility.redact_notices(notices, None)
    assert redacted["source_name"] == "<hidden>"


def test_redact_notices_shows_admin_everything():
    notices = [_hand_notice(controller="alice")]
    assert visibility.redact_notices(notices, visibility.ADMIN) == notices


def test_redact_notices_fails_closed_when_the_controller_is_unknown():
    """`OpeningHandNotice`'s own shape (`controller=None`, `commands.
    _new_notices`'s fail-closed default) - blanked for every viewer but
    `ADMIN`, including the player whose hand it probably was."""
    notices = [_hand_notice(controller=None)]
    for viewer in ("alice", "bob", None):
        (redacted,) = visibility.redact_notices(notices, viewer)
        assert redacted["source_name"] == "<hidden>"
    assert visibility.redact_notices(notices, visibility.ADMIN) == notices


def test_redact_notices_never_touches_a_public_zone():
    notices = [_public_notice()]
    for viewer in ("alice", "bob", None, visibility.ADMIN):
        assert visibility.redact_notices(notices, viewer) == notices
