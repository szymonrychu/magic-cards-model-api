#!/bin/sh
# FC2 manual repro, NOT part of the test suite: runs the real `claude
# --print` CLI with the exact argv `wrapper.oneshot._argv` builds, pointed
# at a FAKE `mcp-seat` on PATH, and asks it to try several different
# invocation SHAPES of one allowed subcommand (`plays check`) - bare, piped,
# absolute-path, env-var-prefixed, and `cd ... &&`-chained. It then reads the
# fake `mcp-seat`'s own invocation log back to say, per shape, whether it
# actually ran.
#
# This spawns a real `claude --print` process and costs real API time and
# tokens - it is not run by CI or by `mise run test`. Run it by hand when the
# permission-rule SHAPE (not the subcommand set) needs re-verifying against a
# new `claude` CLI version. It never touches this machine's real HOME: only
# PATH is extended (with a throwaway fake binary) and a scratch workspace and
# guides directory are created under a temp dir, so real credentials keep
# resolving exactly as they do for any other `claude` invocation in this
# shell, and nothing under `$HOME` is read or written.
set -eu

root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

bin="$work/bin"
mkdir -p "$bin" "$work/workspace" "$work/guides"
log="$work/mcp-seat-invocations.log"
: >"$log"

# A fake `mcp-seat`: logs exactly what it was called with and answers ok, so
# a shape that actually ran leaves a line in $log and a shape the permission
# layer refused leaves nothing.
cat >"$bin/mcp-seat" <<EOF
#!/bin/sh
printf '%s\n' "\$*" >> "$log"
echo '{"result": "ok (fake mcp-seat)"}'
EOF
chmod +x "$bin/mcp-seat"

cd "$root"
mise exec -- uv run python - "$work" "$log" "$bin" <<'PY'
import json
import os
import sys
from pathlib import Path

work = Path(sys.argv[1])
log = Path(sys.argv[2])
fake_bin = sys.argv[3]

sys.path.insert(0, "src")
from mtg_agent.wrapper import oneshot  # noqa: E402
from mtg_agent.wrapper.config import Settings  # noqa: E402

# PATH is extended, never replaced, and only for THIS subprocess: the fake
# bin dir goes first so both `shutil.which` (mcp_seat_entrypoint, called
# below and again inside `_argv` -> `review_allowed_tools`) and the real
# `claude` CLI's own PATH lookups resolve to the fake `mcp-seat`.
child_env = dict(os.environ)
child_env["PATH"] = f"{fake_bin}{os.pathsep}{child_env.get('PATH', '')}"

guides_dir = work / "guides"
mcp_config = work / "mcp.json"
mcp_config.write_text(json.dumps({"mcpServers": {}}))

settings = Settings.from_env({"GUIDES_ROOT": str(guides_dir)})

# `mcp_seat_entrypoint`/`review_bash_rules`/`review_allowed_tools` all
# resolve `mcp-seat` off `os.environ["PATH"]` (`shutil.which`) - set it here,
# in THIS process, before building argv, so the absolute-path Bash rule
# `_argv` embeds is the fake binary's own path, not this process's real one.
os.environ["PATH"] = child_env["PATH"]
entrypoint = oneshot.mcp_seat_entrypoint()
argv = oneshot._argv(settings, "claude-haiku-4-5", "low", mcp_config)

print(f"resolved mcp-seat entrypoint: {entrypoint}", file=sys.stderr)
print(f"argv: {argv}", file=sys.stderr)

prompt = (
    "You are probing which shell command SHAPES your current Bash permission "
    "rules actually allow. A fake `mcp-seat` binary is on your PATH; its "
    f"absolute path is exactly: {entrypoint}\n\n"
    "Attempt every one of the five shapes below, in order, even if an earlier "
    "one is refused - do not stop early, and do not ask for confirmation. "
    "After each attempt, on its own line, say exactly RAN or REFUSED for that "
    "shape and nothing else on that line:\n\n"
    "1 (bare): mcp-seat plays check --root /tmp\n"
    "2 (piped): mcp-seat plays check --root /tmp 2>&1 | head\n"
    f"3 (absolute path): {entrypoint} plays check --root /tmp\n"
    "4 (env-prefixed): MTG_GAME_ID=probe mcp-seat plays check --root /tmp\n"
    "5 (cd-chained): cd /tmp && mcp-seat plays check --root /tmp\n\n"
    "When all five are attempted, print one final line shaped exactly like "
    "this, with RAN or REFUSED in each slot:\n"
    "SUMMARY: bare=<RAN|REFUSED> piped=<RAN|REFUSED> absolute=<RAN|REFUSED> "
    "env=<RAN|REFUSED> cd=<RAN|REFUSED>"
)

import subprocess  # noqa: E402

result = subprocess.run(
    argv,
    cwd=settings.workspace if settings.workspace.is_dir() else work / "workspace",
    env=child_env,
    input=prompt,
    capture_output=True,
    text=True,
    timeout=180,
)
print("=== claude --print stdout ===")
print(result.stdout.strip())
if result.returncode != 0:
    print("=== claude --print stderr (nonzero exit) ===", file=sys.stderr)
    print(result.stderr.strip(), file=sys.stderr)
PY

echo
echo "=== fake mcp-seat invocation log (what actually ran) ==="
if [ -s "$log" ]; then
  cat "$log"
else
  echo "(empty: no shape reached the fake mcp-seat)"
fi
