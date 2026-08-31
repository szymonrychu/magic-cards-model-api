"""Casting: allowed, refused with the engine's own reason, and resolved."""

from conftest import add_card, find, life_of, state


def bolt_id(client: str, game: str) -> str:
    return state(client, game)["hands"]["alice"][0]["id"]


def test_cast_allowed_and_resolves(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")
    bolt = board["hands"]["alice"][0]

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "targets": ["bob"],
            "mana_sources": [mountain["id"]],
        },
    )
    cast_body = cast.json()
    assert isinstance(cast_body.pop("log_seq"), int)  # the audit entry's own idempotency marker
    assert cast_body == {
        "allowed": True,
        "reason": None,
        "spell_id": bolt["id"],
        "cost": "{R}",
    }

    stack = state(client, game)["stack"]
    assert stack[0]["kind"] == "spell"
    assert stack[0]["name"] == "Lightning Bolt"
    assert stack[0]["targets"] == ["bob"]
    assert stack[0]["controller"] == "alice"

    resolved = client.post(f"/api/games/{game}/resolve-top").json()
    assert resolved["resolved"] == "Lightning Bolt"
    final = state(client, game)
    assert life_of(final, "bob") == 17
    assert [card["name"] for card in final["graveyards"]["alice"]] == ["Lightning Bolt"]
    assert final["stack"] == []


def test_dry_run_refused_without_mana(client, game):
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = state(client, game)["hands"]["alice"][0]
    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": bolt["id"], "targets": ["bob"], "dry_run": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] is False
    assert body["reason"] == "pool {} cannot pay {R}"
    assert body["cost"] == "{R}"
    # nothing changed: no spell, no tapped land
    assert state(client, game)["stack"] == []


def test_failed_cast_leaves_nothing_tapped(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    mountain = find(state(client, game)["battlefield"], "Mountain")

    cast = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card": "Grizzly Bears", "mana_sources": [mountain["id"]]},
    )
    assert cast.json()["allowed"] is False
    # The old message quoted a raw pool repr (`{<Color.RED: 'R'>: 1}`); the
    # engine's auto-tap solver now refuses with a plain sentence naming the
    # cost that could not be paid.
    assert cast.json()["reason"] == "no combination of the available mana sources can pay {1}{G}"
    board = state(client, game)
    assert board["stack"] == []
    assert find(board["battlefield"], "Mountain")["tapped"] is False
    assert [card["name"] for card in board["hands"]["alice"]] == ["Grizzly Bears"]


def test_an_allowed_dry_run_leaves_the_stack_clean(client, game):
    """The mirror of the refused dry run above, and the one nothing covered.

    CR 601.2a moves the card to the stack as the cast is *proposed*, which
    happens before anything knows whether the cost can be paid - so an
    affordable probe puts the card on the stack exactly as an unaffordable
    one does, and has exactly as much to rewind (CR 601.6, CR 733.1). This
    path is the hot one: `seatkit.available_actions` dry-runs every card in
    hand on every poll, and the castable ones are the ones that come back
    `allowed: true`.
    """
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")
    bolt = find(board["hands"]["alice"], "Lightning Bolt")

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "targets": ["bob"],
            "mana_sources": [mountain["id"]],
            "dry_run": True,
        },
    )
    body = response.json()
    assert body["allowed"] is True
    assert body["cost"] == "{R}"

    after = state(client, game)
    assert after["stack"] == []
    assert find(after["battlefield"], "Mountain")["tapped"] is False
    assert [card["name"] for card in after["hands"]["alice"]] == ["Lightning Bolt"]
    # The probe may be repeated forever: the second answer is the first one.
    again = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "targets": ["bob"],
            "mana_sources": [mountain["id"]],
            "dry_run": True,
        },
    ).json()
    assert again["allowed"] is True
    assert state(client, game)["stack"] == []


def test_an_announcement_that_raises_leaves_nothing_on_the_stack(client, game):
    """Exception safety for CR 601.2a's move.

    A refusal is a `ValueError` the engine raises deliberately; this is the
    other kind of exit - an announcement that blows up part way through. The
    request names an `alternative_cost` and, as the objects it is paid with
    (`choosing`), a card id that is in no zone of this game, so `find_card`
    raises `KeyError` while CR 601.2b is being announced and after CR 601.2a
    has already moved the card to the stack. The card must be back in hand
    regardless: CR 733.1 reverses an action that was started and could not be
    completed, and it does not care which exception carried the news.
    """
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Subtlety", "alice", "hand")  # prints an evoke cost
    subtlety = find(state(client, game)["hands"]["alice"], "Subtlety")
    missing = "00000000-0000-4000-8000-000000000000"

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": subtlety["id"],
            "alternative_cost": 0,
            "choosing": [missing],
        },
    )
    # The KeyError reaches the app's own handler rather than being swallowed:
    # the caller asked with a card id that does not exist and should be told.
    assert response.status_code == 404, response.text

    after = state(client, game)
    assert after["stack"] == []
    assert [card["name"] for card in after["hands"]["alice"]] == ["Subtlety"]


def test_cast_refused_off_timing(client, game):
    client.put(f"/api/games/{game}/step", json={"step": "declare_attackers"})
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    response = client.post(
        f"/api/games/{game}/actions/cast", json={"player": "alice", "card": "Grizzly Bears"}
    )
    body = response.json()
    assert body["allowed"] is False
    assert "main phase" in body["reason"]


def test_cast_refused_off_timing_reason_is_the_engine_sentence_exactly(client, game):
    """The refusal above ("Grizzly Bears is not an instant and has no
    flash...") is raised inside `Casting.model_post_init`, a pydantic
    validator - so pydantic wraps it into a `ValidationError` before
    `engine_api.cast_card` ever sees it. `ValidationError.__str__` is
    pydantic's own multi-line report ("1 validation error for Casting", the
    raw `repr()` of `input_value`, a docs URL) and not the sentence the
    engine actually wrote; a full-string assertion is the only way to catch
    that framing coming back, since a substring check like
    `"main phase" in reason` passes whether or not the wrapper is still
    there.
    """
    client.put(f"/api/games/{game}/step", json={"step": "declare_attackers"})
    add_card(client, game, "Grizzly Bears", "alice", "hand")
    response = client.post(
        f"/api/games/{game}/actions/cast", json={"player": "alice", "card": "Grizzly Bears"}
    )
    body = response.json()
    assert body["reason"] == (
        "Grizzly Bears is not an instant and has no flash, so alice can only "
        "cast it during their own main phase with the stack empty "
        "(CR 117.1a, CR 702.8a)"
    )
    for leak in ("validation error", "input_value", "pydantic.dev", "type=value_error"):
        assert leak not in body["reason"]


def test_cast_refused_with_wrong_target_kind(client, game):
    add_card(client, game, "Mountain", "alice", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    board = state(client, game)
    mountain = find(board["battlefield"], "Mountain")
    bolt = board["hands"]["alice"][0]

    response = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt["id"],
            "targets": ["not-a-player-nor-id"],
            "mana_sources": [mountain["id"]],
        },
    )
    body = response.json()
    assert body["allowed"] is False
    assert "neither a player name nor a card id" in body["reason"]


def test_cast_from_empty_hand_is_refused(client, game):
    response = client.post(
        f"/api/games/{game}/actions/cast", json={"player": "alice", "card": "Lightning Bolt"}
    )
    body = response.json()
    assert body["allowed"] is False
    assert body["reason"] == "no Lightning Bolt in alice's hand"
