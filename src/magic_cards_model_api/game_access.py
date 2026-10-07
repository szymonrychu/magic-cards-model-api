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
reassigned. **A game with no owner is as READABLE as every game was before
this module existed** - this is the whole migration story: no bearer token
has ever reached this deployment (this api has no OIDC issuer configured
yet, and even once oauth2-proxy fronts the play UI - GitHub PR #18 - the
play UI itself does not forward a token to this api, and the api's own
ingress carries no bypass past the proxy at all yet; see `oidc.py`'s own
note on what that bypass needs before it is safe), so every game created
today gets no owner, and no read of one changed. The moment a caller *does*
start sending a valid bearer token - this task's own manual verification
does, and a real OIDC-integrated frontend eventually will - every game it
creates is owned, and this module's ownership gate activates for exactly
those games, immediately, with no further deployment change required.

**Writing is the exception, and it is not about ownership at all.** Every
mutating request - POST, PUT, PATCH, DELETE (`MUTATING_METHODS`) - requires
a credential *for that game*, owned or not: a seat uid, the admin key, or
the owner's own bearer token. An ownerless game used to be world-*writable*
along with world-readable, and that is what made the advisor's read-only-ness
unenforceable rather than merely undocumented: `_advisor_may` is a deny-list
keyed on a credential, and a deny-list keyed on a credential cannot restrict
a caller who omits the credential. With no `uid` the advisor branch never
ran and the owner check answered `True`, so the buddy - which already knows
the `game_id`, since it needs it to read - walked past its own restriction
by dropping one query parameter. See
`tests/test_mutation_needs_a_credential.py`.

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

## The advisor credential, the one gate that is not about ownership

An advisor uid (`repository.Advisor`, stored in its own `games.advisors`
map) is a fourth kind of credential, and the only one whose verdict depends
on what the request is *doing* rather than only on who is asking. It reads
its human's game - `visibility.resolve_viewer` resolves it to that human's
username, so its view is byte-identical to the seat's - and it may not act:
`_advisor_may` lets through any GET, a POST whose *matched route template*
ends in `/advice` or `/report`, and a POST carrying `dry_run: true` to a
route that actually declares a `dry_run` field (`DRY_RUN_ROUTES`), and
refuses everything else through the same 404 as any other denial.

Both of those read the route, never the caller's own spelling of the
request. That is not fastidiousness: matching `request.url.path`'s tail is a
trap for the first mutating route that ends in a path parameter, and reading
`dry_run` off the raw body was the hole itself - pydantic discards an
undeclared key silently, so on every shape that does not declare the field
the gate said "probe" while the route committed for real. `schemas.
RequestModel`'s `extra="forbid"` is the independent second half of that
fix.

That check runs *before* the ownership question, not after it, because the
restriction is a property of the uid rather than of the game: an ownerless
game (which is every game today) is otherwise reachable by anyone who knows
its id, so an advisor asked about one would be waved through before its own
rule was ever consulted. Enforcing it here, once, is also what makes it true
of every mutating route at once - including ones written after this - rather
than of the handful anybody remembered to guard.

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

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from . import oidc
from .store import player_for_advisor_uid, store

__all__ = ["GameAccessRoute", "authorize_game_access", "can_access_game", "seat_for_uid"]

#: The only POST routes an advisor credential may reach. Both are ways of
#: *saying* something about a game rather than doing something to it:
#: `/advice` is the advisor's own channel (its route body is a separate
#: task; this gate is what will let it through when it lands) and `/report`
#: files a problem into the log, which anyone watching a game must be able
#: to do. Matched on the tail of the **matched route template**
#: (`_route_template`) so the same rule covers whichever router eventually
#: owns them - never on the caller's own spelling of the path, which is the
#: trap the first mutating route ending in a `{param}` would otherwise have
#: walked into.
#:
#: `/script-runs` is the third: a buddy running play scripts in advise mode
#: reports each run there, and a run report records what the runner did -
#: its moves were already refused or proposed through the other routes.
#:
#: `/plays-outcome` is the fourth: the buddy's driver files what its
#: handover did to the DSL, post-game, for the human it watches - a record
#: of work done in the guides repo, never a move in the game.
ADVISOR_POST_SUFFIXES = ("/advice", "/report", "/script-runs", "/plays-outcome")

