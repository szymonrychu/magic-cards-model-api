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


def _credential_probe(tmp_path):
    """A stub 'claude' that prints which credential variables it can see."""
    script = tmp_path / "creds.py"
    script.write_text(
        "import os, sys\n"
        "sys.stdout.write('KEY=%s TOKEN=%s\\n' % ("
        "os.environ.get('ANTHROPIC_API_KEY', '-'),"
        "os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '-')))\n"
        "sys.stdout.flush()\n"
    )
    return script


def test_a_setup_token_is_passed_as_a_token_and_never_as_an_api_key(tmp_path, monkeypatch):
    """`claude setup-token` produces an OAuth token, not an API key.

    They authenticate differently and the CLI reads them from different
    variables, so a token placed in ANTHROPIC_API_KEY is sent as an API key
    and the API answers 401. That was seen live in this stack: an AI seat that
    booted fine and then failed every turn with `API Error: 401`.

    An ANTHROPIC_API_KEY inherited from the environment must not survive
    either - it takes precedence over the token, so leaving it set makes a
    valid token fail.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-inherited-and-stale")
    script = _credential_probe(tmp_path)
    settings = Settings.from_env(
        {
            "CLAUDE_PATH": sys.executable,
            "WORKSPACE": str(tmp_path),
            "HOME_DIR": str(tmp_path),
            "MODEL": "",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-from-setup-token",
        }
    )
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    try:
        seen = read_until(proc, b"KEY=").decode(errors="replace")
    finally:
        proc.close()
        proc.wait()

    assert "TOKEN=oauth-token-from-setup-token" in seen
    assert "KEY=-" in seen, "the stale inherited API key must not shadow the token"


def test_an_api_key_still_works_on_its_own(tmp_path, monkeypatch):
    """The other credential, unchanged - and the token variable cleared for
    the same shadowing reason in reverse."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale-token")
    script = _credential_probe(tmp_path)
    settings = Settings.from_env(
        {
            "CLAUDE_PATH": sys.executable,
            "WORKSPACE": str(tmp_path),
            "HOME_DIR": str(tmp_path),
            "MODEL": "",
            "ANTHROPIC_API_KEY": "sk-ant-real-key",
        }
    )
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    try:
        seen = read_until(proc, b"KEY=").decode(errors="replace")
    finally:
        proc.close()
        proc.wait()

    assert "KEY=sk-ant-real-key" in seen
    assert "TOKEN=-" in seen


def _thinking_probe(tmp_path):
    """A stub 'claude' that prints MAX_THINKING_TOKENS as it sees it."""
    script = tmp_path / "thinking.py"
    script.write_text(
        "import os, sys\n"
        "sys.stdout.write('MTT=%s\\n' % os.environ.get('MAX_THINKING_TOKENS', '-'))\n"
        "sys.stdout.flush()\n"
    )
    return script


def test_max_thinking_tokens_reaches_the_child_env(tmp_path):
    """`Settings.max_thinking_tokens` has no CLI flag - it is carried only
    through this env var, and `claude_args` never emits it."""
    script = _thinking_probe(tmp_path)
    settings = Settings.from_env(
        {
            "CLAUDE_PATH": sys.executable,
            "WORKSPACE": str(tmp_path),
            "HOME_DIR": str(tmp_path),
            "MODEL": "",
            "MAX_THINKING_TOKENS": "12000",
        }
    )
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    try:
        seen = read_until(proc, b"MTT=").decode(errors="replace")
    finally:
        proc.close()
        proc.wait()

    assert "MTT=12000" in seen


def test_max_thinking_tokens_unset_does_not_inherit_a_stale_boot_value(tmp_path, monkeypatch):
    """A restart with a cleared budget (`max_thinking_tokens=None`) must not
    inherit whatever this container originally booted with - `spawn_claude`
    builds its env from `os.environ`, which still carries the old value."""
    monkeypatch.setenv("MAX_THINKING_TOKENS", "99999")
    script = _thinking_probe(tmp_path)
    settings = Settings.from_env(
        {
            "CLAUDE_PATH": sys.executable,
            "WORKSPACE": str(tmp_path),
            "HOME_DIR": str(tmp_path),
            "MODEL": "",
        }
    )
    assert settings.max_thinking_tokens is None
    proc = spawn_claude(settings, resume=False, extra_args=[str(script)])
    try:
        seen = read_until(proc, b"MTT=").decode(errors="replace")
    finally:
        proc.close()
        proc.wait()

    assert "MTT=-" in seen, "a cleared budget must not inherit the stale boot env"
