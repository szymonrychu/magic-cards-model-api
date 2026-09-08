"""The lobby side's seam onto the api component's HTTP surface.

`decks_api.start_game_from_decklists` used to import `validate_decklist_text`
straight out of the same module that now lives in `api_decks.py`
(`magic_cards_model.models.{Card, Deck, DeckIllegal}` plus `scryfall.py` -
see that module's docstring for why those must not travel into the lobby).
2b's T5 (map Finding 1) replaces that import with a call through `ApiClient`,
so the lobby-owned caller depends on a protocol, not on the api component's
internals.

Pre-cut, "the api" is this same process, so `InProcessApiClient` calls
`api_decks.validate_decklist_text` directly - no HTTP round trip, and nothing
for the single-process test suite to route around. Post-cut (stage B of the
2b plan), an `HttpApiClient` built on `httpx` - not part of this task -
implements the same protocol against the deployed api component's URL;
`start_game_from_decklists` depends on `ApiClient` and never has to change
when that swap happens.
"""

from __future__ import annotations

from typing import Any, Protocol

from . import api_decks


class ApiClient(Protocol):
    """What the lobby needs from the api component. One method today - deck
    resolution and legality (map Finding 1) - the seam future lobby-side
    imports of api internals replace the same way."""

    def validate_decklist(self, text: str) -> dict[str, Any]:
        """Same shape `POST /api/decks/validate` returns: `valid`, `errors`,
        `warnings`, and - only once parsing got far enough to count cards -
        `mainboard_size`/`sideboard_size`."""
        ...


class InProcessApiClient:
    """The only implementation this task ships. Calls the api component's
    own validation function directly, in this process - correct for as long
    as lobby and api are one deployable, which is true until stage B's cut."""

    def validate_decklist(self, text: str) -> dict[str, Any]:
        return api_decks.validate_decklist_text(text)
