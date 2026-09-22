"""Git plumbing shared by `plays admit` and `guides propose`.

Every command either of them runs goes through `run`, one place to read what
reaches a subprocess. Both push straight to mtg-decks main and never talk to
the GitHub REST API: git authenticates the push itself, through the
credential helper A13's `mtg_agent.wrapper.bootstrap.write_gitconfig` writes
(it reads the mounted Secret file each time), so no code here ever holds the
token.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from mtg_agent.wrapper.bootstrap import GUIDES_DIR

#: The default `--root` of every command that works the pod's guides clone.
GUIDES_ROOT = Path(GUIDES_DIR)
MAIN = "main"
UPSTREAM = f"origin/{MAIN}"
#: Push attempts onto main before giving up with a retryable `error`, the
#: change left uncommitted in the checkout for the next run.
PUSH_ATTEMPTS = 3
DELTAS_FILE = "mtg-plays-deltas.json"
#: Where each game first found each play's priority, for the per-game bound.
GAME_PRIORITY_FILE = "mtg-plays-priority-games.json"
#: How far one `plays priority` call, and one game, may move a priority.
PRIORITY_STEP_LIMIT = 5
PRIORITY_MIN, PRIORITY_MAX = 0, 100
#: The post-game reviewer's own role. It is a THIRD writer for the same game
#: and the same deck as the seat that played it and the buddy that watched
#: it, so it gets a role of its own and its commits on main are authored as
#: `mtg-reviewer`, never passed off as the seat's.
REVIEWER_ROLE = "reviewer"

#: MTG_ROLE -> the name a commit is authored under.
ROLE_NAMES = {"seat": "seat", "advisor": "buddy", REVIEWER_ROLE: "reviewer"}

#: How long a writer waits for the `/guides` checkout before giving up
#: (`checkout_lock`). Generous enough to cover a whole `plays admit` -
#: fetch, rebase, the in-pod gate, up to three push attempts - and far
#: inside `handover.HANDOVER_BUDGET_SECONDS`, so a handover turn that finds
#: the checkout busy still has time to say so rather than being killed
#: mid-wait with no result at all.
CHECKOUT_LOCK_TIMEOUT = 300.0

#: The lock file, inside the checkout it guards: one file per checkout, so
#: two unrelated clones never contend, and inside `.git/` so it can never be
#: mistaken for a tracked change by `git.changed_paths()` (which is exactly
#: how a lock file at the checkout root would have made every `admit` refuse
#: itself with "plays admit only takes changes under decks/*/plays/").
CHECKOUT_LOCK_NAME = "mcm-checkout.lock"

#: The results journal, beside the lock inside `.git/`: one JSON line per
#: `plays priority`, `plays admit`, `plays no-change` and `guides propose`
#: run, `{ts, command, game_id, decks, role, result, sha?, files?,
#: dsl_change?, ...}`. The driver reads it through its read-only /guides
#: mount after the handover turn to learn what the seat actually landed -
#: the command output itself only ever reaches the agent. Inside `.git/` for
#: the lock's reason: `changed_paths()` never sees it, so it can never make
#: an admit refuse itself.
RESULTS_JOURNAL = "mcm-results.jsonl"

#: The result keys a journal line copies from a command's own result, when
#: the command set them.
_JOURNALED_KEYS = ("sha", "files", "dsl_change", "retryable", "evidence")


@contextlib.contextmanager
def checkout_lock(root: Path | str, timeout: float | None = None) -> Iterator[None]:
    """Hold the `/guides` checkout exclusively for the body of the `with`.

    **Defect D-1.** Both write channels run against the same single clone:
    `plays admit` does a fetch-and-rebase and then pushes `HEAD:main` with up
    to three attempts, and `guides propose` resets the checkout onto origin
    main, commits there and pushes that. A second committer inside the same working tree during
    either sequence does not merely lose a race - it corrupts it, because the
    index, HEAD and the working tree are shared state that neither sequence
    re-reads between its steps. The post-game AI review runs on a background
    thread while the handover turn is pushed into the interactive session
    concurrently, and the handover turn's step 2 is `mcp-seat plays admit`,
    so the two really can arrive together.

    **Why a file lock and not a `threading.Lock`.** The writers are separate
    PROCESSES in one container - the reviewer's own `claude --print` tool
    call and `mcp-seat` - so an in-process lock would guard nothing. `flock`
    is held by the open file description and released by the kernel when the
    holder exits, so a `mcp-seat` that is killed mid-admit does not leave the
    checkout locked for ever.

    **Bounded.** A wedged holder must not hang a handover inside its own time
    budget, so the wait gives up after `CHECKOUT_LOCK_TIMEOUT` and raises
    `Outcome("error", ...)` naming the checkout - an infrastructure failure,
    never a verdict on the change, which is exactly what `error` means here.
    """
    root = Path(root)
    limit = CHECKOUT_LOCK_TIMEOUT if timeout is None else timeout
    # Inside `.git/` when there is one, so `git status` never sees it; beside
    # the checkout otherwise, which is the case in a test tmpdir and for a
    # root that is not a clone at all.
    holder = root / ".git" if (root / ".git").is_dir() else root
    path = holder / CHECKOUT_LOCK_NAME
    try:
        holder.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
    except OSError as exc:
        raise Outcome("error", f"could not open the guides checkout lock {path}: {exc}") from exc
    deadline = time.monotonic() + limit
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise Outcome(
                        "error", f"could not lock the guides checkout {root}: {exc}"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise Outcome(
                        "error",
                        f"the guides checkout {root} was still held by another writer after "
                        f"{limit:g}s; nothing was changed",
                    ) from exc
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def journal_path(root: Path | str) -> Path:
    """`<git-dir>/mcm-results.jsonl`: `.git/` of a plain clone, beside the
    checkout otherwise (the lock's own rule)."""
    root = Path(root)
    holder = root / ".git" if (root / ".git").is_dir() else root
    return holder / RESULTS_JOURNAL


def append_journal_line(root: Path | str, entry: dict[str, Any]) -> None:
    """Append `entry` as one line. Never raises: the journal is a record of a
    command, and a record that could not be written must not turn a push
    that landed into a failure. One `write` of one line to a file opened for
    append, so a line is never interleaved with another writer's."""
    try:
        line = json.dumps(entry, sort_keys=True, default=str) + "\n"
        with journal_path(root).open("a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception:  # noqa: BLE001 - see the docstring: never fail the command
        pass


def journal(
    root: Path | str,
    command: str,
    game_id: str | None,
    decks: list[str],
    outcome: dict[str, Any],
    **extra: Any,
) -> None:
    """One journal line for one run of `command` that ended in `outcome`.
    The caller holds `checkout_lock`; `extra` carries what only that command
    knows (`dsl_change`, a no-change's candidate and reason)."""
    try:
        who = os.environ.get("MTG_ROLE", "").strip() or "seat"
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "command": command,
            "game_id": game_id,
            "decks": list(decks),
            "role": who,
            "result": outcome.get("result"),
        }
        entry.update({k: outcome[k] for k in _JOURNALED_KEYS if k in outcome})
        entry.update(extra)
    except Exception:  # noqa: BLE001 - never fail the command
        return
    append_journal_line(root, entry)


def run_journaled(
    root: Path,
    command: str,
    game_id: str | None,
    decks: list[str],
    body: Callable[[], tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Run `body` holding the checkout, journal what it returned, return it.

    `body` returns `(result, extra)`: the command's own result and what only
    it knows for the journal line; an `Outcome` it raises is the result.
    The line is appended under `checkout_lock`. Only a lock that could not
    be taken at all is journaled outside it - still one `write` of one line,
    so still never interleaved - because every run appends one line, and a
    run that found the checkout busy is one the driver needs to see."""
    try:
        with checkout_lock(root):
            try:
                outcome, extra = body()
            except Outcome as refused:
                outcome, extra = refused.result, {}
            journal(root, command, game_id, decks, outcome, **extra)
            return outcome
    except Outcome as busy:
        journal(root, command, game_id, decks, busy.result)
        return busy.result


#: The results a command exits 0 on. `error` is an infrastructure failure
#: (git itself broke, the push was refused for a reason other than a race),
#: never a verdict on the change; one carrying `retryable: true` means main
#: kept moving for `PUSH_ATTEMPTS` pushes and the same command can simply be
#: run again.
SUCCESS = frozenset({"admitted", "nothing_to_admit"})

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_INT = re.compile(r"-?(?:0|[1-9][0-9]*)(?![0-9.eE])")
_REJECTED = re.compile(r"\[rejected\]|non-fast-forward|fetch first")


class Outcome(Exception):
    """A typed result that ends the command early."""

    def __init__(self, kind: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.result = result(kind, message, **extra)


def result(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"result": kind, "message": message, **extra}


def run(
    argv: list[str],
    cwd: Path,
    *,
    input: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """The one subprocess call. Never raises on a non-zero exit."""
    return subprocess.run(
        argv, cwd=cwd, input=input, env=env, capture_output=True, text=True, check=False
    )


def tail(done: subprocess.CompletedProcess[str], lines: int = 40) -> str:
    text = "\n".join(part for part in (done.stdout, done.stderr) if part and part.strip())
    return "\n".join(text.splitlines()[-lines:])


def check_name(label: str, value: str) -> None:
    """A deck slug or game id: it names a branch and a trailer, so a ref-safe word."""
    if not _NAME.match(value) or ".." in value or value.endswith((".lock", ".")):
        raise Outcome("refused", f"{label} {value!r} is not a plain name ([A-Za-z0-9._-])")


def role() -> str:
    """This container's MTG_ROLE, `seat` or `advisor` (`seat` when unset)."""
    value = os.environ.get("MTG_ROLE", "").strip() or "seat"
    if value not in ROLE_NAMES:
        raise Outcome("refused", f"MTG_ROLE {value!r} is neither 'seat' nor 'advisor'")
    return value


def identity(deck: str) -> tuple[str, str]:
    """`mtg-<seat|buddy> <deck>` and `mtg-<seat|buddy>@mtg-trainer.invalid`."""
    who = f"mtg-{ROLE_NAMES[role()]}"
    return f"{who} {deck}", f"{who}@mtg-trainer.invalid"


def commit_message(subject: str, game_id: str, priorities: list[tuple[str, int, int]]) -> str:
    lines = [f"Game-Id: {game_id}"]
    lines += [f"Script-Priority: {pid} {old}->{new}" for pid, old, new in priorities]
    return f"{subject}\n\n" + "\n".join(lines) + "\n"


class Git:
    """git in one checkout, as one author, never prompting."""

    def __init__(self, root: Path, author: tuple[str, str] | None = None) -> None:
        self.root = root
        self.env = dict(os.environ)
        self.env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_ASKPASS": ""})
        if author is not None:
            name, email = author
            self.env.update(
                {
                    "GIT_AUTHOR_NAME": name,
                    "GIT_AUTHOR_EMAIL": email,
                    "GIT_COMMITTER_NAME": name,
                    "GIT_COMMITTER_EMAIL": email,
                }
            )

    def run(self, *args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
        return run(["git", *args], self.root, input=input, env=self.env)

    def out(self, *args: str, input: str | None = None) -> str:
        done = self.run(*args, input=input)
        if done.returncode != 0:
            raise Outcome("error", f"git {args[0]} failed: {tail(done, 10)}")
        return done.stdout

    def ok(self, *args: str) -> bool:
        return self.run(*args).returncode == 0

    def require_main_checkout(self) -> None:
        if not self.root.is_dir() or not self.ok("rev-parse", "--is-inside-work-tree"):
            raise Outcome("error", f"{self.root} is not a git checkout")
        git_dir = Path(self.out("rev-parse", "--absolute-git-dir").strip())
        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            raise Outcome(
                "refused",
                "a rebase is in progress in this checkout; finish or abort it first",
            )
        branch = self.run("symbolic-ref", "--short", "HEAD").stdout.strip()
        if branch != MAIN:
            raise Outcome("refused", f"this runs on the checkout's {MAIN}, not on {branch!r}")
        if not self.ok("rev-parse", "--verify", "-q", f"refs/remotes/{UPSTREAM}"):
            raise Outcome("error", f"{UPSTREAM} is not known in this checkout")

    def changed_paths(self) -> list[str]:
        """Every working-tree and index change, untracked files included,
        both sides of a rename."""
        raw = self.out("status", "--porcelain=v1", "-z", "--untracked-files=all")
        entries = raw.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            paths.append(entry[3:])
            if entry[0] in "RC" and index < len(entries):
                paths.append(entries[index])
                index += 1
        return sorted(set(paths))

    def paths_between(self, base: str, head: str) -> list[str]:
        return sorted(set(self.out("diff", "--name-only", "-z", base, head).split("\0")) - {""})

    def ahead(self, base: str, head: str = "HEAD") -> int:
        return int(self.out("rev-list", "--count", f"{base}..{head}").strip())

    def show(self, rev_path: str) -> str | None:
        done = self.run("show", rev_path)
        return done.stdout if done.returncode == 0 else None

    def head(self) -> str:
        return self.out("rev-parse", "HEAD").strip()

    def fetch(self) -> None:
        self.out("fetch", "-q", "origin", f"+refs/heads/{MAIN}:refs/remotes/{UPSTREAM}")

    def push(self, refspec: str) -> bool:
        """True when pushed, False on a rejection (non-fast-forward or an
        existing branch). Never forced."""
        done = self.run("push", "origin", refspec)
        if done.returncode == 0:
            return True
        if _REJECTED.search(done.stderr):
            return False
        raise Outcome("error", f"git push failed: {tail(done, 10)}")

    def git_path(self, name: str) -> Path:
        path = Path(self.out("rev-parse", "--git-path", name).strip())
        return path if path.is_absolute() else self.root / path

    def merge_file(self, current: str, base: str, other: str) -> str | None:
        """A three-way text merge; None when it conflicts."""
        text, conflicted = self.merge_file_markers(current, base, other)
        return None if conflicted else text

    def merge_file_markers(self, mine: str, base: str, upstream: str) -> tuple[str, bool]:
        """A three-way text merge and whether it conflicted. A conflicted
        merge keeps git's markers, labelled `yours` / `base` / `origin/main`,
        for the agent to resolve by hand."""
        with tempfile.TemporaryDirectory() as tmp:
            names = []
            for label, text in (("yours", mine), ("base", base), ("upstream", upstream)):
                path = Path(tmp) / label
                path.write_text(text, encoding="utf-8")
                names.append(str(path))
            done = self.run(
                "merge-file", "-p", "-q", "-L", "yours", "-L", "base", "-L", UPSTREAM, *names
            )
        if done.returncode < 0 or done.returncode > 127:
            raise Outcome("error", f"git merge-file failed: {tail(done, 10)}")
        return done.stdout, done.returncode != 0


# -- a play's top-level priority, edited in place ------------------------------------------


def _string_end(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == '"':
            return index + 1
        index += 1
    return len(text)


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def top_level_int(text: str, key: str) -> tuple[int, int, int] | None:
    """(start, end, value) of the integer under `key` of the top-level JSON
    object, located in the text itself so an edit can leave every other byte."""
    depth, expecting_key, index = 0, False, 0
    while index < len(text):
        char = text[index]
        if char == '"':
            end = _string_end(text, index)
            if depth == 1 and expecting_key:
                colon = _skip_space(text, end)
                try:
                    name = json.loads(text[index:end])
                except ValueError:
                    return None
                if name == key and text[colon : colon + 1] == ":":
                    match = _INT.match(text, _skip_space(text, colon + 1))
                    if match is None:
                        return None
                    return match.start(), match.end(), int(match.group())
            expecting_key = False
            index = end
            continue
        if char in "{[":
            depth += 1
            expecting_key = char == "{" and depth == 1
        elif char in "}]":
            depth -= 1
        elif char == "," and depth == 1:
            expecting_key = True
        index += 1
    return None


def replace_top_level_int(text: str, key: str, value: int) -> str | None:
    span = top_level_int(text, key)
    if span is None:
        return None
    start, end, _ = span
    return text[:start] + str(value) + text[end:]


def clamp_priority(value: int) -> int:
    return max(PRIORITY_MIN, min(PRIORITY_MAX, value))
