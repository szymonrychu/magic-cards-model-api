"""Run one of `ui/*.js`'s ES modules under Node and evaluate an expression.

The browser is served these files as-is (no build step), so the tests run the
exact modules it runs. `bindings` become JSON consts in the script's scope,
and the module is bound to `m`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

UI = Path(__file__).resolve().parents[1] / "ui"

requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def run_module(module: str, expression: str, **bindings: Any) -> Any:
    consts = "\n".join(f"const {name} = {json.dumps(value)};" for name, value in bindings.items())
    script = f"""
    import {{ pathToFileURL }} from "node:url";
    const m = await import(pathToFileURL({json.dumps(str(UI / module))}).href);
    {consts}
    const __result = await ({expression});
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
