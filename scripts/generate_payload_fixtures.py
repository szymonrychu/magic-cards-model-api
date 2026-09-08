"""2b's T2: `fixtures/payloads/<case>.json` is a generated, committed corpus of
real payloads this API actually emits - not hand-written.

`tests/test_ui_fixture_shape.py` is this generator's own drift test, and this
module's whole reason to exist is what that file's own history already
proved: a hand-written fixture drifts from the engine silently.
`target_minimum: None` and `moved_label: None` both sat in a hand-built
`PendingDecision` fixture for a real review cycle, both wrong (the engine
never emits either as `None` - `target_minimum` is an `int`, the labels
default to `""`), and nothing caught it because `ui/app.js`'s own
`pending.target_minimum || 0` reads a real `0` and a wrong `None` identically.
The fix generalised here: never hand-write the shape a client is tested
against. Play a real board through the real HTTP API and capture what it
actually said.

**The seven cases**, matched to what the six hybrid UI tests
(`tests/test_card_split_labels.py`, `tests/test_fetchland_search_choice.py`,
`tests/test_trigger_order_labels.py`, `tests/test_kozilek_command_ui_flow.py`,
`tests/test_flow.py::test_declare_attackers`, `tests/test_mulligans.py::
test_a_booted_game_rests_in_the_mulligan_step`) already exercise on a real
board today, plus one mid-game full state view:

- `split_card_labels` - Devourer of Destiny's first-upkeep `scry`-kind
  decision naming its own two destinations ("top of your library" /
  "exiled"), the reported bug `test_card_split_labels.py` guards.
- `fetchland_search_choice` - Wooded Foothills' CR 701.23a search as one
  `choose_cards` question naming every legal find by `card_labels`.
- `trigger_order_labels` - two Delver of Secrets triggering in the same CR
  603.3b batch, named by `card_labels` before either is in any zone.
- `kozilek_command_zone_flow` - Kozilek's Command cast with both modes
  targeting the caster twice (CR 115.3: separate instances of "target"), the
  stack entry's own `target_refs`.
- `paused_combat_declaration` - a real attacker paused on CR 508.1's
  `declare_attackers` question mid-`advance`.
- `mulligan_prompt` - a freshly dealt game resting in CR 103.5, both seats
  still owing a declaration.
- `mid_game_full_state` - one richer, later-turn board (turn 3, both sides
  with permanents, a filled hand, a graveyard card) - not tied to a single
  bug report, just the one case that is a genuinely full `GameStateView`.

Every card here is a real Scryfall printing out of `tests/fixtures/scryfall`,
the same fixture pool the API's own test suite reads (`conftest.py` pins
`MCM_CACHE_DIR` there) - nothing is hand-built and nothing is mocked. The two
cases that boot from a decklist (`split_card_labels`, `mulligan_prompt`) reuse
the exact synthetic decklists and seeds `test_card_split_labels.py` and
`test_mulligans.py` already carry (`DEVOURER_DECK`/`PLAIN_DECK`, seed 4;
`PLAIN_DECK`/`PLAIN_DECK`, seed 1) rather than a real tournament list: Devourer
of Destiny is not a card any real Modern list runs, and the plan's own words
for this task are "exactly what the six hybrid UI tests need today" - so this
generator reaches for the same boards those tests already play, the same way
`magic-cards-model-api`'s own `CLAUDE.md` scopes the real-tournament-decklist
rule to `magic-cards-model`'s card-rules tests, not to this repo's ordinary
test practice.

**Determinism.** `mise run fixtures:payloads` must produce byte-identical
output every run - no consumer downstream (M11: `magic-cards-frontend` pins
this corpus by sha and runs its node tests against it) can tolerate a fixture
that only sometimes matches what is committed. Two things in a real API
response are not seeded by anything a caller controls and vary every run:
`game_id` (`uuid4().hex`, `repository.py`) and every card/ability's own `id`
(`CardState`/`AbilityOnStack`, minted per-game off a random `game_uid` -
`ids.py`). `canonicalize()` is the seed substitute for both: it walks the
whole response and replaces every id-shaped string - a dashed UUID or a bare
32-hex-digit one - with a placeholder numbered by first appearance in the
payload's own (otherwise deterministic) key/list order, so two runs that mint
different random ids still produce the same numbered placeholders in the same
positions. Nothing else in a captured payload is a source of nondeterminism:
`serialize.py` stamps no timestamp anywhere, and every zone this API reports
is a plain list (insertion order), never a `set`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.store import store

REPO_ROOT = Path(__file__).resolve().parent.parent
PAYLOADS_DIR = REPO_ROOT / "fixtures" / "payloads"

#: Real Scryfall printings, out of `tests/fixtures/scryfall` - see this
#: module's own docstring for why these two decklists are synthetic rather
#: than real tournament lists.
DEVOURER_DECK = "56 Swamp\n4 Devourer of Destiny"
PLAIN_DECK = "60 Forest"

#: With this seed the engine's own shuffle deals alice exactly one Devourer of
#: Destiny in her opening seven and makes her the starting player - the same
#: seed `test_card_split_labels.py` uses, for the same reason.
SEED_WITH_A_DEVOURER_IN_ALICES_HAND = 4

#: `test_mulligans.py`'s own seed for a plain, uneventful deal.
PLAIN_SEED = 1


def case_path(name: str) -> Path:
    return PAYLOADS_DIR / f"{name}.json"


# -- id canonicalisation ------------------------------------------------


_DASHED_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_BARE_UUID = re.compile(r"^[0-9a-f]{32}$")


def _placeholder(serial: int, *, dashed: bool) -> str:
    digits = format(serial, "032x")
    if not dashed:
        return digits
    return f"{digits[0:8]}-{digits[8:12]}-{digits[12:16]}-{digits[16:20]}-{digits[20:32]}"


def canonicalize(payload: Any) -> Any:
    """Replace every id-shaped string in `payload` with a placeholder
    numbered by first appearance - see this module's own docstring for why.
    A fresh `mapping` per call, so each case file numbers its own ids from 1
    rather than sharing a running count with any other case."""
    mapping: dict[str, str] = {}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, str):
            dashed = bool(_DASHED_UUID.match(node))
            if dashed or _BARE_UUID.match(node):
                if node not in mapping:
                    mapping[node] = _placeholder(len(mapping) + 1, dashed=dashed)
                return mapping[node]
        return node

    return walk(payload)


def render(payload: dict[str, Any]) -> str:
    return json.dumps(canonicalize(payload), indent=2, sort_keys=True) + "\n"


# -- small HTTP helpers, mirroring tests/conftest.py's own ------------------


def _add_card(client: TestClient, game_id: str, name: str, player: str, zone: str) -> None:
    response = client.post(
        f"/api/games/{game_id}/cards", json={"card": name, "player": player, "zone": zone}
    )
    assert response.status_code == 200, response.text


def _state(client: TestClient, game_id: str, uid: str) -> dict[str, Any]:
    response = client.get(f"/api/games/{game_id}", params={"uid": uid})
    assert response.status_code == 200, response.text
    return response.json()


def _new_bare_game(client: TestClient) -> tuple[str, dict[str, str], str]:
    """A board-first game (no decklists, no pregame) - `POST /api/games`
    leaves it resting at turn 1's precombat main, ready for `_add_card`."""
    created = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    ).json()
    game_id = created["game_id"]
    uids = {name: uid for uid, name in store.seat_uids(game_id).items()}
    return game_id, uids, created["admin_key"]


