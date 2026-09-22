"""`mcp-seat plays priority` and `mcp-seat plays admit`.

`priority` edits one play's top-level `priority` in place - clamped to
0..100, every other byte of the file left alone - and records the change in
the untracked `.git/mtg-plays-deltas.json`, so a rebase that meets someone
else's priority change can replay this one on top of theirs. It moves a
priority at most `PRIORITY_STEP_LIMIT` (5) per call and, when the game is
known, per game: `.git/mtg-plays-priority-games.json` keeps where each game
first found each play, and survives `admit`. A larger step is refused as
`priority_limit` with the file untouched (game 16a9cf8f lowered two plays by
20 that never had a window).

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
   most three pushes. After the third the commit is soft-reset, the change
   is left uncommitted on the newest upstream, and the result is `error`
   with `retryable: true`: run the same command again.

Before step 2, `admit` picks up the driver's evidence file for this game:
`$MTG_EVIDENCE_DIR/<game_id>/<deck>.json`, when it exists and parses as
JSON, is copied to `decks/<deck>/plays/evidence/<game_id>.json` and lands
with the commit - one file per game, so two games' evidence never conflicts,
and not a play (`_play_file` takes `decks/<deck>/plays/<id>.json` only), so
it is never linted or reprioritised. An admit carrying nothing but evidence
is `admitted` with `dsl_change: false`.

`admitted` also says what landed: `files`, and `dsl_change` - true when a
play file was added or deleted, or changed beyond its top-level `priority`.

`no-change` changes nothing: it records, for one post-game candidate (a play
by `--play`, or a hand-played line by `--line`), why it needs no DSL change.

Every `priority`, `admit` and `no-change` run appends one line to the
checkout's results journal (`gitwork.RESULTS_JOURNAL`), under
`gitwork.checkout_lock`, which the driver reads after the handover turn.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import gitwork
from .gitwork import (
    MAIN,
    PRIORITY_STEP_LIMIT,
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

#: Where the driver drops each game's evidence record for `admit` to pick up
#: (`<dir>/<game_id>/<deck>.json`), set on the agent container.
EVIDENCE_DIR_ENV = "MTG_EVIDENCE_DIR"
#: The per-game evidence files' directory under `decks/<deck>/plays/`.
EVIDENCE_DIR = "evidence"

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


def _load_game_starts(git: Git) -> dict[str, Any]:
    try:
        doc = json.loads(git.git_path(gitwork.GAME_PRIORITY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def set_priority(
    root: Path,
    deck: str,
    play_id: str,
    *,
    delta: int | None,
    value: int | None,
    game_id: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()

    def body() -> tuple[dict[str, Any], dict[str, Any]]:
        done = _set_priority(root, deck, play_id, delta, value, game_id)
        return done, {"files": [done["path"]], "dsl_change": False}

    return gitwork.run_journaled(root, "priority", game_id, [deck], body)


def _set_priority(
    root: Path,
    deck: str,
    play_id: str,
    delta: int | None,
    value: int | None,
    game_id: str | None = None,
) -> dict[str, Any]:
    check_name("deck", deck)
    check_name("play id", play_id)
    if game_id:
        check_name("game id", game_id)
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
    requested = value if value is not None else old + (delta or 0)
    if abs(requested - old) > PRIORITY_STEP_LIMIT:
        raise Outcome(
            "priority_limit",
            f"{play_id}: a priority moves at most {PRIORITY_STEP_LIMIT} per call and per game; "
            f"{old}->{requested} is {requested - old:+d}",
            id=play_id,
            path=rel,
            old=old,
            requested=requested,
            limit=PRIORITY_STEP_LIMIT,
        )
    new = clamp_priority(requested)
    starts = _load_game_starts(git) if game_id else {}
    start = starts.get(game_id, {}).get(rel, old) if game_id else old
    if abs(new - start) > PRIORITY_STEP_LIMIT:
        raise Outcome(
            "priority_limit",
            f"{play_id}: a priority moves at most {PRIORITY_STEP_LIMIT} per game, and this game "
            f"found it at {start}; {new} would move it {new - start:+d}",
            id=play_id,
            path=rel,
            old=old,
            requested=requested,
            game_start=start,
            limit=PRIORITY_STEP_LIMIT,
        )
    updated = replace_top_level_int(text, "priority", new)
    if updated is None:
        raise Outcome("error", f"{rel} has no top-level integer priority")
    path.write_bytes(updated.encode("utf-8"))
    if game_id:
        starts.setdefault(game_id, {}).setdefault(rel, old)
        git.git_path(gitwork.GAME_PRIORITY_FILE).write_text(
            json.dumps(starts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

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
    """One admit, with the checkout held exclusively for the whole of it.

    D-1: `_admit`'s fetch-and-rebase-and-push sequence shares the index,
    HEAD and the working tree between its steps and re-reads none of them, so
    a second committer arriving inside it corrupts the sequence rather than
    merely losing a race. `gitwork.checkout_lock` is what makes "one writer
    in this checkout at a time" true across processes - the handover turn's
    subagents and the reviewer's own write channel included."""
    root = Path(root).resolve()
    return gitwork.run_journaled(
        root, "admit", game_id, [deck], lambda: (_admit(root, deck, game_id), {})
    )


