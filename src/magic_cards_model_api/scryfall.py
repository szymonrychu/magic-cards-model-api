"""Scryfall client: any card by name, with a disk cache so a card is fetched once.

Card data is always a real Scryfall printing. The engine's `Card` model drops
image URIs by design, so the raw JSON is kept as well and the serializer reads
image links out of the cache.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from magic_cards_model.models import Card
from pydantic import ValidationError

BASE_URL = "https://api.scryfall.com"
USER_AGENT = "magic-cards-model-api/0.1 (self-test harness)"
MIN_REQUEST_INTERVAL = 0.1  # seconds between outgoing requests

_rate_lock = threading.Lock()
_last_request = 0.0
_suggest_cache: dict[str, list[str]] = {}


class ScryfallError(Exception):
    """Scryfall could not be reached or answered with an error."""


class CardNotFound(ScryfallError):
    """Scryfall has no card by that name."""


def cache_dir() -> Path:
    env = os.environ.get("MCM_CACHE_DIR")
    base = Path(env) if env else Path(__file__).resolve().parents[2] / ".scryfall-cache"
    base.mkdir(parents=True, exist_ok=True)
    return base


def slug(name: str) -> str:
    """Filename for a card name: lowercase, non-alphanumeric to underscore."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def cache_path(name: str) -> Path:
    return cache_dir() / f"{slug(name)}.json"


def load_cached(name: str) -> dict[str, Any] | None:
    path = cache_path(name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _get(path: str, params: dict[str, str]) -> dict[str, Any]:
    global _last_request
    with _rate_lock:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = httpx.get(
                BASE_URL + path,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise ScryfallError(f"Scryfall request failed: {exc}") from exc
        finally:
            _last_request = time.monotonic()

    if response.status_code == 404:
        detail = response.json().get("details", "not found") if response.text else "not found"
        raise CardNotFound(f"Scryfall has no card matching {params!r}: {detail}")
    if response.status_code >= 400:
        raise ScryfallError(f"Scryfall returned {response.status_code}: {response.text[:200]}")
    data = response.json()
    if not isinstance(data, dict):
        raise ScryfallError("Scryfall returned something that is not an object")
    return data


def fetch_card(name: str) -> dict[str, Any]:
    """The raw Scryfall card object for `name`, from cache when possible."""
    cached = load_cached(name)
    if cached is not None:
        return cached
    try:
        raw = _get("/cards/named", {"exact": name})
    except CardNotFound:
        raw = _get("/cards/named", {"fuzzy": name})

    for key in {name, raw.get("name", name)}:
        cache_path(key).write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return raw


def image_uris(raw: dict[str, Any]) -> dict[str, str]:
    """The image links worth having, off the card or its front face."""
    uris = raw.get("image_uris")
    if not uris and raw.get("card_faces"):
        uris = raw["card_faces"][0].get("image_uris") or {}
    return {kind: uris[kind] for kind in ("normal", "small", "art_crop") if kind in uris}


def modern_status(raw: dict[str, Any]) -> str:
    return raw.get("legalities", {}).get("modern", "not_legal")


def lookup_card(name: str) -> dict[str, Any]:
    """Fetch, validate against the engine's Card model, and report."""
    raw = fetch_card(name)
    result: dict[str, Any] = {
        "requested": name,
        "name": raw.get("name"),
        "scryfall_id": raw.get("id"),
        "images": image_uris(raw),
        "modern_status": modern_status(raw),
        "modern_legal": modern_status(raw) == "legal",
    }
    try:
        card = Card.model_validate(raw)
    except ValidationError as exc:
        # pydantic 2.13 removed the `include_url` kwarg from `errors()`.
        first = exc.errors()[0]
        result["ok"] = False
        result["error"] = (
            f"card data does not fit the engine's Card model: "
            f"{exc.error_count()} validation error(s), first: {first}"
        )
        return result
    result["ok"] = True
    result["card"] = card.model_dump(mode="json")
    return result


def autocomplete(query: str) -> list[str]:
    """Card-name suggestions, cached in memory for the life of the process."""
    query = query.strip()
    if len(query) < 2:
        return []
    if query in _suggest_cache:
        return _suggest_cache[query]
    data = _get("/cards/autocomplete", {"q": query})
    names = [name for name in data.get("data", []) if isinstance(name, str)]
    _suggest_cache[query] = names
    return names
