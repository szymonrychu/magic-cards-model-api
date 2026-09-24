"""The handover's fixed parts, and the driver container's preStop.

A seat hands over once: at a natural end (after the reflection turn), on
`/release`, or when Kubernetes deletes the Job. Each of those reaches the one
idempotent `POST /handover` in `app.py`; this module holds what that route
shares with the preStop: the marker both containers wait on, the time budget,
and the compact conflict-warning block the handover prompt carries.

A handover is also where the post-game AI review is filed for every game
that did not reach one through a completed reflection turn - the preStop's
`POST /handover` included. `app.py`'s `TERMINAL_PATHS` and
`consider_ai_review` own that decision; nothing in this module makes it, and
nothing here waits on it.

`python -m mtg_agent.driver.handover --wait N` is the driver container's
preStop (`deploy/seat-job.yaml`). It POSTs the local driver's `/handover`
with no outcome, so the driver names `killed` unless it knows a winner or a
draw, then blocks until the marker exists or N seconds pass. It always exits
0: a preStop that fails only makes the kubelet log noise, never a better
handover.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .config import DriverSettings

if TYPE_CHECKING:
    from magic_cards_model_api.playscripts.lint import PlaysWarning
    from magic_cards_model_api.playscripts.report import DevelopmentCandidates

    from .guides import ScriptsRegistration

log = logging.getLogger("driver.handover")

#: The file both containers wait on, under the driver's workspace
#: (`/driver-workspace/handover.done` in the seat Job). It holds the
#: handover's status JSON whether it succeeded, timed out or failed.
MARKER_NAME = "handover.done"

#: When the one "push now" nudge goes out, in seconds since the handover began:
#: 23 minutes, five before the budget runs out.
NUDGE_AFTER_SECONDS = 1380.0

#: When a handover still waiting on its turn gives up and writes the marker:
#: 28 minutes, sized for a handover that admits, then develops scripts and
#: guides through subagents (game 16a9cf8f ran out 510 s doing that). D2b made
#: that development mandatory - a change admitted or a `plays no-change` per
#: candidate, retiring a play by deletion in the same admit at the
#: `MAX_PLAYS` (16) cap - and its one re-push runs inside this same budget,
#: never past it. Inside
#: the driver preStop's `PRESTOP_WAIT_SECONDS`, itself inside the Job's 1800 s
#: grace, so the retire after the marker still fits before the kubelet's
#: SIGKILL.
HANDOVER_BUDGET_SECONDS = 1680.0

#: The driver container's preStop wait (`deploy/seat-job.yaml`), the default
#: for `--wait`.
PRESTOP_WAIT_SECONDS = 1740.0

#: How long the handover turn's push is retried while the agent answers 409
#: (its interrupted turn not ended yet) before the handover fails and writes
#: the marker. The wrapper ends an interrupted turn `INTERRUPT_SETTLE_SECONDS`
#: (20 s) after the interrupt, so this is several settles, and far inside 1740 s.
HANDOVER_PUSH_BUDGET_SECONDS = 120.0

#: The first wait after a 409, doubled after each further one up to the max.
#: Live, a flat 50 ms retry sent 5523 pushes in one 510 s budget.
HANDOVER_BACKOFF_START_SECONDS = 0.5
HANDOVER_BACKOFF_MAX_SECONDS = 5.0

#: How often the waiting handover looks at its clock.
POLL_SECONDS = 0.05

#: How long the preStop gives the local driver to accept the POST.
POST_TIMEOUT_SECONDS = 10.0

Clock = Callable[[], float]
Sleep = Callable[[float], None]


def wait_for_marker(
    path: Path,
    wait: float,
    *,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
    poll: float = 0.5,
) -> bool:
    """True as soon as `path` exists, False once `wait` seconds pass first."""
    deadline = clock() + wait
    while not path.exists():
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(poll, remaining))
    return True


def write_marker(path: Path, status: Mapping[str, Any]) -> None:
    """Write the status whole or not at all, so a waiter never reads half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(json.dumps(dict(status), indent=2, sort_keys=True))
    partial.replace(path)


