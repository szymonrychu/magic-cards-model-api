"""`ui/config.js` under Node: the resolved API/lobby origins `app.js`'s
`gamePath()` and every `/api/ui/...` call build on top of (T4, M5).

The two facts worth pinning: with no `/config.json` reachable (this repo's
own case today - it serves API and UI from one origin, so it ships no such
file) the defaults are `/api` for both, exactly the paths every other Node
test already hardcodes; and when one *is* reachable (M5's ConfigMap-mounted
file, in the eventual split frontend container), its `apiBase`/`lobbyBase`
override those defaults.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

CONFIG_JS = Path(__file__).resolve().parents[1] / "ui" / "config.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def run_config(expression: str, **bindings: Any) -> Any:
    consts = "\n".join(f"const {name} = {json.dumps(value)};" for name, value in bindings.items())
    script = f"""
    import {{ pathToFileURL }} from "node:url";
    const m = await import(pathToFileURL({json.dumps(str(CONFIG_JS))}).href);
    {consts}
    const __result = await eval({json.dumps(expression)});
    process.stdout.write(JSON.stringify(__result === undefined ? null : __result));
    """
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout or "null")


def test_both_bases_default_to_slash_api_with_no_config_json_fetched() -> None:
    """The default before `loadConfig` ever runs - what every other Node
    test's hardcoded `/api/...`/`/api/ui/...` strings depend on staying
    true."""
    result = run_config("({ apiBase: m.apiBase(), lobbyBase: m.lobbyBase() })")
    assert result == {"apiBase": "/api", "lobbyBase": "/api"}


def test_a_404_config_json_leaves_the_defaults_standing() -> None:
    result = run_config(
        """(async () => {
          const config = await m.loadConfig(() => Promise.resolve({ ok: false, status: 404 }));
          return { config, apiBase: m.apiBase(), lobbyBase: m.lobbyBase() };
        })()"""
    )
    assert result["config"] == {"apiBase": "/api", "lobbyBase": "/api"}
    assert result["apiBase"] == "/api"
    assert result["lobbyBase"] == "/api"


def test_a_rejected_fetch_also_leaves_the_defaults_standing() -> None:
    result = run_config(
        """(async () => {
          const config = await m.loadConfig(() => Promise.reject(new Error("network down")));
          return { config, apiBase: m.apiBase(), lobbyBase: m.lobbyBase() };
        })()"""
    )
    assert result == {
        "config": {"apiBase": "/api", "lobbyBase": "/api"},
        "apiBase": "/api",
        "lobbyBase": "/api",
    }, result


def test_a_real_config_json_overrides_both_bases() -> None:
    result = run_config(
        """(async () => {
          const fetchImpl = () => Promise.resolve({
            ok: true,
            json: async () => ({
              apiBase: "https://api.example/api",
              lobbyBase: "https://lobby.example/api",
            }),
          });
          const config = await m.loadConfig(fetchImpl);
          return { config, apiBase: m.apiBase(), lobbyBase: m.lobbyBase() };
        })()"""
    )
    assert result["config"] == {
        "apiBase": "https://api.example/api",
        "lobbyBase": "https://lobby.example/api",
    }
    assert result["apiBase"] == "https://api.example/api"
    assert result["lobbyBase"] == "https://lobby.example/api"
