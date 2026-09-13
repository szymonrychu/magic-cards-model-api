"""Shared scaffolding for `plays admit` and `guides propose`: tmp bare git
repos shaped like mtg-decks, isolated git config with the A13 credential
helper, and a fake GitHub REST server.

The api repo is tested by ordinary practice, so the stand-ins here are
legitimate: a `scripts/check_plays.py` and a `scripts/render_guides.py` small
enough to read, a bare repo standing in for github.com, and an HTTP server
standing in for the pulls endpoint.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from playscript_testkit import FETCH_SHOCK, FETCH_SURVEIL, SLUG, make_root

TOKEN = "ghp_TESTTOKEN0123456789abcdefSECRET"
REPO = "szymonrychu/mtg-decks"
GAME = "g-2026-0913-a"
RUN_NEW = f"{SLUG}-modern-2026-09-10"
RUN_OLD = f"{SLUG}-modern-2026-08-01"
PLAN = f"decks/{SLUG}/analysis/{RUN_NEW}/domain3-matchup-threat/boarding-plan.json"
OLD_PLAN = f"decks/{SLUG}/analysis/{RUN_OLD}/domain3-matchup-threat/boarding-plan.json"
GAMEPLAN = f"decks/{SLUG}/GAMEPLAN_GUIDE.md"

CHECK_PLAYS = """\
import os
import sys

if os.environ.get("MTG_TEST_GATE_FAIL"):
    print("check_plays: forced failure", file=sys.stderr)
    sys.exit(1)
