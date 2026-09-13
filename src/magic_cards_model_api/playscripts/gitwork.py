"""Git and GitHub plumbing shared by `plays admit` and `guides propose`.

Every command either of them runs goes through `run`, one place to read what
reaches a subprocess. The GitHub token is read only through git's credential
helper (the one A13's `mtg_agent.wrapper.bootstrap.write_gitconfig` writes,
reading the mounted Secret file) and, failing that, that mounted file itself.
It is used for exactly one thing - the `Authorization` header of a REST call -
and is never printed, never put in argv or a subprocess environment, and
never written to `.git/config`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from mtg_agent.wrapper.bootstrap import GH_TOKEN_FILE, GUIDES_DIR

#: The default `--root` of every command that works the pod's guides clone.
GUIDES_ROOT = Path(GUIDES_DIR)
MAIN = "main"
UPSTREAM = f"origin/{MAIN}"
DEFAULT_GITHUB_API = "https://api.github.com"
#: Push attempts onto main before falling back to a branch and a pull request.
PUSH_ATTEMPTS = 3
DELTAS_FILE = "mtg-plays-deltas.json"
PRIORITY_MIN, PRIORITY_MAX = 0, 100
#: MTG_ROLE -> the name a commit is authored under.
ROLE_NAMES = {"seat": "seat", "advisor": "buddy"}

#: The results a command prints. `error` is an infrastructure failure (no
#: token, GitHub refused, git itself broke), never a verdict on the change.
#: `pr_opened` is `plays admit`'s own rare fallback (3 non-fast-forward
#: pushes); `awaiting_owner_review` is every `guides propose` pull request -
#: mtg-decks has no CI budget, so the owner merges guide PRs by hand.
SUCCESS = frozenset({"admitted", "pr_opened", "awaiting_owner_review", "nothing_to_admit"})

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


def identity(deck: str) -> tuple[str, str]:
    """`mtg-<seat|buddy> <deck>` and `mtg-<seat|buddy>@mtg-trainer.invalid`."""
    role = os.environ.get("MTG_ROLE", "").strip() or "seat"
    if role not in ROLE_NAMES:
        raise Outcome("refused", f"MTG_ROLE {role!r} is neither 'seat' nor 'advisor'")
    who = f"mtg-{ROLE_NAMES[role]}"
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
        with tempfile.TemporaryDirectory() as tmp:
            names = []
            for label, text in (("current", current), ("base", base), ("other", other)):
                path = Path(tmp) / label
                path.write_text(text, encoding="utf-8")
                names.append(str(path))
            done = self.run("merge-file", "-p", "-q", *names)
        return done.stdout if done.returncode == 0 else None


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


# -- the token and the pulls endpoint --------------------------------------------------------


def github_token(git: Git) -> str:
    """Through git's credential helper first, then the mounted Secret file."""
    done = run(
        ["git", "credential", "fill"],
        git.root,
        input="protocol=https\nhost=github.com\n\n",
        env=git.env,
    )
    if done.returncode == 0:
        for line in done.stdout.splitlines():
            if line.startswith("password=") and line[len("password=") :]:
                return line[len("password=") :]
    path = Path(os.environ.get("GH_PAT_FILE") or GH_TOKEN_FILE)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if token:
        return token
    raise Outcome(
        "error",
        f"no GitHub token: the git credential helper answered none and {path} is not readable",
    )


def github_repo(git: Git) -> str:
    """owner/name: MTG_GITHUB_REPO, else read off the origin URL."""
    configured = os.environ.get("MTG_GITHUB_REPO", "").strip()
    if configured:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", configured):
            raise Outcome("error", "MTG_GITHUB_REPO is not owner/name")
        return configured
    url = git.out("remote", "get-url", "origin").strip()
    match = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
    if match is None:
        raise Outcome("error", "origin is not a github.com repository and MTG_GITHUB_REPO is unset")
    return f"{match.group(1)}/{match.group(2)}"


def _github(method: str, url: str, token: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "mcp-seat",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def open_pull_request(git: Git, branch: str, title: str, body: str) -> str:
    """The html_url of a pull request from `branch` into main, an already
    open one for that branch included."""
    api = os.environ.get("MTG_GITHUB_API", "").strip().rstrip("/") or DEFAULT_GITHUB_API
    repo = github_repo(git)
    token = github_token(git)
    pulls = f"{api}/repos/{repo}/pulls"
    try:
        created = _github(
            "POST", pulls, token, {"title": title, "head": branch, "base": MAIN, "body": body}
        )
        return str(created["html_url"])
    except urllib.error.HTTPError as exc:
        if exc.code == 422:
            owner = repo.split("/")[0]
            query = urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": "open"})
            try:
                existing = _github("GET", f"{pulls}?{query}", token)
            except (urllib.error.URLError, OSError, ValueError):
                existing = []
            if isinstance(existing, list) and existing and "html_url" in existing[0]:
                return str(existing[0]["html_url"])
        raise Outcome("error", f"GitHub refused the pull request: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        raise Outcome(
            "error", f"the pull request could not be opened: {type(exc).__name__}"
        ) from None