#: The PUT routes an advisor credential may reach, and only for the corpus
#: registered under its own uid (`{uid}` in the path must be the credential
#: itself). The buddy's seat Job runs under its advisor uid, so that is the
#: uid it registers scripts with; registering a corpus says what the runner
#: will try, it does not move the game.
ADVISOR_OWN_PUT_ROUTES = frozenset({"/api/games/{game_id}/seats/{uid}/scripts"})

#: Every POST route whose own request model declares `dry_run` - i.e. every
#: route that can actually *honour* a probe (`engine_api.dry_run_copy`).
#:
#: This list exists because the body cannot be trusted to say what the route
#: will do with it. Only these shapes declare the field; every other request
#: model is a plain `BaseModel` whose `model_config` is empty, so pydantic
#: **discards** an unexpected `dry_run` key rather than rejecting it, and the
#: route then commits for real - a gate that read `dry_run` off the raw body
#: was therefore reading a field most routes throw away. On `POST .../seats`
#: that was privilege escalation rather than a stray mutation: `app.
#: mint_seat` takes no uid, checks nothing beyond this gate, and hands back a
#: fresh *unrestricted* seat credential, so one extra key let an advisor
#: upgrade itself out of its own restriction. `schemas.py` now also sets
#: `extra="forbid"` on the request models, which turns a `dry_run` a route
#: cannot honour into a 422 instead of a silent commit - the two defences are
#: independent on purpose, because `POST .../cards/{card_id}/tap` takes a
#: raw `dict[str, bool]` that no `model_config` can ever protect.
#:
#: Asserted against `openapi.json` by
#: `tests/test_game_access_advisor.py::
#: test_the_dry_run_allowlist_matches_the_routes_that_declare_the_field`, so
#: it cannot drift when a shape gains or loses the field.
DRY_RUN_ROUTES = frozenset(
    {
        "/api/games/{game_id}/actions/activate",
        "/api/games/{game_id}/actions/attack",
        "/api/games/{game_id}/actions/cast",
        "/api/games/{game_id}/actions/crew",
        "/api/games/{game_id}/actions/station",
        "/api/games/{game_id}/actions/cycle",
        "/api/games/{game_id}/actions/equip",
        "/api/games/{game_id}/actions/level-up",
        "/api/games/{game_id}/actions/reconfigure",
        "/api/games/{game_id}/actions/foretell",
        "/api/games/{game_id}/actions/play-land",
        "/api/games/{game_id}/actions/plot",
        "/api/games/{game_id}/actions/suspend",
        "/api/games/{game_id}/actions/turn-face-up",
        "/api/games/{game_id}/actions/companion",
        "/api/games/{game_id}/actions/unlock-door",
    }
)

#: The methods that can change a game, and so the ones the positive rule
#: below applies to. `GET`/`HEAD`/`OPTIONS` are reads and keep the migration
#: story an ownerless game has always had.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def seat_for_uid(game_id: str, uid: str | None) -> str | None:
    """The seat `uid` opens in `game_id`, or `None`. **Never an advisor, and
    never the admin key.**

    The authorization twin of `visibility.resolve_viewer`, and named apart
    from it because the two answer different questions that were being asked
    with one function. `resolve_viewer` answers *what may this uid see*, and
    it resolves an advisor uid to the human seat it watches on purpose -
    that is what makes an advisor's view byte-identical to its human's by
    construction rather than by two code paths agreeing. Four routes then
    used the same answer to decide *who is acting*
    (`app.decide`, `app._answer_declaration`, `app._pregame_seat`), and
    widening the viewer widened them too: an advisor uid started satisfying
    "this uid opens the seat this request names", which left this module's
    gate as the only thing between a read-only credential and a real move.

    Every acting-seat check calls this instead, so the two questions cannot
    be confused again. `app.declare_plan` and the archived-game check were
    already doing it correctly through `store.player_for_uid` directly; this
    is the same answer with a name that says which question it is.

    **The archive counts.** A game the lobby has ended is soft-deleted -
    `store.get` answers `None` for it - but the seat uid it was played with
    is still that seat's credential, and `POST .../reflection` exists
    precisely to be written afterwards (`app.declare_reflection` authorises
    against `store.archived(...).seat_uids` for exactly this reason, and
    the driver posts a game's reflection after the lobby has already ended
    it). `store.archived` is consulted only when the live lookup found
    nothing, so the ordinary path costs one dictionary read as before.
    """
    if not uid:
        return None
    player = store.player_for_uid(game_id, uid)
    if player is not None:
        return player
    archived = store.archived(game_id)
    return None if archived is None else archived.seat_uids.get(uid)


