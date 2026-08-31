# Continuous MTG Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port `tatara-claude-code-wrapper` (Go) to Python inside `magic-cards-model-api`, and stand up a docker-compose where a long-lived interactive `claude` plays Magic against you through the same seat API the webUI uses, with deck guides and cross-game memory.

**Architecture:** Three containers on one docker network. `game` runs the existing FastAPI app (`/api`, `/play/` UI). `agent` runs the ported wrapper as PID 1, supervising one interactive `claude` over a PTY, which reaches the game through the `mtg` MCP server over stdio. `driver` watches for a dispatched AI seat, renders the agent's deck and guides into turn 0, pushes it to the wrapper, and pushes a reflection turn when the game ends.

**Tech Stack:** Python 3.13, FastAPI, httpx, `prometheus_client`, stdlib `pty`/`termios`/`threading`, pytest, ruff, uv, Docker Compose.

## Global Constraints

- Python `>=3.13`. Ruff `line-length = 100`, `target-version = "py313"`, lint select `["E", "F", "I", "UP", "B", "SIM"]`.
- All new code lives under `src/mtg_agent/`. The only pre-existing source file this plan modifies is `src/magic_cards_model_api/ai_seat.py` (Task 16). Do not touch `app.py`, `engine_api.py`, `affordances.py`, `decisions.py`, `seatkit.py`, `seat_cli.py`, `serialize.py`, `visibility.py`, or anything in `ui/` - another session owns those.
- Never touch `../magic-cards-model`.
- `claude` is spawned interactively under a PTY. `--print`/`-p` and `--dangerously-skip-permissions` are forbidden in the wrapper.
- The Stop hook always exits 0, on every path.
- The repo's no-mocks rule governs *rules* tests. No test in this plan constructs a game state or touches the engine; a stub `claude` script is a test double for a subprocess, not for the rules.
- Tests run with `uv run pytest`. Lint with `uv run ruff check .`.
- Commit after every task. Conventional-commit subject lines.

---

## File Structure

**New package `src/mtg_agent/`:**

| Path | Responsibility |
|---|---|
| `wrapper/config.py` | env -> typed `Settings` |
| `wrapper/transcript.py` | JSONL transcript parsing: last assistant text, usage, per-turn token sums |
| `wrapper/stop_hook.py` | the Stop-hook entry point (console script `cc-stop-hook`) |
| `wrapper/turns.py` | turn record + thread-safe store |
| `wrapper/ring.py` | PTY ring buffer with ANSI/whitespace-normalised matching |
| `wrapper/pty_proc.py` | spawn `claude` under a PTY; the process seam tests substitute |
| `wrapper/session.py` | boot navigation, submit, complete, watch, state machine |
| `wrapper/probe.py` | mid-turn probe tracker |
| `wrapper/bootstrap.py` | renders every file `claude` reads at startup |
| `wrapper/webhook.py` | retrying callback delivery |
| `wrapper/metrics.py` | `ccw_*` Prometheus collectors |
| `wrapper/api.py` | FastAPI public router + loopback internal router |
| `wrapper/__main__.py` | wiring: config -> bootstrap -> session -> two servers |
| `driver/guides.py` | decklist fingerprinting against `/guides/decks/*` |
| `driver/prompt.py` | `current-game.md` + turn-0 and reflection prompts |
| `driver/app.py` | `/dispatch`, `/turn-done`, `/status`, `/healthz` |
| `driver/__main__.py` | uvicorn entry point |

**Tests:** `tests/wrapper/test_*.py`, `tests/driver/test_*.py`, `tests/testdata/stubclaude.py`.

**Containers:** `Dockerfile`, `Dockerfile.agent`, `docker-compose.yml`, `.env.example`.

---

### Task 1: Package scaffold and config

**Files:**
- Create: `src/mtg_agent/__init__.py`, `src/mtg_agent/wrapper/__init__.py`, `src/mtg_agent/wrapper/config.py`
- Modify: `pyproject.toml`
- Test: `tests/wrapper/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings` dataclass with fields `http_addr: str`, `internal_addr: str`, `claude_path: str`, `model: str`, `workspace: Path`, `home_dir: Path`, `permission_mode: str`, `hook_command: str`, `turn_timeout: float`, `boot_timeout: float`, `submit_delay: float`, `webhook_retries: int`, `default_callback_url: str`, `shared_secret: str`, `anthropic_api_key: str`, `enable_all_mcp: bool`, `base_mcp_path: Path | None`, `mcp_overlay_dir: Path | None`, `project_claude_md_path: Path | None`, `global_claude_md_path: Path | None`, `skills_dirs: list[Path]`, `log_level: str`. Classmethod `Settings.from_env(env: Mapping[str, str]) -> Settings`. Method `claude_args(resume: bool) -> list[str]`.

- [ ] **Step 1: Add the package and deps to pyproject.toml**

In `[project] dependencies` add `"prometheus-client>=0.21"`. Add these blocks:

```toml
[project.scripts]
cc-stop-hook = "mtg_agent.wrapper.stop_hook:main"

[tool.hatch.build.targets.wheel]
packages = ["src/magic_cards_model_api", "src/mtg_agent"]
```

(The `[tool.hatch.build.targets.wheel]` block already exists with one package - replace its `packages` line.)

