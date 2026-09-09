"""Card lookup: real Scryfall printings from the recorded fixtures, engine-validated."""


def test_lookup_lightning_bolt(client):
    result = client.get("/api/cards/Lightning%20Bolt").json()
    assert result["ok"] is True
    assert result["name"] == "Lightning Bolt"
    assert result["modern_legal"] is True
    assert result["card"]["mana_cost"] == "{R}"
    assert "image" not in result or isinstance(result["images"], dict)
    # This api's own asset proxy, never Scryfall directly - see
    # `tests/test_asset_proxy.py` for the full "no scryfall.io leak" and
    # proxy-route coverage.
    assert result["images"]["normal"].startswith("/api/card-art?name=")


def test_lookup_validates_through_the_engine(client):
    result = client.get("/api/cards/Mountain").json()
    assert result["ok"] is True
    assert result["card"]["type_line"] == "Basic Land — Mountain"
    assert result["card"]["produced_mana"] == ["R"]


def test_unknown_card_is_404(client):
    assert client.get("/api/cards/Zzzz%20Nope").status_code == 404


def test_lookup_by_query_param(client):
    result = client.get("/api/cards", params={"name": "Grizzly Bears"}).json()
    assert result["ok"] is True
    assert result["card"]["power"] == "2"
