"""Per-checkout `docker compose` project name and postgres host port.

Every `git worktree` checkout of this repo lives in a directory whose
basename is also `magic-cards-model-api` (worktrees keep the repo's own
name), and none of `mise.toml`'s compose-invoking tasks ever pass
`--project-name`/`COMPOSE_PROJECT_NAME` - so `docker compose` falls back to
its own default, the compose file directory's basename, and every worktree
derives the SAME project name as the main checkout.

That collision is silent and destructive: `docker compose up -d postgres`
from a worktree attaches to the project name the main checkout is already
using, which means the SAME named volume (`<project>_postgres-data`) and the
SAME container (`<project>-postgres-1`) - but `scripts/postgres-password.sh`
reads a *per-checkout* password (the `POSTGRES_PASSWORD` env var, else the
gitignored `.postgres-password` file, which is never shared between
checkouts). Whichever checkout ran `docker compose up` most recently is the
one whose password the shared volume's already-initialised postgres actually
has - the other checkout is now locked out of its own database.

A git worktree checkout has a `.git` FILE (`gitdir: <path-to-.git/worktrees/
name>`); the main checkout has a `.git` DIRECTORY. That is the one signal
that tells the two apart without relying on the directory name, which is
identical everywhere. The main checkout keeps docker compose's own default
project name, so its existing volume and container stay attached with no
migration; a worktree gets a name suffixed with a short hash of its own
absolute path, so two worktrees never collide with each other either.

The postgres host port collides too - the main checkout's `postgres` is
routinely already bound to host port 5432, so a worktree's `docker compose
up -d postgres` would fail outright to bind that port a second time. A
worktree therefore asks for host port `0` (docker picks any free ephemeral
port); the caller reads the ACTUAL bound port back with
`docker compose port postgres 5432` after bringing the service up - see
`mise.toml`'s `test:pg`/`eval:gauntlet` tasks, which never hardcode `:5432`
in the connection string they build.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

BASE_PROJECT_NAME = "magic-cards-model-api"


def is_worktree_checkout(repo_root: Path) -> bool:
    """True when `repo_root` is a `git worktree add` checkout rather than the
    main checkout - detected by `.git` being a file (`gitdir: ...`) instead
    of a directory. Missing entirely (not a git checkout at all) counts as
    the main case: nothing to disambiguate from."""
    git_path = repo_root / ".git"
    return git_path.is_file()


def compose_project_name(repo_root: Path, base_name: str = BASE_PROJECT_NAME) -> str:
    """The main checkout keeps `base_name` unchanged - docker compose's own
    default for this directory - so its existing container/volume stay
    attached. A worktree gets `base_name` plus an 8-hex-char digest of its
    own resolved absolute path: deterministic across repeated runs from the
    same worktree, and distinct across different worktrees."""
    if not is_worktree_checkout(repo_root):
        return base_name
    digest = hashlib.sha256(str(repo_root.resolve()).encode()).hexdigest()[:8]
    return f"{base_name}-{digest}"


def postgres_host_port(repo_root: Path, default_port: int = 5432) -> int:
    """`0` tells `docker compose`/`docker run` to bind an OS-assigned
    ephemeral host port instead of a fixed one. The main checkout keeps the
    fixed default (existing tooling and habits point at `localhost:5432`);
    a worktree gets `0` so any number of worktrees' postgres services can run
    at once without a host-port collision, either with the main checkout or
    with each other."""
    if is_worktree_checkout(repo_root):
        return 0
    return default_port


def render_env(repo_root: Path) -> str:
    """`COMPOSE_PROJECT_NAME=...`/`POSTGRES_HOST_PORT=...` lines, safe to
    `eval` directly in the `sh` task bodies `mise.toml` runs - the derivation
    lives in one Python module tests can exercise directly rather than being
    re-typed as shell in every task that needs it."""
    project = compose_project_name(repo_root)
    port = postgres_host_port(repo_root)
    return f"COMPOSE_PROJECT_NAME={project}\nPOSTGRES_HOST_PORT={port}\n"


def main() -> None:
    print(render_env(Path(__file__).resolve().parent.parent), end="")


if __name__ == "__main__":
    main()
