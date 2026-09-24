"""Env to a typed Settings.

Scalars arrive as UPPER_SNAKE env vars; anything list-, map- or
multiline-shaped is a mounted file whose *path* arrives as env. Ported from
tatara-claude-code-wrapper's cmd/wrapper/config.go.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


def _str(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key, "").strip()
    return value or default


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    return int(_float(env, key, float(default)))


def _int_or_none(env: Mapping[str, str], key: str) -> int | None:
    raw = env.get(key, "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _path(env: Mapping[str, str], key: str) -> Path | None:
    raw = env.get(key, "").strip()
    return Path(raw) if raw else None


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    http_addr: str = ":8080"
    internal_addr: str = "127.0.0.1:8090"
    claude_path: str = "claude"
    model: str = ""
    effort: str = ""
    #: The model the seat's background `rules-researcher` subagent runs on
    #: (`bootstrap.write_agents`). Its own setting, cheaper than the seat's:
    #: a rules lookup is one question and a few searches, not a game.
    rules_researcher_model: str = "sonnet"
    max_thinking_tokens: int | None = None
    workspace: Path = Path("/workspace")
    home_dir: Path = Path("/root")
    #: The writable deck-guides checkout `deploy/guides-clone.sh` clones and
    #: both deployment targets mount at `/guides`. The same directory as
    #: `bootstrap.GUIDES_DIR` (which `~/.gitconfig`'s `safe.directory` names)
    #: and as `playscripts.gitwork.GUIDES_ROOT` (every `mcp-seat` guides
    #: command's own default `--root`) - spelled here rather than imported
    #: from either, because `bootstrap` imports THIS module and the
    #: playscripts package pulls the whole game app in with it. A test pins
    #: that the three agree rather than leaving the duplication unguarded.
    guides_dir: Path = Path("/guides")
    permission_mode: str = "bypassPermissions"
    hook_command: str = "cc-stop-hook"
    turn_timeout: float = 1800.0
    #: How long the post-game AI review's one-shot `claude --print` call
    #: (`POST /v1/review`, `oneshot.py`) may run before it is killed and
    #: reported as a timeout - separate from `turn_timeout`, which bounds a
    #: turn of the supervised *interactive* session, not this independent,
    #: non-PTY subprocess.
    #:
    #: **3600s (DR3, up from 1200), re-justified again for per-decision DSL
    #: submission.** 600 was the bound on a tool-less call: one model round
    #: trip, nothing else. 1200 covered a review that reads and edits a
    #: deck's guide and takes its changes to mtg-decks through `mcp-seat`:
    #: the FIRST thing `plays admit` / `guides propose` do is wait for the
    #: `/guides` checkout (`playscripts.gitwork.checkout_lock`), bounded at
    #: `CHECKOUT_LOCK_TIMEOUT` = 300s, because the handover turn is working
    #: the same clone concurrently (defect D-1), plus a model call, a fetch,
    #: a merge or rebase, the gates and up to three push attempts.
    #:
    #: DR3 adds a `plays check`/`plays admit` pair PER DECISION the seat
    #: made this game (refine or draft, then land), a second `plays check`/
    #: `plays admit` pair per declined offer (the corner-case play), and a
    #: `guides propose` per properly generalized decision - a game with a
    #: few dozen decisions can now run several times as many `mcp-seat`
    #: calls as the flat review 1200s was sized for, each one still capable
    #: of hitting `CHECKOUT_LOCK_TIMEOUT` behind the handover's own
    #: concurrent write. 3600 is not a re-derivation from first principles,
    #: it is the same "1200 was one lock wait plus one round of work" budget
    #: given three rounds of work instead of one.
    #:
    #: Every timeout stacked on top of this one must outlast it -
    #: `driver.app.REVIEW_HTTP_TIMEOUT` (the driver's own client timeout for
    #: `POST /v1/review`) and `RETIRE_REVIEW_JOIN_TIMEOUT` over that, both
    #: pinned by a test against this number.
    review_timeout: float = 3600.0
    boot_timeout: float = 60.0
    submit_delay: float = 0.4
    #: How long an interrupted turn gets to end on its own before the wrapper
    #: ends it (`Manager.escalate_interrupt`): claude runs no Stop hook for an
    #: interrupted turn, so without this the turn slot is never freed.
    interrupt_settle: float = 20.0
    webhook_retries: int = 3
    default_callback_url: str = ""
    shared_secret: str = ""
    anthropic_api_key: str = ""
    claude_code_oauth_token: str = ""
    """A token from `claude setup-token`, which is NOT an API key.

    The two authenticate differently and the CLI reads them from different
    variables. Putting a setup-token in `ANTHROPIC_API_KEY` sends it as an
    API key and the API answers 401 - observed live in this stack, where it
    presented as an AI seat that booted fine and then failed every turn.
    """
    enable_all_mcp: bool = True
    base_mcp_path: Path | None = None
    mcp_overlay_dir: Path | None = None
    project_claude_md_path: Path | None = None
    global_claude_md_path: Path | None = None
    skills_dirs: list[Path] = field(default_factory=list)
    log_level: str = "info"
    #: How much of the agent's own activity reaches stdout: off, turns,
    #: messages, tools. See `activity.py`.
    agent_log_level: str = "tools"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        raw_skills = env.get("SKILLS_DIRS", "").strip()
        skills = [Path(p) for p in raw_skills.split(":") if p] if raw_skills else []
        return cls(
            http_addr=_str(env, "HTTP_ADDR", ":8080"),
            internal_addr=_str(env, "INTERNAL_ADDR", "127.0.0.1:8090"),
            claude_path=_str(env, "CLAUDE_PATH", "claude"),
            model=_str(env, "MODEL", ""),
            effort=_str(env, "EFFORT", ""),
            rules_researcher_model=_str(env, "RULES_RESEARCHER_MODEL", "sonnet"),
            max_thinking_tokens=_int_or_none(env, "MAX_THINKING_TOKENS"),
            workspace=Path(_str(env, "WORKSPACE", "/workspace")),
            home_dir=Path(_str(env, "HOME_DIR", "/root")),
            # GUIDES_ROOT, the same name the driver container reads it under
            # (deploy/seat-job.yaml); unset in the agent container, where the
            # mount path is fixed.
            guides_dir=Path(_str(env, "GUIDES_ROOT", "/guides")),
            permission_mode=_str(env, "PERMISSION_MODE", "bypassPermissions"),
            hook_command=_str(env, "HOOK_COMMAND", "cc-stop-hook"),
            turn_timeout=_float(env, "TURN_TIMEOUT_SECONDS", 1800.0),
            review_timeout=_float(env, "REVIEW_TIMEOUT_SECONDS", 3600.0),
            boot_timeout=_float(env, "BOOT_TIMEOUT_SECONDS", 60.0),
            submit_delay=_float(env, "SUBMIT_DELAY_SECONDS", 0.4),
            interrupt_settle=_float(env, "INTERRUPT_SETTLE_SECONDS", 20.0),
            webhook_retries=_int(env, "WEBHOOK_RETRIES", 3),
            default_callback_url=_str(env, "DEFAULT_CALLBACK_URL", ""),
            shared_secret=_str(env, "WRAPPER_SHARED_SECRET", ""),
            anthropic_api_key=_str(env, "ANTHROPIC_API_KEY", ""),
            claude_code_oauth_token=_str(env, "CLAUDE_CODE_OAUTH_TOKEN", ""),
            enable_all_mcp=_bool(env, "ENABLE_ALL_PROJECT_MCP_SERVERS", True),
            base_mcp_path=_path(env, "BASE_MCP_PATH"),
            mcp_overlay_dir=_path(env, "MCP_OVERLAY_DIR"),
            project_claude_md_path=_path(env, "PROJECT_CLAUDE_MD_PATH"),
            global_claude_md_path=_path(env, "GLOBAL_CLAUDE_MD_PATH"),
            skills_dirs=skills,
            log_level=_str(env, "LOG_LEVEL", "info"),
            agent_log_level=_str(env, "AGENT_LOG_LEVEL", "tools"),
        )

    def claude_args(self, resume: bool) -> list[str]:
        """Interactive launch flags. NO permission flag: bypass is configured
        through settings.json's defaultMode instead, and the bypass warning
        dialog appears regardless (session.boot_wait accepts it). MCP comes
        from the workspace .mcp.json plus enableAllProjectMcpServers, so no
        --mcp-config either."""
        args: list[str] = []
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        if resume:
            args.append("--continue")
        return args
