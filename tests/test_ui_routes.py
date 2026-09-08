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


def test_the_lobby_defaults_the_effort_dropdown_to_medium(client: TestClient) -> None:
    """A live-pod audit found latency correlates almost perfectly with output
    tokens and `high` effort - not context size - is what makes a seat slow.
    `low` is deliberately not the default (documented as stopping to ask when
    a task is multi-step, but a seat's `AskUserQuestion` is denied and it is
    forbidden from stopping). The dropdown itself must still offer every
    level - a combo-heavy deck can still be dispatched at `high` - only the
    pre-selected option moves."""
    text = client.get("/").text
    assert '<option value="medium" selected>medium</option>' in text
    assert '<option value="high">high</option>' in text
    for level in ("low", "medium", "high", "xhigh", "max"):
        assert f'value="{level}"' in text


def test_the_thinking_budget_field_explains_when_it_applies(client: TestClient) -> None:
    """`max_thinking_tokens` is silently ignored by the default model
    (Sonnet 5, adaptive reasoning) - the field must say so rather than let
    someone set it and wonder why nothing changed."""
    text = client.get("/").text
    assert 'id="ai-thinking"' in text
    assert "adaptive" in text.lower()
    assert "effort" in text.lower().split('id="ai-thinking"', 1)[1][:600]


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


def test_a_game_is_served_at_its_own_bare_uuid_path(client: TestClient, game: str) -> None:
    """T7: the owner's own ask - `https://<base>/<game uuid>` - is now the
    canonical address, served the same `index.html` `/g/<uuid>/` always was."""
    response = client.get(f"/{game}/")
    assert response.status_code == 200, response.text
    assert 'id="lobby"' in response.text
    assert 'id="game"' in response.text
    assert response.text == client.get("/").text


