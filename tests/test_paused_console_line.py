"""Defect 1 (2026-09-08): a paused command printed as `refused: None`.

The owner's compose log was full of `pass_priority -> refused: None`. There
was no refusal: `app.action_pass_priority` writes three shapes for `allowed`
- `True`, `False` with a `reason`, and `None` with `paused: True` and no
`reason` at all - but `audit._console_line`'s `if detail["allowed"]:` had
only two arms, so `None` fell into the refusal one and `detail.get("reason")`
read back `None`.

The fix gives the console line a real third arm, and says what the pause is
actually waiting on - whose decision, and what kind - the same information
already on the wire in the paused response's own `pending_decision`, threaded
into the persisted row as `detail["paused_on"]` so a reader of the log (not
just a live console) can see it too.

Everything here drives a real pause through the real HTTP routes
(`test_pause_slot_clobber.crack_the_strand`, already used by
`test_pause_generation.py` the same way) and reads the real persisted log
row and the real console output - never a hand-built entry.
"""

from __future__ import annotations

from test_container_logging import capturing
from test_pause_slot_clobber import crack_the_strand

from magic_cards_model_api import audit


def entry_for(client, game_id: str, action: str) -> dict:
    log = client.get(f"/api/games/{game_id}/log").json()["log"]
    matches = [entry for entry in log if entry["action"] == action]
    assert matches, [entry["action"] for entry in log]
    return matches[-1]


def test_a_paused_pass_priority_console_line_is_never_reported_as_a_refusal(client, game):
    with capturing("mcm.audit") as printed:
        paused = crack_the_strand(client, game)
    assert paused["paused"] is True, paused

    lines = [
        record.getMessage() for record in printed.records if "pass_priority" in record.getMessage()
    ]
    assert lines, printed.text
    line = lines[-1]
    assert "refused" not in line, line
    assert "-> refused: None" not in line, line
    assert "paused" in line, line


def test_the_paused_console_line_names_who_and_what_it_paused_on(client, game):
    with capturing("mcm.audit") as printed:
        paused = crack_the_strand(client, game)
    pending = paused["pending_decision"]

    line = [
        record.getMessage() for record in printed.records if "pass_priority" in record.getMessage()
    ][-1]
    assert pending["player"] in line, line
    assert pending["kind"] in line, line


def test_a_genuine_paused_row_persists_the_pending_decisions_player_and_kind(client, game):
    """Assert on the real shape of a genuine `allowed=True`/`paused=True`
    row, not a hand-built restatement of it - the row this test reads is the
    one `crack_the_strand`'s real pass-priority call actually wrote.

    FD5 (3): a paused row is recorded as a *committing* row - `allowed:
    True` - since the command ran far enough to be asked a real question.
    The HTTP response's own `allowed: None` is untouched (see
    `test_pause_slot_clobber.py`); only the persisted ledger row changed
    shape."""
    paused = crack_the_strand(client, game)
    pending = paused["pending_decision"]

    entry = entry_for(client, game, "pass_priority")
    assert entry["detail"]["allowed"] is True, entry
    assert entry["detail"]["paused"] is True, entry
    assert entry["detail"]["paused_on"]["player"] == pending["player"], entry
    assert entry["detail"]["paused_on"]["kind"] == pending["kind"], entry

    # The console line, rebuilt from the persisted row itself (a replayed
    # read, exercising the same function a live emission goes through) says
    # the same thing a live capture already proved above.
    line = audit._console_line(entry)
    assert "refused" not in line, line
    assert pending["player"] in line, line
    assert pending["kind"] in line, line


def test_an_allowed_true_row_still_says_allowed(client, game):
    """Regression: the fix must not disturb the other two arms."""
    from conftest import add_card, state

    add_card(client, game, "Mountain", "alice", "battlefield")
    mountain = next(c for c in state(client, game)["battlefield"] if c["name"] == "Mountain")
    client.post(
        f"/api/games/{game}/actions/activate",
        json={"player": "alice", "card_id": mountain["id"]},
    )
    entry = entry_for(client, game, "activate")
    assert entry["detail"]["allowed"] is True
    line = audit._console_line(entry)
    assert line.endswith("-> allowed") or " -> allowed" in line
    assert "refused" not in line


def test_a_refused_row_still_says_refused_with_the_reason(client, game):
    """Regression: an ordinary refusal (`allowed: False`) must still print
    its reason, not the paused wording."""
    from conftest import add_card, state

    add_card(client, game, "Lightning Bolt", "alice", "hand")
    bolt = state(client, game)["hands"]["alice"][0]
    client.post(
        f"/api/games/{game}/actions/cast",
        json={"player": "alice", "card_id": bolt["id"], "targets": ["bob"]},
    )
    entry = entry_for(client, game, "cast")
    assert entry["detail"]["allowed"] is False
    line = audit._console_line(entry)
    assert "-> refused:" in line
    assert entry["detail"]["reason"] in line