- [ ] **Step 2: Write the failing test**

`tests/wrapper/test_config.py`:

```python
from pathlib import Path

from mtg_agent.wrapper.config import Settings


def test_defaults_when_env_is_empty():
    s = Settings.from_env({})
    assert s.http_addr == ":8080"
    assert s.internal_addr == "127.0.0.1:8090"
    assert s.claude_path == "claude"
    assert s.workspace == Path("/workspace")
    assert s.permission_mode == "bypassPermissions"
    assert s.turn_timeout == 1800.0
    assert s.boot_timeout == 60.0
    assert s.submit_delay == 0.4
    assert s.webhook_retries == 3
    assert s.shared_secret == ""
    assert s.enable_all_mcp is True


def test_env_overrides_scalars_and_paths():
    s = Settings.from_env(
        {
            "HTTP_ADDR": ":9000",
            "MODEL": "sonnet",
            "WORKSPACE": "/tmp/ws",
            "TURN_TIMEOUT_SECONDS": "60",
            "WRAPPER_SHARED_SECRET": "hunter2",
            "MCP_OVERLAY_DIR": "/etc/wrapper/mcp.d",
            "SKILLS_DIRS": "/templates/skills:/etc/wrapper/skills",
        }
    )
    assert s.http_addr == ":9000"
    assert s.model == "sonnet"
    assert s.workspace == Path("/tmp/ws")
    assert s.turn_timeout == 60.0
    assert s.shared_secret == "hunter2"
    assert s.mcp_overlay_dir == Path("/etc/wrapper/mcp.d")
    assert s.skills_dirs == [Path("/templates/skills"), Path("/etc/wrapper/skills")]


def test_claude_args_never_carry_a_permission_or_print_flag():
    s = Settings.from_env({"MODEL": "sonnet"})
    assert s.claude_args(resume=False) == ["--model", "sonnet"]
    assert s.claude_args(resume=True) == ["--model", "sonnet", "--continue"]
    assert "-p" not in s.claude_args(resume=False)
    assert "--print" not in s.claude_args(resume=False)
    assert "--dangerously-skip-permissions" not in s.claude_args(resume=False)


def test_claude_args_omit_model_when_unset():
    assert Settings.from_env({}).claude_args(resume=False) == []
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_config.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'mtg_agent'`.

- [ ] **Step 4: Implement config.py**

```python
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
    enable_all_mcp: bool = True
    base_mcp_path: Path | None = None
    mcp_overlay_dir: Path | None = None
    project_claude_md_path: Path | None = None
    global_claude_md_path: Path | None = None
    skills_dirs: list[Path] = field(default_factory=list)
    log_level: str = "info"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        raw_skills = env.get("SKILLS_DIRS", "").strip()
        skills = [Path(p) for p in raw_skills.split(":") if p] if raw_skills else []
        return cls(
            http_addr=_str(env, "HTTP_ADDR", ":8080"),
            internal_addr=_str(env, "INTERNAL_ADDR", "127.0.0.1:8090"),
            claude_path=_str(env, "CLAUDE_PATH", "claude"),
            model=_str(env, "MODEL", ""),
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
            enable_all_mcp=_bool(env, "ENABLE_ALL_PROJECT_MCP_SERVERS", True),
            base_mcp_path=_path(env, "BASE_MCP_PATH"),
            mcp_overlay_dir=_path(env, "MCP_OVERLAY_DIR"),
            project_claude_md_path=_path(env, "PROJECT_CLAUDE_MD_PATH"),
            global_claude_md_path=_path(env, "GLOBAL_CLAUDE_MD_PATH"),
            skills_dirs=skills,
            log_level=_str(env, "LOG_LEVEL", "info"),
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
        if resume:
            args.append("--continue")
        return args
```

Also create empty `src/mtg_agent/__init__.py` and `src/mtg_agent/wrapper/__init__.py`, and an empty `tests/wrapper/__init__.py` is NOT needed (pytest rootdir config already handles `tests/`).

- [ ] **Step 5: Run the tests**

Run: `uv sync && uv run pytest tests/wrapper/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add pyproject.toml src/mtg_agent tests/wrapper
git commit -m "feat(wrapper): typed settings from env, ported from the Go config"
```

---

### Task 2: Transcript parsing

**Files:**
- Create: `src/mtg_agent/wrapper/transcript.py`
- Test: `tests/wrapper/test_transcript.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `last_assistant(path: Path) -> tuple[str, dict | None, str]` returning `(text, usage, stop_reason)`; `sum_turn_tokens(path: Path) -> list[TurnTokens]` where `TurnTokens` is a dataclass with `model: str, input: int, output: int, cache_read: int, cache_creation: int`.

- [ ] **Step 1: Write the failing test**

`tests/wrapper/test_transcript.py`:

```python
import json
from pathlib import Path

from mtg_agent.wrapper.transcript import last_assistant, sum_turn_tokens


def write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def assistant(text: str, *, model: str = "claude-sonnet", stop: str = "end_turn", **usage):
    full = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    } | usage
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": text}],
            "model": model,
            "usage": full,
            "stop_reason": stop,
        },
    }


