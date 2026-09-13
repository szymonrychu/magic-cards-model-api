"""Export the play-script schema as `contract/playscript.schema.json`.

`src/magic_cards_model_api/playscripts/schema.py`'s `Play` is the authority
on what `decks/<slug>/plays/<id>.json` may hold. This writes it out as JSON
Schema for readers that are not Python - an editor, a reviewer, the stdlib
gate's authors - and `tests/test_playscript_export_drift.py` fails when the
committed file no longer matches the model. Deterministic: sorted keys, no
timestamp, so `mise run playscript:export` twice leaves `git status` clean.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "contract" / "playscript.schema.json"


def build_schema() -> dict[str, Any]:
    from magic_cards_model_api.playscripts.schema import json_schema

    return json_schema()


def render_schema(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render_schema(build_schema()))


if __name__ == "__main__":
    main()
