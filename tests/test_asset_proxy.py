"""`assets.py` - the browser never talks to Scryfall directly.

Card art (`serialize.py`'s game state view, `scryfall.lookup_card`'s
`/api/cards*` routes) and mana-symbol SVGs (`symbology.py`) used to hand the
browser raw `cards.scryfall.io`/`svgs.scryfall.io` URLs. This file covers,
with no real network call anywhere: every payload that used to carry one no
longer does; the proxy routes serve a precached asset without re-fetching;
an unfetchable or unknown asset degrades to a 404 rather than a 500; and
`scryfall.fetch_asset_bytes` - the one function allowed to actually open a
socket to Scryfall's asset CDNs - refuses anything outside its host
allowlist before it ever tries.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest
from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api import assets, scryfall, symbology

FIXTURES_SCRYFALL = Path(__file__).parent / "fixtures" / "scryfall"

#: Lightning Bolt's real, cached Scryfall printing - `tests/fixtures/scryfall
#: /lightning_bolt.json` - carries these `image_uris` verbatim.
_BOLT_NORMAL_URL = (
    "https://cards.scryfall.io/normal/front/7/6/7673784e-db4b-43a1-8d55-1bb9fc1e284f.jpg?1783903008"
)


@pytest.fixture()
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy of the checked-in Scryfall fixture cache, so a test
    that makes this module actually write a binary asset to disk never
    touches the real, git-tracked `tests/fixtures/scryfall` directory - only
    ever a throwaway `tmp_path` copy."""
    cache = tmp_path / "scryfall-cache"
    shutil.copytree(FIXTURES_SCRYFALL, cache)
    monkeypatch.setenv("MCM_CACHE_DIR", str(cache))
    return cache


# --------------------------------------------------------------- no leaks --


def test_the_game_state_view_never_emits_a_scryfall_io_url(client: TestClient, game: str) -> None:
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    assert "scryfall.io" not in json.dumps(board)


def test_the_game_state_view_still_points_at_this_apis_own_card_art_route(
    client: TestClient, game: str
) -> None:
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    card = find(state(client, game)["hands"]["alice"], "Lightning Bolt")
    assert card["image"].startswith("/api/card-art?name=")
    assert card["small_image"].startswith("/api/card-art?name=")
    assert card["art_crop"].startswith("/api/card-art?name=")


def test_the_symbology_route_never_emits_a_scryfall_io_url(client: TestClient) -> None:
    body = client.get("/api/symbology").json()
    assert "scryfall.io" not in json.dumps(body)
    entry = body["symbols"]["{R}"]
    assert entry["svg"] == "/api/symbol-svg?token=%7BR%7D"


def test_the_card_lookup_route_never_emits_a_scryfall_io_url(client: TestClient) -> None:
    result = client.get("/api/cards/Lightning%20Bolt").json()
    assert result["ok"] is True
    assert "scryfall.io" not in json.dumps(result)
    assert result["images"]["normal"].startswith("/api/card-art?name=")


def test_the_card_lookup_query_route_never_emits_a_scryfall_io_url(client: TestClient) -> None:
    result = client.get("/api/cards", params={"name": "Grizzly Bears"}).json()
    assert "scryfall.io" not in json.dumps(result)


# ------------------------------------------------------------- disk cache --