def _delete(client: TestClient, game_id: str, uid: str) -> None:
    client.delete(f"/api/games/{game_id}", params={"uid": uid})


# -- the seven cases ----------------------------------------------------


def _case_split_card_labels(client: TestClient) -> dict[str, Any]:
    """Devourer of Destiny's own destinations, from a real pregame through a
    real CR 603.7 delayed trigger resolving - the flow
    `tests/test_card_split_labels.py::boot`/`reach_the_devourer_upkeep` drive,
    minus the background thread that file needs only because it also has to
    keep polling for the pause: `POST .../resolve-top` itself already returns
    the instant it crosses the question (T7b's pause-and-resume), so a
    synchronous call is enough here."""
    created = client.post(
        "/api/ui/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": DEVOURER_DECK, "ai": False},
                {"username": "bob", "decklist_text": PLAIN_DECK, "ai": False},
            ],
            "seed": SEED_WITH_A_DEVOURER_IN_ALICES_HAND,
        },
    ).json()
    game_id = created["game_id"]
    uids = created["seat_uids"]
    try:
        for player in ("alice", "bob"):
            kept = client.post(
                f"/api/games/{game_id}/pregame/mulligan",
                params={"uid": uids[player]},
                json={"player": player, "take": False},
            )
            assert kept.status_code == 200, kept.text
        options = _state(client, game_id, uids["alice"])
        (offer,) = options["pregame"]["opening_hand_options"]["alice"]
        taken = client.post(
            f"/api/games/{game_id}/pregame/opening-hand/action",
            params={"uid": uids["alice"]},
            json={"player": "alice", "card_id": offer["card_id"]},
        )
        assert taken.status_code == 200, taken.text
        for player in ("alice", "bob"):
            done = client.post(
                f"/api/games/{game_id}/pregame/opening-hand/finish",
                params={"uid": uids[player]},
                json={"player": player},
            )
            assert done.status_code == 200, done.text
        resolved = client.post(f"/api/games/{game_id}/resolve-top", params={"uid": uids["alice"]})
        assert resolved.status_code == 200, resolved.text
        body = resolved.json()
        assert body["paused"] is True, body
        assert body["pending_decision"]["kind"] == "scry", body
        return body
    finally:
        _delete(client, game_id, uids["alice"])


