"""Scryfall's mana symbol art, cached to disk and served to the browser UI.

A mana pool printed as `2R 1G` is a line of text a player has to decode. The
same pool printed with the actual symbols is a thing they recognise without
reading. Scryfall publishes every symbol's SVG at `/symbology`, keyed by the
exact `{W}`/`{U}`/`{2/R}`/`{T}` token that already appears verbatim in
`Card.mana_cost` and in an activated ability's printed cost - so the UI needs
no symbol table of its own and no guessing at hybrid or Phyrexian shapes: it
looks the token up and gets the right picture.

Mounted under plain `/api`, not the lobby's `/api/ui` (2b's T8, map's
namespace split): this is api-owned data - a Scryfall lookup cached beside
the card cache - with no dependency on anything lobby-owned, so `GET
/api/symbology` lives in the same URL space `api_decks.py`'s routes do.

Fetched once and cached to disk beside the card cache (`scryfall.cache_dir`),
because the symbol list changes about as often as a new mana symbol is
printed. A cold cache with no network still answers - with an empty map, so
the UI falls back to rendering the tokens as text rather than showing nothing.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter

from . import scryfall

router = APIRouter(prefix="/api", tags=["symbology"])

CACHE_NAME = "_symbology.json"


def _cache_path() -> Any:
    return scryfall.cache_dir() / CACHE_NAME


def _parse(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Scryfall's symbology list, reduced to what a UI actually renders."""
    symbols: dict[str, dict[str, Any]] = {}
    for entry in raw.get("data", []):
        token = entry.get("symbol")
        if not isinstance(token, str):
            continue
        symbols[token] = {
            "svg": entry.get("svg_uri"),
            "text": entry.get("english") or token,
            "colors": entry.get("colors") or [],
        }
    return symbols


def load() -> dict[str, dict[str, Any]]:
    """Every mana symbol, from the disk cache, fetching once if it is cold.

    Never raises: Scryfall being unreachable degrades the UI from "real
    symbols" to "the `{R}` tokens as plain text", which is exactly what the
    UI already renders when a token has no entry here. A hard failure would
    instead take down the state poll that carries it.
    """
    path = _cache_path()
    if path.exists():
        try:
            cached = json.loads(path.read_text())
        except json.JSONDecodeError:
            cached = None
        if isinstance(cached, dict) and cached:
            return cached
    try:
        raw = scryfall._get("/symbology", {})
    except scryfall.ScryfallError:
        return {}
    symbols = _parse(raw)
    if symbols:
        path.write_text(json.dumps(symbols, indent=2, sort_keys=True) + "\n")
    return symbols


@router.get("/symbology")
def get_symbology() -> dict[str, Any]:
    """`{"symbols": {"{R}": {"svg": ..., "text": ...}, ...}}`, for the UI."""
    return {"symbols": load()}
