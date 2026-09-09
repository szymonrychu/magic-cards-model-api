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

from . import api_decks, serialize
from .repository import GameUnloadable
from .store import known_players, store


class ApiClient(Protocol):
    """What the lobby needs from the api component. Deck resolution and
    legality (map Finding 1); and, since 2b's T8 (map's S2), one bulk game-
    summary read - the seam future lobby-side imports of api internals
    replace the same way."""

    def validate_decklist(self, text: str) -> dict[str, Any]:
        """Same shape `POST /api/decks/validate` returns: `valid`, `errors`,
        `warnings`, and - only once parsing got far enough to count cards -
        `mainboard_size`/`sideboard_size`."""
        ...

    def game_summaries(self, game_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Same per-game shape `GET /api/games/summary?ids=...` returns
        (`schemas.GameSummaryScalars`), keyed by game id - unknown ids are
        simply absent from the result, not an error. S2's fix for
        `list_lobby_games`'s old per-game `serialize.state_view` N+1: one
        call for every id a poll asks about, not one per game."""
        ...


class InProcessApiClient:
    """The only implementation this task ships. Calls the api component's
    own functions directly, in this process - correct for as long as lobby
    and api are one deployable, which is true until stage B's cut."""

    def validate_decklist(self, text: str) -> dict[str, Any]:
        return api_decks.validate_decklist_text(text)

    def game_summaries(self, game_ids: list[str]) -> dict[str, dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        for game_id in game_ids:
            # Per game, never around the loop: one row this build cannot
            # decode must cost exactly that row, not the whole answer. It
            # is still answered for - with `unloadable` set - rather than
            # dropped, matching `app.game_summaries_route`, which is the
            # route this stands in for until the cut.
            try:
                game = store.get(game_id)
            except GameUnloadable as exc:
                summaries[game_id] = serialize.unloadable_game_summary(
                    game_id, exc.reason, seat_count=len(known_players(game_id))
                )
                continue
            if game is None:
                continue
            summaries[game_id] = serialize.game_summary(game, game_id)
        return summaries