def _case_fetchland_search_choice(client: TestClient) -> dict[str, Any]:
    """Wooded Foothills' CR 701.23a search, the flow
    `tests/test_fetchland_search_choice.py::stock_the_fetch`/
    `activate_and_resolve` drive: a Forest and a Breeding Pool in the
    library (`"Land - Forest Island"` is a legal find by type, per that
    file's own docstring), one `choose_cards` question naming both."""
    game_id, uids, admin_key = _new_bare_game(client)
    try:
        _add_card(client, game_id, "Wooded Foothills", "alice", "battlefield")
        _add_card(client, game_id, "Forest", "alice", "library")
        _add_card(client, game_id, "Breeding Pool", "alice", "library")
        board = _state(client, game_id, admin_key)
        foothills = next(c for c in board["battlefield"] if c["name"] == "Wooded Foothills")
        activated = client.post(
            f"/api/games/{game_id}/actions/activate",
            json={"player": "alice", "card_id": foothills["id"], "ability_index": 0},
        )
        assert activated.status_code == 200, activated.text
        resolved = client.post(f"/api/games/{game_id}/resolve-top", params={"uid": uids["alice"]})
        assert resolved.status_code == 200, resolved.text
        body = resolved.json()
        assert body["paused"] is True, body
        assert body["pending_decision"]["kind"] == "choose_cards", body
        return body
    finally:
        _delete(client, game_id, uids["alice"])


def _case_trigger_order_labels(client: TestClient) -> dict[str, Any]:
    """Two Delver of Secrets triggering in the same CR 603.3b batch, the flow
    `tests/test_trigger_order_labels.py::stage_order_triggers_decision`
    drives: an untap-to-upkeep `advance` fires both "at the beginning of your
    upkeep" abilities at once, named by `card_labels` before either ability
    is on the stack in any zone the API can resolve."""
    game_id, uids, _admin_key = _new_bare_game(client)
    try:
        _add_card(client, game_id, "Delver of Secrets", "alice", "battlefield")
        _add_card(client, game_id, "Delver of Secrets", "alice", "battlefield")
        step = client.put(f"/api/games/{game_id}/step", json={"step": "untap", "player": "alice"})
        assert step.status_code == 200, step.text
        advanced = client.post(
            f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
        )
        assert advanced.status_code == 200, advanced.text
        body = advanced.json()
        assert body["paused"] is True, body
        assert body["pending_decision"]["kind"] == "order_triggers", body
        return body
    finally:
        _delete(client, game_id, uids["alice"])


