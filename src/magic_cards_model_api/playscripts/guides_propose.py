"""`mcp-seat guides propose`: a seat's matchup-plan edit as a guides pull request.

Only two things may change: `matchups[].play_plan` and `matchups[].notes` of
the deck's newest `analysis/*/domain3-matchup-threat/boarding-plan.json`
(compared as JSON against HEAD, so formatting is free and every other key is
pinned), and `decks/<slug>/NOTES.md`. Anything else - another key of that
file, the decklist, evidence, sim profiles, shell or archetype, an older run,
a play, any other path - is `refused` before anything runs.

The same discipline applies to the CONTENT of those two files. A deck's guide
is play review only, so the text a proposal ADDS (diffed against HEAD, so
nothing already committed is re-judged) goes through `content_gate` before
anything is rendered, committed or pushed. Text that reads as a system problem
rather than as how the deck is piloted is `refused_content` - its own outcome,
told apart from the file-path `refused` because the remedy is different - with
a message naming the `report_problem` tool and quoting what it read. The gate
is a heuristic that fails closed: when it cannot read the added text it
refuses and says it could not decide, and it never edits, truncates or filters
the text on the way through. `content_gate`'s own docstring states that
ceiling, and says why `plays admit` is deliberately not gated this way.

Then, on a local `seat/<game>-<deck>-<role>-guides` branch (`guides_branch`),
one per proposing seat or buddy, so two decks of one game never share a pull
request: `python3 scripts/render_guides.py
write` for the deck, `check` for it (`gate_failed` on either, the generated
files put back), a commit of the edit and the regenerated guides with the
admit identity and `Game-Id` trailer, a push of the branch (never main, never
forced) and a pull request through the GitHub REST API. The clone returns to
an untouched main.

mtg-decks is a private repo with no GitHub Actions budget, so its `ci` and
`automerge` workflows never run there: the pull request sits
`awaiting_owner_review` until the owner reviews and merges it by hand. This
command returns the moment the PR is open - it never polls for checks,
automerge, or merge state, and the guide edit is not live until the owner
merges it. Play scripts are unaffected: `mcp-seat plays admit` still pushes
straight to mtg-decks main after the in-pod gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import content_gate, gitwork
from .content_gate import CONTENT_REFUSED, Undecidable
from .gitwork import (
    MAIN,
    UPSTREAM,
    Git,
    Outcome,
    check_name,
    commit_message,
    identity,
    result,
    role,
)
from .inputs import newest_run

EDITABLE_MATCHUP_KEYS = ("play_plan", "notes")
_MISSING = object()


def guides_branch(game_id: str, deck: str, role: str) -> str:
    """The proposer's own branch: a seat and a buddy, or the two seats of one
    game, each get theirs, and the same proposer proposing again reuses it."""
    return f"seat/{game_id}-{deck}-{role}-guides"


def plan_path(root: Path, deck: str) -> str | None:
    run = newest_run(root / "decks" / deck)
    if run is None:
        return None
    plan = run / "domain3-matchup-threat" / "boarding-plan.json"
    return plan.relative_to(root).as_posix()


def forbidden_plan_changes(before: Any, after: Any) -> list[str]:
    """The JSON paths that changed outside matchups[].play_plan / notes."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["<the whole document>"]
    changed = [
        key
        for key in sorted(set(before) | set(after))
        if key != "matchups" and before.get(key, _MISSING) != after.get(key, _MISSING)
    ]
    old, new = before.get("matchups", _MISSING), after.get("matchups", _MISSING)
    if not isinstance(old, list) or not isinstance(new, list) or len(old) != len(new):
        if old != new:
            changed.append("matchups")
        return changed
    for index, (a, b) in enumerate(zip(old, new, strict=True)):
        if not isinstance(a, dict) or not isinstance(b, dict):
            if a != b:
                changed.append(f"matchups[{index}]")
            continue
        changed += [
            f"matchups[{index}].{key}"
            for key in sorted(set(a) | set(b))
            if key not in EDITABLE_MATCHUP_KEYS and a.get(key, _MISSING) != b.get(key, _MISSING)
        ]
    return changed


def propose(root: Path, deck: str, game_id: str) -> dict[str, Any]:
    """One proposal, with the checkout held exclusively for the whole of it.

    D-1: the review's own write channel and this one work the same single
    clone, so the git phase is serialized by `gitwork.checkout_lock` rather
    than by a comment about the risk - see that function for why a file lock
    and not a `threading.Lock`."""
    root = Path(root).resolve()
    try:
        with gitwork.checkout_lock(root):
            return _propose(root, deck, game_id)
    except Outcome as outcome:
        return outcome.result


