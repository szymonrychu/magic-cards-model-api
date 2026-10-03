"""Render every file claude reads at startup, before it is spawned.

A fresh HOME otherwise walks into several interactive dialogs that a
headless container has nobody to answer. Onboarding, folder trust and the
custom-API-key prompt are all suppressed by seeding ~/.claude.json. The
bypass-permissions warning is NOT seedable and is handled at boot by
session.boot_wait instead.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import shutil
from pathlib import Path
from typing import Any

from .. import seat_pair
from .config import Settings

log = logging.getLogger("wrapper.bootstrap")

#: Interactive tools a headless agent cannot use: a picker with nobody at the
#: terminal can only stall the turn. Deny wins even under bypassPermissions.
DENY_TOOLS = ["AskUserQuestion", "ExitPlanMode", "EnterPlanMode"]


def _write_json(path: Path, doc: Any, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    path.chmod(mode)


def write_claude_json(settings: Settings) -> None:
    """The no-dialog seed. Merged into any existing file rather than
    overwriting, so claude-written state from a prior run survives."""
    path = settings.home_dir / ".claude.json"
    doc: dict[str, Any] = {}
    # absent or corrupt: treat as empty
    with contextlib.suppress(OSError, ValueError):
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict):
            doc = loaded

    doc["hasCompletedOnboarding"] = True
    doc["autoUpdates"] = True

    projects = doc.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    project = projects.get(str(settings.workspace))
    if not isinstance(project, dict):
        project = {}
    project["hasTrustDialogAccepted"] = True
    projects[str(settings.workspace)] = project
    doc["projects"] = projects

    if settings.anthropic_api_key:
        suffix = settings.anthropic_api_key[-20:]
        responses = doc.get("customApiKeyResponses")
        if not isinstance(responses, dict):
            responses = {}
        approved = [a for a in responses.get("approved", []) if isinstance(a, str)]
        if suffix not in approved:
            approved.append(suffix)
        responses["approved"] = approved
        responses.setdefault("rejected", [])
        doc["customApiKeyResponses"] = responses

    _write_json(path, doc, 0o600)


def write_settings_json(settings: Settings) -> None:
    doc: dict[str, Any] = {
        "hooks": {
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": settings.hook_command}],
                }
            ]
        },
        "enableAllProjectMcpServers": settings.enable_all_mcp,
        "permissions": {"deny": list(DENY_TOOLS)},
    }
    if settings.pair_active and settings.seat_agent_channel is seat_pair.Channel.TEAMS:
        doc["env"] = {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}
        doc["teammateMode"] = "in-process"
    if settings.permission_mode:
        doc["permissions"]["defaultMode"] = settings.permission_mode
    _write_json(settings.home_dir / ".claude" / "settings.json", doc, 0o644)


def merge_mcp(settings: Settings) -> None:
    """Base config unioned with every *.json in the overlay dir. Overlay keys
    win. 0600 because an overlay can carry a URL with a token in it."""
    servers: dict[str, Any] = {}
    if settings.base_mcp_path is not None:
        try:
            base = json.loads(settings.base_mcp_path.read_text())
            servers.update(base.get("mcpServers") or {})
        except (OSError, ValueError) as exc:
            log.warning("base mcp unreadable, ignoring: %s", exc)
    if settings.mcp_overlay_dir is not None and settings.mcp_overlay_dir.is_dir():
        for entry in sorted(settings.mcp_overlay_dir.iterdir()):
            if entry.is_dir() or entry.suffix != ".json":
                continue
            try:
                fragment = json.loads(entry.read_text())
            except (OSError, ValueError) as exc:
                log.warning("mcp overlay %s unreadable, ignoring: %s", entry.name, exc)
                continue
            servers.update(fragment.get("mcpServers") or {})
    _write_json(settings.workspace / ".mcp.json", {"mcpServers": servers}, 0o600)


def write_claude_md(settings: Settings) -> None:
    pairs = [
        (settings.project_claude_md_path, settings.workspace / "CLAUDE.md"),
        (settings.global_claude_md_path, settings.home_dir / ".claude" / "CLAUDE.md"),
    ]
    for source, target in pairs:
        if source is None or not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


#: The seat's rules-research subagent: the seat hands it one rules question
#: in the background while it waits, and reads the answer when it next acts
#: (`driver/prompt.py`, "Rules questions go to your researcher").
RULES_RESEARCHER = "rules-researcher"

#: Everything it may call: the `spellslinger` rules and card lookups, and the
#: read-only file tools for the guides. Nothing on the `mtg` server - a
#: subagent never acts in the game - and nothing that writes or runs.
RULES_RESEARCHER_TOOLS = (
    "mcp__spellslinger__knowledge_search",
    "mcp__spellslinger__resolve_cards",
    "Read",
    "Grep",
    "Glob",
)

#: Denied on top of the allowlist, so a tool that is inherited by default
#: (every MCP server the seat has) cannot slip through: the whole `mtg`
#: server, shell and file writes, and spawning subagents of its own.
RULES_RESEARCHER_DENY = (
    "mcp__mtg",
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "Agent",
    "Task",
)

RULES_RESEARCHER_PROMPT = """You answer one rules question for a Magic: the Gathering player
who is in the middle of a game and will read your answer when they next act.
You cannot act in the game and must not try: you have no game tools.