def test_last_assistant_returns_text_usage_and_stop_reason(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [
            {"type": "system", "message": "ignored"},
            assistant("first", output_tokens=1),
            {"type": "user", "message": {"content": "next turn"}},
            assistant("second", output_tokens=7, stop="end_turn"),
        ],
    )
    text, usage, stop = last_assistant(path)
    assert text == "second"
    assert usage["output_tokens"] == 7
    assert stop == "end_turn"


def test_last_assistant_concatenates_text_blocks_and_skips_non_text(tmp_path):
    line = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "a"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "b"},
            ],
            "usage": {},
            "stop_reason": "end_turn",
        },
    }
    text, _, _ = last_assistant(write_jsonl(tmp_path / "t.jsonl", [line]))
    assert text == "ab"


def test_last_assistant_keeps_last_nonempty_text_but_latest_usage(tmp_path):
    """A trailing tool-call-only assistant line carries usage but no text."""
    tool_only = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": "Bash"}],
            "usage": {"output_tokens": 99},
            "stop_reason": "tool_use",
        },
    }
    path = write_jsonl(tmp_path / "t.jsonl", [assistant("said this"), tool_only])
    text, usage, stop = last_assistant(path)
    assert text == "said this"
    assert usage["output_tokens"] == 99
    assert stop == "tool_use"


def test_last_assistant_tolerates_corrupt_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("not json\n" + json.dumps(assistant("ok")) + "\n")
    assert last_assistant(path)[0] == "ok"


def test_sum_turn_tokens_sums_the_last_turn_only(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [
            assistant("turn one", input_tokens=100, output_tokens=10),
            {"type": "user", "message": {"content": "a typed prompt"}},
            assistant("step", input_tokens=5, output_tokens=3),
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            assistant("step", input_tokens=6, output_tokens=4, cache_read_input_tokens=20),
        ],
    )
    (totals,) = sum_turn_tokens(path)
    assert totals.model == "claude-sonnet"
    assert totals.input == 11
    assert totals.output == 7
    assert totals.cache_read == 20
    assert totals.cache_creation == 0


def test_sum_turn_tokens_groups_by_model(tmp_path):
    path = write_jsonl(
        tmp_path / "t.jsonl",
        [
            {"type": "user", "message": {"content": "go"}},
            assistant("a", model="opus", output_tokens=1),
            assistant("b", model="haiku", output_tokens=2),
        ],
    )
    by_model = {t.model: t.output for t in sum_turn_tokens(path)}
    assert by_model == {"opus": 1, "haiku": 2}