def _case_kozilek_command_zone_flow(client: TestClient) -> dict[str, Any]:
    """Kozilek's Command cast with modes 1 and 2 both targeting the caster
    (CR 115.3: each mode is its own instance of "target"), the flow
    `tests/test_kozilek_command_ui_flow.py` drives up to the cast landing on
    the stack - the deterministic half of that file; the pass/resolve tail is
    left out on purpose, since whether bob's second pass pauses on the mode
    2 scry or resolves outright is a real race in that test (`if
    bob_response.get("paused"):`), and this corpus needs one fixed answer,
    not either of two."""
    game_id, uids, admin_key = _new_bare_game(client)
    try:
        _add_card(client, game_id, "Kozilek's Command", "alice", "hand")
        for _ in range(3):
            _add_card(client, game_id, "Wastes", "alice", "battlefield")
        # Mode 2's "then draws a card" needs a library card to draw, the same
        # reason `test_kozilek_command_ui_flow.py` stocks one.
        _add_card(client, game_id, "Island", "alice", "library")
        board = _state(client, game_id, admin_key)
        command = next(c for c in board["hands"]["alice"] if c["name"] == "Kozilek's Command")
        lands = [c["id"] for c in board["battlefield"] if c["name"] == "Wastes"]
        cast = client.post(
            f"/api/games/{game_id}/actions/cast",
            json={
                "player": "alice",
                "card_id": command["id"],
                "targets": ["alice", "alice"],
                "modes": [1, 2],
                "x_value": 1,
                "mana_sources": lands,
            },
        )
        assert cast.status_code == 200, cast.text
        assert cast.json()["allowed"] is True, cast.json()
        view = _state(client, game_id, uids["alice"])
        assert len(view["stack"]) == 1, view["stack"]
        assert (
            view["stack"][0]["target_refs"]
            == [{"kind": "player", "id": "alice", "name": "alice"}] * 2
        ), view["stack"]
        return view
    finally:
        _delete(client, game_id, uids["alice"])


def _case_paused_combat_declaration(client: TestClient) -> dict[str, Any]:
    """A real attacker, paused on CR 508.1's own question - the flow
    `tests/test_flow.py::test_declare_attackers` drives: Grizzly Bears cast
    and resolved for mana, its summoning sickness cleared by hand (the
    harness's own way to stage "not this creature's first turn" without
    playing one), `advance` from beginning of combat pausing on
    `declare_attackers` before any attacker is named."""
    game_id, uids, admin_key = _new_bare_game(client)
    try:
        _add_card(client, game_id, "Forest", "alice", "battlefield")
        _add_card(client, game_id, "Mountain", "alice", "battlefield")
        _add_card(client, game_id, "Grizzly Bears", "alice", "hand")
        board = _state(client, game_id, admin_key)
        forest = next(c for c in board["battlefield"] if c["name"] == "Forest")
        mountain = next(c for c in board["battlefield"] if c["name"] == "Mountain")
        cast = client.post(
            f"/api/games/{game_id}/actions/cast",
            json={
                "player": "alice",
                "card": "Grizzly Bears",
                "mana_sources": [{"card_id": mountain["id"]}, {"card_id": forest["id"]}],
            },
        )
        assert cast.status_code == 200 and cast.json()["allowed"] is True, cast.text
        resolved = client.post(f"/api/games/{game_id}/resolve-top")
        assert resolved.status_code == 200, resolved.text
        bears = next(
            c
            for c in _state(client, game_id, admin_key)["battlefield"]
            if c["name"] == "Grizzly Bears"
        )
        patched = client.patch(
            f"/api/games/{game_id}/cards/{bears['id']}", json={"summoning_sick": False}
        )
        assert patched.status_code == 200, patched.text
        step = client.put(f"/api/games/{game_id}/step", json={"step": "beginning_of_combat"})
        assert step.status_code == 200, step.text
        advanced = client.post(
            f"/api/games/{game_id}/advance", params={"uid": uids["alice"]}, json={"player": "alice"}
        )
        assert advanced.status_code == 200, advanced.text
        body = advanced.json()
        assert body["paused"] is True, body
        assert body["pending_decision"]["kind"] == "declare_attackers", body
        return body
    finally:
        _delete(client, game_id, uids["alice"])


def _case_mulligan_prompt(client: TestClient) -> dict[str, Any]:
    """A freshly dealt game resting in CR 103.5, the flow
    `tests/test_mulligans.py::test_a_booted_game_rests_in_the_mulligan_step`
    drives: both seats hold seven, neither has declared, `phase` is `null`
    (CR 500.1 makes a phase part of a turn, and this step is before CR
    103.8's first turn)."""
    created = client.post(
        "/api/ui/games",
        json={
            "players": [
                {"username": "alice", "decklist_text": PLAIN_DECK, "ai": False},
                {"username": "bob", "decklist_text": PLAIN_DECK, "ai": False},
            ],
            "seed": PLAIN_SEED,
        },
    ).json()
    game_id = created["game_id"]
    uids = created["seat_uids"]
    try:
        view = _state(client, game_id, uids["alice"])
        assert view["step"] == "mulligan", view
        assert view["pregame"]["awaiting_mulligan"] == ["alice", "bob"], view
        return view
    finally:
        _delete(client, game_id, uids["alice"])