- Look every rule up with `knowledge_search` (type "rules"), and every card's
  text with `resolve_cards` - never from memory. The Comprehensive Rules are
  revised every set and rule numbers move, so a CR number you did not look up
  in this session is wrong until proven otherwise.
- Cite the CR numbers that decide the answer, each one verified by a lookup.
- Answer the question you were asked, for the situation it names, and stop.
  Lead with the answer (yes / no / what happens), then the citations, then at
  most a few lines of why. No general tutorial.
- If the lookups do not settle it, say exactly what is unsettled rather than
  guessing.
"""


#: The MCP tool contract the `seat-player` allowlist is built from. In the
#: image the repo is `/app`, so this resolves there too.
TOOLS_CONTRACT = Path(__file__).resolve().parents[3] / "contract" / "tools.json"

SEAT_PLAYER_SPELLSLINGER = (
    "mcp__spellslinger__knowledge_search",
    "mcp__spellslinger__resolve_cards",
)

SEAT_PLAYER_DENY = (
    "mcp__mtg__pass_baton",
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "Agent",
    "Task",
    "TeamCreate",
    "TeamDelete",
)

SEAT_PLAYER_PROMPT = """You are a seat-player: one of a pair of agents playing one seat of a
Magic: the Gathering game for an orchestrator. At any moment one of you holds the
`current` role and the other holds the `future` role; the orchestrator tells you
which. It is one definition because `future` is promoted to `current` by
continuing you, with your context intact.

Start by reading /workspace/current-game.md: your deck, the opponent, the matchup
card and the seat's briefing. Look rules up with the spellslinger tools, never
from memory.

## The future role

You plan; you do not act. Use only the read-only tools in BATON_EXEMPT_TOOLS
(`battlefield_state`, `game_log`, `list_scripts`, `report_problem`) plus the
lookups and Read/Grep/Glob, and never call an acting tool: not `cast`, `decide`,
`activate`, `pass`, `pass_until`, `run_script`, `declare_plan`, `stage_script`,
nor any other that moves the game. Pre-plan the next decision as conditional DSL
plans: when-clauses for what the opponent might do, and arm lists for what should
stop a wait. Keep the plans current as the board moves - re-read the board and
rewrite them whenever it changes - so that on promotion your plan is already
fresh. Send plans to the orchestrator; do not wait to be asked.

## The current role

You hold the baton and you act. Play one wait then one committing call per
decision: a single `pass_until` or `pass`, then exactly one committing call
(`cast`, `decide`, `activate`, `play_land`, `declare_attackers`,
`declare_blockers`, `run_script`), nothing in between. Be DSL-first: put any
multi-step sequence into `run_script`. Plan ahead: call `declare_plan` during the
opponent's turn (`for_turn="next"`, with the baton - it never touches the board),
then arm your wait with `pass_until(..., arm=[...])`, copying the answer's own
`arm_call`. Armed plays never autofire: arming never runs a play, it only judges
which armed play applies; when one does, start the armed play yourself with
`run_script` as that decision's committing call. You must pass the baton on every acting call: give
the baton you were handed as the `baton` argument. When you are promoted from
the future role, revalidate the inherited plan against the fresh answer before
using it - the opponent may have acted since you wrote it; adjust or discard it.
The moment the decision commits, report `committed: <one line>` to the
orchestrator (what you did, nothing more) so the next decision can be planned.

## Refusals

