"""The baton: who may act for a seat.

Several agents can share one seat uid (an orchestrator with a "current" and a
"future" agent). Only the holder of the seat's current baton may use the
acting MCP tools; everyone else reads (`BATON_EXEMPT_TOOLS`) and sends plans to
the orchestrator, which rotates the baton with `pass_baton`.

Enforcement is per uid: on once a baton was issued for it, or for every uid
when the server runs with `--require-baton` (`MTG_REQUIRE_BATON=1`). With
neither, nothing is checked - the legacy, subprocess and advisor paths.

The baton lives only in the MCP process; the game API wire is unchanged.
"""

from __future__ import annotations

import hmac
import secrets
import threading
from datetime import UTC, datetime

#: The read-only tools (and the orchestrator's `pass_baton`) that never act in
#: the game, so they never need the baton.
BATON_EXEMPT_TOOLS = frozenset(
    {"battlefield_state", "game_log", "list_scripts", "report_problem", "pass_baton"}
)

BATON_OK = "ok"
BATON_REQUIRED = "BATON_REQUIRED"
BATON_STALE = "BATON_STALE"
BATON_ADVISOR = "BATON_ADVISOR"

#: `pass_baton`'s answer to an advisor (buddy) uid. On the http `MCP_URL`
#: path a buddy shares this process and its one store with the seats; its
#: advisor uid is the only credential it holds, so refusing that uid is what
#: keeps a buddy from rotating anything.
ADVISOR_REFUSAL = (
    f"{BATON_ADVISOR}: this uid is an advisor's (a buddy); an advisor has no baton and "
    "may not rotate one. Only the seat's own orchestrator calls pass_baton. Nothing changed."
)


class BatonStore:
    """Per-seat-uid batons, thread-safe.

    Prod topology assumption: the game Deployment is `replicas: 1`, so an
    http-served MCP is one process with one store; a second MCP replica would
    hold its own batons and must not be run behind a round-robin.
    """

    def __init__(self, *, require_all: bool = False) -> None:
        self.require_all = require_all
        self._current: dict[str, str] = {}
        self._rotated_at: dict[str, str] = {}
        self._lock = threading.Lock()

    def issue(self, uid: str) -> str:
        """Rotate: a fresh unguessable token for `uid`; the previous is dead."""
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._current[uid] = token
            self._rotated_at[uid] = datetime.now(UTC).isoformat(timespec="seconds")
        return token

    def enforced(self, uid: str) -> bool:
        with self._lock:
            return self.require_all or uid in self._current

    def check(self, uid: str, token: str) -> str:
        """`BATON_OK`, `BATON_REQUIRED` (no token given) or `BATON_STALE`
        (a token that is not the current one)."""
        with self._lock:
            current = self._current.get(uid)
            if not (self.require_all or current is not None):
                return BATON_OK
        if not token:
            return BATON_REQUIRED
        if current is not None and hmac.compare_digest(token, current):
            return BATON_OK
        return BATON_STALE

    def rotated_at(self, uid: str) -> str:
        with self._lock:
            return self._rotated_at.get(uid, "an unknown time")

    def holder(self, uid: str, token: str) -> str:
        """For the timing log: `-` when not enforced, `current` for a valid
        token, `reader` otherwise."""
        if not self.enforced(uid):
            return "-"
        return "current" if self.check(uid, token) == BATON_OK else "reader"


def refusal(code: str, rotated_at: str) -> str:
    """The plain tool answer for a refused acting call."""
    if code == BATON_REQUIRED:
        return (
            "BATON_REQUIRED: acting tools on this seat need the current baton; "
            "the orchestrator issues it with pass_baton. Read with battlefield_state/"
            "game_log/list_scripts and send your plan to the orchestrator."
        )
    return (
        f"BATON_STALE: this seat's turn to act moved to a newer agent at {rotated_at}; "
        "you are not the current player. Read with battlefield_state/game_log/list_scripts "
        "and send your plan to the orchestrator."
    )
