"""The URL `ui/app.js` polls the game on, checked against the real router.

Found by clicking through the play screen in a browser: the game screen came
up completely blank and the connection pill read "error: Not Found", because
`gamePath("")` built `/api/games/{id}/` - one trailing slash - and that path
answers 404. It is not a redirect: the router carries sibling routes under
the same prefix (`log`, `decide`, `actions/...`), so Starlette's
redirect-slashes never fires for it. Every Node test passed throughout,
because the fake `fetch` they run against answers any path at all.

So this file pins both halves at once: the exact string `app.js` builds, and
that the real app answers it.

T4 extends it: once a game is served at `/g/<uuid>/`
(`tests/test_ui_routes.py` pins the server side of that), the page's own
`document.baseURI` is what tells the play modules which game they are
looking at, and `apiBase()`/`lobbyBase()` (`ui/config.js`) are what keep
every API call absolute regardless of that path - `gamePath` must build the
same `/api/games/<id>` whether the page's own base is `/` or `/g/<id>/`,
while the relative `href`/`src` values the shipped `ui/index.html` and
`ui/render/lobby.js` carry must resolve, once served from `/g/<id>/`, to
requests under that same prefix.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_ui_app import run_app

UI = Path(__file__).resolve().parents[1] / "ui"
INDEX_HTML = (UI / "index.html").read_text()


def test_the_game_poll_path_has_no_trailing_slash() -> None:
    """`gamePath("")` is the game itself, and must end at the id."""
    built = run_app(
        """(() => {
          m.store.set({ gameId: "abc123" });
          return { base: m.gamePath(""), log: m.gamePath("log?since=0") };
        })()"""
    )
    assert built["base"] == "/api/games/abc123"
    assert built["log"] == "/api/games/abc123/log?since=0"


def test_the_router_answers_that_path_and_404s_the_slashed_one(
    client: TestClient, game: str
) -> None:
    """Why the slash matters, stated against the router rather than by
    assertion: without it the poll is a 200, with it a 404."""
    assert client.get(f"/api/games/{game}").status_code == 200
    assert client.get(f"/api/games/{game}/", follow_redirects=True).status_code == 404


# -- T4: /g/<uuid>/ addressing ------------------------------------------------


def test_the_game_id_resolves_from_a_g_uuid_base_uri() -> None:
    """The play modules read the game id from `document.baseURI`, not from
    `store` - a fresh page load at `/g/<uuid>/` (a bookmark, a shared link, a
    refresh) has to work with nothing in the store at all."""
    resolved = run_app(
        """({
          fromGamePath: m.gameIdFromBaseURI("https://host/g/abc-123/"),
          noTrailingSlash: m.gameIdFromBaseURI("https://host/g/abc-123"),
          deeperSubpath: m.gameIdFromBaseURI("https://host/g/abc-123/whatever"),
          lobbyRoot: m.gameIdFromBaseURI("https://host/"),
          unrelatedPath: m.gameIdFromBaseURI("https://host/docs"),
        })"""
    )
    assert resolved["fromGamePath"] == "abc-123"
    assert resolved["noTrailingSlash"] == "abc-123"
    assert resolved["deeperSubpath"] == "abc-123"
    assert resolved["lobbyRoot"] is None
    assert resolved["unrelatedPath"] is None


def test_api_calls_stay_absolute_when_the_page_is_served_under_g() -> None:
    """`gamePath` must build the exact same `/api/games/<id>` whether the
    page's own `document.baseURI` is `/` or `/g/<id>/` - the API lives at one
    place regardless of which path served the HTML."""
    built = run_app(
        """(() => {
          const gameId = m.gameIdFromBaseURI("https://host/g/g42/");
          m.store.set({ gameId });
          return { base: m.gamePath(""), log: m.gamePath("log?since=0") };
        })()"""
    )
    assert built["base"] == "/api/games/g42"
    assert built["log"] == "/api/games/g42/log?since=0"


def test_relative_assets_in_the_shipped_index_html_resolve_under_the_g_path() -> None:
    """The literal `href`/`src` values `ui/index.html` ships - read from the
    real file, not retyped here, so a later absolute-path regression in the
    markup fails this rather than a stale hardcoded string - resolve, once
    the page is served from `/g/<uuid>/`, to requests under that same
    prefix. This is the browser's own relative-URL resolution against
    `document.baseURI`; nothing in `app.js` has to compute it, which is
    exactly why the markup must stay relative."""
    css_href = re.search(r'<link rel="stylesheet" href="([^"]+)"', INDEX_HTML)
    script_src = re.search(r'<script type="module" src="([^"]+)"', INDEX_HTML)
    assert css_href and script_src, "index.html's own tags moved - update the regexes above"
    assert not css_href.group(1).startswith("/"), "style.css must stay a relative reference"
    assert not script_src.group(1).startswith("/"), "app.js must stay a relative reference"
    resolved = run_app(
        """({
          css: new URL(cssHref, "https://host/g/abc-123/").pathname,
          script: new URL(scriptSrc, "https://host/g/abc-123/").pathname,
          lobbyJs: new URL("render/lobby.js", "https://host/g/abc-123/").pathname,
        })""",
        cssHref=css_href.group(1),
        scriptSrc=script_src.group(1),
    )
    assert resolved["css"] == "/g/abc-123/style.css"
    assert resolved["script"] == "/g/abc-123/app.js"
    assert resolved["lobbyJs"] == "/g/abc-123/render/lobby.js"


def test_the_lobby_root_still_resolves_the_same_assets_to_the_root() -> None:
    """The other half of the same guarantee: nothing about serving `/g/<id>/`
    changes what the plain lobby root resolves to."""
    css_href = re.search(r'<link rel="stylesheet" href="([^"]+)"', INDEX_HTML).group(1)
    script_src = re.search(r'<script type="module" src="([^"]+)"', INDEX_HTML).group(1)
    resolved = run_app(
        """({
          css: new URL(cssHref, "https://host/").pathname,
          script: new URL(scriptSrc, "https://host/").pathname,
        })""",
        cssHref=css_href,
        scriptSrc=script_src,
    )
    assert resolved["css"] == "/style.css"
    assert resolved["script"] == "/app.js"
