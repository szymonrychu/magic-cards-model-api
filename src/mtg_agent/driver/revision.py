"""Which revision of the deck-guides corpus this container is playing against.

The guides are a third repository (`github.com/szymonrychu/mtg-decks`) and
they reach a container two ways, which have to answer the same question the
same way:

- **kubernetes** (`deploy/seat-job.yaml`): the `guides` initContainer fetches
  `GUIDES_REF` (default `main`) into an emptyDir and stamps the sha it
  checked out into `GUIDES_REVISION_FILE` inside that volume, which the
  driver mounts at `GUIDES_ROOT`.
- **compose** (`docker-compose.yml`): `${MTG_DECKS}` is bind-mounted at
  `/guides`, so there is no stamp - but the operator's own checkout comes
  with a real `.git`, and the revision is read straight out of it.

The `.git` half is read by hand rather than by shelling out to `git`: the
driver image carries no git binary, and the bind-mounted checkout belongs to
another uid, so `git rev-parse` there refuses on `safe.directory` before it
prints anything. Reading `HEAD` (and `packed-refs`, and the `gitdir:` file a
linked worktree has instead of a directory) is the whole of what `rev-parse
HEAD` does for this case anyway.

This is a stamp, not a pin: `main` stays the default because play wants
current guides. Only an evaluation harness, which needs two runs to have read
the same corpus, sets `GUIDES_REF` to something fixed.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Where the kubernetes `guides` initContainer records what it checked out,
#: relative to the guides root. Named here and asserted against the committed
#: Job template by `tests/test_k8s_seats.py`, so the two ends cannot drift.
GUIDES_REVISION_FILE = ".guides-revision"

#: What the briefing and the plan-file header say when neither mechanism left
#: a revision behind - guides copied into place by hand, say. Never an
#: exception: a missing revision costs a game its provenance, not its seat.
UNKNOWN_REVISION = "unknown"

_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def guides_revision(root: Path | str) -> str:
    """The corpus revision under `root`, or `UNKNOWN_REVISION`."""
    root = Path(root)
    return _from_stamp(root) or _from_git(root) or UNKNOWN_REVISION


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _from_stamp(root: Path) -> str:
    return _read(root / GUIDES_REVISION_FILE)


def _git_dir(root: Path) -> Path | None:
    """`root/.git`, following the `gitdir: <path>` file a linked worktree has
    in place of a directory."""
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    pointer = _read(dot_git)
    if not pointer.startswith("gitdir:"):
        return None
    target = Path(pointer[len("gitdir:") :].strip())
    if not target.is_absolute():
        target = root / target
    return target if target.is_dir() else None


def _from_git(root: Path) -> str:
    git_dir = _git_dir(root)
    if git_dir is None:
        return ""
    head = _read(git_dir / "HEAD")
    if not head.startswith("ref:"):
        return head if _SHA.match(head) else ""
    ref = head[len("ref:") :].strip()
    return _loose_ref(git_dir, ref) or _packed_ref(git_dir, ref)


def _loose_ref(git_dir: Path, ref: str) -> str:
    value = _read(git_dir / ref)
    return value if _SHA.match(value) else ""


def _packed_ref(git_dir: Path, ref: str) -> str:
    """`git pack-refs` (and every fresh clone) moves refs out of their own
    files into one `packed-refs` list; a checkout that has been packed is the
    ordinary case, not an exotic one."""
    for line in _read(git_dir / "packed-refs").splitlines():
        if line.startswith(("#", "^")):
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _SHA.match(sha):
            return sha
    return ""