def _case_mid_game_full_state(client: TestClient) -> dict[str, Any]:
    """One later-turn, fuller board - not a single bug's regression cover,
    the one case that is a genuinely representative full `GameStateView`:
    both sides with permanents (some summoning-sickness-cleared by having
    survived a turn, not patched by hand), alice holding a card, a graveyard
    card for bob. Both libraries are stocked before advancing - a bare
    `POST /api/games` board starts both players at zero cards in library,
    and CR 104.3c ends the game the instant either draws from an empty one,
    which a bare `advance` loop reaches within two turns."""
    game_id, uids, admin_key = _new_bare_game(client)
    try:
        for _ in range(10):
            _add_card(client, game_id, "Forest", "alice", "library")
            _add_card(client, game_id, "Mountain", "bob", "library")
        _add_card(client, game_id, "Forest", "alice", "battlefield")
        _add_card(client, game_id, "Forest", "alice", "battlefield")
        _add_card(client, game_id, "Grizzly Bears", "alice", "battlefield")
        _add_card(client, game_id, "Mountain", "bob", "battlefield")
        _add_card(client, game_id, "Savannah Lions", "bob", "battlefield")
        _add_card(client, game_id, "Lightning Bolt", "alice", "hand")
        _add_card(client, game_id, "Lightning Bolt", "bob", "graveyard")

        target_turn, target_step = 3, "precombat_main"
        while True:
            board = _state(client, game_id, admin_key)
            if board["turn_number"] == target_turn and board["step"] == target_step:
                break
            advanced = client.post(
                f"/api/games/{game_id}/advance", json={"player": board["active_player"]}
            )
            assert advanced.status_code == 200, advanced.text
            body = advanced.json()
            if body.get("paused"):
                pending = body["pending_decision"]
                assert pending["kind"] in ("declare_attackers", "declare_blockers"), pending
                decided = client.post(
                    f"/api/games/{game_id}/decide",
                    params={"uid": uids[pending["player"]]},
                    json={"player": pending["player"], "pairs": []},
                )
                assert decided.status_code == 200 and decided.json()["accepted"] is True, (
                    decided.text
                )
        return _state(client, game_id, admin_key)
    finally:
        _delete(client, game_id, uids["alice"])


#: Fixed order, matched to the plan's own list. `dict` preserves it, which is
#: what makes `build_all()` deterministic across the whole corpus - each
#: case's own `canonicalize()` call numbers its ids from 1 regardless, but
#: the order cases run in is still worth pinning so a stray cross-case
#: interaction (a leaked live game, say) would show up the same way twice.
CASES: dict[str, Callable[[TestClient], dict[str, Any]]] = {
    "split_card_labels": _case_split_card_labels,
    "fetchland_search_choice": _case_fetchland_search_choice,
    "trigger_order_labels": _case_trigger_order_labels,
    "kozilek_command_zone_flow": _case_kozilek_command_zone_flow,
    "paused_combat_declaration": _case_paused_combat_declaration,
    "mulligan_prompt": _case_mulligan_prompt,
    "mid_game_full_state": _case_mid_game_full_state,
}


def build_all() -> dict[str, dict[str, Any]]:
    """Every case's raw (not yet canonicalised) payload, from one shared
    `TestClient` - each case builder cleans up its own game before
    returning, so the cases never interact."""
    with TestClient(app) as client:
        return {name: builder(client) for name, builder in CASES.items()}


def render_all(payloads: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {name: render(payload) for name, payload in payloads.items()}


def main() -> None:
    PAYLOADS_DIR.mkdir(parents=True, exist_ok=True)
    rendered = render_all(build_all())
    for name, text in rendered.items():
        case_path(name).write_text(text)
        print(f"wrote {case_path(name)}")


if __name__ == "__main__":
    main()