def clear_marker(path: Path) -> None:
    """Drop a marker left by an earlier match (a long-lived compose driver)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not clear the old handover marker %s: %s", path, exc)


def format_conflicts(warnings: Sequence[PlaysWarning]) -> str:
    """One line per warning: its code, the script ids, the overlapping
    dimensions. The message is left out; the prompt already says what to do."""
    if not warnings:
        return "none: no two enabled scripts in the registered corpus can apply at one moment."
    return "\n".join(
        f"{warning.code}: {', '.join(warning.plays)} ({', '.join(warning.dimensions)})"
        for warning in warnings
    )


def conflict_warnings(registration: ScriptsRegistration | None, deck_slug: str) -> str:
    """`plays check`'s conflict warnings over the corpus this seat registered
    at dispatch, formatted by `format_conflicts`. Errors are not reported: the
    registration route refuses a corpus with any, so a registered one has
    none."""
    if registration is None or registration.status != "registered":
        status = registration.status if registration is not None else "not attempted"
        return f"none: no corpus is registered for this seat ({status})."
    # Imported here: the preStop imports this module and needs none of it.
    from magic_cards_model_api.playscripts.script_tools import static_conflicts  # noqa: PLC0415

    corpus = registration.corpus
    return format_conflicts(static_conflicts(list(corpus.scripts), list(corpus.inputs), deck_slug))


# -- D2b: the mandatory DSL development step ---------------------------------------

#: The results journal `mcp-seat plays priority`, `plays admit`, `guides
#: propose` and `plays no-change` append one JSON line to, in the guides
#: checkout's own git dir (D2a). The driver reads it through its read-only
#: `/guides` mount once the handover turn ends.
JOURNAL_NAME = "mcm-results.jsonl"

#: The directory under the driver's workspace this game's evidence record is
#: written to (`<workspace>/evidence/<game>/<deck>.json`), which the agent
#: container sees as `MTG_EVIDENCE_DIR` (`deploy/seat-job.yaml`).
EVIDENCE_DIR_NAME = "evidence"

#: Results of `plays admit` and `guides propose` that mean the write landed
#: (or had nothing to land) - `gitwork.SUCCESS`, repeated here because the
#: preStop imports this module and must not pull in the git plumbing.
WRITE_SUCCESS = frozenset({"admitted", "nothing_to_admit"})


def journal_path(guides_root: Path) -> Path:
    """`<git-dir>/mcm-results.jsonl` of the checkout at `guides_root`. A
    linked checkout's `.git` is a file naming its real git dir
    (`gitdir: <path>`), followed here the way git itself follows it."""
    dot_git = guides_root / ".git"
    if dot_git.is_file():
        try:
            text = dot_git.read_text().strip()
        except OSError:
            text = ""
        if text.startswith("gitdir:"):
            target = Path(text.removeprefix("gitdir:").strip())
            return (target if target.is_absolute() else guides_root / target) / JOURNAL_NAME
    return dot_git / JOURNAL_NAME


def read_journal(guides_root: Path, game_id: str, role: str) -> list[dict[str, Any]]:
    """This game's journal lines written under `role` (`seat` or `advisor`,
    the container's `MTG_ROLE`), in the order they were written. The
    reviewer shares the checkout and the game but writes as `reviewer`, so
    its lines are its own, never the handover's. A line that is not a JSON
    object is skipped; a journal that does not exist is empty."""
    try:
        text = journal_path(guides_root).read_text()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(entry, dict)
            and entry.get("game_id") == game_id
            and entry.get("role") == role
        ):
            entries.append(entry)
    return entries


def evidence_path(workspace: Path, game_id: str, deck: str) -> Path:
    """Where the driver writes `deck`'s evidence record for `game_id`: the
    `$MTG_EVIDENCE_DIR/<game_id>/<deck>.json` `plays admit` reads."""
    return workspace / EVIDENCE_DIR_NAME / game_id / f"{deck}.json"


def write_evidence(workspace: Path, record: Mapping[str, Any]) -> Path:
    """Write `record` (`report.evidence_record`) whole or not at all, so an
    admit running beside it never commits half a file."""
    path = evidence_path(workspace, str(record["game_id"]), str(record["deck"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(json.dumps(dict(record), indent=2, sort_keys=True))
    partial.replace(path)
    return path


def _command(entry: Mapping[str, Any]) -> str:
    """A journal line's command, as one word: `admit`, `propose`,
    `priority` or `no_change`, however the line spells it (`plays admit`,
    `plays-admit`, `admit`)."""
    words = str(entry.get("command") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if words.endswith("no_change"):
        return "no_change"
    return words.rsplit("_", 1)[-1]


def _normal(text: Any) -> str:
    """A candidate as the seat typed it: whitespace collapsed, case kept."""
    return " ".join(str(text or "").split())


@dataclass(frozen=True)
class DevelopmentVerdict:
    """What the journal says the handover did about its development
    candidates, and what the `plays_outcome` row reports of it.

    `satisfied`: a `plays admit` landed a `dsl_change` (a conditions or
    advice-only change, anything beyond `priority`), or a `plays no-change`
    answered every candidate. `missing` names what is not, in the words the
    re-push turn uses. `last_write_failed` is the last `plays admit` or
    `guides propose` when its result was not a success, else `None`."""

    satisfied: bool
    missing: tuple[str, ...]
    last_write_failed: str | None
    admitted: tuple[dict[str, Any], ...]
    proposed: tuple[dict[str, Any], ...]
    no_change: tuple[dict[str, Any], ...]


#: The `missing` line for a handover with no `dsl_change` admit.
NO_DSL_CHANGE = "no conditions or advice-only change was admitted (`dsl_change`)"


def assess(
    entries: Sequence[Mapping[str, Any]], candidates: DevelopmentCandidates
) -> DevelopmentVerdict:
    """Judge this game's journal lines against its development candidates."""
    admitted: list[dict[str, Any]] = []
    proposed: list[dict[str, Any]] = []
    no_change: list[dict[str, Any]] = []
    answered: set[str] = set()
    last_write: Mapping[str, Any] | None = None
    dsl_change = False
    for entry in entries:
        kind = _command(entry)
        result = str(entry.get("result") or "")
        if kind == "admit":
            last_write = entry
            if result == "admitted":
                admitted.append(
                    {
                        "sha": entry.get("sha"),
                        "dsl_change": bool(entry.get("dsl_change")),
                        # DR5's `admit._landing` classification, carried
                        # through the journal (`gitwork._JOURNALED_KEYS`) -
                        # empty when a legacy journal line never carried it.
                        "added": [str(p) for p in entry.get("added") or ()],
                        "updated": [str(p) for p in entry.get("updated") or ()],
                        "reprioritized": [str(p) for p in entry.get("reprioritized") or ()],
                    }
                )
                dsl_change = dsl_change or bool(entry.get("dsl_change"))
        elif kind == "propose":
            last_write = entry
            row = {"sha": entry["sha"]} if entry.get("sha") else {}
            proposed.append(row | {"result": result})
        elif kind == "no_change":
            candidate = _normal(entry.get("play") or entry.get("line") or entry.get("candidate"))
            if candidate:
                answered.add(candidate.casefold())
                no_change.append({"candidate": candidate, "reason": _normal(entry.get("reason"))})
    open_plays = [play for play in candidates.plays if _normal(play).casefold() not in answered]
    open_lines = [line for line in candidates.lines if _normal(line).casefold() not in answered]
    satisfied = dsl_change or not (open_plays or open_lines)
    missing: list[str] = []
    if not satisfied:
        missing = [
            NO_DSL_CHANGE,
            *(f"play `{play}`" for play in open_plays),
            *(f"line `{line}`" for line in open_lines),
        ]
    failed = None
    if last_write is not None and str(last_write.get("result") or "") not in WRITE_SUCCESS:
        name = "plays admit" if _command(last_write) == "admit" else "guides propose"
        failed = f"{name} answered `{last_write.get('result') or 'nothing'}`"
    return DevelopmentVerdict(
        satisfied=satisfied,
        missing=tuple(missing),
        last_write_failed=failed,
        admitted=tuple(admitted),
        proposed=tuple(proposed),
        no_change=tuple(no_change),
    )


