"""`GET /api/symbology` - Scryfall's mana-symbol SVG map, api-owned data with
no lobby dependency (`symbology.py`). 2b's T8 moved this off the lobby's
`/api/ui` namespace onto plain `/api`, alongside `api_decks.py`'s routes -
had no test of its own before this task, on either the old or the new path.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_symbology_answers_with_a_symbols_map(client: TestClient) -> None:
    response = client.get("/api/symbology")
    assert response.status_code == 200
    body = response.json()
    assert "symbols" in body
    assert isinstance(body["symbols"], dict)


def test_the_old_ui_prefixed_symbology_route_is_gone(client: TestClient) -> None:
    """Proves the move happened rather than the route being merely
    duplicated at the new path - the same T5 precedent
    (`test_decks_api.test_the_old_ui_prefixed_deck_routes_are_gone`): the
    catch-all static mount at `/` finds no matching file for a GET here, so
    it 404s rather than answering."""
    assert client.get("/api/ui/symbology").status_code == 404
