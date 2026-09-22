"""`mcp-seat guides propose`: a seat's deck-guide edit, pushed to mtg-decks main.

`--deck` names every deck the edit touches (repeatable). For each listed deck
the allowlist, read off HEAD, is:

- a guided deck (`decks/<deck>/decklist.txt` exists at HEAD):
  `decks/<deck>/NOTES.md` and the deck's newest
  `analysis/*/domain3-matchup-threat/boarding-plan.json`;
- a stub deck (no decklist - an opponent archetype with no guide yet, new or
  already committed): `decks/<deck>/NOTES.md` and nothing else, ever. A NEW
  stub whose slug is a guided deck's directory or `slugify` of its
  `archetype.json` "archetype" is refused, naming that deck.

In the plan, an existing row may change only `play_plan` and `notes` (compared
as JSON against HEAD, so formatting is free and every other key and the row
order are pinned). A row may be APPENDED only in the seat shape: `name` (new
under `slugify`), `origin: "seat"`, a `play_plan` of exactly the five strings
`PLAY_PLAN_KEYS`, and `notes` as a list of strings. A changed path under a
deck that was not listed is `refused` with the `--deck` it is missing; any
other path is `refused`. A changed file holding a git conflict marker line is
`refused` before anything parses it.

The same discipline applies to the CONTENT of those files. A deck's guide is
play review only, so the text a proposal ADDS (diffed against HEAD, so
nothing already committed is re-judged) goes through `content_gate` before
anything is rendered, committed or pushed. Text that reads as a system problem
rather than as how the deck is piloted is `refused_content` - its own outcome,
told apart from the file-path `refused` because the remedy is different - with
a message naming the `report_problem` tool and quoting what it read. The gate
is a heuristic that fails closed: when it cannot read the added text it
refuses and says it could not decide, and it never edits, truncates or filters
the text on the way through. `content_gate`'s own docstring states that
ceiling, and says why `plays admit` is deliberately not gated this way.

Then, all under `gitwork.checkout_lock`: the edited files are captured and
origin main fetched. When main moved, each file is replayed onto it with a
3-way merge (`Git.merge_file_markers`); the checkout is reset hard onto
origin main - safe only because the allowlist has already refused every other
change - and the merged files are written back. A clash is `conflict`: git's
markers are left in the working tree, no rebase or merge state is, and the
agent resolves the markers and runs the same command again, every gate
included. Otherwise every gate runs again against the new HEAD,
`scripts/render_guides.py write` then `check` run once over the guided decks
(stubs are never rendered), `scripts/verify_sidecars.py --domain 3` runs on
each changed plan's run, and the edit and its regenerated guides are
committed with the admit identity and a `Game-Id` trailer and pushed to
`main`, never forced. A lost race soft-resets the commit, puts the rendered
files back and goes round again; after `PUSH_ATTEMPTS` the result is `error`
with `retryable: true` and the edit left uncommitted on the newest upstream.

Nobody reviews or merges a guide edit afterwards: `admitted` means it is live
on main, and its `files` are the edited paths (the regenerated guides aside).
Every run appends one line to the checkout's results journal
(`gitwork.RESULTS_JOURNAL`), under the lock, for the driver to read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from mtg_agent.driver.guides import slugify

from . import content_gate, gitwork
from .content_gate import CONTENT_REFUSED, Undecidable
from .gitwork import (
    MAIN,
    PUSH_ATTEMPTS,
    UPSTREAM,
    Git,
    Outcome,
    check_name,
    commit_message,
    identity,
    result,
)

EDITABLE_MATCHUP_KEYS = ("play_plan", "notes")
#: The only shape a seat may append a matchups[] row in.
SEAT_ROW_KEYS = ("name", "origin", "play_plan", "notes")
SEAT_ORIGIN = "seat"
PLAY_PLAN_KEYS = ("keep", "goldfish_for", "watch_for", "posture", "posture_why")
_MISSING = object()
_RUN_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})$")


def plan_path(git: Git, deck: str) -> str | None:
    """The deck's newest boarding plan at HEAD, by the run's date suffix
    (the order `inputs.newest_run` uses on disk). Read off HEAD, so a run
    directory the agent adds never becomes the editable plan."""
    listed = git.run("ls-tree", "-d", "--name-only", "HEAD", f"decks/{deck}/analysis/").stdout
    runs = [line for line in listed.splitlines() if line]
    if not runs:
        return None

    def order(run: str) -> tuple[str, str]:
        name = run.rsplit("/", 1)[-1]
        match = _RUN_DATE.search(name)
        return (match.group(1) if match else "", name)

    return f"{max(runs, key=order)}/domain3-matchup-threat/boarding-plan.json"


def forbidden_plan_changes(before: Any, after: Any) -> list[str]:
    """The JSON paths that changed outside an existing row's play_plan /
    notes, plus every way an appended row departs from the seat shape."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["<the whole document>"]
    changed = [
        key
        for key in sorted(set(before) | set(after))
        if key != "matchups" and before.get(key, _MISSING) != after.get(key, _MISSING)
    ]
    old, new = before.get("matchups", _MISSING), after.get("matchups", _MISSING)
    if not isinstance(old, list) or not isinstance(new, list):
        if old != new:
            changed.append("matchups")
        return changed
    for index, a in enumerate(old):
        if index >= len(new):
            changed.append(f"matchups[{index}]")
            continue
        b = new[index]
        if not isinstance(a, dict) or not isinstance(b, dict):
            if a != b:
                changed.append(f"matchups[{index}]")
            continue
        changed += [
            f"matchups[{index}].{key}"
            for key in sorted(set(a) | set(b))
            if key not in EDITABLE_MATCHUP_KEYS and a.get(key, _MISSING) != b.get(key, _MISSING)
        ]
    taken = {
        slugify(row["name"])
        for row in old
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    for index in range(len(old), len(new)):
        changed += _seat_row_gaps(new[index], f"matchups[{index}]", taken)
    return changed


def _seat_row_gaps(row: Any, where: str, taken: set[str]) -> list[str]:
    if not isinstance(row, dict):
        return [where]
    gaps = [f"{where}.{key}" for key in sorted(set(row) - set(SEAT_ROW_KEYS))]
    name = row.get("name")
    if not isinstance(name, str) or not name.strip() or slugify(name) in taken:
        gaps.append(f"{where}.name")
    else:
        taken.add(slugify(name))
    if row.get("origin") != SEAT_ORIGIN:
        gaps.append(f"{where}.origin")
    plan = row.get("play_plan")
    if not isinstance(plan, dict):
        gaps.append(f"{where}.play_plan")
    else:
        gaps += [
            f"{where}.play_plan.{key}"
            for key in sorted(set(PLAY_PLAN_KEYS) | set(plan))
            if key not in PLAY_PLAN_KEYS or not isinstance(plan.get(key), str)
        ]
    notes = row.get("notes")
    if not isinstance(notes, list) or any(not isinstance(note, str) for note in notes):
        gaps.append(f"{where}.notes")
    return gaps


def has_conflict_markers(text: str) -> bool:
    return any(
        line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "=======" for line in text.splitlines()
    )


def propose(root: Path, decks: str | Sequence[str], game_id: str) -> dict[str, Any]:
    """One proposal, with the checkout held exclusively for the whole of it.

    D-1: the review's own write channel and this one work the same single
    clone, so the git phase is serialized by `gitwork.checkout_lock` rather
    than by a comment about the risk - see that function for why a file lock
    and not a `threading.Lock`."""
    root = Path(root).resolve()
    listed = [decks] if isinstance(decks, str) else list(dict.fromkeys(decks))
    return gitwork.run_journaled(
        root, "propose", game_id, listed, lambda: (_propose(root, listed, game_id), {})
    )


class _Scope:
    """What the listed decks allow, read off HEAD."""

    def __init__(self, git: Git, decks: list[str]) -> None:
        self.decks = decks
        self.guided = [d for d in decks if git.ok("cat-file", "-e", f"HEAD:decks/{d}/decklist.txt")]
        self.plans = {d: plan for d in self.guided if (plan := plan_path(git, d))}
        self.allowed = {f"decks/{d}/NOTES.md" for d in decks} | set(self.plans.values())


def _propose(root: Path, decks: list[str], game_id: str) -> dict[str, Any]:
    if not decks:
        raise Outcome("refused", "name at least one --deck")
    for deck in decks:
        check_name("deck", deck)
    check_name("game id", game_id)
    git = Git(root, identity(decks[0]))
    git.require_main_checkout()
    if git.ahead(UPSTREAM):
        raise Outcome(
            "refused", f"HEAD carries a local commit not on {UPSTREAM}; admit or drop it first"
        )
    if not git.changed_paths():
        return result("nothing_to_admit", "no guide change to propose")
    _gates(git, decks)
    for _ in range(PUSH_ATTEMPTS):
        landed = _attempt(git, decks, game_id)
        if landed is not None:
            return landed
    git.out("reset", "-q")
    return result(
        "error",
        f"{MAIN} moved under each of {PUSH_ATTEMPTS} pushes, so nothing landed; your edit is in "
        f"the tree, uncommitted, on the newest {UPSTREAM} - run the same command again",
        retryable=True,
    )


def _gates(git: Git, decks: list[str]) -> _Scope:
    """Every check on the working tree against HEAD, in the order that makes
    `reset --hard` safe: nothing outside the allowlist survives the first."""
    scope = _Scope(git, decks)
    changed = git.changed_paths()
    _check_paths(git, scope, changed)
    _check_markers(git, changed)
    for plan in scope.plans.values():
        if plan in changed:
            _check_plan(git, plan)
    _check_content(git, changed, set(scope.plans.values()))
    return scope


def _check_paths(git: Git, scope: _Scope, changed: list[str]) -> None:
    outside = sorted(p for p in changed if p not in scope.allowed)
    if outside:
        unlisted = sorted(
            {
                p.split("/")[1]
                for p in outside
                if p.startswith("decks/")
                and p.count("/") >= 2
                and p.split("/")[1] not in scope.decks
            }
        )
        message = (
            "guides propose only takes decks/<deck>/NOTES.md for each --deck and, for a guided "
            "deck, matchups[].play_plan / notes of its newest boarding plan (a stub deck gets "
            "NOTES.md alone); revert these: " + ", ".join(outside)
        )
        if unlisted:
            message += (
                "; these are under a deck you did not list - add "
                + ", ".join(f"`--deck {deck}`" for deck in unlisted)
                + " if the edit is meant"
            )
        raise Outcome("refused", message, files=outside)
    stubs = [d for d in scope.decks if d not in scope.guided]
    new = [
        d
        for d in stubs
        if f"decks/{d}/NOTES.md" in changed and git.show(f"HEAD:decks/{d}/NOTES.md") is None
    ]
    if not new:
        return
    guided = _guided_names(git)
    for deck in new:
        clash = sorted(name for name, slugs in guided.items() if deck in slugs)
        if clash:
            raise Outcome(
                "refused",
                f"a new stub deck {deck!r} would shadow the guided deck {clash[0]!r}; "
                f"write to decks/{clash[0]}/ with `--deck {clash[0]}` instead",
                files=[f"decks/{deck}/NOTES.md"],
            )


def _guided_names(git: Git) -> dict[str, set[str]]:
    """Every guided deck at HEAD -> the slugs a stub may not take from it:
    its directory and `slugify` of its archetype.json "archetype"."""
    listed = git.out("ls-tree", "-r", "--name-only", "HEAD", "--", "decks").splitlines()
    names: dict[str, set[str]] = {}
    for path in listed:
        parts = path.split("/")
        if len(parts) != 3 or parts[2] != "decklist.txt":
            continue
        slugs = {parts[1]}
        try:
            doc = json.loads(git.show(f"HEAD:decks/{parts[1]}/archetype.json") or "{}")
        except ValueError:
            doc = {}
        archetype = doc.get("archetype") if isinstance(doc, dict) else None
        if isinstance(archetype, str) and archetype.strip():
            slugs.add(slugify(archetype))
        names[parts[1]] = slugs
    return names


def _check_markers(git: Git, changed: Iterable[str]) -> None:
    marked = []
    for path in changed:
        try:
            text = (git.root / path).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # deleted, or not text: the content gate has its say
        if has_conflict_markers(text):
            marked.append(path)
    if marked:
        raise Outcome(
            "refused",
            "conflict marker lines (<<<<<<< / ======= / >>>>>>>) are still in these files; "
            "resolve each block keeping both sides' intent, delete the marker lines, and run "
            "the same command again: " + ", ".join(marked),
            files=marked,
        )


def _check_content(git: Git, changed: list[str], plans: set[str]) -> None:
    """Refuse a proposal whose ADDED text is about this system rather than
    about playing the deck, before anything is rendered, committed or pushed.

    Only the text this proposal adds is judged - `content_gate` says why -
    and a file the gate cannot read is `refused_content` too, with its own
    "could not decide" reason, rather than admitted unread. Nothing here
    writes: the seat's own text is left exactly as the seat wrote it.
    """
    passages: list[str] = []
    for path in changed:
        try:
            passages += _added_text(git, path, path in plans)
        except Undecidable as exc:
            raise Outcome(
                CONTENT_REFUSED, f"{path}: {content_gate.undecided_message(str(exc))}", files=[path]
            ) from None
    reasons = content_gate.system_problem_reasons(passages)
    if reasons:
        raise Outcome(
            CONTENT_REFUSED,
            content_gate.refusal_message(reasons),
            files=sorted(set(changed)),
        )


def _added_text(git: Git, path: str, is_plan: bool) -> list[str]:
    """What `path` adds over HEAD, as passages the gate can judge."""
    working = git.root / path
    if not working.is_file():
        # A deletion adds no text; the allowlist has already had its say.
        return []
    head = git.show(f"HEAD:{path}")
    try:
        after = working.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Undecidable(f"it is not readable as text ({exc.__class__.__name__})") from None
    if not is_plan:
        return content_gate.added_lines(head, after)
    try:
        return content_gate.added_plan_passages(json.loads(head or "{}"), json.loads(after))
    except ValueError as exc:
        raise Undecidable(f"it is not readable JSON ({exc})") from None


def _check_plan(git: Git, plan: str) -> None:
    head = git.show(f"HEAD:{plan}")
    if head is None:
        raise Outcome("refused", f"{plan} is not committed; only an existing plan is edited")
    try:
        before = json.loads(head)
        after = json.loads((git.root / plan).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Outcome("refused", f"{plan} is not readable JSON: {exc}", files=[plan]) from None
    forbidden = forbidden_plan_changes(before, after)
    if forbidden:
        raise Outcome(
            "refused",
            f"only matchups[].play_plan and matchups[].notes of an existing row of {plan} may "
            'change, and a row may be appended only as {"name", "origin": "seat", "play_plan": '
            "{" + ", ".join(PLAY_PLAN_KEYS) + '}, "notes": [...]} under a new name; '
            "these changed too: " + ", ".join(forbidden),
            files=[plan],
        )


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None


def _whole_file_conflict(mine: str | None, theirs: str | None) -> str:
    def block(text: str | None) -> str:
        return "" if not text else text if text.endswith("\n") else text + "\n"

    return f"<<<<<<< yours\n{block(mine)}=======\n{block(theirs)}>>>>>>> {UPSTREAM}\n"


def _replay(
    git: Git, path: str, is_plan: bool, mine: str | None, base: str | None, theirs: str | None
) -> tuple[str | None, bool]:
    """`mine` replayed onto upstream's `theirs`: (text, conflicted); None
    text is a deletion."""
    if theirs == base:
        return mine, False
    if mine is None or theirs is None:
        return _whole_file_conflict(mine, theirs), True
    text, conflicted = git.merge_file_markers(mine, base or "", theirs)
    if is_plan and not conflicted:
        try:
            json.loads(text)
        except ValueError:
            return _whole_file_conflict(mine, theirs), True
    return text, conflicted


def _attempt(git: Git, decks: list[str], game_id: str) -> dict[str, Any] | None:
    """Fetch, replay the edit onto origin main, gate, render, commit, push.
    None when the push lost a race and the commit was taken back."""
    captured = {path: _read(git.root / path) for path in git.changed_paths()}
    base = git.head()
    git.fetch()
    upstream = git.out("rev-parse", UPSTREAM).strip()
    merged: dict[str, str | None] = dict(captured)
    conflicted: list[str] = []
    if upstream != base:
        plans = set(_Scope(git, decks).plans.values())
        for path, mine in captured.items():
            text, clash = _replay(
                git,
                path,
                path in plans,
                mine,
                git.show(f"{base}:{path}"),
                git.show(f"{upstream}:{path}"),
            )
            merged[path] = text
            if clash:
                conflicted.append(path)
    # Safe: `_gates` has refused every path but NOTES.md and the plans.
    git.out("reset", "-q", "--hard", UPSTREAM)
    for path, text in merged.items():
        target = git.root / path
        if text is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
    if conflicted:
        raise Outcome(
            "conflict",
            f"{UPSTREAM} changed the same lines as your edit; the files now hold both sides "
            "between conflict markers (yours, then origin/main). Resolve each block keeping "
            "both sides' intent, delete the marker lines, and run the same command again: "
            + ", ".join(sorted(conflicted)),
            files=sorted(conflicted),
            base_sha=base,
            upstream_sha=upstream,
        )

    edits = git.changed_paths()
    if not edits:
        return result("nothing_to_admit", f"{UPSTREAM} already carries this edit")
    scope = _gates(git, decks) if upstream != base else _Scope(git, decks)
    touched = [d for d in decks if any(p.startswith(f"decks/{d}/") for p in edits)]
    try:
        rendered = _render(git, scope.guided, edits)
        _verify_sidecars(git, [plan for plan in scope.plans.values() if plan in edits])
    except Outcome:
        _unwind(git, edits)
        raise
    git.out("add", "-A", "--", *edits, *rendered)
    subject = f"Guides: {', '.join(touched)} after game {game_id}"
    git.out("commit", "-q", "--no-verify", "-F", "-", input=commit_message(subject, game_id, []))
    if git.push(f"HEAD:refs/heads/{MAIN}"):
        return result(
            "admitted", f"pushed to {MAIN}", sha=git.head(), decks=touched, files=sorted(edits)
        )
    git.out("reset", "-q", "--soft", "HEAD~1")
    _unwind(git, edits)
    return None


def _render(git: Git, guided: list[str], edits: list[str]) -> list[str]:
    """`render_guides.py write` then `check` over the guided decks; the files
    it regenerated. Stubs are never rendered."""
    if not guided:
        return []
    if not (git.root / "scripts" / "render_guides.py").is_file():
        raise Outcome(
            "gate_failed", "scripts/render_guides.py is missing from the clone; nothing rendered"
        )
    flags = [arg for deck in guided for arg in ("--deck", f"decks/{deck}")]
    for action in ("write", "check"):
        done = gitwork.run(
            ["python3", "scripts/render_guides.py", action, *flags], git.root, env=git.env
        )
        if done.returncode != 0:
            raise Outcome(
                "gate_failed",
                f"render_guides.py {action} failed for {', '.join(guided)}",
                output=gitwork.tail(done),
            )
    rendered = sorted(set(git.changed_paths()) - set(edits))
    stray = [
        p
        for p in rendered
        if p.count("/") != 2 or not any(p.startswith(f"decks/{d}/") for d in guided)
    ]
    if stray:
        raise Outcome(
            "gate_failed",
            "render_guides.py wrote outside the decks' guides: " + ", ".join(stray),
        )
    return rendered


def _verify_sidecars(git: Git, plans: list[str]) -> None:
    """mtg-decks' own domain-3 gate on each changed plan's run, so a row the
    repo's tests would reject never reaches main. Fails closed."""
    if not plans:
        return
    if not (git.root / "scripts" / "verify_sidecars.py").is_file():
        raise Outcome(
            "gate_failed",
            "scripts/verify_sidecars.py is missing from the clone; the gate fails closed",
        )
    for plan in plans:
        wiki = Path(plan).parent.parent.as_posix()
        done = gitwork.run(
            ["python3", "scripts/verify_sidecars.py", "--domain", "3", wiki], git.root, env=git.env
        )
        if done.returncode != 0:
            raise Outcome(
                "gate_failed",
                f"verify_sidecars.py --domain 3 {wiki} failed",
                output=gitwork.tail(done),
            )


def _unwind(git: Git, edits: list[str]) -> None:
    """The rendered files put back; the agent's own edit stays."""
    for path in sorted(set(git.changed_paths()) - set(edits)):
        if git.show(f"HEAD:{path}") is not None:
            git.run("checkout", "-q", "HEAD", "--", path)
        else:
            (git.root / path).unlink(missing_ok=True)