def review_answered_plays(entries: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Every play id this game's post-game AI review already admitted a DSL
    change for - `added` or `updated` (DR5's `admit._landing` classification,
    read off `entries`, which the caller reads with `read_journal(guides_root,
    game_id, "reviewer")`). A priority-only `reprioritized` id never counts,
    the same rule the handover's own mandatory development step already
    applies to a plain `plays priority` call. A legacy entry with neither key
    contributes nothing, never an error.

    The handover's own mandatory development step (`assess` above) must not
    ask the seat to redo a candidate the review already answered while the
    handover turn was itself in flight - the review runs on its own
    background thread, so its admits can land before, during or after the
    handover turn reads its candidates."""
    answered: set[str] = set()
    for entry in entries:
        if _command(entry) != "admit" or str(entry.get("result") or "") != "admitted":
            continue
        answered.update(str(play_id) for play_id in entry.get("added") or ())
        answered.update(str(play_id) for play_id in entry.get("updated") or ())
    return frozenset(answered)


def unfinished_reasons(
    *,
    state: str,
    error: str | None,
    nudged: bool,
    repushed: bool,
    verdict: DevelopmentVerdict,
) -> list[str]:
    """Why a handover counts as unfinished - empty when it is complete: it
    was nudged, it timed out or failed, its last write did not land, or its
    development step is still open (after the one re-push, or with no time
    left for one)."""
    reasons: list[str] = []
    if nudged:
        reasons.append(
            f"nudged: the handover was still running at {int(NUDGE_AFTER_SECONDS // 60)} minutes"
        )
    if state == "timeout":
        reasons.append(
            "timeout: the handover turn did not end within "
            f"{int(HANDOVER_BUDGET_SECONDS // 60)} minutes"
        )
    elif state == "failed":
        reasons.append(f"failed: {error or 'the handover turn could not be run'}")
    if verdict.last_write_failed:
        reasons.append(f"the last write did not land: {verdict.last_write_failed}")
    if not verdict.satisfied:
        when = "after the re-push" if repushed else "with no re-push"
        reasons.append(f"development still open {when}: " + "; ".join(verdict.missing))
    return reasons


def plays_outcome_body(
    *,
    uid: str,
    player: str,
    role: str,
    verdict: DevelopmentVerdict,
    reasons: Sequence[str],
    repushed: bool,
    followup: bool = False,
    deck: str | None = None,
    opponent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The `POST /api/games/{id}/plays-outcome` body (D2a's `PlaysOutcomeIn`).

    `followup` is True only on the row F1's follow-up Job files, which is
    what keeps the api from ever scheduling a second one. `deck` and
    `opponent` (the revealed opponent's `{slug, archetype, confidence}`) are
    what the api builds a follow-up's assignment from, so they ride in the
    ledger row; each is left out when not given. `admitted` carries each
    entry's own `added`/`updated`/`reprioritized` play-id lists straight
    from `verdict.admitted` (DR2's `PlaysOutcomeIn` schema) - this function
    only forwards what `assess` already built, never derives them itself."""
    body: dict[str, Any] = {
        "uid": uid,
        "player": player,
        "role": role,
        "state": "unfinished" if reasons else "complete",
        "admitted": list(verdict.admitted),
        "proposed": list(verdict.proposed),
        "no_change": list(verdict.no_change),
        "unfinished_reasons": list(reasons),
        "repushed": repushed,
        "followup": followup,
    }
    if deck is not None:
        body["deck"] = deck
    if opponent is not None:
        body["opponent"] = dict(opponent)
    return body


def local_url(http_addr: str) -> str:
    _, _, port = http_addr.rpartition(":")
    return f"http://127.0.0.1:{port or '8090'}/handover"


def _post(url: str, body: dict[str, Any]) -> None:
    httpx.post(url, json=body, timeout=POST_TIMEOUT_SECONDS)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    post: Callable[[str, dict[str, Any]], None] = _post,
    clock: Clock = time.monotonic,
    sleep: Sleep = time.sleep,
) -> int:
    started = clock()
    try:
        parser = argparse.ArgumentParser(prog="python -m mtg_agent.driver.handover")
        parser.add_argument("--wait", type=float, default=PRESTOP_WAIT_SECONDS)
        args = parser.parse_args(argv)
        settings = DriverSettings.from_env(os.environ if env is None else env)
        try:
            post(local_url(settings.http_addr), {})
        except Exception as exc:  # noqa: BLE001 - a driver that does not answer is still waited on
            log.warning("could not start the handover: %s", exc)
        remaining = max(0.0, args.wait - (clock() - started))
        wait_for_marker(settings.workspace / MARKER_NAME, remaining, clock=clock, sleep=sleep)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - a preStop always exits 0
        log.warning("handover preStop: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