def test_sum_turn_tokens_on_missing_file_is_empty(tmp_path):
    assert sum_turn_tokens(tmp_path / "nope.jsonl") == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_transcript.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.wrapper.transcript`.

- [ ] **Step 3: Implement transcript.py**

```python
"""Reading the JSONL session transcript claude writes.

Two questions are asked of it. `last_assistant` answers "what did the agent
finally say, with what usage" - the text is only a fallback, since the Stop
hook payload carries `last_assistant_message` authoritatively.
`sum_turn_tokens` answers "what did this turn cost", summed across every
assistant message of the last turn: the single last-message usage badly
undercounts an agentic turn that made twenty tool calls.

A turn boundary is a `user` line whose `content` is a plain string. Tool
results arrive as `user` lines too, but carry an array `content`, so they do
not reset the accumulator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TurnTokens:
    model: str
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0


def _lines(path: Path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except OSError:
        return


def last_assistant(path: Path) -> tuple[str, dict | None, str]:
    """`(text, usage, stop_reason)` of the final assistant line. Text falls
    back to the last line that actually had text, because a turn often ends
    on a tool-call-only assistant line that carries usage but says nothing."""
    text = ""
    usage: dict | None = None
    stop = ""
    for line in _lines(path):
        if line.get("type") != "assistant":
            continue
        message = line.get("message") or {}
        blocks = message.get("content") or []
        joined = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        raw_usage = message.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else usage
        stop = message.get("stop_reason") or ""
        if joined:
            text = joined
    return text, usage, stop


def sum_turn_tokens(path: Path) -> list[TurnTokens]:
    """Per-model token totals for the LAST turn in the transcript."""
    by_model: dict[str, TurnTokens] = {}
    for line in _lines(path):
        kind = line.get("type")
        message = line.get("message") or {}
        if kind == "user" and isinstance(message.get("content"), str):
            by_model = {}
            continue
        if kind != "assistant":
            continue
        usage = message.get("usage") or {}
        model = message.get("model") or ""
        totals = by_model.setdefault(model, TurnTokens(model=model))
        totals.input += int(usage.get("input_tokens") or 0)
        totals.output += int(usage.get("output_tokens") or 0)
        totals.cache_read += int(usage.get("cache_read_input_tokens") or 0)
        totals.cache_creation += int(usage.get("cache_creation_input_tokens") or 0)
    return list(by_model.values())
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/wrapper/test_transcript.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add src/mtg_agent/wrapper/transcript.py tests/wrapper/test_transcript.py
git commit -m "feat(wrapper): parse the session transcript for text, usage and per-turn tokens"
```

---

### Task 3: The Stop hook

**Files:**
- Create: `src/mtg_agent/wrapper/stop_hook.py`
- Test: `tests/wrapper/test_stop_hook.py`

**Interfaces:**
- Consumes: `transcript.last_assistant`, `transcript.sum_turn_tokens` (Task 2).
- Produces: `build_result(payload: bytes, result_json_path: Path) -> dict` with keys `sessionId`, `finalText`, `transcriptPath`, `usage`, `stopReason`, `turnTokens`, `resultJson`; `post_with_retry(url: str, body: bytes, *, attempts: int = 5, backoff: float = 0.5, timeout: float = 5.0) -> None` raising `RuntimeError` when exhausted; `main() -> None` which always exits 0.

- [ ] **Step 1: Write the failing test**

`tests/wrapper/test_stop_hook.py`:

```python
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mtg_agent.wrapper import stop_hook


def payload(**overrides) -> bytes:
    base = {
        "session_id": "a70a2699",
        "transcript_path": "",
        "cwd": "/workspace",
        "hook_event_name": "Stop",
        "last_assistant_message": "PONG",
    }
    return json.dumps(base | overrides).encode()


def test_final_text_prefers_the_payload_over_the_transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "from transcript"}],
                    "usage": {"output_tokens": 3},
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n"
    )
    result = stop_hook.build_result(payload(transcript_path=str(path)), tmp_path / "absent.json")
    assert result["finalText"] == "PONG"
    assert result["usage"]["output_tokens"] == 3
    assert result["stopReason"] == "end_turn"
    assert result["sessionId"] == "a70a2699"
    assert result["transcriptPath"] == str(path)


def test_final_text_falls_back_to_the_transcript_when_the_payload_is_empty(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "from transcript"}],
                    "usage": {},
                    "stop_reason": "end_turn",
                },
            }
        )
        + "\n"
    )
    result = stop_hook.build_result(
        payload(last_assistant_message="", transcript_path=str(path)), tmp_path / "x.json"
    )
    assert result["finalText"] == "from transcript"


def test_result_json_is_folded_in_when_valid(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text('{"total_cost_usd": 0.42}')
    result = stop_hook.build_result(payload(), result_path)
    assert result["resultJson"] == {"total_cost_usd": 0.42}


def test_invalid_result_json_is_ignored(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text("{not json")
    assert stop_hook.build_result(payload(), result_path)["resultJson"] is None


def test_a_missing_transcript_still_produces_a_result(tmp_path):
    result = stop_hook.build_result(
        payload(transcript_path=str(tmp_path / "gone.jsonl")), tmp_path / "x.json"
    )
    assert result["finalText"] == "PONG"
    assert result["turnTokens"] == []


def test_corrupt_payload_raises(tmp_path):
    with pytest.raises(ValueError):
        stop_hook.build_result(b"{not json", tmp_path / "x.json")


def test_post_with_retry_gives_up_after_the_attempt_budget(monkeypatch):
    calls = []

    def always_fail(url, content, headers, timeout):
        calls.append(url)
        raise OSError("connection refused")

    monkeypatch.setattr(stop_hook, "_post_once", always_fail)
    monkeypatch.setattr(stop_hook.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError):
        stop_hook.post_with_retry("http://x/internal", b"{}", attempts=5, backoff=0)
    assert len(calls) == 5


def test_post_with_retry_stops_at_the_first_success(monkeypatch):
    calls = []

    def fail_then_succeed(url, content, headers, timeout):
        calls.append(url)
        if len(calls) < 3:
            raise OSError("nope")

    monkeypatch.setattr(stop_hook, "_post_once", fail_then_succeed)
    monkeypatch.setattr(stop_hook.time, "sleep", lambda _: None)
    stop_hook.post_with_retry("http://x/internal", b"{}", attempts=5, backoff=0)
    assert len(calls) == 3


def test_main_exits_zero_even_when_everything_fails(tmp_path):
    """A Stop hook must never block or alter claude. Run it for real as a
    subprocess with a corrupt payload and an unreachable endpoint."""
    proc = subprocess.run(
        [sys.executable, "-m", "mtg_agent.wrapper.stop_hook"],
        input=b"{not json at all",
        env={
            "PATH": "/usr/bin:/bin",
            "CCW_INTERNAL_URL": "http://127.0.0.1:1/internal/turn-complete",
            "CCW_RESULT_JSON": str(tmp_path / "none.json"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
        },
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_stop_hook.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.wrapper.stop_hook`.

- [ ] **Step 3: Implement stop_hook.py**

```python
"""The Stop hook claude runs at end-of-turn.

It reads the hook payload on stdin, builds the turn result, and POSTs it to
the wrapper's loopback-only internal endpoint. `last_assistant_message` from
the payload is authoritative for the final text; the transcript is read for
usage, stop_reason, per-turn token totals, and as a text fallback.

It ALWAYS exits 0. A hook that fails loudly blocks or alters claude, which is
never worth it - a lost result is recoverable, a wedged agent is not.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from .transcript import last_assistant, sum_turn_tokens

MAX_POST_ATTEMPTS = 5
POST_PER_ATTEMPT = 5.0
POST_BACKOFF = 0.5

log = logging.getLogger("cc-stop-hook")


