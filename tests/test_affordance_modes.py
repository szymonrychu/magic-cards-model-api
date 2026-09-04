"""Each targeting requirement says which mode it came from (CR 700.2f).

Kozilek's Command, modes 1 and 2, has two "target player" requirements that
look identical on the wire; a client filling slots needs to know one belongs
to mode 1 and the other to mode 2. Board and card fixtures follow
`tests/test_modal_x_cast.py`.
"""

from __future__ import annotations

from conftest import add_card, card_id
from fastapi.testclient import TestClient


def boot(client: TestClient, game: str) -> str:
    """Alice holds Kozilek's Command over four Wastes, with a Memnite out."""
    command = card_id(client, game, "Kozilek's Command", "alice", "hand")
    add_card(client, game, "Memnite", "alice", "battlefield")
    for _ in range(4):
        add_card(client, game, "Wastes", "alice", "battlefield")
    return command


def test_requirements_carry_their_mode(client: TestClient, game: str) -> None:
    command = boot(client, game)
    result = client.get(
        f"/api/games/{game}/affordances/{command}",
        params={"player": "alice", "modes": "1,2", "x_value": 1},
    ).json()
    assert [r["mode"] for r in result["requirements"]] == [1, 2]
    assert all(r["mode_text"].startswith("Target player") for r in result["requirements"])
    by_id = {c["id"]: c["requirements"] for c in result["candidates"]}
    assert by_id["alice"] == [0, 1]
    assert by_id["bob"] == [0, 1]


def test_non_modal_requirements_have_no_mode(client: TestClient, game: str) -> None:
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")
    result = client.get(f"/api/games/{game}/affordances/{bolt}", params={"player": "alice"}).json()
    assert [r["mode"] for r in result["requirements"]] == [None]
    assert result["requirements"][0]["mode_text"] is None
