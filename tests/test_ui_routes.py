"""Where the play UI is served: `/`, with `/play/` redirecting to it.

The UI used to be mounted at `/play`, out of the way of the sibling
`magic-cards-model-test-frontend` repo's mount at `/`. It is the front door of
this deployment, so it moved to `/`, the sibling frontend moved to `/frontend`,
and `/play/` redirects rather than 404s - the people most likely to ask for it
are the ones who had it bookmarked.

The half of this file that does not depend on the move is the more important
half: a StaticFiles mount at `/` matches every otherwise-unclaimed path, and
mount order is the only thing keeping it from shadowing the whole API. Those
tests pass with or without the move, and they are what would catch it if a
later edit registered the mount too early.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_the_root_serves_the_lobby(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200, response.text
    assert 'id="lobby"' in response.text
    assert 'id="start-game"' in response.text


def test_the_relative_assets_resolve_from_the_root(client: TestClient) -> None:
    """`index.html` asks for `app.js` and `style.css` relative to wherever it
    was served, so both have to answer at the root too."""
    assert 'src="app.js"' in client.get("/").text
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert "startGame" in client.get("/app.js").text


def test_play_redirects_to_the_root(client: TestClient) -> None:
    for path in ("/play", "/play/"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 307, (path, response.status_code)
        assert response.headers["location"] == "/"


def test_a_bookmarked_asset_under_play_redirects_to_its_new_place(client: TestClient) -> None:
    response = client.get("/play/app.js", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/app.js"


def test_following_the_redirect_lands_on_the_lobby(client: TestClient) -> None:
    response = client.get("/play/", follow_redirects=True)
    assert response.status_code == 200
    assert 'id="lobby"' in response.text


def test_the_api_is_not_shadowed_by_the_root_mount(client: TestClient) -> None:
    """The guard. A catch-all mount at `/` registered before these routes
    would answer them - with 404s, since it has no such files - and every
    seat, every agent and the browser itself would lose the API at once."""
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/games").status_code == 200
    assert client.get("/api/ui/decks").status_code == 200
    assert client.get("/api/card-names").status_code == 200


def test_the_openapi_surface_still_answers(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_an_unknown_path_is_still_a_404(client: TestClient) -> None:
    """`html=True` on the mount serves `index.html` for a directory, not for
    every miss: a typo must not come back as a cheerful 200."""
    assert client.get("/no-such-page.html").status_code == 404