def build_result(payload: bytes, result_json_path: Path) -> dict:
    parsed = json.loads(payload)
    transcript_path = parsed.get("transcript_path") or ""
    result = {
        "sessionId": parsed.get("session_id") or "",
        "finalText": parsed.get("last_assistant_message") or "",
        "transcriptPath": transcript_path,
        "usage": None,
        "stopReason": "",
        "turnTokens": [],
        "resultJson": None,
    }
    if transcript_path:
        path = Path(transcript_path)
        text, usage, stop = last_assistant(path)
        result["usage"] = usage
        result["stopReason"] = stop
        if not result["finalText"]:
            result["finalText"] = text
        result["turnTokens"] = [vars(t) for t in sum_turn_tokens(path)]
    try:
        result["resultJson"] = json.loads(Path(result_json_path).read_text())
    except (OSError, ValueError):
        pass
    return result


def _post_once(url: str, content: bytes, headers: dict, timeout: float) -> None:
    response = httpx.post(url, content=content, headers=headers, timeout=timeout)
    response.raise_for_status()


def post_with_retry(
    url: str,
    body: bytes,
    *,
    attempts: int = MAX_POST_ATTEMPTS,
    backoff: float = POST_BACKOFF,
    timeout: float = POST_PER_ATTEMPT,
) -> None:
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(backoff)
        try:
            _post_once(url, body, {"Content-Type": "application/json"}, timeout)
            return
        except Exception as exc:  # noqa: BLE001 - every failure mode retries alike
            last = exc
    raise RuntimeError(f"post to {url} failed after {attempts} attempts: {last}")


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    try:
        payload = sys.stdin.buffer.read()
        url = os.environ.get("CCW_INTERNAL_URL", "http://127.0.0.1:8090/internal/turn-complete")
        result_path = Path(os.environ.get("CCW_RESULT_JSON", "/workspace/result.json"))
        result = build_result(payload, result_path)
        post_with_retry(url, json.dumps(result).encode())
        log.info("hook post succeeded session_id=%s", result["sessionId"])
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        log.error("cc-stop-hook failed: %s", exc)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/wrapper/test_stop_hook.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add src/mtg_agent/wrapper/stop_hook.py tests/wrapper/test_stop_hook.py
git commit -m "feat(wrapper): the Stop hook that reports a finished turn, always exiting 0"
```

---

### Task 4: Turn record and store

**Files:**
- Create: `src/mtg_agent/wrapper/turns.py`
- Test: `tests/wrapper/test_turns.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `TurnState` str-enum with `RUNNING="running"`, `COMPLETE="complete"`, `FAILED="failed"`. `Record` dataclass: `id, state, text, callback_url, started_at, last_activity_at, completed_at, final_text, result_json, usage, turn_tokens, stop_reason, error`. `Store` with `create(id, text, callback_url, now) -> Record`, `get(id) -> Record | None`, `list() -> list[Record]` in creation order, `complete(id, *, final_text, result_json, usage, turn_tokens, stop_reason, now) -> Record` raising `KeyError`, `fail(id, reason, now) -> Record` raising `KeyError`, `touch(id, now) -> bool`, `counts() -> tuple[int, int]` returning `(completed, finished)`.

- [ ] **Step 1: Write the failing test**

`tests/wrapper/test_turns.py`:

```python
import threading

import pytest

from mtg_agent.wrapper.turns import Record, Store, TurnState


def test_create_then_complete():
    store = Store()
    record = store.create("turn-1", "hello", "https://cb.example/x", 1000.0)
    assert record.state is TurnState.RUNNING
    assert store.get("turn-1").callback_url == "https://cb.example/x"

    done = store.complete(
        "turn-1",
        final_text="PONG",
        result_json={"status": "success"},
        usage={"output_tokens": 3},
        turn_tokens=[],
        stop_reason="end_turn",
        now=1005.0,
    )
    assert done.state is TurnState.COMPLETE
    assert done.final_text == "PONG"
    assert done.stop_reason == "end_turn"
    assert done.completed_at == 1005.0


def test_complete_or_fail_on_an_unknown_id_raises():
    store = Store()
    with pytest.raises(KeyError):
        store.complete(
            "missing",
            final_text="",
            result_json=None,
            usage=None,
            turn_tokens=[],
            stop_reason="",
            now=1.0,
        )
    with pytest.raises(KeyError):
        store.fail("missing", "boom", 1.0)


def test_list_is_in_creation_order_and_carries_state():
    store = Store()
    store.create("a", "1", "", 1.0)
    store.create("b", "2", "", 2.0)
    store.fail("a", "boom", 3.0)
    listed = store.list()
    assert [r.id for r in listed] == ["a", "b"]
    assert listed[0].state is TurnState.FAILED
    assert listed[0].error == "boom"


def test_touch_moves_last_activity_but_not_started_at():
    store = Store()
    record = store.create("turn-1", "hello", "", 1000.0)
    assert record.last_activity_at == 1000.0
    assert store.touch("turn-1", 1007.0) is True
    got = store.get("turn-1")
    assert got.last_activity_at == 1007.0
    assert got.started_at == 1000.0


def test_touch_is_a_noop_on_unknown_or_terminal_turns():
    store = Store()
    assert store.touch("missing", 1.0) is False

    store.create("done", "x", "", 1000.0)
    store.complete(
        "done",
        final_text="ok",
        result_json=None,
        usage=None,
        turn_tokens=[],
        stop_reason="end_turn",
        now=1005.0,
    )
    assert store.touch("done", 2000.0) is False
    assert store.get("done").last_activity_at == 1000.0


def test_counts_separate_successful_turns_from_all_finished_turns():
    store = Store()
    store.create("a", "", "", 1.0)
    store.create("b", "", "", 2.0)
    store.create("c", "", "", 3.0)
    store.complete(
        "a", final_text="", result_json=None, usage=None, turn_tokens=[], stop_reason="", now=4.0
    )
    store.fail("b", "boom", 5.0)
    assert store.counts() == (1, 2)


def test_concurrent_creates_all_land():
    store = Store()

    def make(index: int) -> None:
        store.create(f"turn-{index}", "", "", float(index))

    threads = [threading.Thread(target=make, args=(i,)) for i in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store.list()) == 50
    assert len({r.id for r in store.list()}) == 50


def test_record_summary_omits_the_heavy_fields():
    record = Record(id="t", state=TurnState.RUNNING, text="hi", callback_url="", started_at=1.0)
    summary = record.summary()
    assert set(summary) == {"turnId", "state", "startedAt", "completedAt", "lastActivityAt"}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_turns.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.wrapper.turns`.

