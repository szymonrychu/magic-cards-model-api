"""`POST /v1/messages` `files`: workspace files written before the turn."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mtg_agent.wrapper.api import MAX_FILE_BYTES, build_public_app
from tests.wrapper.test_session import FakeProc, ready_manager


@pytest.fixture
def wired(monkeypatch, tmp_path):
    proc = FakeProc(b"ready")
    from tests.wrapper.test_session import fast_settings

    manager = ready_manager(monkeypatch, proc, fast_settings(WORKSPACE=str(tmp_path / "ws")))
    (tmp_path / "ws").mkdir(exist_ok=True)
    public = TestClient(build_public_app(manager, manager.settings))
    yield manager, proc, public, tmp_path / "ws"
    manager.shutdown()


def test_files_land_in_the_workspace_before_the_turn_is_submitted(wired):
    manager, proc, public, ws = wired
    seen = {}
    real_submit = manager.submit

    def submit(text, callback_url=""):
        seen["file"] = (ws / "current-game.md").read_text()
        return real_submit(text, callback_url)

    manager.submit = submit
    response = public.post(
        "/v1/messages",
        json={"text": "go", "files": {"current-game.md": "# briefing", "notes/a.txt": "hi"}},
    )
    assert response.status_code == 202
    assert seen["file"] == "# briefing"
    assert (ws / "notes" / "a.txt").read_text() == "hi"


def test_a_file_at_the_size_limit_is_accepted(wired):
    _, _, public, ws = wired
    body = {"text": "go", "files": {"big.txt": "a" * MAX_FILE_BYTES}}
    assert public.post("/v1/messages", json=body).status_code == 202
    assert len((ws / "big.txt").read_text()) == MAX_FILE_BYTES


@pytest.mark.parametrize("path", ["/etc/passwd", "../escape.txt", "a/../../escape.txt", "", "a/"])
def test_a_path_outside_the_workspace_is_422_and_writes_nothing(wired, path):
    manager, proc, public, ws = wired
    response = public.post("/v1/messages", json={"text": "go", "files": {path: "x"}})
    assert response.status_code == 422
    assert proc.writes == [], "the turn must not be submitted"
    assert manager.current_turn_id is None
    assert not (ws.parent / "escape.txt").exists()


def test_a_symlink_escape_is_422(wired, tmp_path):
    _, proc, public, ws = wired
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "link").symlink_to(outside)
    response = public.post("/v1/messages", json={"text": "go", "files": {"link/x.txt": "x"}})
    assert response.status_code == 422
    assert not (outside / "x.txt").exists()
    assert proc.writes == []


def test_a_symlinked_file_pointing_out_is_422(wired, tmp_path):
    _, _, public, ws = wired
    target = tmp_path / "target.txt"
    target.write_text("keep")
    (ws / "f.txt").symlink_to(target)
    response = public.post("/v1/messages", json={"text": "go", "files": {"f.txt": "x"}})
    assert response.status_code == 422
    assert target.read_text() == "keep"


def test_an_oversize_file_is_422(wired):
    _, proc, public, ws = wired
    body = {"text": "go", "files": {"big.txt": "a" * (MAX_FILE_BYTES + 1)}}
    assert public.post("/v1/messages", json=body).status_code == 422
    assert not (ws / "big.txt").exists()
    assert proc.writes == []


def test_one_bad_path_writes_none_of_the_files(wired):
    _, _, public, ws = wired
    body = {"text": "go", "files": {"good.txt": "x", "../bad.txt": "y"}}
    assert public.post("/v1/messages", json=body).status_code == 422
    assert not (ws / "good.txt").exists()


def test_files_are_not_written_when_the_session_is_busy(wired):
    _, _, public, ws = wired
    public.post("/v1/messages", json={"text": "first"})
    response = public.post("/v1/messages", json={"text": "second", "files": {"late.txt": "x"}})
    assert response.status_code == 409
    assert not (ws / "late.txt").exists()