Read the engine's refusal text; it says why the action was illegal, so correct
the action and retry. `BATON_REQUIRED` means you forgot the baton. `BATON_STALE`
means you were demoted: stop acting at once, report it, and carry on as the
future role.
"""


def _seat_player_tools(channel: seat_pair.Channel) -> list[str]:
    contract = json.loads(TOOLS_CONTRACT.read_text())["tools"]
    tools = [f"mcp__mtg__{t['name']}" for t in contract if t["name"] != "pass_baton"]
    tools += SEAT_PLAYER_SPELLSLINGER
    tools += ["Read", "Grep", "Glob"]
    if channel is seat_pair.Channel.TEAMS:
        tools.append("SendMessage")
    return tools


def _write_seat_player(settings: Settings, root: Path) -> None:
    text = (
        "---\n"
        f"name: {seat_pair.SEAT_PLAYER}\n"
        "description: Plays one seat of the game as the current (acting) or future "
        "(read-only planning) role, as the orchestrator directs. Continue the same "
        "agent to promote future to current.\n"
        f"tools: {', '.join(_seat_player_tools(settings.seat_agent_channel))}\n"
        f"disallowedTools: {', '.join(SEAT_PLAYER_DENY)}\n"
        f"model: {settings.seat_subagent_model}\n"
        "---\n"
        f"{SEAT_PLAYER_PROMPT}"
    )
    path = root / f"{seat_pair.SEAT_PLAYER}.md"
    path.write_text(text)
    path.chmod(0o644)


def write_agents(settings: Settings) -> None:
    """The seat's subagents, as project agents under the workspace (claude's
    cwd). Rendered from code so the tool allowlist is pinned by tests rather
    than living in a hand-edited file."""
    root = settings.workspace / ".claude" / "agents"
    root.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        f"name: {RULES_RESEARCHER}\n"
        "description: Answers one Magic rules question with verified CR citations. "
        "Launch it with run_in_background: true whenever the decision in front of "
        "you does not depend on the answer - keep playing, then read the answer "
        "when it arrives; run it in the foreground only when the decision you are "
        "making right now does depend on it. Read-only; cannot act in the game.\n"
        f"tools: {', '.join(RULES_RESEARCHER_TOOLS)}\n"
        f"disallowedTools: {', '.join(RULES_RESEARCHER_DENY)}\n"
        f"model: {settings.rules_researcher_model}\n"
        "---\n"
        f"{RULES_RESEARCHER_PROMPT}"
    )
    path = root / f"{RULES_RESEARCHER}.md"
    path.write_text(text)
    path.chmod(0o644)
    if settings.pair_active:
        _write_seat_player(settings, root)


def copy_skills(settings: Settings) -> None:
    """Later directories win, so a mounted custom skill overrides a baked one
    of the same name."""
    root = settings.workspace / ".claude" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    for source in settings.skills_dirs:
        if not source.is_dir():
            continue
        shutil.copytree(source, root, dirs_exist_ok=True)


#: Where the seat Job mounts Secret `mtg-decks-git`'s `GH_PAT` key
#: (deploy/seat-job.yaml). A file, not an env var, so the token never sits in
#: the agent's environment for a stray `env` to print.
GH_TOKEN_FILE = Path("/var/run/mtg-decks-git/GH_PAT")

#: The writable deck-guides checkout `deploy/guides-clone.sh` makes.
GUIDES_DIR = "/guides"


def _gitconfig_value(value: str) -> str:
    """A gitconfig value, double-quoted. Quoting is not optional here: an
    unquoted `;` or `#` starts a comment, and a shell helper is full of
    both."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_gitconfig(settings: Settings, token_file: Path = GH_TOKEN_FILE) -> None:
    """~/.gitconfig: a credential helper for https://github.com that reads the
    token from `token_file` each time git asks, plus `safe.directory` for the
    guides checkout. The token itself is never written - a rotated Secret is
    picked up on the next push. The helper answers only `get`, and stays
    silent when the file is absent (compose mounts no token), so git falls
    through to its own failure rather than sending an empty password."""
    quoted = shlex.quote(str(token_file))
    helper = (
        '!f() { test "$1" = get || exit 0; '
        f"test -r {quoted} || exit 0; "
        "echo username=x-access-token; "
        f'printf "password=%s\\n" "$(cat {quoted})"; '
        "}; f"
    )
    text = (
        '[credential "https://github.com"]\n'
        f"\thelper = {_gitconfig_value(helper)}\n"
        "[safe]\n"
        f"\tdirectory = {GUIDES_DIR}\n"
    )
    path = settings.home_dir / ".gitconfig"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o644)


def render(settings: Settings) -> None:
    settings.workspace.mkdir(parents=True, exist_ok=True)
    settings.home_dir.mkdir(parents=True, exist_ok=True)
    write_gitconfig(settings)
    write_claude_md(settings)
    merge_mcp(settings)
    write_settings_json(settings)
    write_claude_json(settings)
    copy_skills(settings)
    write_agents(settings)
    log.info("bootstrap rendered workspace=%s home=%s", settings.workspace, settings.home_dir)
