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
    max_thinking_tokens: int | None = None
    workspace: Path = Path("/workspace")
    home_dir: Path = Path("/root")
    permission_mode: str = "bypassPermissions"
    hook_command: str = "cc-stop-hook"
    turn_timeout: float = 1800.0
    boot_timeout: float = 60.0
    submit_delay: float = 0.4
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
            max_thinking_tokens=_int_or_none(env, "MAX_THINKING_TOKENS"),
            workspace=Path(_str(env, "WORKSPACE", "/workspace")),
            home_dir=Path(_str(env, "HOME_DIR", "/root")),
            permission_mode=_str(env, "PERMISSION_MODE", "bypassPermissions"),
            hook_command=_str(env, "HOOK_COMMAND", "cc-stop-hook"),
            turn_timeout=_float(env, "TURN_TIMEOUT_SECONDS", 1800.0),
            boot_timeout=_float(env, "BOOT_TIMEOUT_SECONDS", 60.0),
            submit_delay=_float(env, "SUBMIT_DELAY_SECONDS", 0.4),
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