- [ ] **Step 3: Implement turns.py**

```python
"""One turn's record, and the in-memory store of every turn this process ran.

Process-lifetime state: a restart forgets it, exactly like the game server's
own session store. The transcript on disk is what survives.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TurnState(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class Record:
    id: str
    state: TurnState
    text: str
    callback_url: str
    started_at: float
    last_activity_at: float = 0.0
    completed_at: float | None = None
    final_text: str = ""
    result_json: Any = None
    usage: dict | None = None
    turn_tokens: list[dict] = field(default_factory=list)
    stop_reason: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if not self.last_activity_at:
            self.last_activity_at = self.started_at

    def summary(self) -> dict:
        return {
            "turnId": self.id,
            "state": str(self.state),
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "lastActivityAt": self.last_activity_at,
        }

    def full(self) -> dict:
        return self.summary() | {
            "text": self.text,
            "finalText": self.final_text,
            "resultJson": self.result_json,
            "usage": self.usage,
            "turnTokens": self.turn_tokens,
            "stopReason": self.stop_reason,
            "error": self.error,
            "callbackUrl": self.callback_url,
        }


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, Record] = {}
        self._order: list[str] = []

    def create(self, id: str, text: str, callback_url: str, now: float) -> Record:
        record = Record(
            id=id,
            state=TurnState.RUNNING,
            text=text,
            callback_url=callback_url,
            started_at=now,
        )
        with self._lock:
            self._by_id[id] = record
            self._order.append(id)
        return record

    def get(self, id: str) -> Record | None:
        with self._lock:
            return self._by_id.get(id)

    def list(self) -> list[Record]:
        with self._lock:
            return [self._by_id[i] for i in self._order]

    def _require(self, id: str) -> Record:
        record = self._by_id.get(id)
        if record is None:
            raise KeyError(f"turn: not found: {id}")
        return record

    def complete(
        self,
        id: str,
        *,
        final_text: str,
        result_json: Any,
        usage: dict | None,
        turn_tokens: list[dict],
        stop_reason: str,
        now: float,
    ) -> Record:
        with self._lock:
            record = self._require(id)
            record.state = TurnState.COMPLETE
            record.final_text = final_text
            record.result_json = result_json
            record.usage = usage
            record.turn_tokens = turn_tokens
            record.stop_reason = stop_reason
            record.completed_at = now
            return record

    def fail(self, id: str, reason: str, now: float) -> Record:
        with self._lock:
            record = self._require(id)
            record.state = TurnState.FAILED
            record.error = reason
            record.completed_at = now
            return record

    def touch(self, id: str, now: float) -> bool:
        with self._lock:
            record = self._by_id.get(id)
            if record is None or record.state is not TurnState.RUNNING:
                return False
            record.last_activity_at = now
            return True

    def counts(self) -> tuple[int, int]:
        """`(completed, finished)` - successful turns, and all terminal ones."""
        with self._lock:
            records = list(self._by_id.values())
        completed = sum(1 for r in records if r.state is TurnState.COMPLETE)
        finished = sum(1 for r in records if r.state is not TurnState.RUNNING)
        return completed, finished
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/wrapper/test_turns.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add src/mtg_agent/wrapper/turns.py tests/wrapper/test_turns.py
git commit -m "feat(wrapper): thread-safe turn records and store"
```

---

### Task 5: The PTY ring buffer

**Files:**
- Create: `src/mtg_agent/wrapper/ring.py`
- Test: `tests/wrapper/test_ring.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RING_CAP = 64 * 1024`; `strip_ansi(text: str) -> str`; `strip_ws(text: str) -> str`; `Ring` with `write(data: bytes) -> int`, `reset() -> None`, `written() -> int`, `contains(needle: str) -> bool`, `tail(n: int) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/wrapper/test_ring.py`:

