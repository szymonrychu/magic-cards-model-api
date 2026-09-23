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
        `mainboard_size`/`sideboard_size`/`support_warnings` (flow 6's W2:
        the engine's `card_support` verdict per card, never a legality
        gate)."""
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
    survives a block, whether a hit is lethal), plus one way to act.

    Three calls, and the candidate list for a target is *not* a fourth: it
    rides on the enumeration itself, which is what `legal_actions`'
    `candidates` flag asks for (`app._attach_target_candidates`). There is no
    `affordances` method here on purpose - a seat that went back to `GET
    .../affordances/{card_id}` per cast paid a second round trip, and so a
    whole extra inference cycle of latency, for a list the answer it was
    already holding carried. Guessing a target off the board rather than
    reading the enumeration's own list is the other thing this seam refuses
    to make possible: it would put refusals in the ledger that the scorecard
    would then report as rules errors nobody made.

    The protocol exists so the policy depends on a seam and not on `httpx`:
    the test suite drives it against the in-process app through the same
    interface a deployed baseline uses over HTTP, which is what keeps "the
    baseline plays a real game" from being a claim about a fake.
    """

    board_seq: int | None
    """The ledger position the most recent `legal_actions` answer was
    computed against (`schemas.LegalActionsResponse.last_seq`), or `None`
    before the first one.

    An attribute rather than a second return value because it is *read*
    state, not part of the answer a policy chooses from - and the same idiom
    `seatkit.Seat.last_seq` already uses for the same number. What reads it
    is `baseline_seat.BaselineSeat.step`, which names it as
    `schemas.BoardRead.expected_seq` on the action it posts: that is what
    tells a refusal this seat collected apart from a race it lost, and
    without it every refusal of the *control* arm was unsplittable while the
    wrapper agent seat's were measured (`scripts/eval_report.py`'s `unknown`
    bucket, MEMORY.md 2026-09-12).

    It is on the protocol, not just the implementation, because a double that
    quietly lacked it would put the control arm straight back in the
    `unknown` bucket with nothing failing to say so."""

    def game_state(self, game_id: str, uid: str) -> dict[str, Any]:
        """This seat's own filtered view of the board - what
        `GET /api/games/{id}?uid=...` returns (`schemas.GameStateView`),
        already redacted by the server."""
        ...

    def legal_actions(
        self, game_id: str, player: str, uid: str, *, candidates: bool = False
    ) -> list[dict[str, Any]]:
        """`GET /api/games/{id}/legal-actions?player=...&uid=...`'s `actions`
        - one `models.action.Action` per legal move, `model_dump(mode="json")`
        -ed. Empty means "nothing is legal"; a call that could not be made at
        all raises rather than answering empty, because the two must never
        look alike (see `seatkit.available_actions` for the same rule).

        `candidates=True` adds `?candidates=true`, which folds each targeting
        action's own legal candidates into its row as `targets`
        (`schemas.ActionTargets`, built from the engine's own
        `TargetRequirement.rejects`, so it cannot disagree with the
        announcement that follows). That is how a seat names a target: out of
        the answer it already has, in one round trip rather than two. Nothing
        in it is hidden information - every candidate is an object in a
        public zone (CR 400.2) or a player - and it costs the board walk
        `affordances.py` charges for, which is why it is opt-in rather than
        always on (`app.get_legal_actions`)."""
        ...

    def act(self, game_id: str, path: str, body: dict[str, Any], uid: str) -> dict[str, Any]:
        """One mutating call, `path` relative to `/api/games/{id}/` (e.g.
        `"actions/play-land"`). Returns the route's own JSON answer; a non-2xx
        comes back in this api's own refusal shape (`allowed: False` plus
        `reason`) rather than raising, so a caller reads one thing whether the
        engine refused or the transport did.

        **"One thing" is not "the same thing", and a 5xx is the difference.**
        A 4xx is the api's own answer that this action did not happen and
        would not: nothing ran, and a caller may treat it as settled. A 5xx
        says only that the server failed - possibly part way through the
        action - so it carries `unknown_outcome: True` alongside
        `allowed: False` and a caller must treat it as "nobody knows", never
        as a refusal (`_refusal`, and `baseline_seat.BaselineSeat.step` for
        what that changes)."""
        ...


#: The one sentence a caller gets instead of a refusal when the transport,
#: rather than the engine, is what failed. Repeated rather than imported from
#: `seatkit.UNKNOWN_OUTCOME` for the reason `_refusal` gives: this module
#: stays free of the transports.
UNKNOWN_OUTCOME = (
    "the server failed while answering this call, so the outcome of it is UNKNOWN. "
    "The action may already have gone through. Do NOT treat this as a refusal: "
    "read the board (or the game log's last log_seq) to see what actually happened."
)


