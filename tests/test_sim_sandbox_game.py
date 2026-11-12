"""SandboxGame: the engine-side sandbox the sim worker drives."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.sim.sandbox_game import SandboxGame, resolve_labels

BOLT_SPEC = json.loads((Path(__file__).parent / "fixtures" / "sim" / "bolt_bears.json").read_text())


@pytest.fixture
def sandbox():
    game = SandboxGame(TestClient(app))
    yield game
    game.close()


def names(cards):
    return [c["name"] for c in cards]


def test_create_builds_the_board_and_returns_labels(sandbox):
    out = sandbox.create(BOLT_SPEC)
    assert out["labels"].keys() == {"bolt", "bears"}
    assert names(out["state"]["players"]["bob"]["battlefield"]) == ["Grizzly Bears"]
    assert out["state"]["players"]["bob"]["battlefield"][0]["label"] == "bears"
    assert out["state"]["players"]["alice"]["mana_pool"] == {"R": 1}


def test_create_applies_counters_damage_and_rejects_unknown_fields(sandbox):
    spec = {
        "players": [{"name": "alice"}, {"name": "bob"}],
        "cards": [
            {
                "card": "Grizzly Bears",
                "player": "alice",
                "zone": "battlefield",
                "label": "b",
                "counters": {"+1/+1": 1},
                "damage": 1,
            }
        ],
    }
    card = sandbox.create(spec)["state"]["players"]["alice"]["battlefield"][0]
    assert (card["power"], card["toughness"], card["damage"]) == (3, 3, 1)
    with pytest.raises(ValueError):
        sandbox.create({**spec, "bogus": 1})


def test_cast_bolt_on_bears_moves_bears_to_graveyard(sandbox):
    sandbox.create(BOLT_SPEC)
    sandbox.act("cast", "alice", {"card_id": "@bolt", "targets": ["@bears"]})
    sandbox.act("pass", "alice", {})
    sandbox.act("pass", "bob", {})
    st = sandbox.state()["state"]
    assert "Grizzly Bears" in names(st["players"]["bob"]["graveyard"])


def test_dry_run_leaves_the_state_unchanged(sandbox):
    sandbox.create(BOLT_SPEC)
    before = sandbox.state()
    out = sandbox.act("cast", "alice", {"card_id": "@bolt", "targets": ["@bears"]}, dry_run=True)
    assert out["result"].get("allowed") is not False
    assert sandbox.state() == before


def test_dry_run_on_a_setup_action_is_refused(sandbox):
    sandbox.create(BOLT_SPEC)
    out = sandbox.act("add_card", None, {"card": "Shock", "player": "alice"}, dry_run=True)
    assert out["result"] == {"allowed": False, "reason": "dry_run is not supported for add_card"}


def test_setup_actions_work_through_path_params(sandbox):
    sandbox.create(BOLT_SPEC)
    sandbox.act("patch_player", None, {"player": "bob", "life": 7})
    sandbox.act("tap_card", None, {"card_id": "@bears", "tapped": True})
    st = sandbox.state()["state"]
    assert st["players"]["bob"]["life"] == 7
    assert st["players"]["bob"]["battlefield"][0]["tapped"] is True


def test_act_surfaces_a_new_pending_decision(sandbox):
    sandbox.create(
        {
            "players": [{"name": "alice"}, {"name": "bob"}],
            "cards": [
                {"card": "Utopia Sprawl", "player": "alice", "label": "sprawl"},
                {"card": "Forest", "player": "alice", "zone": "battlefield", "label": "forest"},
            ],
            "mana_pool": {"alice": "G"},
        }
    )
    sandbox.act("cast", "alice", {"card_id": "@sprawl", "targets": ["@forest"]})
    sandbox.act("pass", "alice", {})
    out = sandbox.act("pass", "bob", {})
    pending = out["state"]["pending_decision"]
    assert pending["kind"] == "choose_a_quality"
    assert pending["options"]


def test_unknown_action_and_unknown_label_are_refused(sandbox):
    sandbox.create(BOLT_SPEC)
    out = sandbox.act("teleport", "alice", {})
    assert out["result"]["allowed"] is False and "teleport" in out["result"]["reason"]
    out = sandbox.act("cast", "alice", {"card_id": "@nope"})
    assert out["result"] == {"allowed": False, "reason": "unknown label @nope"}


def test_engine_refusal_text_is_passed_through(sandbox):
    spec = {**BOLT_SPEC, "mana_pool": {}, "cards": [BOLT_SPEC["cards"][0], BOLT_SPEC["cards"][2]]}
    sandbox.create(spec)
    out = sandbox.act("cast", "alice", {"card_id": "@bolt", "targets": ["@bears"]})
    assert out["result"]["allowed"] is False and out["result"]["reason"]


def test_resolve_labels_replaces_whole_strings_at_any_depth():
    assert resolve_labels({"a": ["@x", {"b": "@x"}], "c": "mail@x"}, {"x": "1"}) == {
        "a": ["1", {"b": "1"}],
        "c": "mail@x",
    }
    with pytest.raises(ValueError, match="unknown label @y"):
        resolve_labels("@y", {})


def test_no_answer_ever_contains_a_credential(sandbox):
    answers = [
        sandbox.create(BOLT_SPEC),
        sandbox.state(full=True),
        sandbox.legal_actions("alice"),
        sandbox.act("pass", "alice", {}),
    ]
    secrets = [sandbox._admin_key, *sandbox._seat_uids.values()]
    blob = json.dumps(answers)
    assert not any(s in blob for s in secrets)


def test_close_deletes_the_game(sandbox):
    sandbox.create(BOLT_SPEC)
    game_id = sandbox._game_id
    sandbox.close()
    assert TestClient(app).get(f"/api/games/{game_id}").status_code == 404


def test_create_closes_the_half_built_game_when_a_card_is_unresolvable(sandbox, monkeypatch):
    closed = []
    real_close = SandboxGame.close

    def spy(self):
        closed.append(self._game_id)
        real_close(self)

    monkeypatch.setattr(SandboxGame, "close", spy)
    spec = {
        "players": [{"name": "alice"}, {"name": "bob"}],
        "cards": [
            {"card": "Mountain", "player": "alice"},
            {"card": "Nonexistent", "player": "alice"},
        ],
    }
    with pytest.raises(ValueError, match="Nonexistent"):
        sandbox.create(spec)
    game_id = closed[-1]
    assert game_id is not None and sandbox._game_id is None
    assert TestClient(app).get(f"/api/games/{game_id}").status_code == 404
    with pytest.raises(RuntimeError):
        sandbox.state()


def test_pending_decision_for_the_opponent_names_that_player(sandbox):
    sandbox.create(
        {
            "players": [{"name": "alice"}, {"name": "bob"}],
            "cards": [
                {"card": "Mind Rot", "player": "alice", "label": "rot"},
                {"card": "Grizzly Bears", "player": "bob"},
                {"card": "Forest", "player": "bob"},
                {"card": "Island", "player": "bob"},
            ],
            "mana_pool": {"alice": "BBB"},
        }
    )
    sandbox.act("cast", "alice", {"card_id": "@rot", "targets": ["bob"]})
    out = sandbox.act("pass", "alice", {})
    assert out["state"]["pending_decision"] is None
    out = sandbox.act("pass", "bob", {})
    pending = out["state"]["pending_decision"]
    assert pending["player"] == "bob"
    assert pending["kind"] == "choose_cards"
    assert len(pending["cards"]) == 3 and pending["amount"] == 2


@pytest.mark.parametrize(
    ("action", "player", "body", "param"),
    [
        ("tap_card", None, {"card_id": "../../../decks/x", "tapped": True}, "card_id"),
        ("patch_card", None, {"card_id": "..", "tapped": True}, "card_id"),
        ("patch_card", None, {"card_id": "a/b"}, "card_id"),
        ("patch_player", None, {"player": "../../../../health", "life": 1}, "username"),
        ("mana_pool", None, {"player": "bob/../../x"}, "username"),
        ("patch_player", None, {"player": "bob?x=1"}, "username"),
    ],
)
def test_path_params_with_traversal_are_refused_and_nothing_is_sent(
    sandbox, action, player, body, param
):
    sandbox.create(BOLT_SPEC)
    sent = []
    real = sandbox._http.request

    def spy(method, url, **kw):
        sent.append((method, url))
        return real(method, url, **kw)

    sandbox._http.request = spy
    out = sandbox.act(action, player, body)
    assert out["result"]["allowed"] is False
    assert f"invalid {param}" in out["result"]["reason"]
    mutating = [s for s in sent if s[0] != "GET"]
    assert mutating == [], mutating


def test_add_card_with_a_label_registers_it_for_later_acts(sandbox):
    sandbox.create(BOLT_SPEC)
    out = sandbox.act("add_card", None, {"card": "Shock", "player": "alice", "label": "shock"})
    assert out["result"]["added"]["name"] == "Shock"
    assert out["labels"]["shock"] == out["result"]["added"]["card_id"]
    hand = out["state"]["players"]["alice"]["hand"]
    assert {"Shock": "shock"}.items() <= {c["name"]: c.get("label") for c in hand}.items()
    again = sandbox.act("add_card", None, {"card": "Shock", "player": "alice", "label": "shock"})
    assert again["result"] == {"allowed": False, "reason": "duplicate label shock"}
    assert "Shock" in names(sandbox.state()["state"]["players"]["alice"]["hand"])
    assert names(sandbox.state()["state"]["players"]["alice"]["hand"]).count("Shock") == 1


def _advance_to_alices_draw(sandbox):
    """From bob's end step through cleanup, untap and upkeep into alice's draw step."""
    out = None
    for _ in range(4):
        out = sandbox.act("advance", None, {})
    return out["state"]


EMPTY_LIBRARY_SPEC = {
    "players": [{"name": "alice"}, {"name": "bob"}],
    "active_player": "bob",
    "step": "end_step",
    "cards": [{"card": "Grizzly Bears", "player": "alice", "zone": "battlefield"}],
}


def test_drawing_from_an_empty_library_loses_the_game(sandbox):
    sandbox.create(EMPTY_LIBRARY_SPEC)
    st = _advance_to_alices_draw(sandbox)
    assert (st["step"], st["active_player"], st["winner"]) == ("draw", "alice", "bob")


def test_library_filler_keeps_the_draw_step_alive(sandbox):
    spec = {
        **EMPTY_LIBRARY_SPEC,
        "players": [
            {"name": "alice", "library_filler": {"name": "Island", "count": 3}},
            {"name": "bob"},
        ],
        "cards": [
            *EMPTY_LIBRARY_SPEC["cards"],
            {"card": "Shock", "player": "alice", "zone": "library", "position": "top"},
        ],
    }
    out = sandbox.create(spec)
    assert out["state"]["players"]["alice"]["library_count"] == 4
    assert out["state"]["players"]["bob"]["library_count"] == 0
    st = _advance_to_alices_draw(sandbox)
    assert (st["step"], st["winner"]) == ("draw", None)
    assert names(st["players"]["alice"]["hand"]) == ["Shock"]  # the spec card stays on top
    assert st["players"]["alice"]["library_count"] == 3


@pytest.mark.parametrize("count", [0, 61])
def test_library_filler_count_is_bounded(sandbox, count):
    spec = {"players": [{"name": "alice", "library_filler": {"name": "Island", "count": count}}]}
    with pytest.raises(ValueError, match="count"):
        sandbox.create(spec)


def test_face_down_card_is_marked_and_hidden_from_the_other_seat(sandbox):
    """A foretold card sits face down in exile: the board marks it, bob's seat sees no name."""
    out = sandbox.create(
        {
            "players": [{"name": "alice"}, {"name": "bob"}],
            "cards": [
                {"card": "Shock", "player": "alice", "zone": "exile", "face_up": False},
                {"card": "Lightning Bolt", "player": "alice", "zone": "exile"},
            ],
        }
    )
    exile = {c["name"]: c for c in out["state"]["players"]["alice"]["exile"]}
    assert exile["Shock"]["face_down"] is True
    assert "face_down" not in exile["Lightning Bolt"]
    status, view = sandbox._request("GET", "/api/games/{game_id}", uid=sandbox._seat_uids["bob"])
    assert status == 200
    seen = [c.get("name") for c in view["exile"]]
    assert "Shock" not in seen and "Lightning Bolt" in seen
