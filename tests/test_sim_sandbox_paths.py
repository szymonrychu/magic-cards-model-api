"""Every engine path value is validated: spec names, mana pool keys and `_request` itself."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api.main import app
from magic_cards_model_api.sim.sandbox_game import ACTIONS, SandboxGame


@pytest.fixture
def sandbox():
    game = SandboxGame(TestClient(app))
    yield game
    game.close()


def _spy(sandbox):
    sent = []
    real = sandbox._http.request

    def spy(method, url, **kw):
        sent.append((method, url))
        return real(method, url, **kw)

    sandbox._http.request = spy
    return sent


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        (
            {"players": [{"name": "alice"}, {"name": "bob"}], "mana_pool": {"..": "R"}},
            "mana_pool key",
        ),
        ({"players": [{"name": "../x"}, {"name": "bob"}]}, "players.name"),
    ],
)
def test_unsafe_spec_names_are_refused_before_anything_is_sent(sandbox, spec, field):
    sent = _spy(sandbox)
    with pytest.raises(ValueError, match=field):
        sandbox.create(spec)
    assert sent == []
    assert sandbox._game_id is None


def test_request_refuses_an_unsafe_path_value_and_create_leaves_no_game(sandbox, monkeypatch):
    # bypass the parse-time check to prove _request enforces it on its own
    monkeypatch.setattr(
        "magic_cards_model_api.sim.sandbox_game.SandboxSpec.model_validate",
        lambda spec: type(
            "P",
            (),
            {
                "players": [type("Pl", (), {"name": "alice", "life": 20})()],
                "active_player": None,
                "step": "precombat_main",
                "cards": [],
                "mana_pool": {"..": "R"},
            },
        )(),
    )
    sent = _spy(sandbox)
    closed = []
    real_close = SandboxGame.close
    monkeypatch.setattr(
        SandboxGame, "close", lambda self: (closed.append(self._game_id), real_close(self))
    )
    with pytest.raises(ValueError, match="invalid username"):
        sandbox.create({})
    assert [s for s in sent if s[0] == "PUT"] == []
    game_id = closed[-1]
    assert game_id is not None and sandbox._game_id is None
    assert TestClient(app).get(f"/api/games/{game_id}").status_code == 404


def test_request_checks_every_path_value(sandbox):
    sandbox._game_id = "g"
    for bad in ("..", ".", "a/b", ""):
        with pytest.raises(ValueError, match="invalid username"):
            sandbox._request("PUT", ACTIONS["mana_pool"][1], username=bad)
