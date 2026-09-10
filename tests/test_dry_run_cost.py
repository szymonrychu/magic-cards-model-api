"""What a dry run is allowed to cost.

A dry run is a probe, not an action: `audit_outcome` returns before
`audit.record` for it (`log_seq: None`), and `_run_command` skips
`store.replace` outright, so a dry run writes no `records` row at all.
`legal_actions_before` exists to be written into that row - it is hidden
information handed back only to the seat it was enumerated for, through
`visibility.redact_log_entry`, and it appears in no response body. With no
row, there is nothing for it to be written into and nobody to read it: the
whole enumeration was computed inside the per-game lock and thrown away.

That enumeration is not cheap. `machines.legal_actions` does a mana-candidate
scan of the battlefield, then cast/land/marked-exile/off-zone enumeration for
every card in hand, graveyard and shared exile, then `computed_board()` for
the full CR 613 layer stack with a per-permanent ability probe on top. The UI
fires one dry run per candidate before every real cast, so this is the
per-keystroke cost of the play surface, serialised behind the lock that every
other seat's real action is also waiting on.

The ledger consumers were checked before this was removed, since this
project's audit log feeds game review and agent training: `ui/review_model.js`
reads `review.entries` (action/detail/reasoning/snapshot/seq) and never
mentions `legal_actions_before` or dry runs; `visibility.redact_log_entry`
and `seatkit`'s recent-log view read the field off rows that exist; and no
dry-run row has ever existed for any of them to read. Removing the
computation removes nothing any consumer can observe - which is what the
first test here pins.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import _ADMIN_KEYS, card_id
from fastapi.testclient import TestClient

from magic_cards_model_api import records


def _counting(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Records the `player` of every `legal_actions_before` call, real answer
    still returned."""
    calls: list[str | None] = []
    real = records.legal_actions_before

    def spy(game: Any, player: str | None, pending: Any) -> Any:
        calls.append(player)
        return real(game, player, pending)

    monkeypatch.setattr(records, "legal_actions_before", spy)
    return calls


def test_a_dry_run_cast_enumerates_no_legal_actions(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, game: str
) -> None:
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")
    mountain = card_id(client, game, "Mountain", "alice", "battlefield")
    calls = _counting(monkeypatch)

    answer = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt,
            "targets": ["bob"],
            "mana_sources": [mountain],
            "dry_run": True,
        },
    )

    assert answer.status_code == 200, answer.text
    assert answer.json()["allowed"] is True, answer.text
    assert answer.json()["log_seq"] is None, "a dry run still writes no ledger row"
    assert calls == [], "a probe that writes no row must not enumerate for one"


def test_a_refused_dry_run_enumerates_no_legal_actions(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, game: str
) -> None:
    """The common case, and the expensive one: the UI probes every card in
    hand, and most of them come back refused."""
    counterspell = card_id(client, game, "Counterspell", "alice", "hand")
    calls = _counting(monkeypatch)

    answer = client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": counterspell, "dry_run": True},
    )

    assert answer.status_code == 200, answer.text
    assert answer.json()["allowed"] is False, answer.text
    assert calls == []


def test_a_real_cast_still_enumerates_and_records_them(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, game: str
) -> None:
    """The other half: nothing about the *recorded* path changes. The row a
    real cast writes still carries the actor's enumeration, which is what
    review and training read."""
    bolt = card_id(client, game, "Lightning Bolt", "alice", "hand")
    mountain = card_id(client, game, "Mountain", "alice", "battlefield")
    calls = _counting(monkeypatch)

    answer = client.post(
        f"/api/games/{game}/actions/cast",
        json={
            "player": "alice",
            "card_id": bolt,
            "targets": ["bob"],
            "mana_sources": [mountain],
        },
    )

    assert answer.status_code == 200, answer.text
    assert answer.json()["allowed"] is True, answer.text
    assert calls == ["alice"]

    # The admin key the `game` fixture stashed: the only viewer
    # `redact_log_entry` hands a `legal_actions_before` it did not enumerate.
    log = client.get(f"/api/games/{game}/log", params={"uid": _ADMIN_KEYS[game]}).json()
    cast_rows = [entry for entry in log["log"] if entry["action"] == "cast"]
    assert cast_rows, log
    assert cast_rows[-1]["legal_actions_before"], cast_rows[-1]
    assert cast_rows[-1]["legal_actions_for"] == "alice"
