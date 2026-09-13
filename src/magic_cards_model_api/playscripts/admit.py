"""`mcp-seat plays priority` and `mcp-seat plays admit`.

`priority` edits one play's top-level `priority` in place - clamped to
0..100, every other byte of the file left alone - and records the change in
the untracked `.git/mtg-plays-deltas.json`, so a rebase that meets someone
else's priority change can replay this one on top of theirs.

`admit` takes a checkout's play-script changes to mtg-decks main:

1. refuses (`refused`) any change outside `decks/*/plays/**`, in the working
   tree or in a local commit not yet on origin main;
2. commits as `mtg-<seat|buddy> <deck>` with a `Game-Id` trailer and one
   `Script-Priority: <id> <old>-><new>` per priority change;
3. fetches origin main and rebases; a conflict that is only priority is
   resolved by re-applying the recorded delta onto upstream's value, any
   other aborts the rebase (`conflict`, the commit left intact);
4. gates: `mcp-seat plays check` and the clone's `scripts/check_plays.py`,
   run with `python3` because the agent image has no mise (`gate_failed`,
   fail closed when the script is missing);
5. ratchets conflicts: a play-conflict or next-conflict warning present after
   the change and not before it is refused (`conflict_regression`);
6. pushes `HEAD:main`, never forced; a non-fast-forward goes back to 3, at
   most three pushes, then the commit goes to `seat/<game>-plays` and a pull
   request (`pr_opened`).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import gitwork
from .gitwork import (
    MAIN,
    PUSH_ATTEMPTS,
    UPSTREAM,
    Git,
    Outcome,
    check_name,
    clamp_priority,
    commit_message,
    identity,
    replace_top_level_int,
    result,
    tail,
    top_level_int,
)
from .inputs import INPUTS_FILE
from .lint import CardIndex, PlaysWarning, check_deck

REGRESSION_ADVICE = (
    "extend the plays' conditions so that at most one applies (an input condition such as "
    "opposing_archetype, a step or whose_turn, or a condition one play requires and the other "
    "excludes); reordering priority does not resolve a conflict"
)


def is_plays_path(path: str) -> bool:
    parts = path.split("/")
    return len(parts) >= 4 and parts[0] == "decks" and parts[2] == "plays"


def _play_file(path: str) -> bool:
    parts = path.split("/")
    return (
        is_plays_path(path)
        and len(parts) == 4
        and path.endswith(".json")
        and (parts[3] != INPUTS_FILE)
    )


def _priority_of(text: str | None) -> int | None:
    span = top_level_int(text, "priority") if text is not None else None
    return span[2] if span else None


# -- the deltas file ----------


def _load_deltas(git: Git) -> dict[str, Any]:
    try:
        doc = json.loads(git.git_path(gitwork.DELTAS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _save_deltas(git: Git, deltas: dict[str, Any]) -> None:
    path = git.git_path(gitwork.DELTAS_FILE)
    if not deltas:
        path.unlink(missing_ok=True)
        return
    path.write_text(json.dumps(deltas, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _entry(path: str, old: int, new: int) -> dict[str, Any]:
    parts = path.split("/")
    return {"deck": parts[1], "id": Path(parts[3]).stem, "old": old, "new": new, "delta": new - old}


# -- plays priority ----------


def set_priority(
    root: Path, deck: str, play_id: str, *, delta: int | None, value: int | None
) -> dict[str, Any]:
    try:
        return _set_priority(Path(root).resolve(), deck, play_id, delta, value)
    except Outcome as outcome:
        return outcome.result


def _set_priority(
    root: Path, deck: str, play_id: str, delta: int | None, value: int | None
) -> dict[str, Any]:
    check_name("deck", deck)
    check_name("play id", play_id)
    git = Git(root)
    if not root.is_dir() or not git.ok("rev-parse", "--is-inside-work-tree"):
        raise Outcome("error", f"{root} is not a git checkout")
    rel = f"decks/{deck}/plays/{play_id}.json"
    path = root / rel
    if not path.is_file():
        raise Outcome("error", f"no play {play_id!r} in deck {deck!r} ({rel})")
    text = path.read_bytes().decode("utf-8")
    old = _priority_of(text)
    if old is None:
        raise Outcome("error", f"{rel} has no top-level integer priority")
    new = clamp_priority(value if value is not None else old + (delta or 0))
    updated = replace_top_level_int(text, "priority", new)
    if updated is None:
        raise Outcome("error", f"{rel} has no top-level integer priority")
    path.write_bytes(updated.encode("utf-8"))

    deltas = _load_deltas(git)
    first = deltas.get(rel, {}).get("old", old)
    if new == first:
        deltas.pop(rel, None)
    else:
        deltas[rel] = _entry(rel, first, new)
    _save_deltas(git, deltas)
    return result(
        "priority_set", f"{play_id} priority {old}->{new}", id=play_id, path=rel, old=old, new=new
    )


# -- plays admit ----------


def admit(root: Path, deck: str, game_id: str) -> dict[str, Any]:
    try:
        return _admit(Path(root).resolve(), deck, game_id)
    except Outcome as outcome:
        return outcome.result


def _admit(root: Path, deck: str, game_id: str) -> dict[str, Any]:
    check_name("deck", deck)
    check_name("game id", game_id)
    git = Git(root, identity(deck))
    git.require_main_checkout()

    changed = git.changed_paths()
    local = git.paths_between(UPSTREAM, "HEAD") if git.ahead(UPSTREAM) else []
    outside = sorted(p for p in set(changed) | set(local) if not is_plays_path(p))
    if outside:
        raise Outcome(
            "refused",
            "plays admit only takes changes under decks/*/plays/; revert these, or propose "
            "guide edits with `mcp-seat guides propose`: " + ", ".join(outside),
            files=outside,
        )
    if not changed and not local:
        _save_deltas(git, {})
        return result("nothing_to_admit", "no play-script change to admit")

    deltas = _sync_deltas(git, sorted(set(changed) | set(local)))
    committed = False
    if changed:
        priorities = _priority_changes(git, changed)
        git.out("add", "-A", "--", *changed)
        message = commit_message(f"Plays: {deck} after game {game_id}", game_id, priorities)
        git.out("commit", "-q", "--no-verify", "-F", "-", input=message)
        committed = True

    for _ in range(PUSH_ATTEMPTS):
        git.fetch()
        _rebase(git, deltas)
        try:
            _gate(git)
            _ratchet(git)
        except Outcome:
            if committed:
                # The change goes back to the index, on the new upstream, for
                # the agent to fix and admit again as one commit.
                git.out("reset", "-q", "--soft", "HEAD~1")
            raise
        if git.push(f"HEAD:refs/heads/{MAIN}"):
            _save_deltas(git, {})
            return result("admitted", f"pushed to {MAIN}", sha=git.head())

    branch = f"seat/{game_id}-plays"
    sha = git.head()
    if not git.push(f"HEAD:refs/heads/{branch}"):
        raise Outcome("error", f"{MAIN} kept moving and branch {branch} already exists on origin")
    url = gitwork.open_pull_request(
        git,
        branch,
        f"Plays: {deck} after game {game_id}",
        f"`mcp-seat plays admit` could not push to {MAIN} after {PUSH_ATTEMPTS} "
        f"non-fast-forward attempts, so the gated change is proposed here.\n\n"
        f"Game-Id: {game_id}\n",
    )
    git.out("branch", "-f", branch, "HEAD")
    git.out("reset", "-q", "--keep", UPSTREAM)
    _save_deltas(git, {})
    return result(
        "pr_opened", f"opened a pull request from {branch}", pr_url=url, branch=branch, sha=sha
    )


def _priority_changes(git: Git, paths: list[str]) -> list[tuple[str, int, int]]:
    """(id, HEAD priority, working priority) for each play whose priority moved."""
    found = []
    for path in paths:
        if not _play_file(path):
            continue
        old = _priority_of(git.show(f"HEAD:{path}"))
        new = _priority_of(_read(git.root / path))
        if old is not None and new is not None and old != new:
            found.append((Path(path).stem, old, new))
    return found


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _sync_deltas(git: Git, paths: list[str]) -> dict[str, Any]:
    """Each changed play's priority delta against origin main's merge base,
    which is what a conflict replays - whether it was made through
    `plays priority` or by hand."""
    base = git.out("merge-base", UPSTREAM, "HEAD").strip()
    deltas: dict[str, Any] = {}
    for path in paths:
        if not _play_file(path):
            continue
        old = _priority_of(git.show(f"{base}:{path}"))
        new = _priority_of(_read(git.root / path))
        if old is not None and new is not None and old != new:
            deltas[path] = _entry(path, old, new)
    _save_deltas(git, deltas)
    return deltas


def _rebase(git: Git, deltas: dict[str, Any]) -> None:
    done = git.run("rebase", "-q", UPSTREAM)
    resolved: dict[str, tuple[int, int]] = {}
    while done.returncode != 0:
        conflicted = [
            p for p in git.out("diff", "--name-only", "--diff-filter=U").splitlines() if p
        ]
        if not conflicted:
            if git.ok("diff", "--cached", "--quiet"):
                # The replayed commit came out empty: nothing of it is left to keep.
                done = git.run("rebase", "--skip")
                continue
            git.run("rebase", "--abort")
            raise Outcome("error", f"git rebase failed: {tail(done, 10)}")
        unresolved = []
        for path in conflicted:
            merged = _replay_priority(git, path, deltas.get(path))
            if merged is None:
                unresolved.append(path)
                continue
            text, upstream, new = merged
            (git.root / path).write_bytes(text.encode("utf-8"))
            git.out("add", "--", path)
            resolved[path] = (upstream, new)
        if unresolved:
            git.run("rebase", "--abort")
            raise Outcome(
                "conflict",
                "rebasing onto origin main conflicts beyond priority; your commit is intact on "
                "HEAD - rebuild the change on origin/main and admit again: "
                + ", ".join(sorted(unresolved)),
                files=sorted(unresolved),
                sha=git.head(),
            )
        done = git.run("rebase", "--continue")
    if resolved:
        _retrail(git, deltas, resolved)


def _replay_priority(
    git: Git, path: str, entry: dict[str, Any] | None
) -> tuple[str, int, int] | None:
    """Upstream's file with this change's other edits merged in and its
    priority moved by the recorded delta; None unless priority is all that
    conflicts."""
    if not entry or not _play_file(path):
        return None
    base, upstream, mine = (git.show(f":{stage}:{path}") for stage in (1, 2, 3))
    if base is None or upstream is None or mine is None:
        return None
    anchor = _priority_of(base)
    upstream_priority = _priority_of(upstream)
    if anchor is None or upstream_priority is None or _priority_of(mine) is None:
        return None
    normalized = [replace_top_level_int(t, "priority", anchor) for t in (upstream, base, mine)]
    if any(text is None for text in normalized):
        return None
    current, common, other = (str(text) for text in normalized)
    merged = git.merge_file(current, common, other)
    if merged is None:
        return None
    new = clamp_priority(upstream_priority + int(entry["delta"]))
    text = replace_top_level_int(merged, "priority", new)
    if text is None:
        return None
    try:
        json.loads(text)
    except ValueError:
        return None
    return text, upstream_priority, new


def _retrail(git: Git, deltas: dict[str, Any], resolved: dict[str, tuple[int, int]]) -> None:
    """The replayed priorities, in the trailers and the deltas file."""
    message = git.out("log", "-1", "--format=%B")
    lines = message.rstrip("\n").split("\n")
    for path, (upstream, new) in resolved.items():
        play_id = Path(path).stem
        deltas[path] = {**deltas[path], "old": upstream, "new": new}
        prefix = f"Script-Priority: {play_id} "
        lines = [
            f"{prefix}{upstream}->{new}" if line.startswith(prefix) else line for line in lines
        ]
    _save_deltas(git, deltas)
    git.out("commit", "-q", "--amend", "--no-verify", "-F", "-", input="\n".join(lines) + "\n")


def _gate(git: Git) -> None:
    root = git.root
    if not (root / "scripts" / "check_plays.py").is_file():
        raise Outcome(
            "gate_failed", "scripts/check_plays.py is missing from the clone; the gate fails closed"
        )
    commands = (
        [
            sys.executable,
            "-m",
            "magic_cards_model_api.seat_cli",
            "plays",
            "check",
            "--root",
            str(root),
        ],
        ["python3", "scripts/check_plays.py"],
    )
    for argv in commands:
        label = " ".join(argv[-4:] if argv[0] == sys.executable else argv)
        try:
            done = gitwork.run(argv, root, env=git.env)
        except OSError as exc:
            raise Outcome("gate_failed", f"{label} could not run: {exc}") from None
        if done.returncode != 0:
            raise Outcome("gate_failed", f"{label} failed", output=tail(done))


def _conflicts(git: Git, rev: str, decks: list[str]) -> dict[tuple[Any, ...], PlaysWarning]:
    """The conflict warnings of `decks` as they stand at `rev`, keyed so a
    pair reads the same whichever play the linter named first."""
    found: dict[tuple[Any, ...], PlaysWarning] = {}
    with tempfile.TemporaryDirectory() as tmp:
        corpus = Path(tmp)
        card_data = git.show(f"{rev}:meta/card-data.json")
        if card_data is None:
            raise Outcome(
                "gate_failed",
                f"meta/card-data.json is missing from {rev}; the conflict ratchet fails closed "
                "rather than lint plays against an empty card index",
            )
        cards_path = corpus / "meta" / "card-data.json"
        cards_path.parent.mkdir(parents=True, exist_ok=True)
        cards_path.write_text(card_data, encoding="utf-8")
        cards = CardIndex.load(cards_path)
        for deck in decks:
            listed = git.out(
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                rev,
                "--",
                f"decks/{deck}/plays",
                f"decks/{deck}/analysis",
            )
            for path in (p for p in listed.split("\0") if p):
                parts = path.split("/")
                target = corpus / path
                if _play_file(path) or (is_plays_path(path) and parts[3] == INPUTS_FILE):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(git.show(f"{rev}:{path}") or "", encoding="utf-8")
                elif len(parts) == 7 and parts[4:6] == ["domain3-matchup-threat", "matchups"]:
                    # Only the stems are read, so no analysis blob is fetched.
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("{}", encoding="utf-8")
            deck_dir = corpus / "decks" / deck
            _, warnings, _ = check_deck(deck_dir, corpus, cards_path, cards)
            for warning in warnings:
                head, rest = warning.plays[:1], warning.plays[1:]
                if warning.code == "play-conflict":
                    key = (deck, warning.code, *sorted(warning.plays))
                else:
                    key = (deck, warning.code, *head, *sorted(rest))
                found[key] = warning
    return found


def _ratchet(git: Git) -> None:
    decks = sorted(
        {p.split("/")[1] for p in git.paths_between(UPSTREAM, "HEAD") if is_plays_path(p)}
    )
    if not decks:
        return
    before = _conflicts(git, UPSTREAM, decks)
    after = _conflicts(git, "HEAD", decks)
    new = [after[key] for key in sorted(set(after) - set(before))]
    if not new:
        return
    named = "; ".join(f"{w.code} {' / '.join(w.plays)}" for w in new)
    raise Outcome(
        "conflict_regression",
        f"this change introduces {len(new)} play conflict(s) that were not there before "
        f"({named}): {REGRESSION_ADVICE}",
        new_conflicts=[
            {"code": w.code, "path": w.path, "plays": w.plays, "message": w.message} for w in new
        ],
    )
