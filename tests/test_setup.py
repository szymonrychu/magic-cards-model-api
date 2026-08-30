"""Board setup: zones, life, counters, mana pool, tapping, steps, moves."""

from conftest import add_card, find, state


def test_put_card_in_zones(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "graveyard")
    board = state(client, game)
    assert [card["name"] for card in board["battlefield"]] == ["Mountain"]
    assert board["battlefield"][0]["controller"] == "alice"
    assert board["battlefield"][0]["owner"] == "alice"
    assert [card["name"] for card in board["hands"]["alice"]] == ["Lightning Bolt"]
    assert [card["name"] for card in board["graveyards"]["bob"]] == ["Grizzly Bears"]
    assert board["hands"]["alice"][0]["image"]


def test_library_positions(client, game):
    add_card(client, game, "Mountain", "alice", "library", position="top")
    add_card(client, game, "Lightning Bolt", "alice", "library", position="bottom")
    board = state(client, game)
    library = [card["name"] for card in board["libraries"]["alice"]]
    assert library == ["Mountain", "Lightning Bolt"]


def test_set_life_and_counters(client, game):
    client.patch(f"/api/games/{game}/players/bob", json={"life": 3, "counters": {"poison": 4}})
    board = state(client, game)
    bob = next(player for player in board["players"] if player["username"] == "bob")
    assert bob["life"] == 3
    assert bob["counters"] == {"poison": 4}


def test_mana_pool(client, game):
    client.put(f"/api/games/{game}/players/alice/mana-pool", json={"add": {"R": 2, "G": 1}})
    pool = state(client, game)["players"][0]["mana_pool"]["floating"]
    assert pool == {"R": 2, "G": 1}
    client.put(f"/api/games/{game}/players/alice/mana-pool", json={"set": {"U": 3}})
    pool = state(client, game)["players"][0]["mana_pool"]["floating"]
    assert pool == {"U": 3}
    client.put(f"/api/games/{game}/players/alice/mana-pool", json={"empty": True})
    assert state(client, game)["players"][0]["mana_pool"]["floating"] == {}


def test_mana_pool_rejects_unknown_color(client, game):
    response = client.put(f"/api/games/{game}/players/alice/mana-pool", json={"add": {"Q": 1}})
    assert response.status_code == 400


def test_tap_and_untap(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    mountain = find(state(client, game)["battlefield"], "Mountain")

    tapped = client.post(f"/api/games/{game}/cards/{mountain['id']}/tap", json={"tapped": True})
    assert tapped.json()["allowed"] is True
    assert state(client, game)["battlefield"][0]["tapped"] is True

    again = client.post(f"/api/games/{game}/cards/{mountain['id']}/tap", json={"tapped": True})
    assert again.json()["allowed"] is False
    assert again.json()["reason"] == "Mountain is already tapped"

    untapped = client.post(f"/api/games/{game}/cards/{mountain['id']}/tap", json={"tapped": False})
    assert untapped.json()["allowed"] is True
    assert state(client, game)["battlefield"][0]["tapped"] is False


def test_tapping_a_hand_card_is_refused(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = state(client, game)["hands"]["alice"][0]
    response = client.post(f"/api/games/{game}/cards/{bolt['id']}/tap", json={"tapped": True})
    assert response.json()["allowed"] is False
    assert "only battlefield cards tap" in response.json()["reason"]


def test_move_card_between_zones(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = state(client, game)["hands"]["alice"][0]
    moved = client.post(
        f"/api/games/{game}/cards/{bolt['id']}/move",
        json={"to_zone": "graveyard", "player": "bob"},
    )
    assert moved.json()["allowed"] is True
    board = state(client, game)
    assert [card["name"] for card in board["graveyards"]["bob"]] == ["Lightning Bolt"]
    assert board["hands"]["alice"] == []


def test_set_step_forward_and_rewind(client, game):
    client.put(f"/api/games/{game}/step", json={"step": "declare_attackers"})
    assert state(client, game)["step"] == "declare_attackers"
    client.put(f"/api/games/{game}/step", json={"step": "draw"})
    assert state(client, game)["step"] == "draw"


def test_counters_on_a_card(client, game):
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    bears = find(state(client, game)["battlefield"], "Grizzly Bears")
    client.patch(
        f"/api/games/{game}/cards/{bears['id']}", json={"counters": {"+1/+1": 2}, "damage": 1}
    )
    patched = find(state(client, game)["battlefield"], "Grizzly Bears")
    assert patched["counters"] == {"+1/+1": 2}
    assert patched["damage"] == 1


def test_summoning_sick_reports_the_rule_not_the_raw_flag(client, game):
    """The serializer reads CardState.is_summoning_sick (CR 302.6 binds
    creatures only): a land the turn it arrives reports False, a creature
    reports True, and patching the raw flag still drives the creature's
    answer."""
    add_card(client, game, "Forest", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "battlefield")
    board = state(client, game)
    assert find(board["battlefield"], "Forest")["summoning_sick"] is False
    bears = find(board["battlefield"], "Grizzly Bears")
    assert bears["summoning_sick"] is True

    client.patch(f"/api/games/{game}/cards/{bears['id']}", json={"summoning_sick": False})
    patched = find(state(client, game)["battlefield"], "Grizzly Bears")
    assert patched["summoning_sick"] is False
