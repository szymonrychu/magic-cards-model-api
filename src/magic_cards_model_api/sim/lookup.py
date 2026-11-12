"""sim_card_lookup: verify card names (from a photo, a chat) without a sandbox.

Each name goes through `scryfall.fetch_card` (cache, bundled printings, then Scryfall: exact,
then fuzzy) and the engine's card-support verdict the sandboxes already report as
`support_warnings`. Runs in the server process: no worker, no sandbox slot.
"""

from __future__ import annotations

import re
import time
from typing import Any

from magic_cards_model.models import Card
from pydantic import ValidationError

from magic_cards_model_api import decks, scryfall
from magic_cards_model_api.sim.manager import SimRefused

MAX_LOOKUP_NAMES = 40
_ORACLE_CHARS = 400


def card_lookup(names: list[str], budget_s: float) -> dict[str, Any]:
    """One entry per distinct name, in order; names reached after `budget_s` are `not_checked`.

    The budget is checked between names only: one slow Scryfall call can overrun it."""
    if not 1 <= len(names) <= MAX_LOOKUP_NAMES:
        raise SimRefused(f"Give 1 to {MAX_LOOKUP_NAMES} names (got {len(names)}).")
    deadline = time.monotonic() + budget_s
    cards = []
    for query in dict.fromkeys(n.strip() for n in names if n.strip()):
        if time.monotonic() >= deadline:
            cards.append({"query": query, "status": "not_checked", "reason": "time budget used"})
        else:
            cards.append(_lookup_one(query))
    return {"cards": cards}


def _lookup_one(query: str) -> dict[str, Any]:
    try:
        raw = scryfall.fetch_card(query)
    except scryfall.CardNotFound:
        return {"query": query, "status": "not_found"}
    except scryfall.ScryfallError:
        return {"query": query, "status": "error", "reason": "Scryfall could not be reached"}
    exact = _is_exact(query, str(raw.get("name") or ""))
    out: dict[str, Any] = {
        "query": query,
        # "fuzzy": Scryfall guessed; the card may differ from the one meant (treat as uncertain)
        "status": "found" if exact else "fuzzy",
        "exact": exact,
        "name": raw.get("name"),
        "mana_cost": _faces(raw, "mana_cost"),
        "type_line": raw.get("type_line") or _faces(raw, "type_line"),
        "oracle_text": _short(_faces(raw, "oracle_text")),
    }
    if raw.get("power") is not None:
        out["power_toughness"] = f"{raw['power']}/{raw.get('toughness')}"
    if raw.get("loyalty") is not None:
        out["loyalty"] = raw["loyalty"]
    try:
        card = Card.model_validate(raw)
    except ValidationError:
        out["engine_support"] = "unloadable"  # the engine cannot load this printing at all
        return out
    warnings = decks.deck_support_warnings_from_cards({out["name"] or query: card})
    if not warnings:
        out["engine_support"] = "handled"
        return out
    out["engine_support"] = warnings[0]["status"]
    out["unsupported_clauses"] = [clause["text"] for clause in warnings[0]["clauses"]]
    return out


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def _is_exact(query: str, name: str) -> bool:
    """The canonical name (or its front face) equals the query, ignoring case and punctuation."""
    wanted = _norm(query)
    return bool(wanted) and wanted in {_norm(name), _norm(name.split(" // ")[0])}


def _faces(raw: dict[str, Any], key: str) -> str:
    """`key` of the card, or of its faces joined with " // " (split and double-faced cards)."""
    if raw.get(key):
        return str(raw[key])
    return " // ".join(str(f[key]) for f in raw.get("card_faces") or [] if f.get(key))


def _short(text: str) -> str:
    return text if len(text) <= _ORACLE_CHARS else text[: _ORACLE_CHARS - 3] + "..."
