"""Make the repo root importable so wrapper tests can share helpers.

`test_probe`, `test_api` and `test_integration` all reuse `FakeProc` and the
`ready_manager` fixture-builder from `tests.wrapper.test_session`. pytest only
puts a rootdir-relative test file's own directory on `sys.path`, so the
`tests.` package prefix needs the repo root added explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