def test_card_art_serves_a_precached_asset_without_refetching(
    client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = assets._asset_cache_path(_BOLT_NORMAL_URL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"already-on-disk")

    def _refuse(url: str) -> bytes:
        raise AssertionError(f"fetch_asset_bytes must not be called for a cache hit: {url}")

    monkeypatch.setattr(scryfall, "fetch_asset_bytes", _refuse)

    response = client.get("/api/card-art", params={"name": "Lightning Bolt", "size": "normal"})
    assert response.status_code == 200
    assert response.content == b"already-on-disk"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_card_art_fetches_and_caches_on_a_cold_asset_cache(
    client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _fake_fetch(url: str) -> bytes:
        calls.append(url)
        return b"freshly-fetched-bytes"

    monkeypatch.setattr(scryfall, "fetch_asset_bytes", _fake_fetch)

    response = client.get("/api/card-art", params={"name": "Lightning Bolt", "size": "normal"})
    assert response.status_code == 200
    assert response.content == b"freshly-fetched-bytes"
    assert response.headers["content-type"] == "image/jpeg"
    assert calls == [_BOLT_NORMAL_URL]

    path = assets._asset_cache_path(_BOLT_NORMAL_URL)
    assert path.read_bytes() == b"freshly-fetched-bytes"

    # A second request is now a cache hit - no second fetch.
    response2 = client.get("/api/card-art", params={"name": "Lightning Bolt", "size": "normal"})
    assert response2.status_code == 200
    assert calls == [_BOLT_NORMAL_URL]


def test_symbol_svg_serves_a_precached_asset_without_refetching(
    client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svg_url = symbology.load()["{R}"]["svg"]
    path = assets._asset_cache_path(svg_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<svg>cached</svg>")

    def _refuse(url: str) -> bytes:
        raise AssertionError(f"fetch_asset_bytes must not be called for a cache hit: {url}")

    monkeypatch.setattr(scryfall, "fetch_asset_bytes", _refuse)

    response = client.get("/api/symbol-svg", params={"token": "{R}"})
    assert response.status_code == 200
    assert response.content == b"<svg>cached</svg>"
    assert response.headers["content-type"] == "image/svg+xml"


def test_symbol_svg_fetches_and_caches_on_a_cold_asset_cache(
    client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svg_url = symbology.load()["{R}"]["svg"]
    calls: list[str] = []

    def _fake_fetch(url: str) -> bytes:
        calls.append(url)
        return b"<svg>fresh</svg>"

    monkeypatch.setattr(scryfall, "fetch_asset_bytes", _fake_fetch)

    response = client.get("/api/symbol-svg", params={"token": "{R}"})
    assert response.status_code == 200
    assert response.content == b"<svg>fresh</svg>"
    assert calls == [svg_url]


# --------------------------------------------------------- graceful 404s --


def test_card_art_for_an_unknown_card_is_404_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _not_found(name: str) -> dict:
        raise scryfall.CardNotFound(f"no such card: {name}")

    monkeypatch.setattr(scryfall, "fetch_card", _not_found)
    response = client.get("/api/card-art", params={"name": "Zzzz Nope", "size": "normal"})
    assert response.status_code == 404


def test_card_art_traversal_looking_name_is_just_an_unknown_card(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `name` that looks like a path-traversal attempt is never used as a
    filesystem path anywhere in this module - it is only ever a lookup key
    into `scryfall.fetch_card`, which resolves it through the same cache/
    pool/`/cards/named`-by-exact-string path every other card name goes
    through. Forged to look up a name outside the fixture pool without
    touching the network: it 404s exactly like any other unknown card."""

    def _not_found(name: str) -> dict:
        raise scryfall.CardNotFound(f"no such card: {name}")

    monkeypatch.setattr(scryfall, "fetch_card", _not_found)
    response = client.get(
        "/api/card-art", params={"name": "../../../../etc/passwd", "size": "normal"}
    )
    assert response.status_code == 404


def test_card_art_size_missing_from_the_printing_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scryfall, "image_uris", lambda raw: {})
    response = client.get("/api/card-art", params={"name": "Lightning Bolt", "size": "normal"})
    assert response.status_code == 404


def test_card_art_bad_size_is_a_422_not_a_500(client: TestClient) -> None:
    response = client.get("/api/card-art", params={"name": "Lightning Bolt", "size": "gigantic"})
    assert response.status_code == 422


def test_symbol_svg_for_an_unknown_token_is_404_not_500(client: TestClient) -> None:
    response = client.get("/api/symbol-svg", params={"token": "{ZZZ-not-a-real-symbol}"})
    assert response.status_code == 404


def test_card_art_when_scryfall_itself_is_unreachable_is_502_not_500(
    client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unreachable(url: str) -> bytes:
        raise scryfall.ScryfallError("simulated network failure")

    monkeypatch.setattr(scryfall, "fetch_asset_bytes", _unreachable)
    response = client.get("/api/card-art", params={"name": "Lightning Bolt", "size": "normal"})
    assert response.status_code == 502


# ------------------------------------------------------------------ SSRF --


def test_fetch_asset_bytes_refuses_an_internal_address_before_any_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("httpx.get must not be called for a disallowed host")

    monkeypatch.setattr(httpx, "get", _must_not_be_called)
    with pytest.raises(scryfall.ScryfallError, match="disallowed host"):
        scryfall.fetch_asset_bytes(
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
        )


def test_fetch_asset_bytes_refuses_a_cluster_internal_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("httpx.get must not be called for a disallowed host")

    monkeypatch.setattr(httpx, "get", _must_not_be_called)
    with pytest.raises(scryfall.ScryfallError, match="disallowed host"):
        scryfall.fetch_asset_bytes("http://mtg-seat-internal.svc.cluster.local/secret")


def test_fetch_asset_bytes_refuses_a_lookalike_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """`evil.com/cards.scryfall.io` and `cards.scryfall.io.evil.com` are not
    `cards.scryfall.io` - the check is on the parsed hostname, not a
    substring match."""

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("httpx.get must not be called for a disallowed host")

    monkeypatch.setattr(httpx, "get", _must_not_be_called)
    with pytest.raises(scryfall.ScryfallError, match="disallowed host"):
        scryfall.fetch_asset_bytes("https://cards.scryfall.io.evil.example/x.jpg")


def test_fetch_asset_bytes_refuses_the_wrong_scryfall_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`api.scryfall.com` is a real Scryfall host - just not one of the two
    asset CDNs this function exists to fetch from."""

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("httpx.get must not be called for a disallowed host")

    monkeypatch.setattr(httpx, "get", _must_not_be_called)
    with pytest.raises(scryfall.ScryfallError, match="disallowed host"):
        scryfall.fetch_asset_bytes("https://api.scryfall.com/cards/named?exact=Lightning+Bolt")


def test_fetch_asset_bytes_refuses_a_redirect_to_a_disallowed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The initial URL passes the allowlist; the server we (would) talk to
    redirects somewhere else. `fetch_asset_bytes` must catch that too, off
    the *final* resolved URL, not just the one it was asked for."""

    class _FakeResponse:
        status_code = 200
        content = b"whatever"
        url = httpx.URL("http://169.254.169.254/latest/meta-data/")

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse())
    with pytest.raises(scryfall.ScryfallError, match="disallowed host"):
        scryfall.fetch_asset_bytes("https://cards.scryfall.io/normal/front/x.jpg")


def test_the_card_art_route_never_accepts_a_url_parameter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no way to hand this route a URL at all - `name` and `size`
    are the whole query surface, and neither is treated as one. Posting a
    URL-shaped `name` is just an unknown card name to `fetch_card`, which is
    stubbed here (rather than left to hit the real cache/pool/network path)
    only so this test stays network-free - the point being made is that
    `assets.py` itself never parses `name` as a URL anywhere."""

    def _not_found(name: str) -> dict:
        raise scryfall.CardNotFound(f"no such card: {name}")

    monkeypatch.setattr(scryfall, "fetch_card", _not_found)
    response = client.get(
        "/api/card-art",
        params={"name": "http://169.254.169.254/latest/meta-data/", "size": "normal"},
    )
    assert response.status_code == 404
