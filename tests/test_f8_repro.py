from __future__ import annotations

from fastapi.testclient import TestClient

from magic_cards_model_api import affordances
from magic_cards_model_api.store import store


def test_repro_candidate_label_prints_a_face_down_cards_printed_name(
    client: TestClient, game: str
) -> None:
    """FINDING 8: candidate_line prints a face-down card's printed name."""
    from conftest import add_card

    add_card(client, game, "Grizzly Bears", "bob", "battlefield")
    add_card(client, game, "Lightning Bolt", "alice", "hand")
    live = store.get(game)
    bears = next(
        s for s in live.shared_zones[list(live.shared_zones)[0]] if s.card.name == "Grizzly Bears"
    )
    bears.face_up = False

    label = affordances._label(bears)
    line = affordances.candidate_line(bears)
    print("LABEL:", label)
    print("LINE:", line)
    assert label["name"] != "Grizzly Bears", label
    assert "Grizzly Bears" not in line, line

    response = client.get(
        f"/api/games/{game}/legal-actions",
        params={
            "player": "alice",
            "candidates": "true",
            "uid": next(u for u, p in store.seat_uids(game).items() if p == "alice"),
        },
    )
    print("LEGAL ACTIONS STATUS:", response.status_code)
    assert "Grizzly Bears" not in response.text, "printed name of a face-down card in candidates"
