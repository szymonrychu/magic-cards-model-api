"""Which revision of the deck-guides corpus a container is playing against.

Two mechanisms put the guides on a container's filesystem and this module
has to answer the same way for both:

- kubernetes (`deploy/seat-job.yaml`) clones the corpus into an emptyDir and
  stamps the checked-out sha into `.guides-revision` inside it;
- compose bind-mounts the operator's own `$MTG_DECKS` checkout at `/guides`,
  where there is no stamp but there IS a real `.git` to read the revision
  out of.

The `.git` reading is done by hand rather than by shelling out to `git`,
because the driver image has no git binary and the bind-mounted checkout is
owned by another uid - `git rev-parse` there fails on `safe.directory`
before it prints anything.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mtg_agent.driver.revision import (
    GUIDES_REVISION_FILE,
    UNKNOWN_REVISION,
    guides_revision,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "guides"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("guides\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "first")
    return repo


def test_the_stamp_file_wins_when_the_init_container_wrote_one(tmp_path: Path) -> None:
    root = tmp_path / "guides"
    root.mkdir()
    (root / GUIDES_REVISION_FILE).write_text("cafebabe" * 5 + "\n")
    assert guides_revision(root) == "cafebabe" * 5


def test_a_bind_mounted_checkout_answers_from_its_own_git(checkout: Path) -> None:
    assert guides_revision(checkout) == _git(checkout, "rev-parse", "HEAD")


def test_a_checkout_whose_refs_have_been_packed_still_answers(checkout: Path) -> None:
    head = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "pack-refs", "--all")
    assert not (checkout / ".git" / "refs" / "heads" / "main").exists()
    assert guides_revision(checkout) == head


def test_a_detached_head_answers_with_the_commit_it_is_on(checkout: Path) -> None:
    head = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "-q", "--detach", head)
    assert guides_revision(checkout) == head


def test_a_worktree_style_git_file_is_followed(checkout: Path, tmp_path: Path) -> None:
    """`$MTG_DECKS` may itself be a git worktree, where `.git` is a file
    pointing at the real gitdir rather than a directory."""
    linked = tmp_path / "linked"
    _git(checkout, "worktree", "add", "-q", "--detach", str(linked))
    assert (linked / ".git").is_file()
    assert guides_revision(linked) == _git(checkout, "rev-parse", "HEAD")


def test_guides_with_no_git_and_no_stamp_are_unknown_not_a_crash(tmp_path: Path) -> None:
    root = tmp_path / "guides"
    root.mkdir()
    assert guides_revision(root) == UNKNOWN_REVISION


def test_a_missing_guides_root_is_unknown_not_a_crash(tmp_path: Path) -> None:
    assert guides_revision(tmp_path / "nope") == UNKNOWN_REVISION


def test_an_empty_stamp_file_falls_back_to_git(checkout: Path) -> None:
    (checkout / GUIDES_REVISION_FILE).write_text("\n")
    assert guides_revision(checkout) == _git(checkout, "rev-parse", "HEAD")