async def _json_body(request: Request) -> dict[str, Any] | None:
    """This request's JSON body as a dict, or `None` when it has none worth
    reading - no body, not JSON, or JSON that is not an object. Never
    raises. Starlette caches the bytes on first read, so asking here costs
    the route's own later parse nothing."""
    if request.method not in ("POST", "PUT", "PATCH"):
        return None
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


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
    body = await _json_body(request)
    if body is not None:
        uid = uid or body.get("uid")
        key = key or body.get("key")
    return uid, key


def _route_template(request: Request) -> str | None:
    """The template of the route this request matched -
    `/api/games/{game_id}/actions/cast`, never the caller's own
    `/api/games/abc123/actions/cast` - or `None` when nothing matched.

    FastAPI puts the matched `APIRoute` in the scope
    (`fastapi.routing.APIRoute.matches`), and `GameAccessRoute` runs inside
    that route's own handler, so by the time this is asked the scope always
    carries one in the app. It can be absent when `can_access_game` is
    asked directly about a hypothetical path (the tests do this for
    `/advice`, whose route body is a later task), and every caller here
    treats that as "no declared shape to consult" and fails closed.
    """
    path = getattr(request.scope.get("route"), "path", None)
    return path if isinstance(path, str) else None


async def _advisor_may(request: Request, uid: str) -> bool:
    """Whether an advisor credential may make *this* request
    (`repository.Advisor`).

    Read-only, enforced here rather than trusted to the client holding the
    uid - that is the whole point of the credential being its own kind:

    - **Any GET** on a `{game_id}` route. An advisor sees exactly what its
      human sees (`visibility.resolve_viewer` resolves it to that
      username), so no read this gate lets through can show it anything the
      seat could not already see. `HEAD` rides along, being a GET with the
      body dropped - Starlette registers one for every GET route.
    - **A POST to `/advice` or `/report`** (`ADVISOR_POST_SUFFIXES`) - the
      two ways of saying something rather than doing something.
    - **A POST carrying `dry_run: true` to a route that declares a `dry_run`
      field** (`DRY_RUN_ROUTES`) - a question, not a move: those routes
      answer one against an isolated probe copy of the game
      (`engine_api.dry_run_copy`) and commit nothing.

    Both of those are decided off the **matched route template**, never off
    the raw request path or the raw body. The route is what determines what
    the request will actually *do*; the body is the caller's own claim about
    it, and on every shape that does not declare `dry_run` that claim was
    simply discarded by pydantic while this gate believed it - see
    `DRY_RUN_ROUTES`.

    - **A PUT to its own play-script corpus** (`ADVISOR_OWN_PUT_ROUTES`,
      with the path's `{uid}` equal to this credential).

    Everything else is refused, which is every mutating route at once
    because this is asked before any of them runs.
    """
    if request.method in ("GET", "HEAD"):
        return True
    template = _route_template(request)
    if template is None:
        return False
    if request.method == "PUT":
        return template in ADVISOR_OWN_PUT_ROUTES and request.path_params.get("uid") == uid
    if request.method != "POST":
        return False
    if template.rstrip("/").endswith(ADVISOR_POST_SUFFIXES):
        return True
    if template not in DRY_RUN_ROUTES:
        return False
    body = await _json_body(request)
    return body is not None and body.get("dry_run") is True


def _is_admin(game_id: str, uid: str | None, key: str | None) -> bool:
    """Whether either credential slot carries `game_id`'s admin key.

    Both slots, because this repo has always accepted the admin key in
    either: `visibility.resolve_viewer` checks `store.is_admin(game_id,
    uid)` before anything else, `POST .../pregame/settle` *requires* it
    that way, and `conftest.state()` plus a couple of dozen test call sites
    hand it over as `uid=`. Checking only `key=` here - which is what this
    gate used to do - would refuse every one of them the moment the
    positive rule below started requiring a credential at all.
    """
    return any(store.is_admin(game_id, candidate) for candidate in (key, uid) if candidate)