```python
from mtg_agent.wrapper.ring import RING_CAP, Ring, strip_ansi, strip_ws


def test_strip_ansi_removes_csi_osc_and_stray_escapes():
    assert strip_ansi("\x1b[1;32mhello\x1b[0m") == "hello"
    assert strip_ansi("\x1b]0;title\x07body") == "body"
    assert strip_ansi("\x1b=alt\x1b>") == "alt"


def test_strip_ansi_keeps_newlines_and_tabs_but_drops_other_controls():
    assert strip_ansi("a\nb\tc\x00d\x07e") == "a\nb\tcde"


def test_contains_ignores_ansi_and_whitespace():
    """The TUI lays words out with cursor moves, so on-screen text carries no
    literal spaces once the escapes are stripped."""
    ring = Ring()
    ring.write(b"\x1b[2J\x1b[HBypass\x1b[3C Permissions\r\n   mode\x1b[0m")
    assert ring.contains("Bypass Permissions mode")
    assert not ring.contains("Trust this folder")


def test_written_is_monotonic_across_reset():
    ring = Ring()
    ring.write(b"abc")
    ring.reset()
    ring.write(b"de")
    assert ring.written() == 5
    assert ring.tail(100) == "de"


def test_reset_clears_the_matchable_buffer():
    ring = Ring()
    ring.write(b"Bypass Permissions mode")
    assert ring.contains("Bypass Permissions mode")
    ring.reset()
    assert not ring.contains("Bypass Permissions mode")


def test_buffer_is_capped_but_keeps_the_newest_bytes():
    ring = Ring()
    ring.write(b"x" * (RING_CAP + 100))
    ring.write(b"NEEDLE")
    assert ring.contains("NEEDLE")
    assert len(ring.tail(RING_CAP * 2)) <= RING_CAP


def test_tail_returns_at_most_n_de_ansied_bytes():
    ring = Ring()
    ring.write(b"\x1b[31mabcdefghij")
    assert ring.tail(4) == "ghij"


def test_undecodable_bytes_do_not_raise():
    ring = Ring()
    ring.write(b"\xff\xfe ok")
    assert "ok" in ring.tail(100)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_ring.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.wrapper.ring`.

- [ ] **Step 3: Implement ring.py**

```python
"""The last 64 KiB of PTY output.

This is the only window into the interactive TUI, and it is deliberately a
narrow one: it detects boot dialogs and provides a tail for debug logging.
Turn results NEVER come from here - they come from the Stop hook and the
transcript. Parsing a terminal for results is how you build something that
breaks on the next release.
"""

from __future__ import annotations

import re
import threading

RING_CAP = 64 * 1024

_CSI = re.compile(r"\x1b\[[0-9;?:]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_OTHER = re.compile(r"\x1b[@-Z\\-_=>]")


def strip_ansi(text: str) -> str:
    text = _CSI.sub("", text)
    text = _OSC.sub("", text)
    text = _OTHER.sub("", text)
    return "".join(c for c in text if not (c == "\x1b" or (c < "\x20" and c not in "\n\t")))


def strip_ws(text: str) -> str:
    return "".join(c for c in text if c not in " \t\n\r")


class Ring:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._total = 0

    def write(self, data: bytes) -> int:
        with self._lock:
            self._buf.extend(data)
            if len(self._buf) > RING_CAP:
                del self._buf[: len(self._buf) - RING_CAP]
            self._total += len(data)
        return len(data)

    def reset(self) -> None:
        """Clear the buffered output so dialog detection starts fresh, while
        leaving `written` monotonic. Called once per relaunch: a freshly
        booted claude must navigate against its own output, not the dead
        process's stale dialog text."""
        with self._lock:
            self._buf.clear()

    def written(self) -> int:
        with self._lock:
            return self._total

    def _text(self) -> str:
        with self._lock:
            return self._buf.decode("utf-8", "replace")

    def contains(self, needle: str) -> bool:
        return strip_ws(needle) in strip_ws(strip_ansi(self._text()))

    def tail(self, n: int) -> str:
        return strip_ansi(self._text())[-n:]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/wrapper/test_ring.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add src/mtg_agent/wrapper/ring.py tests/wrapper/test_ring.py
git commit -m "feat(wrapper): PTY ring buffer with ANSI- and whitespace-insensitive matching"
```

---

### Task 6: Spawning claude under a PTY

**Files:**
- Create: `src/mtg_agent/wrapper/pty_proc.py`
- Test: `tests/wrapper/test_pty_proc.py`

**Interfaces:**
- Consumes: `Settings` (Task 1).
- Produces: `ClaudeProcess` Protocol with `read(n: int) -> bytes`, `write(data: bytes) -> int`, `wait() -> int`, `close() -> None`, `pid: int`. `PtyProcess` implementing it. `spawn_claude(settings: Settings, resume: bool) -> PtyProcess`. `FakeProcess` is NOT provided here - tests define their own.

- [ ] **Step 1: Write the failing test**

`tests/wrapper/test_pty_proc.py`:

