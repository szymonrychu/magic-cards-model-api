"""What `GET /api/games/{id}/log` is allowed to read.

Every `records` row carries a full `snapshot(game)` plus the acting seat's
whole `legal_actions_before` enumeration - measured at 11,538 bytes on a
28-permanent board by `tests/test_action_answer_size.py`. A poller that asks
"what is newer than seq N" and gets served by loading all N+k rows and then
throwing N of them away in Python pays for the entire game's history on
every tick, and pays more of it the longer the game runs.

So the route reads what it returns, and nothing else. The two properties
that pins down - it never loads the whole ledger, and its cost tracks the
window rather than the game - are the first two tests here.

The third is the one that makes the other two safe to have. `last_seq` must
stay the *true* latest seq even when the answer is empty: it is the
watermark a client resumes from, and a bounded read that computes it from
the rows it happened to fetch would answer 0 for "nothing new" and send
every poller back to the start of the game. `ui/app.js` keeps
`lastSeq: log.last_seq` from every poll, so getting this wrong does not
break loudly - it silently re-fetches, or silently stops fetching.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from magic_cards_model_api.store import store


def _boom(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    raise AssertionError("the log route must never load the whole ledger")


def test_the_log_route_never_loads_the_whole_ledger(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, game: str
) -> None:
    """`store.records()` - the unbounded read - is off limits to this route,
    with or without a `since`. Patched on the singleton itself, so every
    module that bound `store` at import sees the same refusal."""
    monkeypatch.setattr(store, "records", _boom)
    fresh = client.get(f"/api/games/{game}/log")
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["log"], fresh.json()
    resumed = client.get(f"/api/games/{game}/log", params={"since": 1})
    assert resumed.status_code == 200, resumed.text


@pytest.mark.parametrize("size", [10, 500, 5_000])
def test_the_read_tracks_the_window_not_the_ledger(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, game: str, size: int
) -> None:
    """The cost test. A 5,000-row game must cost a resuming poller exactly
    what a 10-row game costs it: the rows it is actually handed."""
    for _n in range(size):
        store.append_record(game, {"action": "pass_priority", "detail": {"player": "alice"}})
    watermark = client.get(f"/api/games/{game}/log").json()["last_seq"]
    store.append_record(game, {"action": "pass_priority", "detail": {"player": "bob"}})

    rows_read = 0
    real_since = store.records_since

    def counting(game_id: str, since: int) -> list[dict[str, Any]]:
        nonlocal rows_read
        rows = real_since(game_id, since)
        rows_read += len(rows)
        return rows

    monkeypatch.setattr(store, "records", _boom)
    monkeypatch.setattr(store, "records_since", counting)
    answer = client.get(f"/api/games/{game}/log", params={"since": watermark}).json()

    assert [entry["seq"] for entry in answer["log"]] == [watermark + 1]
    assert answer["last_seq"] == watermark + 1
    assert rows_read == 1


def test_last_seq_is_the_true_latest_when_nothing_is_newer(client: TestClient, game: str) -> None:
    """The watermark a caught-up poller resumes from.

    An empty `log` must still carry the real latest seq, not 0 and not
    whatever the bounded read happened to see - otherwise the very next poll
    asks `since=0` and is served the entire game again.
    """
    full = client.get(f"/api/games/{game}/log").json()
    last = full["last_seq"]
    assert last == full["log"][-1]["seq"]

    caught_up = client.get(f"/api/games/{game}/log", params={"since": last}).json()
    assert caught_up["log"] == []
    assert caught_up["last_seq"] == last

    # And again from further ahead than anything recorded: a client that
    # somehow holds a seq beyond the ledger is told the truth, not its own
    # number back.
    ahead = client.get(f"/api/games/{game}/log", params={"since": last + 50}).json()
    assert ahead["log"] == []
    assert ahead["last_seq"] == last


def test_last_seq_walks_forward_with_each_new_row(client: TestClient, game: str) -> None:
    """The incremental-fetch loop itself, three polls deep: every entry is
    delivered exactly once, and no entry is ever delivered twice."""
    seen: list[int] = []
    watermark = 0
    for _ in range(3):
        answer = client.get(f"/api/games/{game}/log", params={"since": watermark}).json()
        seen.extend(entry["seq"] for entry in answer["log"])
        assert answer["last_seq"] >= watermark
        watermark = answer["last_seq"]
        store.append_record(game, {"action": "pass_priority", "detail": {"player": "alice"}})

    assert seen == sorted(set(seen))
    assert seen == list(range(1, seen[-1] + 1))