def test_a_bare_uuid_with_no_slash_redirects_to_the_trailing_slash(
    client: TestClient, game: str
) -> None:
    """`document.baseURI` resolution depends on the trailing slash - without
    it, a relative `app.js` would drop the game id segment entirely and
    request `/app.js` instead of `/<id>/app.js`."""
    response = client.get(f"/{game}", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"/{game}/"


def test_assets_under_the_bare_uuid_path_resolve_to_the_real_files(
    client: TestClient, game: str
) -> None:
    """The relative `href`/`src` values `index.html` ships resolve, once
    served from `/<id>/`, to real requests under that same prefix - and
    those must answer with the real file, not another copy of the page."""
    assert client.get(f"/{game}/app.js").status_code == 200
    assert "startGame" in client.get(f"/{game}/app.js").text
    assert client.get(f"/{game}/style.css").status_code == 200
    assert client.get(f"/{game}/api.js").status_code == 200
    assert client.get(f"/{game}/render/lobby.js").status_code == 200


def test_an_unmatched_subpath_under_the_bare_uuid_still_falls_back_to_the_page(
    client: TestClient, game: str
) -> None:
    response = client.get(f"/{game}/no-such-asset")
    assert response.status_code == 200
    assert 'id="lobby"' in response.text


def test_a_bare_uuid_path_does_not_escape_the_ui_directory(client: TestClient, game: str) -> None:
    """`rest` is attacker-controlled - `../../pyproject.toml` must not read a
    file outside `ui/`. Percent-encoded (`%2e%2e`) so the request actually
    reaches this route's own handler instead of being dot-segment-normalised
    away by the test client before it is sent - a plain `../../../` in the
    literal URL never even reaches this route (it resolves client-side to
    `/pyproject.toml`, a 404 through the ordinary root mount, which proves
    nothing about this route's own traversal guard). Falls back to the page
    like any other unmatched subpath, rather than 404ing or serving the
    escaped file."""
    response = client.get(f"/{game}/%2e%2e/%2e%2e/%2e%2e/pyproject.toml")
    assert response.status_code == 200
    assert 'id="lobby"' in response.text
    assert "requires-python" not in response.text


def test_a_malformed_game_id_is_not_treated_as_a_game(client: TestClient) -> None:
    """The disambiguation rule, tested directly: a game id is exactly 32
    lowercase hex characters (`uuid4().hex` - `repository.py`), not "any
    single path segment". Anything shy of that must fall through to the
    ordinary static mount (and 404 there) rather than being swallowed by the
    game route - the actual hazard a bare `/<something>/` creates at the
    root, now resolved at routing time by a dedicated path convertor rather
    than by accident."""
    too_short = "a" * 31
    too_long = "a" * 33
    uppercase = "A" * 32
    non_hex = "g" * 32  # "g" is not a hex digit
    for candidate in (too_short, too_long, uppercase, non_hex):
        response = client.get(f"/{candidate}/")
        assert response.status_code == 404, (candidate, response.status_code)


def test_root_static_assets_are_not_swallowed_by_the_game_route(client: TestClient) -> None:
    """The other half of the same guarantee: every asset the lobby root
    serves must keep answering as itself, never as a fallback to
    `index.html` because some route upstream mistook its name for a game
    id."""
    assert client.get("/config.js").status_code == 200
    assert client.get("/render/ribbon.js").status_code == 200


def test_the_g_path_redirects_to_the_bare_canonical_form(client: TestClient, game: str) -> None:
    """T7: `/g/<uuid>/` is kept working, but only as a redirect - the bare
    path is canonical now (it is the literal shape the owner asked to be
    redirected to on Start), and the owner's own two-tabs use case only
    works cleanly off one canonical address per game."""
    response = client.get(f"/g/{game}/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"/{game}/"


def test_a_bare_g_path_redirects_to_the_bare_canonical_form_in_one_hop(
    client: TestClient, game: str
) -> None:
    """`/g/<uuid>` (no trailing slash) used to redirect to `/g/<uuid>/` and
    stop there. Now that `/g/<uuid>/` is itself only a redirect, this jumps
    straight to the real canonical address instead of bouncing through the
    now-vestigial slashed `/g/` form first."""
    response = client.get(f"/g/{game}", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"/{game}/"


def test_assets_under_the_g_path_still_resolve_once_the_redirect_is_followed(
    client: TestClient, game: str
) -> None:
    """A bookmarked `/g/<id>/app.js` (T4-era) must keep landing on the real
    file, not on the page - `TestClient` follows redirects by default, so
    this is the end-to-end guarantee bookmarks and existing links get."""
    assert client.get(f"/g/{game}/app.js").status_code == 200
    assert "startGame" in client.get(f"/g/{game}/app.js").text
    assert client.get(f"/g/{game}/style.css").status_code == 200
    assert client.get(f"/g/{game}/api.js").status_code == 200
    assert client.get(f"/g/{game}/render/lobby.js").status_code == 200


def test_an_unmatched_subpath_under_the_g_path_still_falls_back_to_the_page(
    client: TestClient, game: str
) -> None:
    response = client.get(f"/g/{game}/no-such-asset")
    assert response.status_code == 200
    assert 'id="lobby"' in response.text


def test_a_g_path_does_not_escape_the_ui_directory(client: TestClient, game: str) -> None:
    """The same traversal guard, exercised through the compatibility
    redirect: a `%2e%2e` escape must never survive into the `Location`
    header (which would just re-pose the same attack one hop later against
    the browser's own dot-segment resolution) - it collapses to the game's
    own root instead, exactly like an unmatched subpath does."""
    response = client.get(f"/g/{game}/%2e%2e/%2e%2e/%2e%2e/pyproject.toml", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"/{game}/"
    followed = client.get(f"/g/{game}/%2e%2e/%2e%2e/%2e%2e/pyproject.toml")
    assert followed.status_code == 200
    assert 'id="lobby"' in followed.text
    assert "requires-python" not in followed.text


def test_the_api_is_not_shadowed_by_the_root_mount(client: TestClient) -> None:
    """The guard. A catch-all mount at `/` registered before these routes
    would answer them - with 404s, since it has no such files - and every
    seat, every agent and the browser itself would lose the API at once."""
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/games").status_code == 200
    assert client.get("/api/lobby/games").status_code == 200
    assert client.get("/api/decks").status_code == 200
    assert client.get("/api/card-names").status_code == 200


def test_the_openapi_surface_still_answers(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_an_unknown_path_is_still_a_404(client: TestClient) -> None:
    """`html=True` on the mount serves `index.html` for a directory, not for
    every miss: a typo must not come back as a cheerful 200."""
    assert client.get("/no-such-page.html").status_code == 404


def test_a_crowded_board_row_scrolls_instead_of_painting_over_its_neighbour(
    client: TestClient,
) -> None:
    """From the browser pass. A bare `1fr` battlefield track collapsed to the
    row's 44px `min-height` once the board ran out of vertical room, and the
    wrapped cards - which do not clip - painted over the rows beneath. A
    Forest ended up on top of the casting strip's mode chips and took the
    click meant for one: Kozilek's Command was uncastable, with nothing in
    the console to say why.

    Layout is not something Node or the API can assert, so this pins the
    declarations that hold it together in the file the browser is served.

    The board is a flex column now, not a grid with an explicit track list:
    the two land rows and the exile strip joined it, and a hand-maintained
    list of tracks that has to stay in step with the row order is exactly
    what the original bug came out of. The guarantee is unchanged, and is
    now stated by the rows themselves - the growing row has a one-card floor,
    and every row scrolls inside itself rather than painting over the next."""
    css = client.get("/style.css").text
    # The declarations themselves, not a substring: each block's own comment
    # names its numbers too, so a bare `in css` stays green with the fix
    # reverted. Scoped to the rule each one is about.
    board = css.split("#mtg-board {", 1)[1].split("}", 1)[0]
    assert "display: flex;" in board and "flex-direction: column;" in board, board
    main = css.split(".main-row {", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 0" in main, "the non-land row is the one that takes the slack"
    assert "min-height: 162px" in main, "and it must never collapse under one card"
    rows = css.split(".hand-row, .battlefield-row, .stack-row {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto" in rows, "a row with more cards than fit must scroll itself"


def test_each_seats_lands_sit_in_the_row_nearest_that_seat(client: TestClient) -> None:
    """The order the rows are written in *is* the layout - `#mtg-board` is a
    flex column, so nothing else decides which row is nearest which player.
    `board.js` fills the rows and cannot see their order, so this is the only
    place the mirroring can be asserted.

    The convention itself is play practice, not a rule: the battlefield is a
    shared zone and the Comprehensive Rules prescribe no arrangement for it.
    """
    page = client.get("/").text
    rows = (
        "opp-hand",
        "opp-lands",
        "opp-battlefield",
        "stack-row",
        "my-battlefield",
        "my-lands",
        "my-hand",
    )
    positions = [page.find(f'id="{row}"') for row in rows]
    assert -1 not in positions, dict(zip(rows, positions, strict=True))
    assert positions == sorted(positions), (
        "the opponent's lands must sit directly under their hand and yours "
        "directly above your own, mirrored around the shared zones: "
        f"{dict(zip(rows, positions, strict=True))}"
    )


def test_the_ribbon_and_the_board_share_one_row_of_the_game_grid(
    client: TestClient,
) -> None:
    """Everything that is not the battlefield - the turn block, the banners,
    the prompt/priority/stops/log panel - is one right-hand column, and the
    board takes the whole of the other one. `#topbar` and `#banners` are still
    separate elements in `index.html`; only the grid puts them in the ribbon,
    so this is what says the three are actually in the same column."""
    css = client.get("/style.css").text
    game = css.split("#game {", 1)[1].split("}", 1)[0]
    areas = [line.strip() for line in game.splitlines() if "grid-template-areas" in line]
    assert areas == ['grid-template-areas: "board top" "board banner" "board side";'], areas


def test_exile_is_its_own_zone_on_the_board(client: TestClient) -> None:
    """Not a line in the zones modal shared with the graveyards: its own
    strip, its own border colour, and its own label."""
    page = client.get("/").text
    assert 'id="exile-row"' in page
    css = client.get("/style.css").text
    row = css.split(".exile-row {", 1)[1].split("}", 1)[0]
    assert "border: 1px dashed var(--warn)" in row, row
    label = css.split(".exile-row::before {", 1)[1].split("}", 1)[0]
    assert 'content: "exile"' in label, label


def test_the_scry_split_row_keeps_room_for_the_card_name(client: TestClient) -> None:
    """From the browser pass. `.split-row span` was `flex: 1` - a 0% basis -
    so in the side panel's narrow column the row's three buttons took every
    pixel and the card name rendered 0px wide. The scry was answerable, but
    without ever showing which card was being ordered."""
    css = client.get("/style.css").text
    row = css.split(".split-row span {", 1)[1].split("}", 1)[0]
    assert "min-width: 6em" in row, "the card's name needs a floor, not a 0% basis"


def test_the_armed_concede_button_actually_looks_armed(client: TestClient) -> None:
    """`ribbon.js` puts an `armed` class on the concede button once the first
    click has armed it. A class with no rule behind it is a change nobody
    sees, and this is the one button where seeing it matters."""
    css = client.get("/style.css").text
    assert "button.danger.armed {" in css
