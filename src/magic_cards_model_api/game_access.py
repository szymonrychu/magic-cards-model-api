"""The layer above `visibility.py`: whether a caller may reach a game **at
all**, before anything asks what of it they may see.

`visibility.py`'s own docstring already draws this line: it "hides another
seat's hand within a game" - it answers "of this game, which parts", never
"this game, at all". Before this module, "at all" had exactly one gate:
knowing `game_id`. `GET /api/games/{id}` with no `uid`, or `GET /api/games`
(no credential of any kind, ever) already answer with the board's public
information or the whole game list to anyone who can reach this process at
all - which is everyone who knows `play-magic.szymonrichert.pl`'s shared
basic-auth password, i.e. everyone the owner is worried about. That is the
gap the owner's own words describe: "no way for 1 user to login as the other
and see other's games."

## The design

**A game either has an owner or it does not.** `owner_user_id` (`repository.
py`) is the OIDC subject (`oidc.AuthenticatedUser.sub`) of whoever created it
through a request that carried a valid bearer token - set once, at creation,
by `app.create_game`/`decks_api.start_game_from_decklists`, never
reassigned. **A game with no owner is exactly as unrestricted as every game
was before this module existed** - this is the whole migration story: no
bearer token has ever reached this deployment (this api has no OIDC issuer
configured yet, and even once oauth2-proxy fronts the play UI - GitHub PR
#18 - the play UI itself does not forward a token to this api, and the api's
own ingress carries no bypass past the proxy at all yet; see `oidc.py`'s own
note on what that bypass needs before it is safe), so every game created
today gets no owner, and this module changes nothing about it. The moment a
caller *does* start sending a valid bearer token -
this task's own manual verification does, and a real OIDC-integrated
frontend eventually will - every game it creates is owned, and this module's
gate activates for exactly those games, immediately, with no further
deployment change required.

**A game that has an owner is reachable three ways, and three ways only:**

1. The caller's own bearer token names that owner (`oidc.current_user`,
   compared by `sub` - the stable subject, never `email`).
2. The caller presents a seat uid `store.player_for_uid` recognises for this
   game.
3. The caller presents this game's admin key (`store.is_admin`).

(2) and (3) are what let a machine client keep working with **no bearer
token at all** - `seat_cli`, `mcp.py` (through `seatkit.Seat`), and the AI
seat runner all authenticate a mutating call with a seat uid today, never an
OIDC identity, and nothing here can change that without breaking every
existing agent integration this repo has. A seat's authority is therefore
independent of its game's owner by design: the owner is the human (or
service account, once one exists) accountable for the game and the only
identity that can *list*/*browse* it by logging in, while a seat is a
capability the owner handed out - at creation (`seat_uids` in the create
response) or later (`POST .../seats`, "the seat back door") - that carries
its own authority for exactly one game and one seat, forever, with no
reference to who is logged in. Both are checked; either is sufficient;
neither implies the other.

**Denied is 404, always** - `raise KeyError(f"no game {game_id}")`, the same
exception and the same message `app.game_or_404` already raises for a game
that genuinely does not exist (`app.py`'s `_key_error` handler is what turns
it into the response). A caller who is not entitled to a game must not be
able to tell "not yours" apart from "never existed" - a 403 already leaks
that much, so this never returns one.

## Where the gate actually runs

`GameAccessRoute` is a `fastapi.routing.APIRoute` subclass that wraps every
route's handler: before the handler (and before FastAPI parses its body or
query parameters at all) it reads `game_id` straight off `request.
path_params` and calls `authorize_game_access`. This is what lets one gate
cover every `/api/games/{game_id}/...` and `/api/lobby/games/{game_id}/...`
operation - cast, activate, put_card, mint_seat, delete, all of them -
without editing each one to accept a credential its function signature never
had a reason to declare. `uid`/`key` are read from the raw request (query
parameters first, a JSON body's own `uid`/`key` keys as a fallback - `POST
.../ai-seat/start` and `.../stop` carry theirs in the body, matching
`ai_seat_api.StartAiSeat`) rather than added as declared FastAPI parameters,
so no route's OpenAPI shape changes because of this gate.

Two routes return more than one game's worth of information from a single
call - `GET /api/games` (`app.list_games`), `GET /api/games/summary`
(`app.game_summaries_route`), and `GET /api/lobby/games` (`decks_api.
list_lobby_games`) - and a per-`game_id` gate cannot wrap "some of a batch
response". Those three filter for themselves, calling `can_access_game` (this
module's non-raising twin of `authorize_game_access`) per game and dropping
what the caller may not see, rather than 404ing an operation that has no
single game to be denied for. This is also the fix for the second half of
the leak: it is not only mutating an unowned game that must be impossible,
it is *learning that game exists at all* by reading the list - dropping the
entry, not merely filtering the credentials embedded in it, the way
`list_lobby_games`'s own `seats` field already did before this task.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from . import oidc
from .store import store

__all__ = ["GameAccessRoute", "authorize_game_access", "can_access_game"]


async def _credentials(request: Request) -> tuple[str | None, str | None]:
    """`(uid, key)` this request presents, query parameters first - every
    seat-driven caller (`seatkit.Seat`) already sends `uid` this way on
    every call - falling back to a JSON body's own `uid`/`key` keys for the
    handful of routes that carry theirs there instead (`ai_seat_api.
    StartAiSeat`). Never raises: a body that is missing, not JSON, or not an
    object simply has neither."""
    uid = request.query_params.get("uid")
    key = request.query_params.get("key")
    if uid is not None or key is not None:
        return uid, key
    if request.method not in ("POST", "PUT", "PATCH"):
        return uid, key
    try:
        body = await request.json()
    except Exception:
        return uid, key
    if isinstance(body, dict):
        uid = uid or body.get("uid")
        key = key or body.get("key")
    return uid, key


async def can_access_game(request: Request, game_id: str) -> bool:
    """Whether `request` may reach `game_id` at all - see this module's
    docstring for the three ways an owned game is reachable. `True` for an
    unowned or unknown game: this function only ever narrows access for a
    game that names an owner. Never raises for an *invalid* bearer token
    either - the same rule `authorize_game_access` documents in more detail,
    reused here so a filtered list can simply drop an entry the caller could
    not prove instead of failing the whole listing over one bad token."""
    owner = store.owner_user_id(game_id)
    if owner is None:
        return True
    user = await run_in_threadpool(oidc.current_user, request)
    if user is not None and user.sub == owner:
        return True
    uid, key = await _credentials(request)
    if uid and store.player_for_uid(game_id, uid) is not None:
        return True
    return bool(key) and store.is_admin(game_id, key)


async def authorize_game_access(request: Request, game_id: str) -> None:
    """Raises `KeyError(f"no game {game_id}")` - `app.py`'s existing
    `KeyError` handler turns this into a 404 indistinguishable from the game
    never having existed - unless `request` may reach `game_id`. A bearer
    token that does not verify raises its own `HTTPException(401)` from
    inside `oidc.current_user` (via `can_access_game`); that is deliberately
    not caught here; see `oidc.py` for why an unprovable identity is refused
    rather than silently treated as anonymous."""
    if not await can_access_game(request, game_id):
        raise KeyError(f"no game {game_id}")


class GameAccessRoute(APIRoute):
    """An `APIRoute` that runs `authorize_game_access` in front of every
    operation whose path names `{game_id}`, before the operation's own body
    or query parameters are ever parsed. Set as the `route_class` of every
    router that declares a `{game_id}` route (`app.py`'s `FastAPI(...)`,
    `decks_api.router`, `ai_seat_api.router`) - a no-op for any route with no
    `game_id` path parameter, so it is harmless to set everywhere rather than
    tracked per-router.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            game_id = request.path_params.get("game_id")
            if isinstance(game_id, str):
                await authorize_game_access(request, game_id)
            return await original_route_handler(request)

        return custom_route_handler