```python
import os
import sys
import time

from mtg_agent.wrapper.config import Settings
from mtg_agent.wrapper.pty_proc import spawn_claude


def read_until(proc, needle: bytes, timeout: float = 20.0) -> bytes:
    seen = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = proc.read(4096)
        except OSError:
            break
        if not chunk:
            break
        seen += chunk
        if needle in seen:
            return seen
    return seen


def test_the_child_gets_a_real_tty_and_the_configured_size(tmp_path):
    """A pipe would make claude render non-interactively. The child must see
    a tty on stdin and the 50x200 winsize the wrapper sets."""
    script = tmp_path / "probe.py"
    script.write_text(
        "import os, sys\n"
        "size = os.get_terminal_size(0)\n"
        "print('TTY', os.isatty(0), size.lines, size.columns, flush=True)\n"
    )
    settings = Settings.from_env(
        {"CLAUDE_PATH": sys.executable, "WORKSPACE": str(tmp_path), "MODEL": ""}
    )
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    try:
        assert b"TTY True 50 200" in read_until(proc, b"TTY")
    finally:
        proc.close()
        proc.wait()


def test_writes_reach_the_child_and_wait_returns_its_exit_code(tmp_path):
    script = tmp_path / "echo.py"
    script.write_text(
        "import sys\n"
        "line = sys.stdin.readline().strip()\n"
        "print('GOT:' + line, flush=True)\n"
        "sys.exit(3)\n"
    )
    settings = Settings.from_env({"CLAUDE_PATH": sys.executable, "WORKSPACE": str(tmp_path)})
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    try:
        proc.write(b"hello\r")
        assert b"GOT:hello" in read_until(proc, b"GOT:")
        assert proc.wait() == 3
    finally:
        proc.close()


def test_the_child_runs_in_the_workspace_directory(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    script = tmp_path / "cwd.py"
    script.write_text("import os; print('CWD', os.getcwd(), flush=True)\n")
    settings = Settings.from_env({"CLAUDE_PATH": sys.executable, "WORKSPACE": str(workspace)})
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    try:
        assert os.path.realpath(str(workspace)).encode() in read_until(proc, b"CWD")
    finally:
        proc.close()
        proc.wait()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/wrapper/test_pty_proc.py -v`
Expected: FAIL, `ModuleNotFoundError: mtg_agent.wrapper.pty_proc`.

- [ ] **Step 3: Implement pty_proc.py**

```python
"""Spawn interactive `claude` with a real controlling terminal.

`pty.fork()` rather than a pipe or a bare `openpty`: the child must be a
session leader whose controlling terminal is the pty slave, or the TUI does
not behave like a TUI. This is the Python equivalent of the Go original's
creack/pty `Start`.

The wrapper never runs `claude -p`. Print mode assembles a different system
prompt and diverges on skills, hooks and permissions; the whole point of this
component is that the agent runs in the harness a human gets.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import signal
import struct
import termios
from typing import Protocol

from .config import Settings

ROWS = 50
COLS = 200


class ClaudeProcess(Protocol):
    """The seam the session supervises. Tests substitute a fake."""

    pid: int

    def read(self, n: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def wait(self) -> int: ...
    def close(self) -> None: ...


class PtyProcess:
    def __init__(self, pid: int, master_fd: int) -> None:
        self.pid = pid
        self._fd = master_fd
        self._status: int | None = None

    def read(self, n: int) -> bytes:
        try:
            return os.read(self._fd, n)
        except OSError as exc:
            if exc.errno == errno.EIO:  # the slave side closed: normal EOF
                return b""
            raise

    def write(self, data: bytes) -> int:
        return os.write(self._fd, data)

    def wait(self) -> int:
        if self._status is None:
            _, status = os.waitpid(self.pid, 0)
            self._status = os.waitstatus_to_exitcode(status)
        return self._status

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass


def _set_winsize(fd: int) -> None:
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    except OSError:
        pass


def spawn_claude(
    settings: Settings, resume: bool, *, extra_args: list[str] | None = None
) -> PtyProcess:
    """Fork `claude` under a pty and return the master side.

    `extra_args` exists for tests, which spawn a stub interpreter script
    rather than the real binary; production callers never pass it.
    """
    argv = [settings.claude_path, *(extra_args or []), *settings.claude_args(resume)]
    env = dict(os.environ)
    env.setdefault("HOME", str(settings.home_dir))
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key

    pid, master_fd = pty.fork()
    if pid == 0:  # child
        try:
            os.chdir(settings.workspace)
            os.execvpe(argv[0], argv, env)
        except Exception:  # noqa: BLE001 - the child must never return
            os._exit(127)
    _set_winsize(master_fd)
    return PtyProcess(pid, master_fd)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/wrapper/test_pty_proc.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
uv run ruff format src/mtg_agent tests/wrapper && uv run ruff check src/mtg_agent tests/wrapper
git add src/mtg_agent/wrapper/pty_proc.py tests/wrapper/test_pty_proc.py
git commit -m "feat(wrapper): spawn interactive claude under a real controlling pty"
```

---

Tasks 7-12 (session manager, probe/interrupt, bootstrap, metrics/webhook, HTTP
API, integration test) are in `2026-08-31-continuous-mtg-agent-part-2.md`.
Tasks 13-17 (the driver, the `ai_seat.py` mode, containers and compose) are in
`2026-08-31-continuous-mtg-agent-part-3.md`.

Note on ordering: Task 7's `session.py` imports `Metrics` from Task 10's
`metrics.py`. Do Task 10 before Task 7, or stub `metrics.py` when you reach
Task 7 - Task 10's implementation has no dependency in the other direction.