async def can_access_game(request: Request, game_id: str) -> bool:
    """Whether `request` may reach `game_id` at all.

    **Reading** an unowned game needs nothing, exactly as it always has -
    see this module's docstring for the migration story, and
    `test_user_separation.py` for the pin. An owned game is readable the
    three ways that docstring lists.

    **Mutating** one - POST, PUT, PATCH or DELETE (`MUTATING_METHODS`) -
    needs a credential *for that game*, always: a uid that opens one of its
    seats, its admin key, or a bearer token naming its owner. This is the
    positive rule, and it exists because the advisor restriction could not
    be enforced without it. `_advisor_may` is a deny-list keyed on a
    credential, and a deny-list keyed on a credential cannot restrict a
    caller who omits the credential - the buddy already knows the
    `game_id`, so dropping `uid` walked past its own restriction into a
    branch that answered `True` for every ownerless game, which is every
    game in this deployment. An ownerless game being world-*writable* is
    pre-existing behaviour rather than a regression this branch introduced,
    but it makes the read-only guarantee unachievable, so it closes here.
    World-*readable* is untouched.

    Never raises for an *invalid* bearer token either - the same rule
    `authorize_game_access` documents in more detail, reused here so a
    filtered list can simply drop an entry the caller could not prove
    instead of failing the whole listing over one bad token.
    """
    uid, key = await _credentials(request)
    seat = seat_for_uid(game_id, uid)
    # The advisor gate comes first, and before the ownership question, on
    # purpose: an advisor uid is read-only *whatever* the game's ownership
    # is, and an unowned game (every game today) would otherwise return
    # `True` below before this was ever asked. A uid that is a seat of this
    # game is never treated as an advisor, so a seat is unaffected even if
    # the same string were somehow registered as both.
    if uid and seat is None and player_for_advisor_uid(game_id, uid) is not None:
        return await _advisor_may(request, uid)
    if seat is not None or _is_admin(game_id, uid, key):
        return True
    owner = store.owner_user_id(game_id)
    if owner is not None:
        user = await run_in_threadpool(oidc.current_user, request)
        return user is not None and user.sub == owner
    # An ownerless game, with no seat uid and no admin key: readable, as it
    # has always been, and not writable.
    return request.method not in MUTATING_METHODS


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
    if request.method in MUTATING_METHODS:
        reason = await _seat_mismatch(request, game_id)
        if reason is not None:
            raise HTTPException(status_code=403, detail=reason)


#: The mutating routes a seat acts through, whose subject is the JSON body's
#: `player`. Every other mutating route either attributes itself from the
#: credential already (`report`, `plan`, `advice`, `decide`, `attack`,
#: `declare-blockers`, the pregame and script routes) or is board setup
#: (`_BOARD_SETUP_ROUTES`).
_BODY_PLAYER_ROUTES = frozenset(
    f"/api/games/{{game_id}}/{rest}"
    for rest in (
        "actions/cast",
        "actions/suspend",
        "actions/plot",
        "actions/foretell",
        "actions/cycle",
        "actions/activate",
        "actions/crew",
        "actions/station",
        "actions/equip",
        "actions/level-up",
        "actions/reconfigure",
        "actions/play-land",
        "actions/pass-priority",
        "actions/concede",
        "advance",
    )
)
#: Board setup and harness routes: no rules, no priority check, and no seat
#: client (the play UI, seatkit, the MCP tools, `mcp-seat`, the baseline
#: seat, the play-script runtime, the driver) calls any of them. They are
#: the operator's and the test harness's - the admin key or the owner - and
#: never a seat's: CR 723.5 hands a controller the controlled player's
#: decisions, never an edit of the board (a life total set to 0 would be a
#: concession CR 723.6 forbids).
_BOARD_SETUP_ROUTES = frozenset(
    {
        "/api/games/{game_id}/cards",
        "/api/games/{game_id}/cards/{card_id}",
        "/api/games/{game_id}/cards/{card_id}/tap",
        "/api/games/{game_id}/cards/{card_id}/move",
        "/api/games/{game_id}/players/{username}",
        "/api/games/{game_id}/players/{username}/mana-pool",
        "/api/games/{game_id}/step",
        "/api/games/{game_id}/state-based-actions",
    }
)
#: Resolving the top of the stack as though every player had passed: no seat
#: client sends it, but a seat may, only while the priority it acts on is
#: its own to decide (CR 723.5) - never with no body standing in for a seat.
_RESOLVE_TOP_ROUTE = "/api/games/{game_id}/resolve-top"
_CONCEDE_ROUTE = "/api/games/{game_id}/actions/concede"
#: Minting a seat credential: only ever for the minting seat's own player.
_MINT_ROUTE = "/api/games/{game_id}/seats"
#: Post-game records a seat files about its own work: the body's `player`
#: must be the seat's own, and never a player it controls - CR 723.5 hands a
#: controller the controlled player's in-game decisions, not that player's
#: record. (An advisor uid is no seat, so it passes here; the route binds it
#: to the human it watches.)
_PLAYS_OUTCOME_ROUTE = "/api/games/{game_id}/plays-outcome"
#: Every route whose body `player` must be the seat uid's own player, with
#: no CR 723.5 exception.
OWN_PLAYER_ROUTES = frozenset({_MINT_ROUTE, _PLAYS_OUTCOME_ROUTE})


