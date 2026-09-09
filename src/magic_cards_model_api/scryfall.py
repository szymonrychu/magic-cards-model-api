"""Scryfall client: any card by name, with a disk cache so a card is fetched once.

Card data is always a real Scryfall printing. The engine's `Card` model drops
image URIs by design, so the raw JSON is kept as well and the serializer reads
image links out of the cache.

Where a card comes from, in order:

1. `cache_dir()` - `MCM_CACHE_DIR`, or `<repo>/.scryfall-cache`. Anything
   fetched before, written by this module.
2. The engine's bundled printing corpus, shipped inside the installed
   `magic_cards_model` package itself at `data/printings/*.json` - **read-
   only**. `MCM_FIXTURE_CARDS_DIR` can override the path outright. Those
   files are whole Scryfall printings, so they serve `image_uris` and
   `legalities` as well as the engine's `Card`. This module never writes into
   that directory, and never needs it to exist.
3. Scryfall itself, rate-limited, written to `cache_dir()`.

Tier 2 exists because tier 3 costs ~0.35s per distinct card and a decklist has
sixty of them: validating `grixis_reanimator` against an empty cache took 35s
of wall clock, which reads in the browser as a hung "validating" spinner. The
corpus ships with the engine package itself (747 real printings), covering
nearly every card in the bundled decks, so on a cold cache the same
validation is disk-speed - in an editable install, a wheel install, or the
container image alike, since all three lay the installed package out as
plain files on disk.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import magic_cards_model
from magic_cards_model.models import Card
from pydantic import ValidationError

BASE_URL = "https://api.scryfall.com"
USER_AGENT = "magic-cards-model-api/0.1 (self-test harness)"
MIN_REQUEST_INTERVAL = 0.1  # seconds between outgoing requests

#: The only hosts an asset URL (`image_uris`/`svg_uri`, both taken verbatim
#: from a Scryfall API response, never from a caller) is ever allowed to
#: resolve to - checked before opening a socket AND again after following any
#: redirect. This is what keeps `fetch_asset_bytes` from ever becoming an
#: open forward proxy: nothing this module fetches, whatever a caller passes
#: to the routes built on top of it, can land anywhere but Scryfall's own
#: asset CDNs.
ASSET_HOSTS = {"cards.scryfall.io", "svgs.scryfall.io"}

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


def _packaged_pool_dir() -> Path:
    """The engine's bundled Scryfall printing corpus, inside the installed
    `magic_cards_model` package itself: `data/printings/*.json`, next to the
    package's own `__init__.py`. This resolves the same way regardless of how
    the engine got installed - an editable/path install (this repo's dev
    setup today), a non-editable wheel install (what a git-sha dependency
    will give), or a wheel unpacked into a container image - because all
    three lay the installed package out as plain files on disk at that same
    relative location. `magic_cards_model.__file__` is always a real path on
    disk in every one of those cases (this project never zipimports), so a
    plain `pathlib` join is enough; there is no zipped-package case to guard
    against with `importlib.resources`.
    """
    return Path(magic_cards_model.__file__).resolve().parent / "data" / "printings"


def fixture_pool_dir() -> Path | None:
    """The engine's read-only, bundled scenario-test card pool, or None if
    absent.

    `MCM_FIXTURE_CARDS_DIR` names it outright; an empty value turns the tier
    off. Otherwise the corpus packaged inside the installed
    `magic_cards_model` distribution is used. A path that is not a directory
    is simply not a source - there is no error, the lookup falls through to
    the cache and the network.
    """
    env = os.environ.get("MCM_FIXTURE_CARDS_DIR")
    if env is not None:
        if not env.strip():
            return None
        candidate = Path(env)
    else:
        candidate = _packaged_pool_dir()
    return candidate if candidate.is_dir() else None


def _faces(name: str) -> list[str]:
    """A multi-face name split into its faces, casefolded.

    Scryfall writes "Wear // Tear"; MTGO decklists write "Wear/Tear"; both
    name the same card, and both turn up in `decks/*.txt`. Normalising the
    separator here is what lets one lookup rule serve both.
    """
    return [face.strip().casefold() for face in name.replace("//", "/").split("/") if face.strip()]


def fixture_slug(name: str) -> str:
    """The pool's own filename rule, copied from the engine's
    `scripts/add_card.py::slugify` - front face only, every non-alphanumeric
    character its own underscore, and `str.isalnum()` rather than an ASCII
    class, so it differs from `slug` three ways at once:

        "Otawara, Soaring City"                -> otawara__soaring_city
        "Tamiyo, Inquisitive Student // ..."   -> tamiyo__inquisitive_student
        "Lorien Revealed" with the accent      -> lorien_revealed, accent kept

    Guessing any of the three wrong turns every pool hit into a miss, silently.
    """
    faces = _faces(name)
    front = faces[0] if faces else ""
    return "".join(c if c.isalnum() else "_" for c in front).strip("_")


def _pool_names_match(requested: str, found: str) -> bool:
    """Whether a pool file really is the card that was asked for.

    The pool slugs a multi-face card by its front face alone, so `wear.json`
    holds "Wear // Tear" - and a request for a genuinely different card named
    "Wear" must not silently receive it. Accept the whole name (however its
    faces are punctuated) or the front face alone; anything else is a miss and
    falls through to Scryfall, which is the safe direction to be wrong in.
    """
    wanted = _faces(requested)
    faces = _faces(found)
    return bool(faces) and (wanted == faces or wanted == faces[:1])


def load_pooled(name: str) -> dict[str, Any] | None:
    """The engine's fixture printing for `name`, read-only, or None."""
    pool = fixture_pool_dir()
    if pool is None:
        return None
    path = pool / f"{fixture_slug(name)}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    return data if _pool_names_match(name, data["name"]) else None


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


def fetch_asset_bytes(url: str) -> bytes:
    """The bytes at `url` - a card-art image or a mana-symbol SVG - rate-
    limited the same way `_get` throttles the JSON API.

    `url` must already be a full URL taken verbatim from a Scryfall API
    response (`image_uris`, `symbology`'s `svg_uri`) - this function never
    accepts, and the routes built on it never forward, a caller-supplied URL.
    The host is checked against `ASSET_HOSTS` before any socket opens, and
    checked again against the final URL after following redirects, so a
    Scryfall response that somehow pointed elsewhere - or a redirect chain
    that tried to - cannot turn this into a fetch of an arbitrary address.
    """
    host = urlsplit(url).hostname
    if host not in ASSET_HOSTS:
        raise ScryfallError(f"refusing to fetch an asset from disallowed host {host!r}")

    global _last_request
    with _rate_lock:
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=15,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise ScryfallError(f"Scryfall asset request failed: {exc}") from exc
        finally:
            _last_request = time.monotonic()

    final_host = response.url.host if response.url else None
    if final_host not in ASSET_HOSTS:
        raise ScryfallError(f"asset request redirected to disallowed host {final_host!r}")
    if response.status_code >= 400:
        raise ScryfallError(f"Scryfall asset request returned {response.status_code}")
    return response.content


def fetch_card(name: str) -> dict[str, Any]:
    """The raw Scryfall card object for `name`, from cache when possible."""
    cached = load_cached(name)
    if cached is not None:
        return cached
    pooled = load_pooled(name)
    if pooled is not None:
        # Deliberately not copied into `cache_dir()`: the pool is already a
        # local file, and writing would make the cache a second copy that
        # goes stale against an engine checkout that moves.
        return pooled
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
