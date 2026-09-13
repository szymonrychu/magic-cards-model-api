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
    log.info("bootstrap rendered workspace=%s home=%s", settings.workspace, settings.home_dir)