def _card_holder(game: Any, card_id: str) -> str | None:
    """Whose card this is: a permanent's (or a stack object's) controller,
    otherwise the player whose zone holds it (`player_control._owner_of`) -
    a stored controller outlives zone changes, so it names nobody off the
    battlefield and the stack."""
    from uuid import UUID

    from . import player_control

    try:
        card = game.find_by_id(UUID(card_id))
    except (ValueError, TypeError):
        return None
    if card is None:
        return None
    from magic_cards_model.models import Zone

    if any(state is card for state in game.shared_zones.get(Zone.STACK, [])):
        return str(card.controller) if card.controller else card.owner
    return player_control._owner_of(game, card)


async def _seat_mismatch(request: Request, game_id: str) -> str | None:
    """Why this seat uid may not make this request, or None.

    A seat uid acts for its own player, or - CR 723.5 - for a player it is
    making the decisions of this turn. It never edits the board
    (`_BOARD_SETUP_ROUTES`) and mints a credential only for its own player.
    The admin key and a caller that presents no seat uid at all (the owner's
    bearer token) are left to the access gate above. Conceding is the one
    decision a controller never makes: CR 723.6, "The controller of another
    player can't make that player concede." """
    template = _route_template(request)
    if template is None:
        return None
    uid, key = await _credentials(request)
    if _is_admin(game_id, uid, key):
        return None
    seat = seat_for_uid(game_id, uid)
    if seat is None:
        return None
    if template == _RESOLVE_TOP_ROUTE:
        from . import player_control

        game = store.get(game_id)
        holder = game.priority_holder if game is not None else None
        if holder is not None and player_control.may_decide_for(game, seat, holder):
            return None
        return (
            f"that uid opens {seat}'s seat, and {seat} does not hold the priority this "
            "would pass - resolve-top is a seat's only while it decides that priority"
        )
    if template in _BOARD_SETUP_ROUTES:
        return (
            f"that uid opens {seat}'s seat, and this route edits the board directly - "
            "it takes the game's admin key or its owner, never a seat"
        )
    if template not in _BODY_PLAYER_ROUTES and template not in OWN_PLAYER_ROUTES:
        return None
    body = await _json_body(request)
    player = body.get("player") if body is not None else None
    subject = player if isinstance(player, str) else None
    if subject is None or subject == seat:
        return None
    if template == _MINT_ROUTE:
        return f"that uid opens {seat}'s seat, and a seat mints a credential only for itself"
    if template in OWN_PLAYER_ROUTES:
        return (
            f"that uid opens {seat}'s seat, not {subject}'s - a seat files its own "
            "post-game record only"
        )
    from . import player_control

    game = store.get(game_id)
    deciding = game is not None and player_control.may_decide_for(game, seat, subject)
    if template == _CONCEDE_ROUTE:
        if deciding:
            return f"{seat} cannot make {subject} concede, even while controlling them (CR 723.6)"
        return f"that uid opens {seat}'s seat, and only {subject} may concede for {subject}"
    if deciding:
        return None
    return (
        f"that uid opens {seat}'s seat, not {subject}'s, and {seat} is not making "
        f"{subject}'s decisions this turn - a seat acts only for its own player"
    )


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