print("OK: check_plays stand-in")
"""

RENDER_GUIDES = """\
import argparse
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["write", "check"])
parser.add_argument("--deck", action="append", default=[])
args = parser.parse_args()
log = os.environ.get("MTG_TEST_RENDER_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps([args.action, args.deck]) + "\\n")
if args.action == "check" and os.environ.get("MTG_TEST_RENDER_CHECK_FAIL"):
    print("stale against the committed boarding plan", file=sys.stderr)
    sys.exit(2)
for deck in args.deck:
    d = Path(deck)
    plans = sorted(d.glob("analysis/*/domain3-matchup-threat/boarding-plan.json"))
    plan = json.loads(plans[-1].read_text(encoding="utf-8"))
    text = "# gameplan\\n" + "".join(
        f"## {m['name']}\\n{json.dumps(m.get('play_plan'), sort_keys=True)}\\n"
        f"{json.dumps(m.get('notes', []))}\\n"
        for m in plan["matchups"]
    )
    target = d / "GAMEPLAN_GUIDE.md"
    if args.action == "write":
        target.write_bytes(text.encode("utf-8"))
    elif not target.exists() or target.read_bytes() != text.encode("utf-8"):
        print("stale against the committed boarding plan", file=sys.stderr)
        sys.exit(2)
print("OK: guides")
"""


def boarding_plan(tactic: str = "race them") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "deck": SLUG,
        "archetype": "boros aggro",
        "format": "modern",
        "matchups": [
            {
                "name": "dimir frog",
                "stance": "race",
                "play": {"in": ["2 Path to Exile"], "out": ["2 Lightning Bolt"]},
                "play_plan": {"our_tactic": tactic, "key_cards": ["Ragavan"]},
                "notes": ["Bolt the frog on sight."],
            },
            {
                "name": "living end",
                "stance": "hate",
                "play": {"in": ["3 Rest in Peace"], "out": ["3 Lightning Bolt"]},
                "play_plan": {"our_tactic": "graveyard hate first"},
            },
        ],
        "gaps": ["no answer to Blood Moon"],
    }


def git(cwd: Path, *args: str, check: bool = True) -> str:
    """Test-side git, with its own identity; never the code under test's runner."""
    done = subprocess.run(
        ["git", "-c", "user.name=tester", "-c", "user.email=tester@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
    return done.stdout.strip()


def dump(doc: Any) -> str:
    return json.dumps(doc, indent=2) + "\n"


def seed_guides(
    tmp_path: Path,
    plays: tuple[dict[str, Any], ...] = (FETCH_SHOCK, FETCH_SURVEIL),
    *,
    check_plays: bool = True,
    render_guides: bool = True,
    card_data: bool = True,
) -> Path:
    """A bare repo whose main is a small mtg-decks: plays, a newest and an
    older analysis run with a boarding plan each, the deck's authored files,
    the gate and the renderer. Returns the bare repo path. `card_data=False`
    seeds a clone that never had `meta/card-data.json`, for the fail-closed
    gate test."""
    work = make_root(tmp_path / "seed")
    if not card_data:
        shutil.rmtree(work / "meta")
    deck = work / "decks" / SLUG
    for doc in plays:
        (deck / "plays" / f"{doc['id']}.json").write_text(dump(doc), encoding="utf-8")
    old_run = deck / "analysis" / RUN_OLD / "domain3-matchup-threat" / "matchups"
    old_run.mkdir(parents=True)
    (old_run / "dimir-frog.json").write_text("{}", encoding="utf-8")
    (work / OLD_PLAN).write_text(dump(boarding_plan("old tactic")), encoding="utf-8")
    (work / PLAN).write_text(dump(boarding_plan()), encoding="utf-8")
    (deck / "decklist.txt").write_text("4 Lightning Bolt\n", encoding="utf-8")
    (deck / "NOTES.md").write_text("# notes\n", encoding="utf-8")
    for name in ("shell.json", "archetype.json", "sim-profiles.json", "evidence-spec.json"):
        (deck / name).write_text("{}\n", encoding="utf-8")
    (deck / "evidence").mkdir()
    (deck / "evidence" / "run.json").write_text("{}\n", encoding="utf-8")
    (work / "scripts").mkdir()
    if check_plays:
        (work / "scripts" / "check_plays.py").write_text(CHECK_PLAYS, encoding="utf-8")
    (work / "scripts" / "render_guides.py").write_text(RENDER_GUIDES, encoding="utf-8")
    subprocess.run(
        ["python3", "scripts/render_guides.py", "write", "--deck", f"decks/{SLUG}"],
        cwd=work,
        check=True,
        capture_output=True,
    )
    if not render_guides:
        (work / "scripts" / "render_guides.py").unlink()
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "seed")
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "origin", "main")
    return bare


def clone(bare: Path, dest: Path) -> Path:
    git(dest.parent, "clone", "-q", str(bare), str(dest))
    return dest


def main_sha(bare: Path) -> str:
    return git(bare, "rev-parse", "refs/heads/main")


def branches(bare: Path) -> set[str]:
    out = git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return set(out.split()) if out else set()


def compete(bare: Path, where: Path, tag: str) -> None:
    """Land an unrelated commit on origin main from another clone."""
    other = where if where.exists() else clone(bare, where)
    git(other, "pull", "-q", "--rebase", "origin", "main")
    notes = other / "decks" / SLUG / "NOTES.md"
    notes.write_text(notes.read_text(encoding="utf-8") + f"- {tag}\n", encoding="utf-8")
    git(other, "commit", "-q", "-am", f"competitor {tag}")
    git(other, "push", "-q", "origin", "HEAD:main")


class FakeGitHub:
    """The REST pulls endpoint: records every request, answers 201 with a PR url."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                entry = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw) if raw else None,
                }
                fake.requests.append(entry)
                return entry

            def _answer(self, status: int, doc: Any) -> None:
                data = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                self._record()
                if self.path == f"/repos/{REPO}/pulls":
                    number = sum(1 for r in fake.requests if r["method"] == "POST")
                    self._answer(
                        201,
                        {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}"},
                    )
                else:
                    self._answer(404, {"message": "Not Found"})

            def do_GET(self) -> None:  # noqa: N802
                self._record()
                self._answer(200, [])

            def log_message(self, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def pulls(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def isolate_git(tmp_path: Path, monkeypatch, github: FakeGitHub) -> Path:
    """HOME with the A13 ~/.gitconfig credential helper reading a tmp token
    file, no system config, and the GitHub endpoint pointed at the fake.
    Returns the token file."""
    from mtg_agent.wrapper.bootstrap import write_gitconfig

    home = tmp_path / "home"
    home.mkdir()
    token_file = tmp_path / "secret" / "GH_PAT"
    token_file.parent.mkdir()
    token_file.write_text(TOKEN, encoding="utf-8")
    write_gitconfig(SimpleNamespace(home_dir=home), token_file=token_file)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_GLOBAL", "GH_PAT_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("MTG_ROLE", "seat")
    monkeypatch.setenv("MTG_GITHUB_API", github.url)
    monkeypatch.setenv("MTG_GITHUB_REPO", REPO)
    return token_file


def interpose(
    monkeypatch, before: Callable[[list[str]], None] | None = None
) -> list[dict[str, Any]]:
    """Record every command the code under test runs (argv and env), and
    optionally act just before each one."""
    gitwork = importlib.import_module("magic_cards_model_api.playscripts.gitwork")
    calls: list[dict[str, Any]] = []
    real = gitwork.run

    def wrapped(argv, *args, **kwargs):
        calls.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
        if before is not None:
            before(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(gitwork, "run", wrapped)
    return calls


def is_main_push(argv: list[str]) -> bool:
    return argv[:2] == ["git", "push"] and any(
        a.endswith(":main") or a.endswith(":refs/heads/main") for a in argv
    )


def cli(capsys, *argv: str) -> tuple[int, dict[str, Any], str, str]:
    """Run seat_cli, returning (rc, the result JSON on stdout, stdout, stderr)."""
    from magic_cards_model_api import seat_cli

    rc = seat_cli.run(list(argv))
    captured = capsys.readouterr()
    result = json.loads(captured.out) if captured.out.strip() else {}
    return rc, result, captured.out, captured.err


def trailers(root: Path, rev: str, key: str) -> list[str]:
    out = git(root, "log", "-1", f"--format=%(trailers:key={key},valueonly)", rev)
    return [line for line in out.splitlines() if line.strip()]
