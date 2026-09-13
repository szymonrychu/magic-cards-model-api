"""`mcp-seat guides propose`: a seat's matchup play_plan / notes edit and its
NOTES.md become regenerated guides on a pushed branch and a pull request,
never a push to main. mtg-decks is private with no GitHub Actions budget, so
its `ci` and `automerge` workflows never run there; the pull request sits
`awaiting_owner_review` until the owner merges it by hand, and this command
never polls for its checks or merge state - it returns the moment the PR is
open.

Runs against a tmp bare repo and a fake GitHub REST server
(`guides_git_testkit`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from guides_git_testkit import (
    GAME,
    GAMEPLAN,
    OLD_PLAN,
    PLAN,
    REPO,
    TOKEN,
    FakeGitHub,
    branches,
    cli,
    clone,
    dump,
    git,
    interpose,
    isolate_git,
    main_sha,
    seed_guides,
    trailers,
)
from playscript_testkit import SLUG

NOTES = f"decks/{SLUG}/NOTES.md"
BRANCH = f"seat/{GAME}-{SLUG}-seat-guides"
ADVISOR_BRANCH = f"seat/{GAME}-{SLUG}-advisor-guides"


@pytest.fixture
def github(tmp_path, monkeypatch):
    fake = FakeGitHub()
    isolate_git(tmp_path, monkeypatch, fake)
    yield fake
    fake.close()


@pytest.fixture
def render_log(tmp_path, monkeypatch):
    log = tmp_path / "render.log"
    monkeypatch.setenv("MTG_TEST_RENDER_LOG", str(log))
    return log


@pytest.fixture
def bare(tmp_path, github):
    return seed_guides(tmp_path)


@pytest.fixture
def root(tmp_path, bare):
    return clone(bare, tmp_path / "guides")


def propose(capsys, root: Path):
    return cli(capsys, "guides", "propose", "--deck", SLUG, "--game-id", GAME, "--root", str(root))


def edit_plan(root: Path, change) -> None:
    doc = json.loads((root / PLAN).read_text(encoding="utf-8"))
    change(doc)
    (root / PLAN).write_text(dump(doc), encoding="utf-8")


def good_edit(doc) -> None:
    doc["matchups"][0]["play_plan"]["our_tactic"] = "go under the frog"
    doc["matchups"][0]["notes"].append("Hold Bolt for the second frog.")
    doc["matchups"][1]["notes"] = ["Board in all three."]


def rendered_log(log: Path) -> list:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_render_runs_and_the_generated_guides_are_committed_on_a_pr_branch(
    capsys, monkeypatch, root, bare, github, render_log
):
    monkeypatch.setenv("MTG_ROLE", "advisor")
    calls = interpose(monkeypatch)
    main_before = main_sha(bare)
    edit_plan(root, good_edit)
    (root / NOTES).write_text("# notes\n- the frog is a race\n", encoding="utf-8")

    rc, result, out, err = propose(capsys, root)
    assert (rc, result["result"]) == (0, "awaiting_owner_review"), result
    assert result["pr_url"] == f"https://github.com/{REPO}/pull/1"
    assert result["branch"] == ADVISOR_BRANCH

    assert rendered_log(render_log) == [["write", [f"decks/{SLUG}"]], ["check", [f"decks/{SLUG}"]]]
    assert main_sha(bare) == main_before
    assert ADVISOR_BRANCH in branches(bare)
    changed = git(bare, "diff", "--name-only", "main", ADVISOR_BRANCH).splitlines()
    assert sorted(changed) == sorted([PLAN, NOTES, GAMEPLAN])
    guide = git(bare, "show", f"{ADVISOR_BRANCH}:{GAMEPLAN}")
    assert "go under the frog" in guide and "Board in all three." in guide
    assert git(bare, "log", "-1", "--format=%an|%ae", ADVISOR_BRANCH) == (
        f"mtg-buddy {SLUG}|mtg-buddy@mtg-trainer.invalid"
    )
    assert trailers(bare, ADVISOR_BRANCH, "Game-Id") == [GAME]

    (pull,) = github.pulls()
    assert (pull["body"]["head"], pull["body"]["base"]) == (ADVISOR_BRANCH, "main")
    assert pull["headers"]["authorization"] == f"Bearer {TOKEN}"
    # The PR body sends merging to a human - mtg-decks has no CI/automerge to
    # rely on - and the command never polls the fake server for anything else.
    assert "automerge" not in pull["body"]["body"].lower()
    assert "review and merge this by hand" in pull["body"]["body"].lower()
    assert len(github.requests) == 1
    # The clone is back on an untouched main; the change lives on the branch.
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(root, "rev-parse", "HEAD") == main_before
    assert git(root, "status", "--porcelain") == ""

    assert TOKEN not in out + err
    assert all(TOKEN not in " ".join(c["argv"]) for c in calls)
    assert all(TOKEN not in json.dumps(c["env"]) for c in calls)
    assert TOKEN not in (root / ".git" / "config").read_text(encoding="utf-8")


def test_notes_md_alone_is_proposed(capsys, root, bare, render_log):
    (root / NOTES).write_text("# notes\n- new\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "awaiting_owner_review"), result
    assert git(bare, "diff", "--name-only", "main", BRANCH).splitlines() == [NOTES]


def test_nothing_to_propose(capsys, root, bare, github, render_log):
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (0, "nothing_to_admit")
    assert branches(bare) == {"main"}
    assert github.pulls() == []


def _set(path, value):
    def change(doc):
        *parents, leaf = path
        target = doc
        for key in parents:
            target = target[key]
        target[leaf] = value

    return change


FORBIDDEN_KEYS = [
    ("matchups[0].play", _set(("matchups", 0, "play"), {"in": [], "out": []})),
    ("matchups[0].stance", _set(("matchups", 0, "stance"), "hate")),
    ("matchups[0].extra", _set(("matchups", 0, "extra"), "new key")),
    ("gaps", _set(("gaps",), [])),
    ("matchups", lambda doc: doc["matchups"].pop()),
    ("matchups", lambda doc: doc["matchups"].reverse()),
]


@pytest.mark.parametrize(("where", "change"), FORBIDDEN_KEYS, ids=[f[0] for f in FORBIDDEN_KEYS])
def test_a_forbidden_boarding_plan_key_is_refused(
    capsys, root, bare, github, render_log, where, change
):
    edit_plan(root, change)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert where in result["message"]
    assert rendered_log(render_log) == []
    assert branches(bare) == {"main"}
    assert github.pulls() == []


FORBIDDEN_FILES = [
    f"decks/{SLUG}/decklist.txt",
    f"decks/{SLUG}/evidence/run.json",
    f"decks/{SLUG}/sim-profiles.json",
    f"decks/{SLUG}/evidence-spec.json",
    f"decks/{SLUG}/shell.json",
    f"decks/{SLUG}/archetype.json",
    f"decks/{SLUG}/plays/fetch-untapped-shock.json",
    GAMEPLAN,
    OLD_PLAN,
    "meta/card-data.json",
    "scripts/render_guides.py",
    "new-file.txt",
]


@pytest.mark.parametrize("path", FORBIDDEN_FILES)
def test_a_forbidden_file_is_refused(capsys, root, bare, github, render_log, path):
    edit_plan(root, good_edit)
    (root / path).write_text("changed\n", encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "refused"), result
    assert path in result["files"]
    assert rendered_log(render_log) == []
    assert branches(bare) == {"main"}
    assert github.pulls() == []


def test_a_render_check_failure_is_gate_failed_and_leaves_the_edit(
    capsys, monkeypatch, root, bare, github, render_log
):
    monkeypatch.setenv("MTG_TEST_RENDER_CHECK_FAIL", "1")
    edit_plan(root, good_edit)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "gate_failed"), result
    assert branches(bare) == {"main"}
    assert github.pulls() == []
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(root, "branch", "--list", BRANCH) == ""
    # The agent's edit stays for it to fix; the half-rendered guide does not.
    assert git(root, "status", "--porcelain") == f"M {PLAN}"


def test_a_missing_renderer_is_gate_failed(capsys, tmp_path, github):
    bare = seed_guides(tmp_path, render_guides=False)
    root = clone(bare, tmp_path / "guides")
    edit_plan(root, good_edit)
    rc, result, _, _ = propose(capsys, root)
    assert (rc, result["result"]) == (1, "gate_failed"), result
    assert "render_guides.py" in result["message"]
    assert branches(bare) == {"main"}


def test_the_newest_run_is_the_one_allowed(capsys, root, bare, render_log):
    # The older run's plan is a different path, refused even for play_plan.
    doc = json.loads((root / OLD_PLAN).read_text(encoding="utf-8"))
    doc["matchups"][0]["play_plan"]["our_tactic"] = "stale"
    (root / OLD_PLAN).write_text(dump(doc), encoding="utf-8")
    rc, result, _, _ = propose(capsys, root)
    assert result["result"] == "refused"
    assert result["files"] == [OLD_PLAN]
    assert branches(bare) == {"main"}


# ---- one branch and one pull request per proposing seat -----------------------------------
# Live, game 8a55cab7: the boros seat and the frog seat both proposed on
# `seat/<game>-guides`, so mtg-decks PR #123 carried both decks' commits.


def test_the_branch_names_the_game_the_deck_and_the_role():
    from magic_cards_model_api.playscripts.guides_propose import guides_branch

    assert guides_branch("g1", "boros-aggro", "seat") == "seat/g1-boros-aggro-seat-guides"
    assert guides_branch("g1", "dimir-frog", "seat") == "seat/g1-dimir-frog-seat-guides"
    assert guides_branch("g1", "dimir-frog", "advisor") == "seat/g1-dimir-frog-advisor-guides"


def test_a_seat_and_a_buddy_of_one_game_each_open_their_own_pull_request(
    capsys, monkeypatch, tmp_path, bare, github, render_log
):
    seat_clone = clone(bare, tmp_path / "seat-guides")
    buddy_clone = clone(bare, tmp_path / "buddy-guides")

    monkeypatch.setenv("MTG_ROLE", "seat")
    edit_plan(seat_clone, good_edit)
    rc, seat, _, _ = propose(capsys, seat_clone)
    assert (rc, seat["result"]) == (0, "awaiting_owner_review"), seat

    monkeypatch.setenv("MTG_ROLE", "advisor")
    (buddy_clone / NOTES).write_text("# notes\n- the buddy saw a race\n", encoding="utf-8")
    rc, buddy, _, _ = propose(capsys, buddy_clone)
    assert (rc, buddy["result"]) == (0, "awaiting_owner_review"), buddy

    assert (seat["branch"], buddy["branch"]) == (BRANCH, ADVISOR_BRANCH)
    assert seat["pr_url"] != buddy["pr_url"]
    assert [p["body"]["head"] for p in github.pulls()] == [BRANCH, ADVISOR_BRANCH]
    assert {BRANCH, ADVISOR_BRANCH} <= branches(bare)
    # Each branch carries its own proposer's one commit and nothing else.
    for branch, author in ((BRANCH, "mtg-seat"), (ADVISOR_BRANCH, "mtg-buddy")):
        log = git(bare, "log", "--format=%an", f"main..{branch}").splitlines()
        assert log == [f"{author} {SLUG}"], (branch, log)


def test_the_same_seat_proposing_again_reuses_its_own_branch(
    capsys, root, bare, github, render_log
):
    edit_plan(root, good_edit)
    rc, first, _, _ = propose(capsys, root)
    assert (rc, first["result"]) == (0, "awaiting_owner_review"), first
    rc, again, _, _ = propose(capsys, root)
    assert (rc, again["result"]) == (0, "awaiting_owner_review"), again
    assert first["branch"] == again["branch"] == BRANCH
    assert [p["body"]["head"] for p in github.pulls()] == [BRANCH, BRANCH]
    assert branches(bare) == {"main", BRANCH}