def no_change(
    root: Path,
    deck: str,
    game_id: str,
    *,
    play: str | None,
    line: str | None,
    reason: str,
) -> dict[str, Any]:
    """`plays no-change`: record why one candidate needs no DSL change.

    A candidate is a play the seat declined or passed over, or a line it
    played by hand more than once; the handover must land a DSL change or
    say, once per candidate, why none is due. Nothing in the checkout
    changes - the journal line is the whole effect."""
    root = Path(root).resolve()

    def body() -> tuple[dict[str, Any], dict[str, Any]]:
        check_name("deck", deck)
        check_name("game id", game_id)
        candidate: dict[str, str] = {}
        if play is not None:
            check_name("play id", play)
            candidate["play"] = play
        if line is not None:
            if not line.strip():
                raise Outcome("refused", "--line names the hand-played line; it is empty")
            candidate["line"] = line.strip()
        if len(candidate) != 1:
            raise Outcome("refused", "name exactly one candidate: --play ID or --line TEXT")
        if not reason.strip():
            raise Outcome("refused", "--reason says why no change is due; it is empty")
        named = candidate.get("play") or candidate.get("line")
        done = result("recorded", f"no change for {named}: {reason.strip()}", **candidate)
        return done, {**candidate, "reason": reason.strip()}

    return gitwork.run_journaled(root, "no_change", game_id, [deck], body)


def _pick_up_evidence(root: Path, deck: str, game_id: str) -> str | None:
    """Copy the driver's evidence record for this game into the deck's
    plays, returning its path; None when there is none or it is not JSON."""
    base = os.environ.get(EVIDENCE_DIR_ENV, "").strip()
    if not base:
        return None
    try:
        text = (Path(base) / game_id / f"{deck}.json").read_bytes().decode("utf-8")
        json.loads(text)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    rel = f"decks/{deck}/plays/{EVIDENCE_DIR}/{game_id}.json"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))
    return rel


def _without_priority(text: str | None) -> Any:
    doc = json.loads(text or "")
    if isinstance(doc, dict):
        doc.pop("priority", None)
    return doc


def _landing(git: Git) -> tuple[list[str], bool]:
    """Every path HEAD carries over origin main, and whether any of them is
    a DSL change: a play file added or deleted, or changed beyond its
    top-level priority."""
    raw = git.out("diff", "--no-renames", "--name-status", "-z", UPSTREAM, "HEAD").split("\0")
    files: list[str] = []
    dsl_change = False
    for status, path in zip(raw[0::2], raw[1::2], strict=False):
        if not path:
            continue
        files.append(path)
        if not _play_file(path) or dsl_change:
            continue
        if status in ("A", "D"):
            dsl_change = True
            continue
        try:
            before = _without_priority(git.show(f"{UPSTREAM}:{path}"))
            after = _without_priority(git.show(f"HEAD:{path}"))
        except ValueError:
            dsl_change = True
            continue
        dsl_change = before != after
    return sorted(files), dsl_change


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
    evidence = _pick_up_evidence(root, deck, game_id)
    if evidence is not None:
        changed = git.changed_paths()
    picked = {"evidence": evidence} if evidence is not None and evidence in changed else {}
    if not changed and not local:
        _save_deltas(git, {})
        return result(
            "nothing_to_admit", "no play-script change to admit", dsl_change=False, **picked
        )

    deltas = _sync_deltas(git, sorted(set(changed) | set(local)))
    committed = False
    if changed:
        priorities = _priority_changes(git, changed)
        # No pathspec: `outside` above already refused any change beyond
        # decks/*/plays/**, and a pathspec-restricted `add -A -- <path>`
        # fails with "did not match any files" for a play retired with
        # `git rm` (fully gone from both tree and index, so no path is left
        # to match) - a plain `add -A` stages it, and everything else, the
        # same way `git add -A` always has.
        git.out("add", "-A")
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
        files, dsl_change = _landing(git)
        if git.push(f"HEAD:refs/heads/{MAIN}"):
            _save_deltas(git, {})
            return result(
                "admitted",
                f"pushed to {MAIN}",
                sha=git.head(),
                files=files,
                dsl_change=dsl_change,
                **picked,
            )

    if committed:
        git.out("reset", "-q", "--soft", "HEAD~1")
    return result(
        "error",
        f"{MAIN} moved under each of {PUSH_ATTEMPTS} pushes, so nothing landed; the change is "
        f"back in the tree, uncommitted, on the newest {UPSTREAM} - run the same command again",
        retryable=True,
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
