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
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_ui_app import run_app


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
