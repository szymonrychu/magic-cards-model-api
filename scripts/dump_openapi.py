"""Regenerate the committed `openapi.json` from the live FastAPI app.

`mise run openapi:dump` runs this. It is deliberately the one-command fix
`test_openapi_drift.py` points to when the committed file goes stale: the app
is the source of truth (routes and their pydantic request/response models),
`openapi.json` is a checked-in cache of `app.openapi()` so a client repo can
pin against a sha without importing this one, and the drift test is what
keeps the cache honest.
"""

from __future__ import annotations

import json
from pathlib import Path

from magic_cards_model_api.app import app

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_JSON = REPO_ROOT / "openapi.json"


def dump() -> str:
    schema = app.openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> None:
    OPENAPI_JSON.write_text(dump())
    print(f"wrote {OPENAPI_JSON}")


if __name__ == "__main__":
    main()
