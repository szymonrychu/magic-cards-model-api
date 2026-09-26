"""A single, non-interactive `claude --print` call - the post-game AI
review's own mechanism (`POST /v1/review` in `api.py`), and deliberately
never the supervised session `session.py`/`pty_proc.py` own.

`pty_proc.py`'s own module docstring says the wrapper never runs `claude -p`
for the *play* session - print mode assembles a different system prompt and
diverges on skills, hooks and permissions, and the whole point of that
component is that the agent plays in the harness a human gets. That
constraint is about the play session specifically; it says nothing about
this module, which exists for an unrelated, one-time job the play session
was never meant to do: read a finished game's own reflection and record,
write a critique, and file what of it is play content back into the deck's
guide. Nothing here touches `session.Manager`, the turn state machine, or
the PTY at all - this is a plain blocking subprocess call, bounded by
`Settings.review_timeout`.

**No longer isolated from the play session, and that is a deliberate
reduction (defect D-2) rather than an oversight.** This module used to run
in a fresh `tempfile.mkdtemp` HOME with `cwd` nowhere near the workspace, so
that a `claude --print` could not read the play session's own
`.claude/settings.json`, its `.mcp.json` or anything else the agent
container carries. That isolation also made the review unable to do the job
it is now asked to do: `~/.gitconfig` - the credential helper for
github.com and `safe.directory = /guides`, written by
`bootstrap.write_gitconfig` - lives in `settings.home_dir`, the seat's MCP
servers live in `settings.workspace / ".mcp.json"` (written by
`bootstrap.merge_mcp`), and no `mcp-seat` run outside them could push to
mtg-decks main. So the review now runs with `cwd =
settings.workspace` and `HOME = settings.home_dir`, as the agent.

What that costs, and what pays for it:

- **The `/guides` checkout is now worked by two processes at once.** The
  review runs on a background thread while the handover turn - whose own
  step 2 is `mcp-seat plays admit` - is pushed into the interactive session.
  The mitigation is T14's `playscripts.gitwork.checkout_lock`, a real
  `flock` on the checkout that `plays admit` and `guides propose` each hold
  for the whole of their git phase. It is a FILE lock precisely because
  these are separate processes in one container. That is why every Bash rule
  on the allowlist below names an `mcp-seat` subcommand and none of them
  names `git`: a raw `git` would walk straight past the lock.
- **The play session's Stop hook is now inside this process's HOME.**
  `bootstrap.write_settings_json` writes `settings.home_dir /
  ".claude/settings.json"` with `hooks.Stop[0].hooks[0].command =
  settings.hook_command` (normally `cc-stop-hook`), and a `claude --print`
  that loaded that file would fire that hook when it finished.
  `wrapper/stop_hook.py` then POSTs a turn completion to `CCW_INTERNAL_URL`,
  which it reads straight off its own environment and DEFAULTS to this
  wrapper's own internal listener - so simply not inheriting the variable
  guards nothing. A completion with no turn id of its own is trusted by
  `session.Manager.complete` and would end whatever turn - possibly the
  handover turn - is actually in flight. Two guards, because one of them
  depends on a CLI flag behaving as documented: `--setting-sources ""` loads
  no settings file at all, so the hook is never registered; and
  `CCW_INTERNAL_URL` is NAMED in the child's environment, pointed at
  `REVIEW_STOP_HOOK_SINK`, so a hook that somehow still fires cannot reach
  the listener that matters.

**A tool allowlist, not "no tools" and not "all tools".** The review reads a
finished game's record and reflection - which can contain opponent-written
or AI-generated text - and then writes play content back into the deck's
guide. `REVIEW_TOOLS` is the built-in set it gets, `review_allowed_tools()` is
what may run without a prompt, `bootstrap.DENY_TOOLS` stays denied, and
`--permission-prompts none` means anything outside that is refused rather
than stalling a headless process for ever. `--strict-mcp-config` stays, now
paired with an explicit `--mcp-config` so the seat's own MCP servers are
exactly what loads and nothing from any other source can.

**Both halves of the routing rule are reachable from here.**
`NOTES_ROUTING` (the rule `driver/review.py` interpolates into the prompt)
has two destinations: play content to the deck's guide, and system problems
to the `report_problem` tool. The guide half is what `review_bash_rules`
below is for. The problem half is `REVIEW_MCP_TOOLS`: `report_problem` is
an MCP tool of the `mtg` server and takes a `player_uid`, which this
process has none of by itself - so `prompt.NOTES_ROUTING_AFTER_GAME` names
the SEAT's uid in the prompt (it outlives the game: `app.find_seat`
resolves a uid against every game the store still holds, and
`app.report_problem` has no "the game is over" guard), and this module
allows that one tool and no other on that server. A rule that named a
destination the process could not reach would be a rule asking for
something impossible, which is how it stood before T13-r1.2.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .bootstrap import DENY_TOOLS
from .config import Settings

#: A generous ceiling on the prompt this call ever sends - not a realistic
#: size (a game record plus a reflection, `driver/review.py`'s own
#: `MAX_DECLARATIONS` cap included, comes nowhere close), but a backstop
#: against passing something pathological straight through to
#: `subprocess.run`'s stdin.
#:
#: **FB1: the prompt travels on stdin, not argv** (see `_argv`'s and
#: `run_review`'s own docstrings) - Linux's `MAX_ARG_STRLEN` (131072 bytes
#: per argv element) made a ~190KB review prompt fail with `OSError: [Errno
#: 7] Argument list too long` when it rode argv as the trailing element
#: after `--`. This ceiling still exists (stdin has no comparable practical
#: bound at this size, but an unbounded read from a pathological caller is
#: still worth refusing with this module's own, clearer `ReviewFailed`
#: rather than an OS-level surprise somewhere downstream).
#:
#: DR3: raised from 200_000 by exactly the 30_000 `driver.review.
#: PROMPT_BUDGET_CHARS` itself grew by (170_000 -> 200_000) to give the new
#: Decisions section its own fixed slice - "only as far as the new
#: PROMPT_BUDGET_CHARS needs", never re-chosen independently of it. The
#: 30_000-character margin between the two is unchanged.
MAX_PROMPT_CHARS = 230_000

#: The built-in tools the review is given (`--tools`), and why each one is
#: here. This is an ALLOWLIST: a built-in tool not named here does not exist
#: for this process, so it is never a permission question in the first
#: place.
#:
#: - `Read` - `matchups[].play_plan`/`notes` in the deck's newest
#:   `boarding-plan.json` and `decks/<slug>/NOTES.md` have to be read before
#:   either can be changed; `guides propose` compares the result against
#:   HEAD and refuses every other key, so a blind edit is a refusal.
#: - `Edit` - the in-place change to those same two files. This is the one
#:   tool the write-back half of the review cannot be done without.
#: - `Write` - a deck whose `NOTES.md` does not exist yet. `guides propose`
#:   allows that exact path whether or not it is already tracked, and `Edit`
#:   cannot create a file.
#: - `Glob` - `boarding-plan.json` lives under `decks/<slug>/analysis/<run>/
#:   domain3-matchup-threat/`, and which run is newest is not known ahead of
#:   time. Without it the reviewer would need a shell just to look.
#: - `Grep` - finding the matchup entry to change inside a plan that carries
#:   every matchup.
#: - `Bash` - the only way to run `mcp-seat`. Scoped to four subcommands,
#:   each reachable bare and by absolute path, by `review_bash_rules` below;
#:   nothing else may run through it.
#:
#: Everything absent is absent on purpose. `WebFetch`/`WebSearch` would let
#: text from the game record - which is untrusted, and marked as such in the
#: prompt - reach the network. `Task` would spawn subagents inside a bounded
#: one-shot process. `NotebookEdit` has nothing here to edit.
REVIEW_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash")

#: The `mcp-seat` subcommands the review may run, each named as `mcp-seat
#: <subcommand>` (the prefix `review_bash_rules` turns into a `Bash(...:*)`
#: rule, bare and by absolute path alike - see that function).
#:
#: **These four, and no `git`.** T14's `playscripts.gitwork.checkout_lock`
#: is taken INSIDE `plays admit` and `guides propose`, so it serializes the
#: review against the concurrent handover turn only for as long as every
#: write to the checkout goes through one of those commands. A rule allowing
#: `git` (or `gh`, or a shell that could reach either) would let the review
#: commit, rebase or push with the lock held by somebody else - which does
#: not lose a race, it corrupts the other writer's fetch-rebase-push
#: sequence, since the index, HEAD and the working tree are shared state
#: that neither sequence re-reads between its steps. `mcp-seat` itself
#: shells out to `git`/`python3` as its own subprocess to do this work -
#: Claude's permission rules do not govern what a permitted command spawns,
#: so those two are not needed here for `mcp-seat` to work, only kept off
#: the allowlist directly. `find` is not needed either: `Glob`/`Grep`/`Read`
#: (on `REVIEW_TOOLS`) already cover looking around the checkout.
#:
#: - `mcp-seat plays check` - the read-only listing: it lints the checkout's
#:   play scripts offline and reports the corpus and its conflicts. It is
#:   the corpus view the review can actually use; the `mtg` MCP server's own
#:   `list_scripts` needs a live seat uid and a live board, and by review
#:   time the game is over.
#: - `mcp-seat plays priority` - the one bounded edit to a play's priority
#:   (at most 5 per call and per game), recorded for `admit`.
#: - `mcp-seat plays admit` - takes those play-script changes to mtg-decks
#:   main, gate included.
#: - `mcp-seat guides propose` - the matchup-plan and NOTES.md edit, pushed
#:   straight to mtg-decks main after its gates.
#:
#: **FC2: each of the four also gets an absolute-path rule, not only the
#: bare one above.** Live evidence (loop game 2's p2 review, ledger seq 299)
#: showed every `mcp-seat` call refused - bare AND by the absolute path
#: `/app/.venv/bin/mcp-seat` alike - while `python3`/`git`/`find` were
#: refused too and p1's review in the SAME game admitted plays fine. The
#: planner's probe of the real CLI (`scripts/probe_review_permissions.sh`)
#: narrowed this to command shape, not a missing subcommand: a bare
#: `fakeseat plays check ...` runs under `Bash(fakeseat plays check:*)`, but
#: an absolute-path invocation, an env-var-prefixed one, or a `cd ... &&`
#: chain is refused even though the subcommand named is identical - Claude's
#: Bash permission match is on the LITERAL prefix of the command as typed,
#: not on which binary it resolves to. So a reviewer that reaches for the
#: absolute path (as the live evidence shows it does, likely because the
#: prompt never named the checkout's own `--root` and it went looking) needs
#: a rule that names that exact shape too - `cd`, an env-var prefix and an
#: `&&` chain are still refused on purpose (see `driver.review`'s prompt
#: sentence about it) and get no rule here.
#:
#: **Prod parity.** `deploy/seat-job.yaml` and `Dockerfile.agent` run this
#: exact wrapper code with `/guides` mounted the same way - the agent
#: image's own venv puts `mcp-seat` on `PATH` at `/app/.venv/bin/mcp-seat`
#: (`Dockerfile.agent`: `ENV PATH="/app/.venv/bin:${PATH}"`), which is
#: exactly what `mcp_seat_entrypoint`'s `shutil.which` resolves to there -
#: so the same gap existed in prod and the same fix covers it, with no
#: change needed to either file: neither overrides `PATH` or `GUIDES_ROOT`
#: in a way this fix depends on (the agent container's `GUIDES_ROOT` is
#: already unset, defaulting to `/guides`, which is the container's own
#: mount path).
_REVIEW_BASH_SUBCOMMANDS = (
    "mcp-seat plays check",
    "mcp-seat plays priority",
    "mcp-seat plays admit",
    "mcp-seat guides propose",
)


def mcp_seat_entrypoint() -> str:
    """The absolute path this process's own `PATH` resolves `mcp-seat` to,
    used only to build the absolute-path half of `review_bash_rules`.

    **Never hardcoded, and resolved fresh on every call** (not cached at
    import time), so a test that changes `PATH` sees the change and a
    future rename of the venv directory needs no edit here. `shutil.which`
    is the primary path - it is exactly what a shell would resolve `PATH`
    to, and it is what the agent image's own `PATH`
    (`Dockerfile.agent`: `/app/.venv/bin` prepended) resolves to in prod.

    Falls back to the sibling of the running interpreter's own venv
    (`sys.executable`'s directory) when `PATH` resolution fails - `uv sync`
    installs `mcp-seat` and the interpreter running this code into the same
    venv, so that sibling path is a real, derived answer, never a guess and
    never `/app` typed into source."""
    resolved = shutil.which("mcp-seat")
    if resolved:
        return resolved
    return str(Path(sys.executable).parent / "mcp-seat")


def review_bash_rules(entrypoint: str | None = None) -> tuple[str, ...]:
    """`Bash(<prefix>:*)` rules for `_REVIEW_BASH_SUBCOMMANDS`, each named
    twice - once bare (`Bash(mcp-seat plays check:*)`), once by the absolute
    entrypoint path (`Bash(/app/.venv/bin/mcp-seat plays check:*)`) - eight
    rules in total. See this module's docstring and the block comment above
    `_REVIEW_BASH_SUBCOMMANDS` for why both shapes are needed and why
    nothing else (`git`, `python3`, `find`, `cd`, a bare `Bash`, or
    `mcp-seat` with no subcommand) is ever added.

    `entrypoint` is resolved through `mcp_seat_entrypoint()` when not given
    - a parameter only so a caller (or a test) can pin an exact path rather
    than depending on this process's own `PATH`."""
    entrypoint = entrypoint or mcp_seat_entrypoint()
    rules: list[str] = []
    for prefix in _REVIEW_BASH_SUBCOMMANDS:
        subcommand = prefix.removeprefix("mcp-seat ")
        rules.append(f"Bash({prefix}:*)")
        rules.append(f"Bash({entrypoint} {subcommand}:*)")
    return tuple(rules)


#: The MCP tools the review may call, and the reason there is exactly one.
#:
#: `--tools` above is the BUILT-IN set only (`claude --help`: "the list of
#: available tools from the built-in set"), so an MCP server named by
#: `--mcp-config` offers all of its tools whatever that flag says; what may
#: actually be called is decided here, on `--allowedTools`, with
#: `--permission-prompts none` denying everything else rather than stalling.
#:
#: - `mcp__mtg__report_problem` - the second destination of
#:   `prompt.NOTES_ROUTING`. A reviewer that finds an api bug, an engine
#:   rules gap, a tool misbehaviour or a timeout files it as a
#:   `report_problem` row in the game's own audit ledger, exactly where the
#:   seat's own report would have gone - and `playscripts.content_gate`
#:   refuses that same sentence in a `guides propose`, so without this the
#:   finding had nowhere at all to go.
#:
#: Nothing else on the `mtg` server is here. The prompt hands this process a
#: live seat credential to call `report_problem` with, and a finished game
#: has no move left to make: `cast`, `pass`, `activate`, `declare_plan` and
#: the rest would only let untrusted text inside the game record talk a
#: reviewer into touching a game it is supposed to be writing ABOUT. The
#: `spellslinger` server is absent for its own reason - it is a network
#: lookup, the same reason `WebFetch`/`WebSearch` are off `REVIEW_TOOLS`.
REVIEW_MCP_TOOLS = ("mcp__mtg__report_problem",)


def review_allowed_tools(entrypoint: str | None = None) -> tuple[str, ...]:
    """What may run without asking (`--allowedTools`): the file tools, Bash
    only through `review_bash_rules(entrypoint)`, and the MCP tools in
    `REVIEW_MCP_TOOLS`. Bare `"Bash"` is deliberately NOT here - an unscoped
    Bash rule would make the scoping above decoration - and neither is a
    server-wide `mcp__mtg` or `mcp__mtg__*`, for the same reason. Paired
    with `--permission-prompts none`, anything outside this list is denied
    automatically rather than prompting a process with nobody at the
    terminal to answer.

    A function, not a module constant, for the same reason
    `review_bash_rules` is: the absolute-path rules it carries have to
    reflect `mcp_seat_entrypoint()` as resolved AT ARGV-BUILD TIME
    (`_argv` calls this with no `entrypoint`), never a value frozen at
    import time before this process's own `PATH` was necessarily final."""
    return (
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
        *review_bash_rules(entrypoint),
        *REVIEW_MCP_TOOLS,
    )


#: Where a Stop hook that fires in this process anyway is sent instead of
#: the wrapper's own internal listener - see the module docstring. Port 1 on
#: loopback: nothing listens there, the connection is refused immediately
#: rather than hanging, and `cc-stop-hook` gives up after its own retries
#: and exits 0 (it always does: "a lost result is recoverable, a wedged
#: agent is not").
REVIEW_STOP_HOOK_SINK = "http://127.0.0.1:1/internal/turn-complete"

#: The variables `mcp-seat`'s guides commands read off their own
#: environment, each named here rather than inherited - see `_env_for`.
#:
#: - `MTG_GAME_ID` - `plays priority`'s `--game-id` default, which is how
#:   the per-game priority bound is attributed.
#: - `MTG_EVIDENCE_DIR` - where the driver wrote this game's play-offer
#:   evidence record, which `plays admit` commits beside the deck's plays.
#:
#: `MTG_ROLE` is NOT in this list: it is set, not passed through, because
#: the container's own value is the seat's. `GIT_COMMITTER_NAME`/
#: `GIT_COMMITTER_EMAIL` are NOT in this list either (FD6 (5), fb3-parity.md
#: item 10): `gitwork.identity` sets the COMMITTER from `MTG_ROLE`, so
#: either variable in this container's own environment never took effect -
#: dead config, dropped here along with `deploy/seat-job.yaml`'s matching
#: env entries.
PASSED_THROUGH = (
    "MTG_GAME_ID",
    "MTG_EVIDENCE_DIR",
)

#: Never passed through from this process's own environment. `HOOK_COMMAND`
#: configures which script the play session's Stop hook runs;
#: `CCW_RESULT_JSON` is `cc-stop-hook`'s own target, read straight off
#: `os.environ` by that script (`wrapper/stop_hook.py`), never through
#: `Settings`. `CCW_INTERNAL_URL` is the third of that family and is not
#: merely withheld - it is overridden with `REVIEW_STOP_HOOK_SINK`, because
#: `cc-stop-hook` defaults to the real listener when it is unset.
_NEVER_INHERITED = ("HOOK_COMMAND", "CCW_RESULT_JSON")


class ReviewFailed(Exception):
    """`claude` exited non-zero, could not be found, could not be executed,
    ran past `Settings.review_timeout`, or the container it was asked to run
    in was never rendered - the one exception `run_review` raises, so
    `api.py`'s route has one thing to catch rather than several."""


def _env_for(settings: Settings) -> dict[str, str]:
    """A minimal env, built from nothing rather than `dict(os.environ)`.

    The discipline is unchanged and load-bearing: every variable the child
    gets is NAMED here, and a variable this wrapper's own process happens to
    carry does not reach the review unless it appears in this function. A
    future env var added to the wrapper has to be named to reach the child
    at all, rather than merely remembered to be stripped - which is why
    `PASSED_THROUGH` reads `os.environ` one key at a time instead of copying
    it and deleting the rest.

    What is named, and why:

    - `HOME` - `settings.home_dir`, so `~/.gitconfig`'s credential helper
      and `safe.directory = /guides` are the ones `bootstrap` wrote.
    - `PATH` - to find `claude`, `mcp-seat`, `git` and whatever they shell
      out to.
    - the auth credential, at the same precedence `pty_proc.spawn_claude`
      applies: an OAuth token from `claude setup-token` is NOT an API key,
      the CLI reads them from different variables, and one placed in the
      other's name is sent as the wrong kind of credential and gets a 401.
    - `WORKSPACE` - `seat_cli` reads it for the drafts directory. Taken from
      `Settings`, not from the environment: it is the wrapper's own setting.
    - `MTG_ROLE` - forced to `gitwork.REVIEWER_ROLE`. The container's own
      value is the SEAT's, and the review is a third writer for the same
      game and the same deck: sharing the role would author its commits on
      main as the seat's.
    - `CCW_INTERNAL_URL` - `REVIEW_STOP_HOOK_SINK`, see the module
      docstring.
    - `PASSED_THROUGH` - the rest of what `mcp-seat` reads, one named key at
      a time, left out entirely when unset rather than set to "".
    """
    # Local, not module-level: `magic_cards_model_api.__init__` builds the
    # whole game FastAPI app on import, and the wrapper's own api module
    # must not drag that in just to learn one string. The same pattern
    # `driver/handover.py` uses for this package.
    from magic_cards_model_api.playscripts.gitwork import REVIEWER_ROLE  # noqa: PLC0415

    env: dict[str, str] = {
        "HOME": str(settings.home_dir),
        "WORKSPACE": str(settings.workspace),
        "MTG_ROLE": REVIEWER_ROLE,
        "CCW_INTERNAL_URL": REVIEW_STOP_HOOK_SINK,
    }
    path = os.environ.get("PATH")
    if path:
        env["PATH"] = path
    if settings.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token
    elif settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    for name in PASSED_THROUGH:
        value = os.environ.get(name, "").strip()
        if value:
            env[name] = value
    return env


def _argv(
    settings: Settings,
    model: str,
    effort: str,
    mcp_config: Path,
    *,
    tools: tuple[str, ...] = REVIEW_TOOLS,
    allowed_tools: tuple[str, ...] | None = None,
) -> list[str]:
    """The argv `claude` is spawned with - see the module docstring for the
    allowlist and the two isolation guards.

    **The prompt is not here.** It used to be the trailing element after a
    bare `--` (`--tools`, `--allowedTools` and the rest are variadic flags,
    and without a `--` a prompt beginning with a dash would be read as more
    tool names - the same reason `ai_seat.command_for` carries one). Linux's
    `MAX_ARG_STRLEN` (131072 bytes per argv element) made a ~190KB review
    prompt fail with `OSError: [Errno 7] Argument list too long` riding argv
    that way, so `run_review` now sends it on stdin instead
    (`subprocess.run(..., input=_capped(prompt))`), which `claude --print`
    reads its prompt from exactly as it would from a terminal pipe - and
    the trailing `--`/prompt pair this function used to return is gone with
    it, not merely moved.

    **`tools`/`allowed_tools` (review fix, round 1 finding 2).** Default to
    the plays pass's own full allowlist (`REVIEW_TOOLS`/
    `review_allowed_tools()`) so every existing caller of this function
    keeps its old shape. `run_review`'s prose-only first pass passes empty
    tuples for both instead - not merely an unused allowlist, a `--tools ""`
    that removes every built-in tool from the process's own surface, so
    that pass cannot run `mcp-seat` (or anything else) even if it tried."""
    resolved_allowed = allowed_tools if allowed_tools is not None else review_allowed_tools()
    return [
        settings.claude_path,
        "--print",
        "--model",
        model,
        "--effort",
        effort,
        # The tool surface: which built-ins exist, which may run unasked,
        # which stay denied, and that nothing may prompt.
        "--tools",
        ",".join(tools),
        "--allowedTools",
        ",".join(resolved_allowed),
        "--disallowed-tools",
        ",".join(DENY_TOOLS),
        "--permission-prompts",
        "none",
        # The guides checkout sits outside `cwd`, so the file tools are only
        # allowed to reach it by naming it.
        "--add-dir",
        str(settings.guides_dir),
        # No settings file loads: HOME is the play session's own now, and
        # its settings.json carries the play session's Stop hook.
        "--setting-sources",
        "",
        # Exactly the seat's own MCP servers, and nothing from any other
        # source - `--strict-mcp-config` is what makes `--mcp-config` the
        # only one that counts.
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
    ]


#: FD6 (2): what `_capped` appends when it had to cut anything to fit, and
#: what `run_review` looks for on its own return value to know whether to
#: append `REVIEW_TRUNCATED_NOTE` to the text it files. A plain marker
#: string rather than a boolean return from `_capped`, so the one existing
#: caller (`test_the_prompt_travels_on_stdin_never_in_argv`) that already
#: pins `_capped`'s return value by equality keeps working unchanged.
_CAPPED_NOTICE = "[truncated: earlier ledger/decision material was cut to fit the prompt budget]"


def _capped(prompt: str) -> str:
    """`prompt`, cut to `MAX_PROMPT_CHARS` when it is over - and, unlike the
    naive `prompt[:MAX_PROMPT_CHARS]` this replaced, cutting the FRONT while
    keeping the TAIL rather than the other way round.

    **Why the front, not the back (FD6 (2)).** `driver.review.review_prompt`
    builds the ledger and the per-decision boards first and appends the
    write bars and the ask LAST - so the old behaviour silently discarded
    exactly the instructions a reviewer needs to act (the DSL ask, the write
    bars, `NOTES_ROUTING`) on the one game large enough to reach this
    guillotine at all, while keeping the ledger material that is cheapest to
    lose. Keeping the tail and cutting the front drops the ledger/decisions
    material instead, and in practice never even reaches the tail at all:
    `driver.review.PROMPT_BUDGET_CHARS` already keeps an ordinary game well
    under this ceiling, so this is a backstop for a genuinely pathological
    game, not the common path.

    Cuts on a line boundary where practical (searching only a short window
    right after the cut point), so the kept text does not open mid-row."""
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    notice = _CAPPED_NOTICE + "\n\n"
    keep = MAX_PROMPT_CHARS - len(notice)
    tail = prompt[-keep:] if keep > 0 else ""
    newline = tail.find("\n")
    if 0 <= newline < 2000:
        tail = tail[newline + 1 :]
    return notice + tail


def _preflight(settings: Settings) -> Path:
    """The two paths `run_review` names on the command line and cannot make
    itself, checked before anything is spawned so each is an operator's own
    sentence rather than whatever the CLI says about a missing path."""
    mcp_config = settings.workspace / ".mcp.json"
    if not mcp_config.is_file():
        raise ReviewFailed(
            f"{mcp_config} is missing: the wrapper's bootstrap (merge_mcp) has not rendered "
            f"the workspace, so the review has no MCP configuration to run against"
        )
    if not settings.guides_dir.is_dir():
        raise ReviewFailed(
            f"the guides checkout {settings.guides_dir} is missing: deploy/guides-clone.sh "
            f"has not run, so the review has nothing to write its play content back into"
        )
    return mcp_config


def _partial_text(raw: Any) -> str:
    """`subprocess.TimeoutExpired.stdout` as a stripped `str`, whatever
    shape it arrived in - CPython's own `Popen.communicate` hands it back as
    `bytes` even when `subprocess.run` was called with `text=True`, since
    the exception is raised (and its `stdout`/`stderr` populated) inside
    `communicate` before that decoding step ever runs."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return (raw or "").strip()


#: FD6 (3): appended to a timeout's own salvaged partial text (see
#: `_partial_text`/`run_review`), so a filed review that ran out of time
#: mid-DSL-submission says so rather than reading as a complete critique
#: that simply stopped early.
REVIEW_TIMED_OUT_NOTE = (
    "review_timed_out: true (the review process was killed after its own timeout; "
    "the text above is whatever it had already written, and its DSL submission may "
    "not have finished)"
)

#: FD6 (2): appended to the filed review when `_capped` had to cut the
#: prompt to fit - see `_capped`'s own docstring for why the cut lands on
#: the ledger/decisions material and never on the ask or the write bars.
REVIEW_TRUNCATED_NOTE = (
    "review_truncated: true (the game record was too large for the prompt budget; "
    "earlier ledger and per-decision board material was cut to fit, and the ask and "
    "write instructions were kept in full)"
)


def run_review(
    settings: Settings, prompt: str, model: str, effort: str, *, timeout: float | None = None
) -> str:
    """Run a bounded `claude --print` with the review's tool allowlist, in
    the agent's own workspace and HOME, and return its stdout, stripped.

    **The prompt travels on stdin, never on argv.** `subprocess.run` is
    called with `input=_capped(prompt)` (`text=True`, so a `str` is fine),
    and `claude --print` reads its prompt from stdin exactly as it would
    from a terminal pipe. Before this, the prompt rode argv as the trailing
    element after a bare `--`, and Linux's `MAX_ARG_STRLEN` (131072 bytes
    per argv element) made a ~190KB review prompt fail with `OSError:
    [Errno 7] Argument list too long` - see `_argv`'s own docstring.

    `timeout`, when given, overrides `settings.review_timeout` for this one
    call - `wrapper.config.scaled_review_timeout` computes it from the
    reviewed game's own decision count (FD6 (3)), so a review with hundreds
    of decisions is not bound by the same flat number a short game is.
    `None` (every caller before this) keeps the old behaviour exactly.

    **A tool-less prose pass runs first (review fix, round 1 finding 2),
    THEN the tool-using plays pass - two separate `claude --print` calls,
    not one.** FD6 (3)'s original shape asked Opus, in a single call, to
    write its whole critique before touching `mcp-seat`, and salvaged
    `subprocess.TimeoutExpired.stdout` when a timeout killed that one call
    mid-DSL-submission. That salvage is real (`_partial_text` below still
    does it, for either pass), but it only ever recovers text the CHILD
    PROCESS had already flushed to its own stdout - and the real `claude
    --print` CLI, in its default non-streaming text output, holds its
    entire answer in memory and writes it in one block right before the
    process would exit normally. A process killed while it is still running
    a tool call has nothing sitting in the pipe for that salvage to find,
    no matter how early in the TRANSCRIPT the prose came before the tool
    call did. `_prose_pass` below is what actually guarantees the prose
    survives: it is given no tools at all (`_argv(..., tools=(),
    allowed_tools=())`), so it cannot get stuck on an `mcp-seat` round trip
    and reliably returns within `settings.review_prose_timeout` - well
    before `_plays_pass` (the original single-call shape, unchanged) ever
    starts. If the plays pass then times out, exits non-zero, or the
    binary vanishes out from under it, THAT failure is degraded to a note
    appended to the prose pass's own text (`_plays_phase_failed_note`)
    rather than raised - the prose already exists and was never at risk
    from the plays pass's own failure. Only the prose pass's own failure
    (nothing to salvage from IT either) still raises `ReviewFailed`, exactly
    as the single-call version always did.

    `timeout`, when given, overrides `settings.review_timeout` for the
    PLAYS pass only - `wrapper.config.scaled_review_timeout` computes it
    from the reviewed game's own decision count (FD6 (3)), so a review with
    hundreds of decisions is not bound by the same flat number a short game
    is. The prose pass's own budget (`settings.review_prose_timeout`) is
    unaffected: it makes no tool round trips, so it does not scale with
    decision count the way the plays pass does. `None` (every caller before
    this) keeps the old behaviour exactly for the plays pass's own bound.

    Raises `ReviewFailed` for every other way this can go wrong - an
    unrendered workspace or a missing guides checkout (before anything is
    spawned), a nonzero exit (the tail of BOTH streams in the message),
    `claude` missing from `PATH`, or any other `OSError` spawning it
    (permission denied on the binary is the seen-in-production case, but
    this covers the class rather than that one instance) - so the caller
    (`api.post_review`) has one exception to turn into a 502/504 rather than
    several call sites each guessing which failure mode it is looking at."""
    mcp_config = _preflight(settings)
    bound = settings.review_timeout if timeout is None else timeout
    capped_prompt = _capped(prompt)
    truncated = len(prompt) > MAX_PROMPT_CHARS

    prose_text = _prose_pass(settings, capped_prompt, model, effort, mcp_config)

    try:
        text = _plays_pass(settings, capped_prompt, model, effort, mcp_config, bound)
    except ReviewFailed as exc:
        text = f"{prose_text}\n\n{_plays_phase_failed_note(exc)}" if prose_text else None
        if text is None:
            raise

    if truncated:
        text = f"{text}\n\n{REVIEW_TRUNCATED_NOTE}"
    return text


#: Prepended to the prose pass's own stdin only (never the plays pass's,
#: which keeps the original prompt verbatim - see `test_run_review_sends_a_
#: 200k_prompt_on_stdin_byte_for_byte`, which pins the plays pass's input to
#: the prompt exactly). The prompt below this preamble still carries the DSL
#: submission ask (`driver.review._dsl_submission_ask`) - this pass simply
#: has no tools to act on it with - so without this sentence the model would
#: otherwise spend part of its own answer explaining that it cannot run
#: `mcp-seat` rather than writing the critique this pass exists to get.
_PROSE_PASS_PREAMBLE = (
    "This is pass 1 of 2. You have no tools in this pass - that is deliberate, not an "
    "error: it guarantees your prose critique and turning points survive even if pass 2 "
    "(with tools, run separately afterward) runs out of time mid-DSL-submission. Write "
    "the prose critique and the turning points section in full now, then stop - do not "
    "attempt the DSL submission section below; pass 2 handles that with the tools you "
    "do not have here.\n\n"
)


def _prose_pass(
    settings: Settings, capped_prompt: str, model: str, effort: str, mcp_config: Path
) -> str:
    """`run_review`'s tool-less first pass - see that function's own
    docstring for why it exists. No tools at all, so it cannot get stuck on
    an `mcp-seat` round trip; its own bound is `settings.review_prose_timeout`,
    fixed rather than scaled, since it makes no tool round trips to scale."""
    return _run_claude(
        settings,
        _PROSE_PASS_PREAMBLE + capped_prompt,
        model,
        effort,
        mcp_config,
        tools=(),
        allowed_tools=(),
        timeout=settings.review_prose_timeout,
    )


def _plays_pass(
    settings: Settings,
    capped_prompt: str,
    model: str,
    effort: str,
    mcp_config: Path,
    bound: float,
) -> str:
    """`run_review`'s original single-call shape, unchanged: the full tool
    allowlist, the full (possibly scaled) bound. Its own timeout/failure
    salvage (`_run_claude`'s `subprocess.TimeoutExpired` handling) stays as
    a backstop - it still recovers real partial text on the rare stub or
    CLI that DOES flush before a kill - but `run_review`'s own prose-pass
    fallback is what makes that recovery load-bearing rather than best hope
    for the realistic case where it recovers nothing."""
    return _run_claude(
        settings,
        capped_prompt,
        model,
        effort,
        mcp_config,
        tools=REVIEW_TOOLS,
        allowed_tools=review_allowed_tools(),
        timeout=bound,
    )


def _run_claude(
    settings: Settings,
    prompt: str,
    model: str,
    effort: str,
    mcp_config: Path,
    *,
    tools: tuple[str, ...],
    allowed_tools: tuple[str, ...],
    timeout: float,
) -> str:
    """One bounded `claude --print` call with the given tool surface - the
    machinery `_prose_pass` and `_plays_pass` both use, differing only in
    `tools`/`allowed_tools`/`timeout`. This is `run_review`'s entire body
    before the prose/plays split (FD6 (3) review fix, round 1): the
    subprocess call, the timeout salvage, and the nonzero-exit failure
    message, unchanged."""
    try:
        result = subprocess.run(
            _argv(settings, model, effort, mcp_config, tools=tools, allowed_tools=allowed_tools),
            cwd=settings.workspace,
            env=_env_for(settings),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ReviewFailed(f"{settings.claude_path} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        salvaged = _partial_text(exc.stdout)
        if salvaged:
            return f"{salvaged}\n\n{REVIEW_TIMED_OUT_NOTE}"
        raise ReviewFailed(f"the review did not finish within {timeout}s") from exc
    except OSError as exc:
        raise ReviewFailed(f"could not run {settings.claude_path}: {exc}") from exc
    if result.returncode != 0:
        # The real `claude` CLI writes some failures - an auth error among
        # them - to STDOUT, not stderr; building this from stderr alone left
        # a bare "claude exited 1" in production logs with nothing telling
        # an operator why, the one time they most need to know. Both
        # streams are captured either way, so both are read here - stdout
        # first, since a `--print` process on a genuine failure path prints
        # nothing else there for this to be confused with.
        combined = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
        )
        tail = combined[-2000:]
        raise ReviewFailed(
            f"claude exited {result.returncode}: {tail}"
            if tail
            else f"claude exited {result.returncode}"
        )
    return result.stdout.strip()


def _plays_phase_failed_note(exc: ReviewFailed) -> str:
    """What `run_review` appends to the prose pass's own text when the
    plays pass could not complete at all (a timeout with nothing to
    salvage, a nonzero exit, or the binary vanishing) - so a filed review
    says its DSL submission never ran, rather than reading as a complete
    critique that simply stopped early. Reuses the `review_timed_out` key
    `REVIEW_TIMED_OUT_NOTE` already uses, since a caller grepping for that
    marker should find every shape of "the plays phase did not finish"."""
    return (
        f"review_timed_out: true (the prose findings above were already written and filed "
        f"before the DSL submission phase ran; that phase did not finish: {exc})"
    )
