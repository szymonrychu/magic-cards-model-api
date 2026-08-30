"""A malformed request body must be answered, not crash the handler.

The registered `RequestValidationError` handler used to call
`exc.errors(include_url=False, include_input=False)` - kwargs pydantic 2.13
removed from `errors()` - so every bad body became a raw 500 with no JSON, and
the frontend logged an opaque `HTTP 500` instead of what was wrong.
"""


def test_a_malformed_body_is_a_422_not_a_500(client):
    response = client.post(
        "/api/games",
        json={"players": [{"username": 42}]},  # username must be a string
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert all(set(error) == {"loc", "msg", "type"} for error in detail)


def test_an_unknown_field_name_is_a_422_not_a_500(client, game):
    response = client.post(
        f"/api/games/{game}/cards",
        json={"name": "Mountain", "player": "alice", "zone": "hand"},  # `card`, not `name`
    )
    assert response.status_code == 422, response.text
    assert "card" in str(response.json()["detail"])
