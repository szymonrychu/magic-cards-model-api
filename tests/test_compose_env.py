"""`scripts.compose_env` is what stops every `git worktree` checkout (same
basename `magic-cards-model-api` everywhere) deriving the same `docker
compose` project name as the main checkout - see that module's own docstring
for the corruption this caused for real. These pin the two signals the fix
depends on: `.git` file vs directory tells a worktree apart from the main
checkout, and that in turn drives both the project name and the postgres
host port.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.compose_env import (
    BASE_PROJECT_NAME,
    compose_project_name,
    is_worktree_checkout,
    postgres_host_port,
    render_env,
)


def _make_checkout(tmp_path: Path, name: str, *, worktree: bool) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    if worktree:
        (root / ".git").write_text("gitdir: /somewhere/.git/worktrees/thing\n")
    else:
        (root / ".git").mkdir()
    return root


def test_main_checkout_has_a_directory_dot_git(tmp_path: Path) -> None:
    root = _make_checkout(tmp_path, "magic-cards-model-api", worktree=False)
    assert is_worktree_checkout(root) is False


def test_worktree_checkout_has_a_file_dot_git(tmp_path: Path) -> None:
    root = _make_checkout(tmp_path, "magic-cards-model-api", worktree=True)
    assert is_worktree_checkout(root) is True


def test_a_checkout_with_no_dot_git_at_all_counts_as_main(tmp_path: Path) -> None:
    root = tmp_path / "magic-cards-model-api"
    root.mkdir()
    assert is_worktree_checkout(root) is False


def test_main_checkout_keeps_dockers_own_default_project_name(tmp_path: Path) -> None:
    """So its existing container/volume (named off that default) stay
    attached with no migration."""
    root = _make_checkout(tmp_path, "magic-cards-model-api", worktree=False)
    assert compose_project_name(root) == BASE_PROJECT_NAME


def test_worktree_project_name_is_suffixed_and_distinct(tmp_path: Path) -> None:
    a = _make_checkout(tmp_path, "worktree-a", worktree=True)
    b = _make_checkout(tmp_path, "worktree-b", worktree=True)
    name_a = compose_project_name(a)
    name_b = compose_project_name(b)
    assert name_a.startswith(f"{BASE_PROJECT_NAME}-")
    assert name_b.startswith(f"{BASE_PROJECT_NAME}-")
    assert name_a != name_b
    assert name_a != BASE_PROJECT_NAME


def test_worktree_project_name_is_deterministic_for_the_same_path(tmp_path: Path) -> None:
    root = _make_checkout(tmp_path, "worktree-a", worktree=True)
    assert compose_project_name(root) == compose_project_name(root)


def test_main_checkout_keeps_the_fixed_postgres_port(tmp_path: Path) -> None:
    root = _make_checkout(tmp_path, "magic-cards-model-api", worktree=False)
    assert postgres_host_port(root) == 5432


def test_worktree_gets_an_ephemeral_postgres_port(tmp_path: Path) -> None:
    """`0` tells docker to bind any free host port - never the fixed 5432 the
    main checkout's own postgres is routinely already holding."""
    root = _make_checkout(tmp_path, "worktree-a", worktree=True)
    assert postgres_host_port(root) == 0


def test_render_env_is_eval_safe_shell_assignment_lines(tmp_path: Path) -> None:
    root = _make_checkout(tmp_path, "magic-cards-model-api", worktree=False)
    rendered = render_env(root)
    assert rendered == "COMPOSE_PROJECT_NAME=magic-cards-model-api\nPOSTGRES_HOST_PORT=5432\n"


@pytest.mark.parametrize("name", ["magic-cards-model-api"])
def test_worktree_names_never_collide_with_the_main_checkouts_name(
    tmp_path: Path, name: str
) -> None:
    """The whole bug was two DIFFERENT checkouts landing on the SAME name -
    pin that a worktree sharing the exact same basename as the main checkout
    still gets a different project name."""
    main = _make_checkout(tmp_path / "main", name, worktree=False)
    worktree = _make_checkout(tmp_path / "wt", name, worktree=True)
    assert compose_project_name(main) != compose_project_name(worktree)
