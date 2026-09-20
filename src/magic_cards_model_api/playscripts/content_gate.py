"""The content half of `guides propose`'s discipline: is this new text about
the game, or about the system that runs it?

`guides_propose` already refuses any FILE outside its allowlist. This is the
same refusal applied to the TEXT. A deck's guide is play review only: it is
read by a human pilot, and nothing in it can fix an api bug, an engine rules
gap, a tool that timed out or a pod that restarted. Text like that belongs in
the `report_problem` tool, which files a row in the game's own audit ledger
where it can actually be acted on. That split is the rule T37 states in both
write-back prompts; this module is the same rule enforced rather than asked
for.

**This is a heuristic and it is not a proof.** Certainty about what a sentence
is about is not reachable without a dictionary of sentences, which this
project forbids on principle and which would be brittle regardless. What is
here instead is a vocabulary of system-domain words that are not Magic words,
plus a set of shapes that are structurally code - a `--flag`, a
`parameter=value`, a backtick-quoted identifier, a path into a source tree -
and a phrase or two naming a remedy that is a code change.

The consequences of that, stated plainly because a gate that pretends to
certainty is worse than no gate:

- A FALSE NEGATIVE lets system content through into a deck's guide. A seat
  that writes about the api in ordinary English, naming nothing, is not
  caught here, and the proposal lands.
- A FALSE POSITIVE refuses legitimate play prose. A note that happens to
  carry one of these words - a card whose name collides, a deck called after
  one - is refused although it is exactly the content the guide wants.

The refusal message exists for the second case. It quotes what was read and
why, and it says the way out is to REWORD, not to argue: the gate never edits,
truncates or filters the text, so the seat keeps every word it wrote and
decides itself whether to rephrase the note or to file a report instead.

Two smaller decisions follow from the same honesty:

- Only ADDED text is judged, diffed against HEAD. Text already committed is
  never re-judged, because a deck whose notes already trip the gate has to
  stay editable - otherwise the first violation would freeze the file.
- When the gate cannot read the added text at all - bytes that are not text,
  a plan whose editable fields are not the shape they are supposed to be - it
  refuses and says it could not decide. Failing closed is the point: admitting
  text unread is the one outcome that cannot be undone by rewording.

**`plays admit` is deliberately not gated by this.** A play script has no
prose field a human reads this way, and every play `intent` in the DECKS
corpus carries a "Limits:" clause discussing the DSL's own vocabulary gaps -
which selector does not exist, which argument the schema will not take - in
exactly the register this gate refuses. Applying it there would produce
nothing but false refusals on text that is doing its job.

Stdlib only, no I/O: text in, verdict out. `guides_propose` reads the files
and raises the outcome, so this runs in-pod next to
`python3 scripts/render_guides.py` with nothing to install.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

#: The outcome a content refusal prints. Distinct from `guides propose`'s
#: existing file-path `refused` on purpose: the two are told apart by a
#: caller, because the remedy is different (revert the file, versus reword
#: the sentence or file a report).
CONTENT_REFUSED = "refused_content"

#: How much of an offending passage the refusal quotes back.
EXCERPT = 160


class Undecidable(Exception):
    """The added text could not be read, so the gate refuses rather than
    admitting it unread. Carries what it could not read."""


#: System-domain vocabulary: words whose subject is the software, not the
#: game. Several entries are deliberately phrases rather than the bare word,
#: because the bare word is ordinary Magic vocabulary and matching it would
#: refuse real play prose: `stack` is CR 405, `pod` and `deploy` are printed
#: on cards, `engine` is what a deck's card advantage runs on, and "with the
#: exception of" is just English.
_TERMS: tuple[tuple[str, str], ...] = (
    (r"\bapis?\b", "names the api"),
    (r"\bendpoints?\b", "names an http endpoint"),
    (r"\bhttps?\b", "names the http protocol"),
    (r"\bstatus code\b", "names an http status code"),
    (r"\bmcp\b", "names the MCP tool layer"),
    (r"\bmcp-seat\b", "names this system's own command"),
    (r"\bmcp tools?\b", "names an MCP tool"),
    (r"\btool (?:arguments?|calls?|names?)\b", "names a tool's own argument"),
    (r"\b(?:missing|wrong|extra) arguments?\b", "names a tool's own argument"),
    (r"\bthe tool\b", "names a tool rather than a card"),
    (r"\btimeouts?\b", "names a timeout"),
    (r"\btimed out\b", "names a timeout"),
    (r"\bcontainers?\b", "names a container"),
    (r"\bkubernetes\b|\bk8s\b", "names the cluster"),
    (r"\bdeployments?\b|\bredeploy(?:ed|s)?\b|\b(?:a|the) deploy\b", "names a deployment"),
    (r"\bin-pod\b|\bpods\b|\bthe pod\b|\bpod (?:logs?|restart|crash)", "names the pod"),
    (r"\bstack trace\b|\btracebacks?\b", "names a stack trace"),
    (r"\bstderr\b|\bstdout\b", "names a process stream"),
    (r"\bexception\b(?!\s+(?:of|to)\b)", "names a raised exception"),
    (r"\bengine bugs?\b|\bapi bugs?\b|\ba bug in\b", "names a bug in this system"),
    (r"\brules gaps?\b|\bcoverage gaps?\b", "names a gap in the rules engine"),
    (r"\brules engine\b|\bthe engine (?:returned|refused|said|answered)\b", "names the engine"),
    (r"\bservers?\b|\bbackend\b|\bfrontend\b", "names a service"),
    (r"\bwebsockets?\b", "names the websocket"),
    (r"\bjson\b|\byamls?\b", "names a serialisation format"),
    (r"\bregressions?\b", "names a regression"),
    (r"\bcrashed?\b|\bcrashes\b|\bcrash logs?\b", "names a crash"),
    (r"\berror (?:messages?|codes?)\b|\breturned an error\b", "names an error the software gave"),
)

#: Shapes that are structurally code rather than prose, whatever words sit
#: around them. These are what catch the sentence that is careful to avoid
#: the vocabulary above and then prints the fix.
_SHAPES: tuple[tuple[str, str], ...] = (
    (r"(?<![\w-])--[a-z][a-z0-9-]*\b", "names a command-line flag"),
    (
        r"\b[a-z_][a-z0-9_]*\s*=\s*(?:true|false|none|null|\d|\"|'|`)",
        "names a parameter and the value to set it to",
    ),
    (r"`[^`\n]*[_/(){}\[\]=:.$*\-][^`\n]*`", "quotes a code identifier in backticks"),
    (
        r"\b(?:src|tests|scripts|docs|contract|decks|meta)/[\w./-]+",
        "names a path into a source tree",
    ),
    (r"\b[\w-]+\.(?:py|json|ya?ml|toml|md|sh|ts|js|lock)\b", "names a source file"),
    (
        r"\bplays (?:admit|check|priority)\b|\bguides propose\b|\bmise run\b",
        "names one of this system's own commands",
    ),
    (r"\breport_problem\b|\bhold_priority\b|\bpass_until\b", "names one of this system's fields"),
    (
        r"\b(?:code change|change the code|open an issue|file a bug|"
        r"the fix (?:is|was)|fixed by (?:setting|passing|adding))\b",
        "names a remedy that is a code change",
    ),
)

_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), why) for pattern, why in (*_TERMS, *_SHAPES)
)


def added_lines(before: str | None, after: str) -> list[str]:
    """The non-blank lines of `after` that `before` did not already have.

    `before` is `None` for a file that is not in HEAD at all, and then every
    line of it is new. Line-level rather than word-level on purpose: a
    sentence is the unit the gate judges and the unit it quotes back.
    """
    old = (before or "").splitlines()
    new = after.splitlines()
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    added: list[str] = []
    for tag, _, _, start, end in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added += [line.strip() for line in new[start:end] if line.strip()]
    return added


def added_plan_passages(before: Any, after: Any) -> list[str]:
    """The strings in the boarding plan's editable fields that are new.

    Only `matchups[].play_plan` and `matchups[].notes` can change at all -
    `guides_propose.forbidden_plan_changes` has already refused anything else
    - so those are the only fields read here. A string that was already
    somewhere in those fields is not new, however it moved.

    Raises `Undecidable` when a field is not the shape it is supposed to be,
    rather than guessing at what the seat meant.
    """
    counts = Counter(_plan_strings(after)) - Counter(_plan_strings(before))
    return [value for value, count in counts.items() for _ in range(count)]


def _plan_strings(plan: Any) -> list[str]:
    if not isinstance(plan, dict):
        raise Undecidable("the boarding plan is not a JSON object")
    matchups = plan.get("matchups", [])
    if not isinstance(matchups, list):
        raise Undecidable("the boarding plan's matchups are not a list")
    found: list[str] = []
    for index, matchup in enumerate(matchups):
        if not isinstance(matchup, dict):
            raise Undecidable(f"matchups[{index}] is not a JSON object")
        notes = matchup.get("notes")
        if notes is not None and (
            not isinstance(notes, list) or any(not isinstance(n, str) for n in notes)
        ):
            raise Undecidable(f"matchups[{index}].notes is not a list of strings")
        found += notes or []
        plan_field = matchup.get("play_plan")
        if plan_field is not None and not isinstance(plan_field, dict):
            raise Undecidable(f"matchups[{index}].play_plan is not a JSON object")
        found += _strings_within(plan_field, f"matchups[{index}].play_plan")
    return found


def _strings_within(value: Any, where: str) -> list[str]:
    """Every string leaf of a `play_plan`, whose shape is not fixed."""
    if value is None or isinstance(value, bool | int | float):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings_within(item, where)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings_within(item, where)]
    raise Undecidable(f"{where} holds a value this gate cannot read as text")


def system_problem_reasons(passages: Iterable[str]) -> list[str]:
    """One reason per passage that reads as a system problem, quoting what it
    read and saying which rule read it that way. Empty means the gate found
    nothing - which is not the same as the text being proven play content."""
    reasons: list[str] = []
    for passage in passages:
        for rule, why in _RULES:
            found = rule.search(passage)
            if found is None:
                continue
            reasons.append(f'"{_excerpt(passage)}" - {why} ("{found.group(0).strip()}")')
            break
    return reasons


def _excerpt(passage: str) -> str:
    text = " ".join(passage.split())
    return text if len(text) <= EXCERPT else text[: EXCERPT - 3] + "..."


def refusal_message(reasons: Sequence[str]) -> str:
    """Why this proposal was refused, where the text should go instead, and
    the way out for the sentence the gate read wrong."""
    return (
        "a deck's guide is play review only, and this proposal's new text reads as a "
        "system problem rather than as how the deck is piloted:\n"
        + "\n".join(f"  - {reason}" for reason in reasons)
        + "\n\nFile it with the `report_problem` tool instead: that goes to this game's own "
        "audit ledger, where a system problem can be acted on. A deck's guide is read by a "
        "human pilot and cannot fix any of it, so it does not belong in NOTES.md, a "
        "play_plan or a matchup note however much it shaped the game. Nothing was changed, "
        "committed or pushed, and your text is untouched. This check is a heuristic and not "
        "a proof: if the text above really is play content, reword it without the wording "
        "named and propose again."
    )


def undecided_message(what: str) -> str:
    """The fail-closed refusal: it could not read the added text, so it did
    not admit it."""
    return (
        f"could not decide whether this proposal's new text is play content or a system "
        f"problem, because it could not be read: {what}. Refusing rather than admitting "
        "text unread - a guide edit that slipped past this check cannot be taken back by "
        "rewording it. Nothing was changed, committed or pushed. Put the file back into "
        "plain text (or the boarding plan's notes back into a list of strings) and propose "
        "again; if the text is about this system rather than the deck, file it with the "
        "`report_problem` tool instead."
    )
