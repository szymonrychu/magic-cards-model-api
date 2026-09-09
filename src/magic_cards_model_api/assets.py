"""Card-art and mana-symbol-SVG proxy: the browser never talks to Scryfall
directly.

`serialize.py`'s card views and `symbology.py`'s symbol map used to hand the
browser Scryfall's own `cards.scryfall.io`/`svgs.scryfall.io` URLs verbatim,
so every `<img>` on the board was a direct browser-to-Scryfall request. This
module is what those two now point at instead: `GET /api/card-art` and
`GET /api/symbol-svg` fetch the asset from Scryfall server-side (rate-limited
through `scryfall.fetch_asset_bytes`, same throttle the JSON client already
uses) and stream it back from this API's own origin.

SSRF: neither route accepts a URL, or anything that becomes one directly.
`card-art` takes a card `name` (already a public lookup key - `GET
/api/cards/{name}` exposes the identical capability today) and a `size`
restricted to a three-value enum; `symbol-svg` takes a mana-symbol `token`
that must already be a key in the locally cached symbology map this api
fetched from Scryfall's own `/symbology` endpoint, so it can never name
anything Scryfall itself did not already vouch for. In both cases the actual
upstream URL fetched comes only from Scryfall's own JSON response
(`image_uris`/`svg_uri`), never from the request - and
`scryfall.fetch_asset_bytes` independently re-checks that URL's host against
an allowlist (`cards.scryfall.io` / `svgs.scryfall.io`) before opening a
socket and again after following any redirect, so even a compromised or
malformed Scryfall response cannot turn this into a fetch of an internal
address.

Cached on disk beside the JSON cache (`scryfall.cache_dir() / "assets"`),
keyed by a hash of the resolved upstream URL - which is immutable (Scryfall's
image/svg URLs carry a version query), so a fetched asset never needs
revalidating and is served client-side with a year-long, `immutable`
`Cache-Control`. A cold cache costs one upstream fetch per distinct asset,
ever; every request after that is a local disk read.

Fails gracefully: an unknown card, a size the printing does not have, or an
unrecognised symbol token all 404 rather than raising - a broken `<img>` is
what a caller already renders as a text fallback (`cards.js`'s `onerror`,
`symbols.js`'s missing-entry branch), never a 500 that would take the page
that embeds it down with it. `scryfall.CardNotFound`/`ScryfallError` raised
by the underlying fetch are left to propagate - `app.py` already maps them to
404/502 for every other route, so this reuses that mapping rather than
re-inventing it.
"""

from __future__ import annotations

import mimetypes
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException
from starlette.responses import Response

from . import scryfall, symbology

router = APIRouter(prefix="/api", tags=["assets"])

ImageSize = Literal["normal", "small", "art_crop"]
IMAGE_SIZES: tuple[str, ...] = ("normal", "small", "art_crop")

#: A year, effectively forever - the asset behind one proxy URL never changes
#: (see module docstring), so there is nothing to revalidate.
_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


def image_proxy_url(name: str, size: str) -> str:
    """This api's own URL for one card's art, in place of a raw Scryfall
    one. Built even when the underlying link may not exist - callers only
    call this once they have already checked `image_uris()` carries `size`."""
    return f"/api/card-art?name={quote(name, safe='')}&size={size}"


def proxy_image_links(name: str, links: dict[str, str]) -> dict[str, str]:
    """A raw Scryfall `image_uris`-shaped dict, rewritten to this api's own
    proxy URLs - one entry per size that was actually present, so a card
    missing (say) `art_crop` stays missing rather than gaining a link to
    nothing. Never emits a `scryfall.io` URL."""
    return {size: image_proxy_url(name, size) for size in IMAGE_SIZES if links.get(size)}


def _asset_cache_path(upstream_url: str) -> Path:
    """Where one asset lives on disk, keyed by a hash of the upstream URL
    itself (immutable per URL, per the module docstring) rather than by
    whatever caller-facing identifier resolved to it - so the cache is
    correct regardless of how many different names or tokens end up pointing
    at the same printing or symbol."""
    digest = sha256(upstream_url.encode()).hexdigest()
    suffix = Path(urlsplit(upstream_url).path).suffix
    return scryfall.cache_dir() / "assets" / f"{digest}{suffix}"


def _content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _serve(upstream_url: str) -> Response:
    """Bytes for `upstream_url`, from the disk cache when possible - a cache
    hit never calls `scryfall.fetch_asset_bytes` at all, so a page re-polling
    the board every second costs this api zero further Scryfall requests per
    card once its art has been fetched once."""
    path = _asset_cache_path(upstream_url)
    if not path.exists():
        content = scryfall.fetch_asset_bytes(upstream_url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return Response(
        content=path.read_bytes(), media_type=_content_type(path), headers=_CACHE_HEADERS
    )


@router.get("/card-art")
def card_art(name: str, size: ImageSize) -> Response:
    raw = scryfall.fetch_card(name)
    url = scryfall.image_uris(raw).get(size)
    if not url:
        raise HTTPException(status_code=404, detail=f"no {size!r} image for {name!r}")
    return _serve(url)


@router.get("/symbol-svg")
def symbol_svg(token: str) -> Response:
    entry = symbology.load().get(token)
    url = entry.get("svg") if entry else None
    if not url:
        raise HTTPException(status_code=404, detail=f"no svg for symbol {token!r}")
    return _serve(url)