def _refusal(status_code: int, parsed: Any, text: str) -> dict[str, Any]:
    """A non-2xx answer in the shape every refusal on this api already takes
    - **except a 5xx, which is not a refusal at all**.

    `records.MissingReasoning`/`MissingPlan` answer 422 with a pydantic-shaped
    `detail` list, `ValueError` answers 400 with a string `detail`, and a 500
    may carry no JSON at all - all three have to read as "this did not
    happen, and here is why", or a caller counts a refused action as a played
    one. `seatkit._normalise_non_2xx` does the same job for the seat
    transports; this is the same rule at this seam, kept here so `api_client`
    does not import a transport module.

    Two things this used to flatten, and both cost a real defect:

    - **A 5xx is not the same claim as a 4xx.** "The engine says this is
      illegal" and "the transport broke" are different facts, and the only
      consumer of this seam (`baseline_seat`) needs them apart: it remembers
      a refusal against the board it happened on, so that a move already
      refused is never posted again. Fingerprinting a transport failure that
      way silenced the seat for the rest of the game - a keyless move
      (`pass_priority`, `advance`, `decide`, a combat or pregame
      declaration) has no next enumerated row to step to, so `_pick`
      answered `None` for every later round and `play_out` conceded a game
      nothing was wrong with. One 503 in a parallel gauntlet - N games
      through one GIL-bound `serve` process - therefore invented a loss for
      the arm every other number is measured against. A 5xx carries
      `unknown_outcome: True` now, the same shape `seatkit._normalise_non_2xx`
      already gives one.
    - **The body's own keys are kept.** This built a fresh dict from
      `detail` alone, so a refusal that came with machine-readable facts lost
      them on the way through - `app._board_moved`'s 409 carries
      `stale: True`, `moved_from`/`moved_to` and `last_seq` precisely so no
      client has to read refusal prose, and a caller of this seam could not
      see any of it. A body that already carries its own `allowed` is that
      api's own refusal shape and is passed through as it stands.
    """
    base = parsed if isinstance(parsed, dict) else {}
    detail = base.get("detail")
    if isinstance(detail, list):
        reason = "; ".join(
            str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail
        )
    elif detail is not None:
        reason = str(detail)
    else:
        reason = text.strip() or f"HTTP {status_code}"
    if status_code >= 500:
        return {
            **base,
            "allowed": False,
            "reason": f"{UNKNOWN_OUTCOME} (HTTP {status_code}: {reason})",
            "http_status": status_code,
            "unknown_outcome": True,
        }
    if "allowed" in base:
        return {**base, "http_status": status_code}
    return {
        **base,
        "allowed": False,
        "reason": f"HTTP {status_code}: {reason}",
        "http_status": status_code,
    }


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
        self.board_seq: int | None = None

    def game_state(self, game_id: str, uid: str) -> dict[str, Any]:
        response = self.http.get(f"/api/games/{game_id}", params={"uid": uid})
        if response.status_code != 200:
            raise RuntimeError(
                f"game state read failed: HTTP {response.status_code} {response.text}"
            )
        return dict(response.json())

    def legal_actions(
        self, game_id: str, player: str, uid: str, *, candidates: bool = False
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"uid": uid, "player": player}
        if candidates:
            # Only when asked: the default answer must stay the cheap one,
            # and a query parameter this api does not know is not an error
            # it reports (`candidates: bool = False`), so sending it
            # unconditionally would hide a version skew rather than show it.
            params["candidates"] = "true"
        response = self.http.get(f"/api/games/{game_id}/legal-actions", params=params)
        if response.status_code != 200:
            # Never an empty list: "you may not ask that" and "there is
            # nothing you can do" are different answers and a policy that
            # confused them would pass its whole game away.
            raise RuntimeError(
                f"legal-actions read failed: HTTP {response.status_code} {response.text}"
            )
        payload = response.json()
        # The position this enumeration was computed against, kept for the
        # action the policy is about to decide on (`board_seq`). An answer
        # from a build that does not carry one leaves it unset rather than
        # guessing at a number: naming a board is optional by construction
        # (`schemas.BoardRead`), and a fabricated seq would either refuse
        # every action as stale or claim a board was current when nothing
        # checked.
        last_seq = payload.get("last_seq")
        self.board_seq = last_seq if isinstance(last_seq, int) else None
        return list(payload.get("actions") or [])

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