def _propose(root: Path, deck: str, game_id: str) -> dict[str, Any]:
    check_name("deck", deck)
    check_name("game id", game_id)
    git = Git(root, identity(deck))
    git.require_main_checkout()
    branch = guides_branch(game_id, deck, role())
    has_branch = git.ok("rev-parse", "--verify", "-q", f"refs/heads/{branch}")

    changed = git.changed_paths()
    if not changed:
        if has_branch and git.ahead(UPSTREAM, branch):
            # A commit from an earlier run whose push or pull request failed.
            return _publish(git, deck, game_id, branch)
        return result("nothing_to_admit", "no guide change to propose")
    if has_branch:
        raise Outcome(
            "refused",
            f"branch {branch} already exists in this checkout; publish or delete it first",
        )

    plan = plan_path(root, deck)
    notes = f"decks/{deck}/NOTES.md"
    allowed = {notes} | ({plan} if plan else set())
    outside = sorted(p for p in changed if p not in allowed)
    if outside:
        raise Outcome(
            "refused",
            f"guides propose only takes matchups[].play_plan / notes in {plan} and {notes}; "
            "revert these: " + ", ".join(outside),
            files=outside,
        )
    if plan in changed:
        _check_plan(git, plan)
    _check_content(git, changed, plan, notes)

    renderer = root / "scripts" / "render_guides.py"
    if not renderer.is_file():
        raise Outcome(
            "gate_failed", "scripts/render_guides.py is missing from the clone; nothing rendered"
        )
    git.out("switch", "-q", "-c", branch)
    try:
        for action in ("write", "check"):
            done = gitwork.run(
                ["python3", "scripts/render_guides.py", action, "--deck", f"decks/{deck}"],
                root,
                env=git.env,
            )
            if done.returncode != 0:
                raise Outcome(
                    "gate_failed",
                    f"render_guides.py {action} failed for {deck}",
                    output=gitwork.tail(done),
                )
        rendered = sorted(set(git.changed_paths()) - set(changed))
        stray = [p for p in rendered if p.count("/") != 2 or not p.startswith(f"decks/{deck}/")]
        if stray:
            raise Outcome(
                "gate_failed",
                "render_guides.py wrote outside the deck's guides: " + ", ".join(stray),
            )
        git.out("add", "-A", "--", *changed, *rendered)
        git.out(
            "commit",
            "-q",
            "--no-verify",
            "-F",
            "-",
            input=commit_message(f"Guides: {deck} after game {game_id}", game_id, []),
        )
    except Outcome:
        _unwind(git, branch, changed)
        raise
    git.out("switch", "-q", MAIN)
    return _publish(git, deck, game_id, branch)


def _check_content(git: Git, changed: list[str], plan: str | None, notes: str) -> None:
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
            passages += _added_text(git, path, plan)
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


def _added_text(git: Git, path: str, plan: str | None) -> list[str]:
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
    if path != plan:
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
            f"only matchups[].play_plan and matchups[].notes of {plan} may change; "
            "these changed too: " + ", ".join(forbidden),
            files=[plan],
        )


def _unwind(git: Git, branch: str, changed: list[str]) -> None:
    """Back to main with the agent's own edit, the rendered files put back."""
    for path in sorted(set(git.changed_paths()) - set(changed)):
        if git.show(f"HEAD:{path}") is not None:
            git.run("checkout", "-q", "HEAD", "--", path)
        else:
            (git.root / path).unlink(missing_ok=True)
    git.run("switch", "-q", MAIN)
    git.run("branch", "-q", "-D", branch)


def _publish(git: Git, deck: str, game_id: str, branch: str) -> dict[str, Any]:
    if not git.push(f"refs/heads/{branch}:refs/heads/{branch}"):
        raise Outcome("error", f"branch {branch} already exists on origin with other commits")
    sha = git.out("rev-parse", branch).strip()
    url = gitwork.open_pull_request(
        git,
        branch,
        f"Guides: {deck} after game {game_id}",
        "A matchup play_plan / notes edit with its regenerated guides, from "
        "`mcp-seat guides propose`. mtg-decks has no CI budget, so please "
        "review and merge this by hand.\n\n"
        f"Game-Id: {game_id}\n",
    )
    return result(
        "awaiting_owner_review",
        f"opened a pull request from {branch}; the owner merges it by hand",
        pr_url=url,
        branch=branch,
        sha=sha,
    )
