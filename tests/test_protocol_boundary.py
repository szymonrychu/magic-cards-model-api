"""2b T11: the api owns `PROTOCOL_VERSION`; `seatkit` no longer lends it one.

`app.py`'s `/api/health` used to answer with `seatkit.PROTOCOL_VERSION` - the
only remaining import in this repo pointing from the api toward the future
`magic-cards-mcp` package, and pointing the wrong way (the api is what every
other service depends on, never the reverse). `protocol.py` is a new module
the api itself owns; `seatkit.py` keeps its own copy (it cannot import the
api's package once the two are separate repos) and the two are kept in sync
by hand, the same way `agent_settings.py`'s cross-repo spellings already are
(see MEMORY.md).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from magic_cards_model_api import app as app_module
from magic_cards_model_api import protocol, seatkit
from magic_cards_model_api.main import app


def test_app_module_no_longer_imports_seatkit() -> None:
    """The reverse import (api -> mcp) is severed outright, not merely
    unused - `protocol.PROTOCOL_VERSION` replaces its only functional use."""
    assert not hasattr(app_module, "seatkit"), (
        "app.py still imports seatkit - the api must not depend on the future mcp package at all"
    )


def test_health_reports_the_apis_own_protocol_version() -> None:
    with TestClient(app) as client:
        answer = client.get("/api/health").json()
    assert answer["protocol_version"] == protocol.PROTOCOL_VERSION


def test_protocol_version_is_kept_in_sync_by_hand_between_api_and_seatkit() -> None:
    """The two are independent literals post-cut (no import can join them),
    so this is the standing pin that catches one side being bumped without
    the other - see `protocol.py`'s and `seatkit.py`'s own docstrings for
    why a hand-sync is the only option here."""
    assert protocol.PROTOCOL_VERSION == seatkit.PROTOCOL_VERSION
