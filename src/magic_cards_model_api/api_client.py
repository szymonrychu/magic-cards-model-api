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


#: Where a seat talks to the api when nobody says otherwise - the same
#: default `seatkit.DEFAULT_API` uses, repeated rather than imported so this
#: module stays free of the transports.
DEFAULT_API = "http://127.0.0.1:8000"


class SeatApiClient(Protocol):
    """What a *seat* needs from the api component, as opposed to what the
    lobby needs (`ApiClient`, above).

    `baseline_seat.py`'s policy is driven by `GET .../legal-actions` - the
    engine's own enumeration of what this seat may do (CR 118.6 keeps an
    unpayable action legal, so `affordable` rather than absence is what says
    it cannot be paid for) - plus this seat's own filtered board view for the
    two judgements the enumeration does not make (whether an attacker
    survives a block, whether a hit is lethal), plus one way to act, plus the
    candidate list for a target.

    That last one is a fourth call rather than a fifth field on the
    enumeration for the api's own stated reason: `Action.target_slots`
    carries a requirement's *shape*, and walking every object on the board
    against every requirement is "the expensive half of `affordances.py`"
    (its own docstring), served on demand instead of on a poll. Guessing a
    target off the board rather than asking would put refusals in the ledger
    that the scorecard would then report as rules errors nobody made.

    The protocol exists so the policy depends on a seam and not on `httpx`:
    the test suite drives it against the in-process app through the same
    interface a deployed baseline uses over HTTP, which is what keeps "the
    baseline plays a real game" from being a claim about a fake.
    """

    def game_state(self, game_id: str, uid: str) -> dict[str, Any]:
        """This seat's own filtered view of the board - what
        `GET /api/games/{id}?uid=...` returns (`schemas.GameStateView`),
        already redacted by the server."""
        ...

    def legal_actions(self, game_id: str, player: str, uid: str) -> list[dict[str, Any]]:
        """`GET /api/games/{id}/legal-actions?player=...&uid=...`'s `actions`
        - one `models.action.Action` per legal move, `model_dump(mode="json")`
        -ed. Empty means "nothing is legal"; a call that could not be made at
        all raises rather than answering empty, because the two must never
        look alike (see `seatkit.available_actions` for the same rule)."""
        ...

    def affordances(self, game_id: str, card_id: str, player: str, uid: str) -> dict[str, Any]:
        """`GET /api/games/{id}/affordances/{card_id}?player=...` - what
        announcing this card asks for, and every object on the board that
        could legally fill each requirement (`affordances.candidates`, built
        from the engine's own `TargetRequirement.rejects`). Nothing in the
        answer is hidden information: every candidate is in a public zone
        (CR 400.2) or is a player."""
        ...

    def act(self, game_id: str, path: str, body: dict[str, Any], uid: str) -> dict[str, Any]:
        """One mutating call, `path` relative to `/api/games/{id}/` (e.g.
        `"actions/play-land"`). Returns the route's own JSON answer; a non-2xx
        comes back in this api's own refusal shape (`allowed: False` plus
        `reason`) rather than raising, so a caller reads one thing whether the
        engine refused or the transport did."""
        ...


def _refusal(status_code: int, parsed: Any, text: str) -> dict[str, Any]:
    """A non-2xx answer in the shape every refusal on this api already takes.

    `records.MissingReasoning`/`MissingPlan` answer 422 with a pydantic-shaped
    `detail` list, `ValueError` answers 400 with a string `detail`, and a 500
    may carry no JSON at all - all three have to read as "this did not
    happen, and here is why", or a caller counts a refused action as a played
    one. `seatkit._normalise_non_2xx` does the same job for the seat
    transports; this is the same rule at this seam, kept here so `api_client`
    does not import a transport module.
    """
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if isinstance(detail, list):
        reason = "; ".join(
            str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail
        )
    elif detail is not None:
        reason = str(detail)
    else:
        reason = text.strip() or f"HTTP {status_code}"
    return {"allowed": False, "reason": f"HTTP {status_code}: {reason}", "http_status": status_code}


class HttpSeatApiClient:
    """`SeatApiClient` over HTTP - the implementation this module's own
    docstring anticipated ("an `HttpApiClient` built on `httpx` ... against
    the deployed api component's URL"), for the seat half of the surface.

    `http` takes any client with `httpx.Client`'s `get`/`post` signature, so
    the tests hand it a `fastapi.testclient.TestClient` and drive the real
    app in-process without a socket. Given none, one is built against `api`.
    """

    def __init__(
        self,
        api: str = DEFAULT_API,
        *,
        http: Any | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api = api.rstrip("/")
        if http is None:
            import httpx

            http = httpx.Client(base_url=self.api, timeout=timeout)
        self.http = http

    def game_state(self, game_id: str, uid: str) -> dict[str, Any]:
        response = self.http.get(f"/api/games/{game_id}", params={"uid": uid})
        if response.status_code != 200:
            raise RuntimeError(
                f"game state read failed: HTTP {response.status_code} {response.text}"
            )
        return dict(response.json())

    def legal_actions(self, game_id: str, player: str, uid: str) -> list[dict[str, Any]]:
        response = self.http.get(
            f"/api/games/{game_id}/legal-actions", params={"uid": uid, "player": player}
        )
        if response.status_code != 200:
            # Never an empty list: "you may not ask that" and "there is
            # nothing you can do" are different answers and a policy that
            # confused them would pass its whole game away.
            raise RuntimeError(
                f"legal-actions read failed: HTTP {response.status_code} {response.text}"
            )
        return list(response.json().get("actions") or [])

    def affordances(self, game_id: str, card_id: str, player: str, uid: str) -> dict[str, Any]:
        response = self.http.get(
            f"/api/games/{game_id}/affordances/{card_id}",
            params={"player": player, "uid": uid},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"affordances read failed: HTTP {response.status_code} {response.text}"
            )
        return dict(response.json())

    def act(self, game_id: str, path: str, body: dict[str, Any], uid: str) -> dict[str, Any]:
        response = self.http.post(
            f"/api/games/{game_id}/{path.lstrip('/')}", params={"uid": uid}, json=body
        )
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if response.status_code >= 400:
            return _refusal(response.status_code, parsed, response.text)
        if not isinstance(parsed, dict):
            return {"allowed": False, "reason": f"HTTP {response.status_code}: {response.text}"}
        return parsed
