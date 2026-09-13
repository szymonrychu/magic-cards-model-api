"""`mcp-seat guides propose`: a seat's matchup-plan edit as a guides pull request.

Only two things may change: `matchups[].play_plan` and `matchups[].notes` of
the deck's newest `analysis/*/domain3-matchup-threat/boarding-plan.json`
(compared as JSON against HEAD, so formatting is free and every other key is
pinned), and `decks/<slug>/NOTES.md`. Anything else - another key of that
file, the decklist, evidence, sim profiles, shell or archetype, an older run,
a play, any other path - is `refused` before anything runs.

Then, on a local `seat/<game>-guides` branch: `python3 scripts/render_guides.py
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

from . import gitwork
from .gitwork import MAIN, UPSTREAM, Git, Outcome, check_name, commit_message, identity, result
from .inputs import newest_run

EDITABLE_MATCHUP_KEYS = ("play_plan", "notes")
_MISSING = object()


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
    try:
        return _propose(Path(root).resolve(), deck, game_id)
    except Outcome as outcome:
        return outcome.result


def _propose(root: Path, deck: str, game_id: str) -> dict[str, Any]:
    check_name("deck", deck)
    check_name("game id", game_id)
    git = Git(root, identity(deck))
    git.require_main_checkout()
    branch = f"seat/{game_id}-guides"
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
